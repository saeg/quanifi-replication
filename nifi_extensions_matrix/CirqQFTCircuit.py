from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class CirqQFTCircuit(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Applies the Quantum Fourier Transform (or its inverse QFT†) using Cirq's "
            "built-in cirq.qft primitive. "
            "In standalone mode a fresh n-qubit QFT circuit is created. "
            "In compose mode the QFT is appended to an existing Cirq circuit received "
            "via the FlowFile content (cirq_json or qasm2). "
            "No measurement is added — connect to CirqSimulator to run the circuit."
        )
        tags = ["quantum", "cirq", "qft", "fourier", "transform", "circuit"]
        dependencies = ["cirq>=1.0.0", "ply"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.qubit_count = PropertyDescriptor(
            name="Qubit Count",
            description="Number of qubits in standalone mode. Ignored when composing onto an existing circuit.",
            required=True,
            default_value="3",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.inverse = PropertyDescriptor(
            name="Inverse",
            description=(
                "Apply the inverse QFT (QFT†). Set to true for the readout stage of "
                "Phase Estimation, or to undo a forward QFT in a composed pipeline."
            ),
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
        )
        self.do_swaps = PropertyDescriptor(
            name="Do Swaps",
            description=(
                "Include the bit-reversal SWAP network at the end of the QFT "
                "(standard mathematical convention). Set to false to skip the swaps — "
                "the output qubit ordering is reversed but the circuit is shorter."
            ),
            required=True,
            default_value="true",
            allowable_values=["true", "false"],
        )
        self.insert_barriers = PropertyDescriptor(
            name="Insert Barriers",
            description="Force the QFT into its own Cirq moment group for clearer circuit diagrams.",
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "'cirq_json' is Cirq's native lossless format (recommended for Cirq-to-Cirq pipelines). "
                "'qasm2' is OpenQASM 2.0 (decomposed to CZ + single-qubit gates; "
                "use to feed QiskitAerSimulator or QrispSimulator)."
            ),
            required=True,
            default_value="cirq_json",
            allowable_values=["cirq_json", "qasm2"],
        )
        self.descriptors = [
            self.qubit_count,
            self.inverse,
            self.do_swaps,
            self.insert_barriers,
            self.output_format,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        import cirq
        from cirq.circuits.qasm_output import QasmUGate
        from cirq.contrib.svg import circuit_to_svg

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        inverse  = get(self.inverse).lower() == "true"
        do_swaps = get(self.do_swaps).lower() == "true"
        barriers = get(self.insert_barriers).lower() == "true"
        fmt      = get(self.output_format)

        incoming_fmt = flowFile.getAttribute("circuit.format")
        raw = bytes(flowFile.getContentsAsBytes())

        if incoming_fmt and raw:
            # --- Compose mode: append QFT to existing circuit ---
            if incoming_fmt == "cirq_json":
                circuit = cirq.read_json(json_text=raw.decode("utf-8"))
            else:
                from cirq.contrib.qasm_import import circuit_from_qasm
                circuit = circuit_from_qasm(raw.decode("utf-8"))
            qubits = sorted(circuit.all_qubits())
            n = len(qubits)
            inherited = {
                "circuit.marked_state": flowFile.getAttribute("circuit.marked_state") or "",
            }
        else:
            # --- Standalone mode: fresh QFT circuit ---
            n = int(get(self.qubit_count))
            qubits = cirq.LineQubit.range(n)
            circuit = cirq.Circuit()
            inherited = {}

        # cirq.qft is the native Cirq primitive for the Quantum Fourier Transform.
        # without_reverse=True skips the bit-reversal SWAP network at the end.
        qft_op = cirq.qft(*qubits, without_reverse=(not do_swaps), inverse=inverse)

        if barriers:
            circuit.append(qft_op, strategy=cirq.InsertStrategy.NEW_THEN_INLINE)
            circuit.append(cirq.Moment())
        else:
            circuit.append(qft_op)

        if fmt == "cirq_json":
            content = cirq.to_json(circuit).encode("utf-8")
        else:
            opt = cirq.optimize_for_target_gateset(circuit, gateset=cirq.CZTargetGateset())
            content = opt.to_qasm().encode("utf-8")

        # Expand the circuit to primitive gates for accurate metric counting.
        expanded = cirq.Circuit(cirq.decompose(circuit))
        all_ops = list(expanded.all_operations())
        t_count = sum(
            1 for op in all_ops
            if hasattr(op.gate, 'exponent') and abs(abs(op.gate.exponent) - 0.25) < 1e-9
        )

        diagram = str(circuit)
        self.logger.warn("CirqQFTCircuit (n={}, inverse={}, do_swaps={}):\n{}".format(
            n, inverse, do_swaps, diagram
        ))

        # circuit_from_qasm (used above for a non-Cirq incoming circuit) turns
        # u3-style gates into QasmUGate, which has no diagram symbol — circuit_to_svg
        # falls back to its verbose repr(). Expand just those ops into Rz/Ry/Rz
        # rotations (which do have symbols) so the SVG stays readable.
        def expand_qasm_u(op):
            return cirq.decompose(op) if isinstance(op.gate, QasmUGate) else op

        diagram_circuit = cirq.Circuit(
            expand_qasm_u(op) for moment in circuit for op in moment.operations
        )

        try:
            svg = circuit_to_svg(diagram_circuit)
        except Exception as exc:
            self.logger.warn("circuit_to_svg failed: {}".format(exc))
            svg = ""

        attrs = {
            "circuit.format":         fmt,
            "circuit.qasm3": "",  # Cirq emits svg/qasm2, not qasm3 (blank stale)
            "circuit.num_qubits":     str(n),
            "circuit.qft_inverse":    str(inverse).lower(),
            "circuit.qft_do_swaps":   str(do_swaps).lower(),
            "circuit.diagram":        diagram,
            "circuit.svg":            svg,
            "circuit.depth":          str(len(expanded)),
            "circuit.gate_count":     str(len(all_ops)),
            "circuit.nonlocal_gates": str(sum(1 for op in all_ops if len(op.qubits) >= 2)),
            "circuit.t_count":        str(t_count),
            **{k: v for k, v in inherited.items() if v},
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
