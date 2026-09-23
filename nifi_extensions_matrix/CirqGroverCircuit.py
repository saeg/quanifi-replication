import io

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class CirqGroverCircuit(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a Grover search circuit using Cirq for a given target bitstring and outputs "
            "the unmeasured circuit in either Cirq JSON or OpenQASM 2.0 format. "
            "Connect to a Cirq simulator processor to run the simulation."
        )
        tags = ["quantum", "cirq", "grover", "circuit"]
        dependencies = ["cirq>=1.0.0"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.marked_state = PropertyDescriptor(
            name="Marked State",
            description=(
                "Target bitstring Grover will search for, e.g. '110'. "
                "Length sets the number of qubits. Bit order is left-to-right "
                "(qubit 0 = leftmost character)."
            ),
            required=True,
            default_value="11",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.num_iterations = PropertyDescriptor(
            name="Num Iterations",
            description=(
                "Number of Grover operator applications. "
                "Optimal is roughly floor(pi/4 * sqrt(2^n)) for one marked state."
            ),
            required=True,
            default_value="1",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.insert_barriers = PropertyDescriptor(
            name="Insert Barriers",
            description=(
                "Force oracle and diffusion stages into separate Cirq moments, "
                "making stage boundaries visible in circuit diagrams."
            ),
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "Format used to serialize the circuit into the FlowFile content. "
                "'cirq_json' is Cirq's native lossless format (recommended for Cirq-to-Cirq pipelines). "
                "'qasm2' is OpenQASM 2.0 text (multi-controlled gates are automatically decomposed "
                "into CZ + single-qubit gates for interoperability)."
            ),
            required=True,
            default_value="cirq_json",
            allowable_values=["cirq_json", "qasm2"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.marked_state,
            self.num_iterations,
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

        target = get(self.marked_state)
        num_iterations = int(get(self.num_iterations))
        barriers = get(self.insert_barriers).lower() == "true"
        fmt = get(self.output_format)
        n = len(target)

        qubits = cirq.LineQubit.range(n)

        def mcz(*qs):
            """Multi-controlled-Z: flips sign of |1...1⟩. CZ for n=2, CCZ for n=3, etc."""
            return cirq.Z(qs[0]) if len(qs) == 1 else cirq.Z.controlled(len(qs) - 1)(*qs)

        def phase_oracle():
            """Phase oracle: applies a -1 phase to |target⟩ only."""
            ops = []
            for i, bit in enumerate(target):
                if bit == '0':
                    ops.append(cirq.X(qubits[i]))
            ops.append(mcz(*qubits))
            for i, bit in enumerate(target):
                if bit == '0':
                    ops.append(cirq.X(qubits[i]))
            return ops

        def diffusion():
            """Grover diffusion: 2|s⟩⟨s| - I (reflect about uniform superposition)."""
            ops = []
            ops.extend(cirq.H(q) for q in qubits)
            ops.extend(cirq.X(q) for q in qubits)
            ops.append(mcz(*qubits))
            ops.extend(cirq.X(q) for q in qubits)
            ops.extend(cirq.H(q) for q in qubits)
            return ops

        # Full circuit: uniform superposition + N Grover iterations (no measurement).
        circuit = cirq.Circuit()
        circuit.append(cirq.H.on_each(*qubits))

        for _ in range(num_iterations):
            if barriers:
                circuit.append(phase_oracle(), strategy=cirq.InsertStrategy.NEW_THEN_INLINE)
                circuit.append(cirq.Moment())
                circuit.append(diffusion(), strategy=cirq.InsertStrategy.NEW_THEN_INLINE)
                circuit.append(cirq.Moment())
            else:
                circuit.append(phase_oracle())
                circuit.append(diffusion())

        # Serialize.
        if fmt == "cirq_json":
            content = cirq.to_json(circuit).encode("utf-8")
        else:
            # Decompose multi-controlled gates into CZ + single-qubit gates for QASM 2.0.
            qasm_circuit = cirq.optimize_for_target_gateset(
                circuit, gateset=cirq.CZTargetGateset()
            )
            content = qasm_circuit.to_qasm().encode("utf-8")

        diagram = str(circuit)
        self.logger.warn("Grover circuit (target={}, iterations={}):\n{}".format(
            target, num_iterations, diagram
        ))

        all_ops = list(circuit.all_operations())
        gate_count = len(all_ops)
        nonlocal_gates = sum(1 for op in all_ops if len(op.qubits) >= 2)
        t_count = sum(
            1 for op in all_ops
            if hasattr(op.gate, 'exponent') and abs(abs(op.gate.exponent) - 0.25) < 1e-9
        )

        try:
            svg = circuit_to_svg(circuit)
        except Exception as exc:
            self.logger.warn("circuit_to_svg failed: {}".format(exc))
            svg = ""

        attrs = {
            "circuit.format":           fmt,
            "circuit.qasm3": "",  # Cirq emits svg/qasm2, not qasm3 (blank stale)
            "circuit.num_qubits":       str(n),
            "circuit.marked_state":     target,
            "circuit.num_iterations":   str(num_iterations),
            "circuit.diagram":          diagram,
            "circuit.svg":              svg,
            "circuit.depth":            str(len(circuit)),
            "circuit.gate_count":       str(gate_count),
            "circuit.nonlocal_gates":   str(nonlocal_gates),
            "circuit.t_count":          str(t_count),
            "circuit.bit_order":        "canonical",
            "builder.component":        "CirqGrover",
            "builder.framework":        "cirq",
        }
        if fmt == "qasm2":
            attrs["circuit.qasm2"] = content.decode("utf-8")

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
