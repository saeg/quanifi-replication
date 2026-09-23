import io
import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators
from nifiapi.__jvm__ import JvmHolder


class QuanifiUnitary(FlowFileTransform):
    """
    Compute the unitary matrix of any circuit-producing processor's output.

    Reads the circuit from the FlowFile content (in cirq_json, qasm2, qasm3, or
    qpy format, determined by the `circuit.format` attribute), computes the full
    2^n by 2^n unitary matrix, and writes it back as JSON with separate real and
    imaginary parts.

    Qiskit-produced circuits are normalised with reverse_bits() so the resulting
    unitary uses the same qubit-ordering convention as Cirq (qubit 0 = most
    significant bit), making cross-framework comparison meaningful.

    Routes to `failure` with a clear error message if the input FlowFile does not
    contain a circuit (i.e. the `circuit.format` attribute is missing) or if the
    circuit is too large to fit the configured memory budget.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Computes the 2^n by 2^n unitary matrix of a quantum circuit and writes it "
            "as JSON ({real: [...], imag: [...]}). Connect between any circuit builder "
            "(CirqGroverCircuit, CirqPhaseOracle, CirqGroverOperator, QiskitGroverCircuit, "
            "QiskitQFTCircuit, etc.) and a downstream consumer such as "
            "QuantumUnitaryComparison. Reads the input format from the circuit.format "
            "attribute (cirq_json, qasm2, qasm3, qpy). Qiskit circuits are normalised "
            "to Cirq qubit-ordering convention so the unitaries are directly comparable "
            "across frameworks. Routes to failure with a clear error if the input is not "
            "a circuit or if the circuit exceeds Max Qubits."
        )
        tags = ["quantum", "unitary", "matrix", "validation", "equivalence", "qiskit", "cirq"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-qasm3-import", "cirq>=1.0.0", "numpy"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.max_qubits = PropertyDescriptor(
            name="Max Qubits",
            description=(
                "Safety cap on circuit size. Unitary memory grows as 4^n times 16 bytes: "
                "n=10 is 16 MB, n=12 is 256 MB, n=14 is 4 GB, n=15 is 16 GB. Circuits "
                "exceeding this limit are routed to failure with an explanatory message "
                "instead of being computed. Raise this if you have the RAM and want to "
                "test larger circuits."
            ),
            required=True,
            default_value="12",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
        )
        self.descriptors = [self.max_qubits]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        import numpy as np

        max_qubits = int(context.getProperty(self.max_qubits).getValue())

        fmt = flowFile.getAttribute("circuit.format")
        raw = bytes(flowFile.getContentsAsBytes())

        # ----- Guard: input must be a circuit -----
        if not fmt:
            error_msg = (
                "Input FlowFile has no `circuit.format` attribute. The QuanifiUnitary "
                "processor must be connected to a circuit-producing processor "
                "(CirqGroverCircuit, CirqPhaseOracle, CirqGroverOperator, "
                "QiskitGroverCircuit, QiskitQFTCircuit, etc.). It cannot be connected "
                "directly to a simulator (which outputs measurement counts, not a "
                "circuit) or to a generic data source (GenerateFlowFile, GetFile)."
            )
            self.logger.error("QuanifiUnitary: " + error_msg)
            return FlowFileTransformResult(
                relationship="failure",
                contents=raw,
                attributes={
                    "unitary.error":      error_msg,
                    "unitary.error_type": "missing_circuit_format_attribute",
                },
            )

        # ----- Parse circuit and compute unitary -----
        try:
            if fmt == "cirq_json":
                import cirq
                circuit = cirq.read_json(json_text=raw.decode("utf-8"))
                circuit = cirq.Circuit(
                    op for op in circuit.all_operations()
                    if not isinstance(op.gate, cirq.MeasurementGate)
                )
                num_qubits = len(circuit.all_qubits())
                if num_qubits > max_qubits:
                    return self._oversize_fail(raw, num_qubits, max_qubits)
                U = cirq.unitary(circuit)

            elif fmt == "qasm2":
                from qiskit import qasm2
                from qiskit.quantum_info import Operator
                circuit = qasm2.loads(
                    raw.decode("utf-8"),
                    custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS,
                )
                circuit.remove_final_measurements(inplace=True)
                num_qubits = circuit.num_qubits
                if num_qubits > max_qubits:
                    return self._oversize_fail(raw, num_qubits, max_qubits)
                U = Operator(circuit.reverse_bits()).data

            elif fmt == "qasm3":
                from qiskit import qasm3
                from qiskit.quantum_info import Operator
                circuit = qasm3.loads(raw.decode("utf-8"))
                circuit.remove_final_measurements(inplace=True)
                num_qubits = circuit.num_qubits
                if num_qubits > max_qubits:
                    return self._oversize_fail(raw, num_qubits, max_qubits)
                U = Operator(circuit.reverse_bits()).data

            elif fmt == "qpy":
                from qiskit import qpy
                from qiskit.quantum_info import Operator
                circuit = qpy.load(io.BytesIO(raw))[0]
                circuit.remove_final_measurements(inplace=True)
                num_qubits = circuit.num_qubits
                if num_qubits > max_qubits:
                    return self._oversize_fail(raw, num_qubits, max_qubits)
                U = Operator(circuit.reverse_bits()).data

            else:
                error_msg = (
                    f"Unsupported circuit.format '{fmt}'. QuanifiUnitary supports: "
                    "cirq_json, qasm2, qasm3, qpy. Check the upstream processor's "
                    "Output Format property."
                )
                self.logger.error("QuanifiUnitary: " + error_msg)
                return FlowFileTransformResult(
                    relationship="failure",
                    contents=raw,
                    attributes={
                        "unitary.error":      error_msg,
                        "unitary.error_type": "unsupported_format",
                    },
                )

        except Exception as exc:
            error_msg = (
                f"Failed to parse {fmt} circuit or compute its unitary: {exc}. "
                f"The input may not be a valid {fmt} circuit, or it may contain "
                "non-unitary operations (e.g. mid-circuit measurements) that could "
                "not be stripped."
            )
            self.logger.error("QuanifiUnitary: " + error_msg)
            return FlowFileTransformResult(
                relationship="failure",
                contents=raw,
                attributes={
                    "unitary.error":      error_msg,
                    "unitary.error_type": "parse_or_compute_failure",
                },
            )

        # ----- Serialise as JSON -----
        dim = U.shape[0]
        n = num_qubits or int(round(np.log2(dim)))
        payload = {
            "real": U.real.tolist(),
            "imag": U.imag.tolist(),
        }
        content = json.dumps(payload).encode("utf-8")

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes={
                "unitary.num_qubits":     str(n),
                "unitary.dimension":      str(dim),
                "unitary.format":         "json_real_imag",
                "unitary.source_format":  fmt,
            },
        )

    def _oversize_fail(self, raw, num_qubits, max_qubits):
        bytes_needed = 16 * (4 ** num_qubits)
        error_msg = (
            f"Circuit has {num_qubits} qubits, which exceeds the configured Max Qubits "
            f"= {max_qubits}. Computing the unitary would require {bytes_needed:,} bytes "
            f"({bytes_needed / 1024**3:.2f} GB) of memory. Raise the Max Qubits "
            "property if you have the RAM, or test the subroutines on a smaller circuit."
        )
        self.logger.error("QuanifiUnitary: " + error_msg)
        return FlowFileTransformResult(
            relationship="failure",
            contents=raw,
            attributes={
                "unitary.error":       error_msg,
                "unitary.error_type":  "circuit_too_large",
                "unitary.num_qubits":  str(num_qubits),
                "unitary.max_qubits":  str(max_qubits),
            },
        )
