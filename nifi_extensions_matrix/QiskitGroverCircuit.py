import io

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitGroverCircuit(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a Grover search circuit for a given target bitstring and outputs "
            "the unmeasured circuit in either QPY (Qiskit binary) or OpenQASM 3 format. "
            "Connect to AerSimulator to run the simulation."
        )
        tags = ["quantum", "qiskit", "grover", "circuit"]
        dependencies = ["qiskit>=2.0.0,<2.5"]

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
                "'qpy' is compact and lossless (recommended for Qiskit-to-Qiskit pipelines). "
                "'qasm3' is human-readable OpenQASM 3 text (interoperable with Qiskit tools). "
                "'qasm2' is OpenQASM 2.0 (use this to feed CirqSimulator or other non-Qiskit tools)."
            ),
            required=True,
            default_value="qasm3",
            allowable_values=["qasm3", "qpy", "qasm2"],
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
        from qiskit import QuantumCircuit
        from qiskit.circuit.library import grover_operator

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        target = get(self.marked_state)
        try:
            num_iterations = int(get(self.num_iterations))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QiskitGroverCircuit: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"grover.error": msg},
            )
        barriers = get(self.insert_barriers).lower() == "true"
        fmt = get(self.output_format)
        n = len(target)

        # Phase oracle: flips the sign of |target⟩ only.
        oracle = QuantumCircuit(n)
        for i, bit in enumerate(target):
            if bit == '0':
                oracle.x(i)
        if n == 1:
            oracle.z(0)
        else:
            oracle.h(n - 1)
            oracle.mcx(list(range(n - 1)), n - 1)
            oracle.h(n - 1)
        for i, bit in enumerate(target):
            if bit == '0':
                oracle.x(i)

        grover_op = grover_operator(oracle, insert_barriers=barriers)

        # Full circuit: uniform superposition + N Grover iterations.
        # No measure_all here — measurement is the simulator's responsibility.
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

        diagram = str(circuit.draw("text"))
        self.logger.warn("Grover circuit (target={}, iterations={}):\n{}".format(
            target, num_iterations, diagram
        ))

        ops = circuit.count_ops()
        attrs = {
            "circuit.format":           fmt,
            "circuit.svg": "",  # blank stale Cirq SVG (NiFi merges attrs)
            "circuit.num_qubits":       str(n),
            "circuit.marked_state":     target,
            "circuit.num_iterations":   str(num_iterations),
            "circuit.diagram":          diagram,
            "circuit.depth":            str(circuit.depth()),
            "circuit.gate_count":       str(sum(v for k, v in ops.items() if k not in ("barrier", "measure"))),
            "circuit.nonlocal_gates":   str(circuit.num_nonlocal_gates()),
            "circuit.t_count":          str(ops.get("t", 0) + ops.get("tdg", 0)),
            "circuit.bit_order":        "canonical",
            "builder.component":        "QiskitGrover",
            "builder.framework":        "qiskit",
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
