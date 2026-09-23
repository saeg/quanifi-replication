import numpy as np

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class CirqPhaseEstimation(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Quantum Phase Estimation (QPE) using Cirq. Estimates the eigenphase φ of a "
            "unitary operator U such that U|ψ⟩ = exp(2πiφ)|ψ⟩. "
            "In standalone mode a builtin Z-rotation gate (T, S, or Z) is used as the unitary. "
            "In compose mode the incoming Cirq circuit is used as the unitary — its qubits become "
            "the target register and are prepared in the |1⟩ eigenstate. "
            "Circuit structure: X on target → H on phase register → controlled-U^(2^k) → "
            "inverse QFT on phase register. "
            "No measurement is added — connect to CirqSimulator; the first "
            "circuit.qpe_phase_register_size bits of the top result encode the phase."
        )
        tags = ["quantum", "cirq", "qpe", "phase-estimation", "circuit"]
        dependencies = ["cirq>=1.0.0", "ply", "numpy"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.phase_register_size = PropertyDescriptor(
            name="Phase Register Size",
            description=(
                "Number of qubits in the phase register. Precision = 2π / 2^m. "
                "3 qubits gives 1/8 precision; 4 qubits gives 1/16."
            ),
            required=True,
            default_value="3",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.builtin_unitary = PropertyDescriptor(
            name="Builtin Unitary",
            description=(
                "Gate to use as the unitary in standalone mode (ignored in compose mode). "
                "T: eigenphase = 1/8 (π/4), expected phase register readout = 001 (3 qubits). "
                "S: eigenphase = 1/4 (π/2), expected readout = 010. "
                "Z: eigenphase = 1/2 (π),   expected readout = 100."
            ),
            required=True,
            default_value="T",
            allowable_values=["T", "S", "Z"],
        )
        self.insert_barriers = PropertyDescriptor(
            name="Insert Barriers",
            description="Insert empty Cirq moments between H, controlled-U, and inverse-QFT stages.",
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "'cirq_json' is Cirq's native lossless format (recommended). "
                "'qasm2' is OpenQASM 2.0 (multi-qubit controlled gates decomposed to CZ + single-qubit)."
            ),
            required=True,
            default_value="cirq_json",
            allowable_values=["cirq_json", "qasm2"],
        )
        self.descriptors = [
            self.phase_register_size,
            self.builtin_unitary,
            self.insert_barriers,
            self.output_format,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        import cirq
        from cirq.contrib.svg import circuit_to_svg

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        m        = int(get(self.phase_register_size))
        builtin  = get(self.builtin_unitary)
        barriers = get(self.insert_barriers).lower() == "true"
        fmt      = get(self.output_format)

        incoming_fmt = flowFile.getAttribute("circuit.format")
        raw = bytes(flowFile.getContentsAsBytes())

        # Phase register: qubits 0..m-1 (sorted first by LineQubit ordering)
        # Target register: qubits m..m+num_target-1 (sorted after phase register)
        # This ordering ensures CirqSimulator's sorted measurement gives
        # phase bits first, target bits last.
        phase_qubits = cirq.LineQubit.range(m)

        circuit = cirq.Circuit()

        if incoming_fmt and raw:
            # --- Compose mode: use incoming circuit as the unitary U ---
            if incoming_fmt == "cirq_json":
                unitary_circuit = cirq.read_json(json_text=raw.decode("utf-8"))
            else:
                from cirq.contrib.qasm_import import circuit_from_qasm
                unitary_circuit = circuit_from_qasm(raw.decode("utf-8"))

            num_target = len(list(unitary_circuit.all_qubits()))
            target_qubits = [cirq.LineQubit(m + i) for i in range(num_target)]

            # Compute unitary matrix for controlled-U^(2^k) operations.
            # Works for any small circuit; 4-qubit+ unitaries have 16x16+ matrices.
            u_matrix = cirq.unitary(unitary_circuit)

            # Prepare target register in |1...1⟩ (eigenstate for diagonal unitaries).
            circuit.append(cirq.X.on_each(*target_qubits))

            if barriers:
                circuit.append(cirq.Moment())

            circuit.append(cirq.H.on_each(*phase_qubits))

            if barriers:
                circuit.append(cirq.Moment())

            for k, ctrl in enumerate(phase_qubits):
                u_k = np.linalg.matrix_power(u_matrix, 2 ** k)
                gate = cirq.MatrixGate(u_k)
                circuit.append(gate.on(*target_qubits).controlled_by(ctrl))

            qpe_builtin_attr = "compose"

        else:
            # --- Standalone mode: builtin Z-rotation gate ---
            target = cirq.LineQubit(m)

            gate_map = {"T": cirq.T, "S": cirq.S, "Z": cirq.Z}
            oracle = gate_map[builtin]

            # Prepare |1⟩ eigenstate: T|1⟩ = e^(iπ/4)|1⟩, eigenphase = 1/8
            circuit.append(cirq.X(target))

            if barriers:
                circuit.append(cirq.Moment())

            circuit.append(cirq.H.on_each(*phase_qubits))

            if barriers:
                circuit.append(cirq.Moment())

            for k, ctrl in enumerate(phase_qubits):
                circuit.append((oracle(target) ** (2 ** k)).controlled_by(ctrl))

            target_qubits = [target]
            qpe_builtin_attr = builtin

        if barriers:
            circuit.append(cirq.Moment())

        # Inverse QFT on phase register.
        # without_reverse=True matches the standard QPE convention where phase_qubits[0]
        # controlled U^1 (LSB) and the inverse QFT maps this to the MSB of the output.
        circuit.append(cirq.qft(*phase_qubits, without_reverse=True) ** -1)

        if fmt == "cirq_json":
            content = cirq.to_json(circuit).encode("utf-8")
        else:
            opt = cirq.optimize_for_target_gateset(circuit, gateset=cirq.CZTargetGateset())
            content = opt.to_qasm().encode("utf-8")

        # Expand to primitive gates for accurate metric counting.
        expanded = cirq.Circuit(cirq.decompose(circuit))
        all_ops = list(expanded.all_operations())
        t_count = sum(
            1 for op in all_ops
            if hasattr(op.gate, 'exponent') and abs(abs(op.gate.exponent) - 0.25) < 1e-9
        )

        diagram = str(circuit)
        num_total_qubits = m + len(target_qubits)
        self.logger.warn(
            "CirqPhaseEstimation (m={}, unitary={}, total_qubits={}):\n{}".format(
                m, qpe_builtin_attr, num_total_qubits, diagram
            )
        )

        try:
            svg = circuit_to_svg(circuit)
        except Exception as exc:
            self.logger.warn("circuit_to_svg failed: {}".format(exc))
            svg = ""

        attrs = {
            "circuit.format":                   fmt,
            "circuit.num_qubits":               str(num_total_qubits),
            "circuit.qpe_phase_register_size":  str(m),
            "circuit.qpe_builtin":              qpe_builtin_attr,
            "circuit.qpe_target_qubits":        str(len(target_qubits)),
            "circuit.diagram":                  diagram,
            "circuit.svg":                      svg,
            "circuit.depth":                    str(len(expanded)),
            "circuit.gate_count":               str(len(all_ops)),
            "circuit.nonlocal_gates":           str(sum(1 for op in all_ops if len(op.qubits) >= 2)),
            "circuit.t_count":                  str(t_count),
            # Declarative decode hint for QuanifiReport's generic
            # _render_derived_result, mirroring QiskitPhaseEstimation. Cirq is
            # MSB-first: the phase register is LineQubit 0..m-1 (sorted first by
            # CirqSimulator), and the without_reverse inverse QFT maps qubit 0 to
            # the readout MSB, so the phase bits occupy string positions 0..m-1
            # already in MSB→LSB order. Empirically verified end-to-end: T/S/Z
            # decode to 1/8, 1/4, 1/2. Emitting positions here keeps all
            # framework/layout knowledge in the builder and the report a pure
            # formatter.
            "result.decode":                    "phase",
            "result.bit_positions":             ",".join(str(j) for j in range(m)),
            "result.label":                     "Estimated phase (φ)",
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
