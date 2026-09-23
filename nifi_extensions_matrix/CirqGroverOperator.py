from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class CirqGroverOperator(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Reads an oracle circuit from the FlowFile content (produced by CirqPhaseOracle), "
            "prepends the uniform superposition H⊗n, and applies the full Grover operator "
            "(oracle + diffuser) the requested number of times. "
            "Equivalent to Classiq's power(reps, grover_operator(oracle, hadamard_transform)). "
            "No measurement is added — connect to CirqSimulator to run the circuit."
        )
        tags = ["quantum", "cirq", "grover", "operator", "diffuser", "circuit"]
        dependencies = ["cirq>=1.0.0", "ply"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.num_iterations = PropertyDescriptor(
            name="Num Iterations",
            description=(
                "Number of times the Grover operator (oracle + diffuser) is applied. "
                "Optimal is roughly floor(π/4 · √(2ⁿ)) for a single marked state."
            ),
            required=True,
            default_value="1",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.insert_barriers = PropertyDescriptor(
            name="Insert Barriers",
            description="Insert empty Cirq moments between oracle and diffuser stages for clearer diagrams.",
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "'cirq_json' is Cirq's native lossless format (recommended for Cirq-to-Cirq pipelines). "
                "'qasm2' is OpenQASM 2.0 (multi-controlled gates decomposed; use to feed non-Cirq simulators)."
            ),
            required=True,
            default_value="cirq_json",
            allowable_values=["cirq_json", "qasm2"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.num_iterations, self.insert_barriers, self.output_format]

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

        num_iterations = int(get(self.num_iterations))
        barriers = get(self.insert_barriers).lower() == "true"
        fmt = get(self.output_format)

        incoming_fmt = flowFile.getAttribute("circuit.format") or "cirq_json"
        raw = bytes(flowFile.getContentsAsBytes())

        if incoming_fmt == "cirq_json":
            oracle_circuit = cirq.read_json(json_text=raw.decode("utf-8"))
        else:
            from cirq.contrib.qasm_import import circuit_from_qasm
            oracle_circuit = circuit_from_qasm(raw.decode("utf-8"))

        qubits = sorted(oracle_circuit.all_qubits())
        n = len(qubits)

        def mcz(*qs):
            return cirq.Z(qs[0]) if len(qs) == 1 else cirq.Z.controlled(len(qs) - 1)(*qs)

        def diffusion():
            # 2|s⟩⟨s| - I, reflection about the uniform superposition (H⊗n assumed).
            ops = []
            ops.extend(cirq.H(q) for q in qubits)
            ops.extend(cirq.X(q) for q in qubits)
            ops.append(mcz(*qubits))
            ops.extend(cirq.X(q) for q in qubits)
            ops.extend(cirq.H(q) for q in qubits)
            return ops

        # Full circuit: H⊗n (state prep) + (oracle + diffuser) × num_iterations.
        circuit = cirq.Circuit(cirq.H.on_each(*qubits))

        for _ in range(num_iterations):
            if barriers:
                circuit.append(oracle_circuit.moments)
                circuit.append(cirq.Moment())
                circuit.append(diffusion(), strategy=cirq.InsertStrategy.NEW_THEN_INLINE)
                circuit.append(cirq.Moment())
            else:
                circuit += oracle_circuit
                circuit.append(diffusion())

        if fmt == "cirq_json":
            content = cirq.to_json(circuit).encode("utf-8")
        else:
            opt = cirq.optimize_for_target_gateset(circuit, gateset=cirq.CZTargetGateset())
            content = opt.to_qasm().encode("utf-8")

        marked_state = flowFile.getAttribute("circuit.marked_state") or ""
        diagram = str(circuit)
        self.logger.warn("GroverOperator (target={}, iterations={}):\n{}".format(
            marked_state, num_iterations, diagram
        ))

        all_ops = list(circuit.all_operations())
        t_count = sum(
            1 for op in all_ops
            if hasattr(op.gate, 'exponent') and abs(abs(op.gate.exponent) - 0.25) < 1e-9
        )
        # circuit_from_qasm (used above for a non-Cirq incoming oracle) turns
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
            "circuit.format":           fmt,
            # NiFi merges attributes downstream; blank the Qiskit-only QASM
            # text attrs so a cross-framework upstream (e.g. QiskitPhaseOracle)
            # can't leave stale source in the report (circuit.qasm2 is set
            # truthfully when emitted).
            "circuit.qasm3":            "",
            "circuit.qasm2":            content.decode("utf-8") if fmt == "qasm2" else "",
            "circuit.num_qubits":       str(n),
            "circuit.num_iterations":   str(num_iterations),
            "circuit.diagram":          diagram,
            "circuit.svg":              svg,
            "circuit.depth":            str(len(circuit)),
            "circuit.gate_count":       str(len(all_ops)),
            "circuit.nonlocal_gates":   str(sum(1 for op in all_ops if len(op.qubits) >= 2)),
            "circuit.t_count":          str(t_count),
        }
        if marked_state:
            attrs["circuit.marked_state"] = marked_state

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
