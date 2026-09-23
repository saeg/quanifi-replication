import io

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitPhaseOracle(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a phase oracle circuit for a given target bitstring using Qiskit. "
            "The oracle applies a -1 phase to |target⟩ and leaves all other states unchanged. "
            "In standalone mode it outputs a bare oracle circuit (no state preparation). "
            "In compose mode it appends the oracle to an existing Qiskit circuit. "
            "Connect to QiskitGroverOperator to build the full Grover search, or feed into "
            "QiskitAmplitudeAmplification. Mirrors CirqPhaseOracle so the two are "
            "interchangeable on the canvas (use Output Format 'qasm2' to cross frameworks)."
        )
        tags = ["quantum", "qiskit", "oracle", "grover", "circuit"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-qasm3-import"]

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
            description="Wrap the oracle in barriers so its boundary is visible in circuit diagrams.",
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "Format used to serialize the circuit into the FlowFile content. "
                "'qasm3' is human-readable OpenQASM 3 text. "
                "'qpy' is compact Qiskit binary (lossless). "
                "'qasm2' is OpenQASM 2.0 (use this to feed CirqGroverOperator or other non-Qiskit tools)."
            ),
            required=True,
            default_value="qasm3",
            allowable_values=["qasm3", "qpy", "qasm2"],
        )
        self.descriptors = [self.marked_state, self.insert_barriers, self.output_format]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _fail(self, flowFile, msg):
        self.logger.error("QiskitPhaseOracle: " + msg)
        return FlowFileTransformResult(
            relationship="failure",
            contents=bytes(flowFile.getContentsAsBytes()),
            attributes={"circuit.error": msg},
        )

    def transform(self, context, flowFile):
        from qiskit import QuantumCircuit

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        target = get(self.marked_state).strip()
        barriers = get(self.insert_barriers).lower() == "true"
        fmt = get(self.output_format)
        n = len(target)

        if not target or any(c not in "01" for c in target):
            return self._fail(flowFile, (
                "Marked State must be a non-empty bitstring of '0'/'1' "
                "characters, got '{}'.".format(target)
            ))

        incoming_fmt = flowFile.getAttribute("circuit.format")
        raw = bytes(flowFile.getContentsAsBytes())

        if incoming_fmt and raw:
            # --- Compose mode: append oracle to an existing circuit ---
            try:
                if incoming_fmt == "qpy":
                    from qiskit import qpy
                    circuit = qpy.load(io.BytesIO(raw))[0]
                elif incoming_fmt == "qasm2":
                    from qiskit import qasm2
                    circuit = qasm2.loads(raw.decode("utf-8"),
                                          custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
                else:
                    from qiskit import qasm3
                    circuit = qasm3.loads(raw.decode("utf-8"))
            except Exception as exc:
                return self._fail(flowFile, (
                    "Could not parse the incoming circuit as '{}': {}".format(incoming_fmt, exc)
                ))
            if circuit.num_qubits != n:
                return self._fail(flowFile, (
                    "Marked State '{}' needs {} qubits but the incoming circuit "
                    "has {}.".format(target, n, circuit.num_qubits)
                ))
        else:
            # --- Standalone mode: bare oracle circuit on fresh qubits ---
            circuit = QuantumCircuit(n)

        # Phase oracle: flip the |0⟩ bits, multi-controlled Z (as H·MCX·H), unflip.
        if barriers:
            circuit.barrier()
        for i, bit in enumerate(target):
            if bit == '0':
                circuit.x(i)
        if n == 1:
            circuit.z(0)
        else:
            circuit.h(n - 1)
            circuit.mcx(list(range(n - 1)), n - 1)
            circuit.h(n - 1)
        for i, bit in enumerate(target):
            if bit == '0':
                circuit.x(i)
        if barriers:
            circuit.barrier()

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

        diagram = str(circuit.draw("text"))
        self.logger.warn("PhaseOracle (target={}):\n{}".format(target, diagram))

        ops = circuit.count_ops()
        attrs = {
            "circuit.format":           fmt,
            # NiFi merges attributes downstream; blank the Cirq-only SVG so a
            # cross-framework upstream can't leave a stale drawing in the report.
            "circuit.svg":              "",
            "circuit.num_qubits":       str(circuit.num_qubits),
            "circuit.marked_state":     target,
            "circuit.diagram":          diagram,
            "circuit.depth":            str(circuit.depth()),
            "circuit.gate_count":       str(sum(v for k, v in ops.items() if k not in ("barrier", "measure"))),
            "circuit.nonlocal_gates":   str(circuit.num_nonlocal_gates()),
            "circuit.t_count":          str(ops.get("t", 0) + ops.get("tdg", 0)),
        }
        if fmt == "qasm3":
            attrs["circuit.qasm3"] = content.decode("utf-8")
        elif fmt == "qasm2":
            attrs["circuit.qasm2"] = content.decode("utf-8")

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
