import io

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitHadamardTransform(FlowFileTransform):
    """
    Applies H⊗n — a Hadamard gate on every qubit.

    Two modes depending on the incoming FlowFile:

    - Circuit present (circuit.format attribute is set): loads the circuit from
      the FlowFile content and appends H to every qubit.  Useful for composing
      with upstream processors like GroverCircuit.

    - No circuit (empty FlowFile, e.g. from GenerateFlowFile): creates a fresh
      n-qubit circuit and applies H to all qubits, producing the uniform
      superposition  H⊗n |0⟩ = (1/√2ⁿ) Σ|x⟩.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Applies a Hadamard gate to every qubit of a circuit. "
            "If the incoming FlowFile already carries a circuit (circuit.format attribute set), "
            "H gates are appended to all its qubits. "
            "If the FlowFile is empty, a fresh n-qubit circuit is created."
        )
        tags = ["quantum", "qiskit", "hadamard", "superposition", "circuit"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-qasm3-import"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.qubit_count = PropertyDescriptor(
            name="Qubit Count",
            description=(
                "Number of qubits when creating a circuit from scratch. "
                "Ignored when the FlowFile already contains a circuit."
            ),
            required=True,
            default_value="2",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "'qasm3' writes human-readable OpenQASM 3 text. "
                "'qpy' writes compact Qiskit binary (lossless). "
                "'qasm2' writes OpenQASM 2.0 (use this to feed CirqSimulator or other non-Qiskit tools)."
            ),
            required=True,
            default_value="qasm3",
            allowable_values=["qasm3", "qpy", "qasm2"],
        )
        self.descriptors = [self.qubit_count, self.output_format]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qiskit import QuantumCircuit

        fmt = (
            context.getProperty(self.output_format)
            .getValue()
        )

        incoming_fmt = flowFile.getAttribute("circuit.format")
        raw = bytes(flowFile.getContentsAsBytes())

        if incoming_fmt and raw:
            # --- Compose mode: append H gates to an existing circuit ---
            if incoming_fmt == "qpy":
                from qiskit import qpy
                circuit = qpy.load(io.BytesIO(raw))[0]
            elif incoming_fmt == "qasm2":
                from qiskit import qasm2
                circuit = qasm2.loads(
                    raw.decode("utf-8"),
                    custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS,
                )
            else:
                from qiskit import qasm3
                circuit = qasm3.loads(raw.decode("utf-8"))

            n = circuit.num_qubits
            circuit.h(range(n))

            inherited_attrs = {
                "circuit.marked_state":   flowFile.getAttribute("circuit.marked_state") or "",
                "circuit.num_iterations": flowFile.getAttribute("circuit.num_iterations") or "",
            }
        else:
            # --- Standalone mode: fresh uniform-superposition circuit ---
            n = int(
                context.getProperty(self.qubit_count)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )
            circuit = QuantumCircuit(n)
            circuit.h(range(n))
            inherited_attrs = {}

        # Serialize
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
            tc = transpile(circuit, basis_gates=["h", "cx", "rz", "x"], optimization_level=0)
            content = qasm3.dumps(tc).encode("utf-8")

        diagram = str(circuit.draw("text"))
        self.logger.warn("HadamardTransform circuit ({} qubits):\n{}".format(n, diagram))

        ops = circuit.count_ops()
        attrs = {
            "circuit.format":          fmt,
            "circuit.svg": "",  # blank stale Cirq SVG (NiFi merges attrs)
            "circuit.num_qubits":      str(n),
            "circuit.diagram":         diagram,
            "circuit.depth":           str(circuit.depth()),
            "circuit.gate_count":      str(sum(v for k, v in ops.items() if k not in ("barrier", "measure"))),
            "circuit.nonlocal_gates":  str(circuit.num_nonlocal_gates()),
            "circuit.t_count":         str(ops.get("t", 0) + ops.get("tdg", 0)),
            **inherited_attrs,
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
