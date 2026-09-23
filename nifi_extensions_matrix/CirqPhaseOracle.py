from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class CirqPhaseOracle(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a phase oracle circuit for a given target bitstring. "
            "The oracle applies a -1 phase to |target⟩ and leaves all other states unchanged. "
            "In standalone mode it outputs a bare oracle circuit (no state preparation). "
            "In compose mode it appends the oracle to an existing Cirq circuit. "
            "Connect to CirqGroverOperator to build the full Grover search, or feed into "
            "amplitude estimation / amplitude amplification circuits."
        )
        tags = ["quantum", "cirq", "oracle", "grover", "circuit"]
        dependencies = ["cirq>=1.0.0", "ply"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.marked_state = PropertyDescriptor(
            name="Marked State",
            description=(
                "Target bitstring the oracle marks with a -1 phase, e.g. '101'. "
                "Length sets the number of qubits. Bit order is left-to-right "
                "(qubit 0 = leftmost character)."
            ),
            required=True,
            default_value="11",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.insert_barriers = PropertyDescriptor(
            name="Insert Barriers",
            description="Force the oracle into its own Cirq moment group for clearer circuit diagrams.",
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "'cirq_json' is Cirq's native lossless format (recommended). "
                "'qasm2' is OpenQASM 2.0 (multi-controlled gates decomposed to CZ + single-qubit gates)."
            ),
            required=True,
            default_value="cirq_json",
            allowable_values=["cirq_json", "qasm2"],
        )
        self.descriptors = [self.marked_state, self.insert_barriers, self.output_format]

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

        target = get(self.marked_state)
        barriers = get(self.insert_barriers).lower() == "true"
        fmt = get(self.output_format)
        n = len(target)

        incoming_fmt = flowFile.getAttribute("circuit.format")
        raw = bytes(flowFile.getContentsAsBytes())

        if incoming_fmt and raw:
            # --- Compose mode: append oracle to an existing circuit ---
            if incoming_fmt == "cirq_json":
                circuit = cirq.read_json(json_text=raw.decode("utf-8"))
            else:
                from cirq.contrib.qasm_import import circuit_from_qasm
                circuit = circuit_from_qasm(raw.decode("utf-8"))
            qubits = sorted(circuit.all_qubits())
        else:
            # --- Standalone mode: bare oracle circuit on fresh qubits ---
            qubits = cirq.LineQubit.range(n)
            circuit = cirq.Circuit()

        def mcz(*qs):
            return cirq.Z(qs[0]) if len(qs) == 1 else cirq.Z.controlled(len(qs) - 1)(*qs)

        oracle_ops = []
        for i, bit in enumerate(target):
            if bit == '0':
                oracle_ops.append(cirq.X(qubits[i]))
        oracle_ops.append(mcz(*qubits))
        for i, bit in enumerate(target):
            if bit == '0':
                oracle_ops.append(cirq.X(qubits[i]))

        if barriers:
            circuit.append(oracle_ops, strategy=cirq.InsertStrategy.NEW_THEN_INLINE)
        else:
            circuit.append(oracle_ops)

        if fmt == "cirq_json":
            content = cirq.to_json(circuit).encode("utf-8")
        else:
            opt = cirq.optimize_for_target_gateset(circuit, gateset=cirq.CZTargetGateset())
            content = opt.to_qasm().encode("utf-8")

        diagram = str(circuit)
        self.logger.warn("PhaseOracle (target={}):\n{}".format(target, diagram))

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

        all_ops = list(circuit.all_operations())
        attrs = {
            "circuit.format":          fmt,
            # NiFi merges attributes downstream; blank the Qiskit-only QASM
            # text attrs so a cross-framework upstream can't leave stale source
            # in the report (circuit.qasm2 is set truthfully when emitted).
            "circuit.qasm3":           "",
            "circuit.qasm2":           content.decode("utf-8") if fmt == "qasm2" else "",
            "circuit.num_qubits":      str(len(qubits)),
            "circuit.marked_state":    target,
            "circuit.diagram":         diagram,
            "circuit.svg":             svg,
            "circuit.depth":           str(len(circuit)),
            "circuit.gate_count":      str(len(all_ops)),
            "circuit.nonlocal_gates":  str(sum(1 for op in all_ops if len(op.qubits) >= 2)),
            "circuit.t_count":         "0",
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
