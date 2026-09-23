from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QrispHadamardTransform(FlowFileTransform):
    """
    Applies H⊗n — a Hadamard gate on every qubit — using Qrisp.

    Cross-framework mirror of QiskitHadamardTransform / CirqHadamardTransform.
    Qrisp's neutral wire format is OpenQASM 2.0 (matching QrispQFTCircuit and
    QrispSimulator), so this processor always emits qasm2 — directly swappable
    with the Qiskit/Cirq Hadamard processors on the canvas.

    Two modes depending on the incoming FlowFile:

    - Standalone (empty FlowFile): builds a fresh n-qubit Qrisp QuantumVariable
      and applies H to every qubit, producing the uniform superposition
      H⊗n |0⟩ = (1/√2ⁿ) Σ|x⟩.

    - Compose (circuit.format == qasm2 set, content present): loads the incoming
      qasm2 circuit and appends H to every qubit — useful for stacking onto an
      upstream circuit processor (e.g. QrispQFTCircuit).
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Applies a Hadamard gate to every qubit (H⊗n) using Qrisp. "
            "In standalone mode (empty FlowFile) a fresh n-qubit circuit is created, "
            "producing the uniform superposition H⊗n|0⟩. "
            "In compose mode the H layer is appended to an existing qasm2 circuit "
            "received via the FlowFile content. Always emits OpenQASM 2.0, compatible "
            "with QrispSimulator, QiskitAerSimulator, and CirqSimulator. "
            "Cross-framework mirror of QiskitHadamardTransform."
        )
        tags = ["quantum", "qrisp", "hadamard", "superposition", "circuit"]
        dependencies = ["qrisp==0.9.5", "qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.qubit_count = PropertyDescriptor(
            name="Qubit Count",
            description=(
                "Number of qubits in standalone mode. "
                "Ignored when composing onto an existing circuit."
            ),
            required=True,
            default_value="2",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.qubit_count]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qrisp import QuantumVariable, QuantumCircuit, h
        from qiskit import qasm2 as qiskit_qasm2
        from qiskit.compiler import transpile

        incoming_fmt = flowFile.getAttribute("circuit.format")
        raw = bytes(flowFile.getContentsAsBytes())

        if incoming_fmt and raw:
            # --- Compose mode: append H to every qubit of the incoming circuit ---
            if incoming_fmt != "qasm2":
                return FlowFileTransformResult(
                    relationship="failure",
                    attributes={
                        "circuit.error": (
                            f"Unsupported incoming circuit.format '{incoming_fmt}'. "
                            "QrispHadamardTransform composes onto qasm2 circuits only. "
                            "Set Output Format = qasm2 on the upstream processor."
                        )
                    },
                )
            qc = QuantumCircuit.from_qasm_str(raw.decode("utf-8"))
            n = qc.num_qubits()
            for i in range(n):
                qc.h(i)
            qk = qc.to_qiskit()
            inherited_attrs = {
                "circuit.marked_state": flowFile.getAttribute("circuit.marked_state") or "",
            }
        else:
            # --- Standalone mode: fresh uniform-superposition circuit ---
            n = int(
                context.getProperty(self.qubit_count)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )
            qv = QuantumVariable(n)
            h(qv)
            qk = qv.qs.compile().to_qiskit()
            inherited_attrs = {}

        content = qiskit_qasm2.dumps(qk).encode("utf-8")

        # Decompose to primitive gates for accurate metrics (matches QrispQFTCircuit).
        qk_decomp = transpile(
            qk,
            basis_gates=['h', 'cx', 'p', 'cp', 'swap', 'x', 'rz', 's', 't', 'sdg', 'tdg', 'id'],
            optimization_level=0,
        )
        ops = qk_decomp.count_ops()
        depth = qk_decomp.depth()
        gate_count = sum(ops.values())
        two_qubit_names = {'cx', 'cz', 'cy', 'ch', 'cp', 'crz', 'crx', 'cry', 'cu', 'ccx', 'swap'}
        nonlocal_gates = sum(count for gate, count in ops.items() if gate in two_qubit_names)
        t_count = ops.get('t', 0)

        diagram = str(qk.draw('text'))
        self.logger.warn("QrispHadamardTransform ({} qubits):\n{}".format(n, diagram))

        attrs = {
            "circuit.format":         "qasm2",
            "circuit.svg": "",  # blank stale Cirq SVG (NiFi merges attrs)
            "circuit.num_qubits":     str(n),
            "circuit.framework":      "qrisp",
            "circuit.diagram":        diagram,
            "circuit.depth":          str(depth),
            "circuit.gate_count":     str(gate_count),
            "circuit.nonlocal_gates": str(nonlocal_gates),
            "circuit.t_count":        str(t_count),
            **{k: v for k, v in inherited_attrs.items() if v},
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
