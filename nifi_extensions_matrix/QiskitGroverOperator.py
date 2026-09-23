import io

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitGroverOperator(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Reads an oracle circuit from the FlowFile content (produced by QiskitPhaseOracle, "
            "or by CirqPhaseOracle with Output Format 'qasm2'), prepends the uniform "
            "superposition H⊗n, and applies the full Grover operator (oracle + diffuser) "
            "the requested number of times via qiskit.circuit.library.grover_operator. "
            "Mirrors CirqGroverOperator so the two are interchangeable on the canvas. "
            "No measurement is added — connect to QiskitAerSimulator to run the circuit."
        )
        tags = ["quantum", "qiskit", "grover", "operator", "diffuser", "circuit"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-qasm3-import"]

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
            description="Add barriers between oracle, inverse state prep, zero reflection, and state prep stages.",
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "Format used to serialize the circuit into the FlowFile content. "
                "'qasm3' is human-readable OpenQASM 3 text. "
                "'qpy' is compact Qiskit binary (lossless). "
                "'qasm2' is OpenQASM 2.0 (use this to feed CirqSimulator or other non-Qiskit tools)."
            ),
            required=True,
            default_value="qasm3",
            allowable_values=["qasm3", "qpy", "qasm2"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.num_iterations, self.insert_barriers, self.output_format]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _fail(self, flowFile, msg):
        self.logger.error("QiskitGroverOperator: " + msg)
        return FlowFileTransformResult(
            relationship="failure",
            contents=bytes(flowFile.getContentsAsBytes()),
            attributes={"circuit.error": msg},
        )

    def transform(self, context, flowFile):
        from qiskit import QuantumCircuit
        from qiskit.circuit.library import grover_operator

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        num_iterations = int(get(self.num_iterations))
        barriers = get(self.insert_barriers).lower() == "true"
        fmt = get(self.output_format)

        incoming_fmt = flowFile.getAttribute("circuit.format")
        raw = bytes(flowFile.getContentsAsBytes())

        if not incoming_fmt or not raw:
            return self._fail(flowFile, (
                "Expects an oracle circuit in the FlowFile content with the "
                "circuit.format attribute set. Connect QiskitPhaseOracle, or "
                "CirqPhaseOracle with Output Format 'qasm2'."
            ))

        try:
            if incoming_fmt == "qpy":
                from qiskit import qpy
                oracle = qpy.load(io.BytesIO(raw))[0]
            elif incoming_fmt == "qasm2":
                from qiskit import qasm2
                oracle = qasm2.loads(raw.decode("utf-8"),
                                     custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
            else:
                from qiskit import qasm3
                oracle = qasm3.loads(raw.decode("utf-8"))
        except Exception as exc:
            return self._fail(flowFile, (
                "Could not parse the oracle circuit as '{}': {}".format(incoming_fmt, exc)
            ))

        n = oracle.num_qubits

        # grover_operator inlines the oracle, and barriers inside it break QASM3
        # export of the assembled circuit (same gotcha as QiskitAmplitudeAmplification).
        oracle.data = [inst for inst in oracle.data if inst.operation.name != "barrier"]

        grover_op = grover_operator(oracle, insert_barriers=barriers)

        # Full circuit: H⊗n (state prep) + (oracle + diffuser) × num_iterations.
        circuit = QuantumCircuit(n)
        circuit.h(range(n))
        for _ in range(num_iterations):
            circuit.compose(grover_op, inplace=True)

        # Serialize.
        if fmt == "qpy":
            from qiskit import qpy
            buf = io.BytesIO()
            qpy.dump(circuit, buf)
            content = buf.getvalue()
        elif fmt == "qasm2":
            from qiskit import qasm2, transpile
            tc = transpile(circuit, basis_gates=["h", "cx", "rz", "x"], optimization_level=0)
            content = qasm2.dumps(tc).encode("utf-8")
        else:  # qasm3
            from qiskit import qasm3, transpile
            # mcx decomposes into a custom gate body that openqasm3 1.0.1 can't
            # parse back. Transpile to stdgates.inc primitives first.
            tc = transpile(circuit, basis_gates=["h", "cx", "rz", "x"], optimization_level=0)
            content = qasm3.dumps(tc).encode("utf-8")

        marked_state = flowFile.getAttribute("circuit.marked_state") or ""
        diagram = str(circuit.draw("text"))
        self.logger.warn("GroverOperator (target={}, iterations={}):\n{}".format(
            marked_state, num_iterations, diagram
        ))

        ops = circuit.count_ops()
        attrs = {
            "circuit.format":           fmt,
            "circuit.num_qubits":       str(n),
            "circuit.num_iterations":   str(num_iterations),
            "circuit.diagram":          diagram,
            # NiFi merges attributes downstream; blank the Cirq-only SVG so a
            # cross-framework upstream (e.g. CirqPhaseOracle) can't leave a
            # stale oracle drawing that QuanifiReport would prefer over the
            # fresh qasm3 of the full circuit.
            "circuit.svg":              "",
            "circuit.depth":            str(circuit.depth()),
            "circuit.gate_count":       str(sum(v for k, v in ops.items() if k not in ("barrier", "measure"))),
            "circuit.nonlocal_gates":   str(circuit.num_nonlocal_gates()),
            "circuit.t_count":          str(ops.get("t", 0) + ops.get("tdg", 0)),
        }
        if marked_state:
            attrs["circuit.marked_state"] = marked_state
        if fmt == "qasm3":
            attrs["circuit.qasm3"] = content.decode("utf-8")
        elif fmt == "qasm2":
            attrs["circuit.qasm2"] = content.decode("utf-8")

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
