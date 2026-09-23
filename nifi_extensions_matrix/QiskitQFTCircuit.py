import io

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitQFTCircuit(FlowFileTransform):
    """
    Applies the Quantum Fourier Transform (or its inverse) to a circuit.

    Two modes, chosen automatically from the incoming FlowFile:

      Standalone — no circuit.format attribute on the FlowFile (e.g. from
        GenerateFlowFile): creates a fresh n-qubit QFT circuit.

      Compose — circuit.format is set: loads the existing circuit from the
        FlowFile content and appends the QFT to all its qubits.  The Qubit
        Count property is ignored; the circuit size is taken from the input.

    The QFT is the building block of QPE, Shor's algorithm, and quantum
    arithmetic.  Setting Inverse = true produces QFT†, needed for the
    read-out stage of QPE.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Applies the Quantum Fourier Transform (QFT) or its inverse to a circuit. "
            "In standalone mode a fresh n-qubit QFT circuit is created. "
            "In compose mode the QFT is appended to an existing circuit received "
            "via the FlowFile content."
        )
        tags = ["quantum", "qiskit", "qft", "fourier", "circuit"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-qasm3-import"]

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
            description="Apply QFT† (inverse) instead of QFT. Required for the readout stage of QPE.",
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
        )
        self.approximation_degree = PropertyDescriptor(
            name="Approximation Degree",
            description=(
                "Drops rotation gates smaller than 2π/2^(n-degree). "
                "0 = exact QFT. Higher values reduce circuit depth at the cost "
                "of precision. Useful for near-term hardware."
            ),
            required=True,
            default_value="0",
            validators=[StandardValidators.INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.do_swaps = PropertyDescriptor(
            name="Do Swaps",
            description=(
                "Include the bit-reversal SWAP network at the end of the QFT "
                "(standard convention). Set to false when the output qubit order "
                "does not matter, e.g. when the result feeds directly into QFT†."
            ),
            required=True,
            default_value="true",
            allowable_values=["true", "false"],
        )
        self.insert_barriers = PropertyDescriptor(
            name="Insert Barriers",
            description="Add barriers between QFT stages for clearer circuit diagrams.",
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "'qasm3' writes human-readable OpenQASM 3. "
                "'qpy' writes compact Qiskit binary (lossless). "
                "'qasm2' writes OpenQASM 2.0 (use this to feed CirqSimulator or other non-Qiskit tools)."
            ),
            required=True,
            default_value="qasm3",
            allowable_values=["qasm3", "qpy", "qasm2"],
        )
        self.descriptors = [
            self.qubit_count,
            self.inverse,
            self.approximation_degree,
            self.do_swaps,
            self.insert_barriers,
            self.output_format,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qiskit import QuantumCircuit
        from qiskit.circuit.library import QFT

        def get(prop):
            return (
                context.getProperty(prop)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )

        fmt         = context.getProperty(self.output_format).getValue()
        inverse     = get(self.inverse).lower() == "true"
        approx      = int(get(self.approximation_degree))
        do_swaps    = get(self.do_swaps).lower() == "true"
        barriers    = get(self.insert_barriers).lower() == "true"

        incoming_fmt = flowFile.getAttribute("circuit.format")
        raw          = bytes(flowFile.getContentsAsBytes())

        if incoming_fmt and raw:
            # --- Compose mode: append QFT to existing circuit ---------------
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
            inherited = {
                "circuit.marked_state":   flowFile.getAttribute("circuit.marked_state") or "",
                "circuit.num_iterations": flowFile.getAttribute("circuit.num_iterations") or "",
                "circuit.state_type":     flowFile.getAttribute("circuit.state_type") or "",
            }
        else:
            # --- Standalone mode: fresh QFT circuit -------------------------
            n = int(get(self.qubit_count))
            circuit  = QuantumCircuit(n)
            inherited = {}

        qft = QFT(
            num_qubits=n,
            approximation_degree=approx,
            do_swaps=do_swaps,
            inverse=inverse,
            insert_barriers=barriers,
        )
        circuit.compose(qft, inplace=True)

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
            # Transpile to stdgates.inc primitives so openqasm3 1.0.1 can parse
            # the output — custom gate bodies (e.g. QFT, cp) break the ANTLR grammar.
            tc = transpile(circuit, basis_gates=["h", "cx", "rz", "x"], optimization_level=0)
            content = qasm3.dumps(tc).encode("utf-8")

        diagram = str(circuit.draw("text"))
        self.logger.warn("QFT circuit ({}{} qubits):\n{}".format(
            "inverse, " if inverse else "", n, diagram
        ))

        ops = circuit.count_ops()
        attrs = {
            "circuit.format":            fmt,
            "circuit.svg": "",  # blank stale Cirq SVG (NiFi merges attrs)
            "circuit.num_qubits":        str(n),
            "circuit.qft_inverse":       str(inverse).lower(),
            "circuit.qft_approx_degree": str(approx),
            "circuit.diagram":           diagram,
            "circuit.depth":             str(circuit.depth()),
            "circuit.gate_count":        str(sum(v for k, v in ops.items() if k not in ("barrier", "measure"))),
            "circuit.nonlocal_gates":    str(circuit.num_nonlocal_gates()),
            "circuit.t_count":           str(ops.get("t", 0) + ops.get("tdg", 0)),
            **{k: v for k, v in inherited.items() if v},
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
