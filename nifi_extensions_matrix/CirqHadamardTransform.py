import io

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class CirqHadamardTransform(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Applies a Hadamard gate to every qubit (H⊗n). "
            "In standalone mode a fresh n-qubit circuit is created, producing the uniform "
            "superposition H⊗n|0⟩. "
            "In compose mode the H layer is appended to an existing Cirq circuit received "
            "via the FlowFile content (circuit.format must be cirq_json or qasm2)."
        )
        tags = ["quantum", "cirq", "hadamard", "superposition", "circuit"]
        dependencies = ["cirq>=1.0.0", "ply"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.qubit_count = PropertyDescriptor(
            name="Qubit Count",
            description="Number of qubits in standalone mode. Ignored when composing onto an existing circuit.",
            required=True,
            default_value="2",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "'cirq_json' is Cirq's native lossless format (recommended for Cirq-to-Cirq pipelines). "
                "'qasm2' is OpenQASM 2.0 (use this to feed QiskitAerSimulator or QrispSimulator)."
            ),
            required=True,
            default_value="cirq_json",
            allowable_values=["cirq_json", "qasm2"],
        )
        self.descriptors = [self.qubit_count, self.output_format]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        import cirq
        from cirq.circuits.qasm_output import QasmUGate
        from cirq.contrib.svg import circuit_to_svg

        fmt = context.getProperty(self.output_format).getValue()
        incoming_fmt = flowFile.getAttribute("circuit.format")
        raw = bytes(flowFile.getContentsAsBytes())

        if incoming_fmt and raw:
            # --- Compose mode: append H to every qubit of the incoming circuit ---
            if incoming_fmt == "cirq_json":
                circuit = cirq.read_json(json_text=raw.decode("utf-8"))
            else:
                from cirq.contrib.qasm_import import circuit_from_qasm
                circuit = circuit_from_qasm(raw.decode("utf-8"))
            qubits = sorted(circuit.all_qubits())
            circuit.append(cirq.H.on_each(*qubits))
            inherited = {
                "circuit.marked_state": flowFile.getAttribute("circuit.marked_state") or "",
            }
        else:
            # --- Standalone mode: fresh uniform-superposition circuit ---
            n = int(
                context.getProperty(self.qubit_count)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )
            qubits = cirq.LineQubit.range(n)
            circuit = cirq.Circuit(cirq.H.on_each(*qubits))
            inherited = {}

        n = len(qubits)

        if fmt == "cirq_json":
            content = cirq.to_json(circuit).encode("utf-8")
        else:
            opt = cirq.optimize_for_target_gateset(circuit, gateset=cirq.CZTargetGateset())
            content = opt.to_qasm().encode("utf-8")

        diagram = str(circuit)
        self.logger.warn("HadamardTransform ({} qubits):\n{}".format(n, diagram))

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
            "circuit.qasm3": "",  # Cirq emits svg/qasm2, not qasm3 (blank stale)
            "circuit.num_qubits":      str(n),
            "circuit.diagram":         diagram,
            "circuit.svg":             svg,
            "circuit.depth":           str(len(circuit)),
            "circuit.gate_count":      str(len(all_ops)),
            "circuit.nonlocal_gates":  str(sum(1 for op in all_ops if len(op.qubits) >= 2)),
            "circuit.t_count":         "0",
            **{k: v for k, v in inherited.items() if v},
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
