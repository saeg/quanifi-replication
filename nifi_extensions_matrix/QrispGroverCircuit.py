import contextlib
import io

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QrispGroverCircuit(FlowFileTransform):
    """Build-only Qrisp Grover builder.

    Qrisp's own Grover entry point (``QrispGroverSearch``) builds *and* samples in
    one processor, so its branch cannot be split into a builder factor and an
    engine factor. This processor builds the circuit and stops, emitting
    OpenQASM 2.0 so that any counts engine in the library can execute it.

    Bit order is normalised **in the circuit**, not in the counts. Qrisp decodes a
    raw ``QuantumVariable`` little-endian (``qv[0]`` is the rightmost character),
    so an unmodified export sampled on Aer returns ``011`` when ``110`` was
    requested. ``QrispGroverSearch`` compensates by reversing the keys of the
    counts it produced itself; a builder cannot do that, because the reversal has
    to survive a hand-off to an engine it does not control. The qubits are
    therefore reversed here, and ``circuit.bit_order = canonical`` declares it.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a Qrisp Grover search circuit for a given target bitstring and "
            "outputs the unmeasured circuit as OpenQASM 2.0, so it can be executed "
            "by any Quanifi counts engine. Qubit order is normalised to the "
            "canonical convention (qubit 0 = leftmost character). Use "
            "QrispGroverSearch instead if you want Qrisp to build and sample in one "
            "processor."
        )
        tags = ["quantum", "qrisp", "grover", "circuit"]
        dependencies = ["qrisp", "qiskit>=2.0.0,<2.5"]

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
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "Format used to serialize the circuit into the FlowFile content. "
                "Only 'qasm2' is supported: it is the format every counts engine in "
                "the library accepts, and it is what makes this builder "
                "interchangeable with the Qiskit, Cirq and PennyLane builders."
            ),
            required=True,
            default_value="qasm2",
            allowable_values=["qasm2"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.marked_state,
            self.num_iterations,
            self.output_format,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qrisp import QuantumVariable
        from qrisp.grover import grovers_alg, tag_state

        def get(prop):
            return (
                context.getProperty(prop)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )

        target = get(self.marked_state)
        try:
            num_iterations = int(get(self.num_iterations))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QrispGroverCircuit: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"grover.error": msg},
            )
        if set(target) - {"0", "1"}:
            msg = "Marked State must be a bitstring, got {!r}".format(target)
            self.logger.error("QrispGroverCircuit: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"grover.error": msg},
            )
        fmt = get(self.output_format)
        n = len(target)

        try:
            # Qrisp prints a tqdm progress bar; NiFi's py4j bridge uses stdout, so
            # anything written there corrupts the channel ("null response" crash).
            with contextlib.redirect_stdout(io.StringIO()):
                qv = QuantumVariable(n)

                def oracle(qv):
                    tag_state({qv: target}, binary_values=True)

                if num_iterations == 0:
                    grovers_alg(qv, oracle)
                else:
                    grovers_alg(qv, oracle, iterations=num_iterations)
                qiskit_circuit = qv.qs.compile().to_qiskit()
        except Exception as exc:
            msg = "Qrisp circuit construction failed: {}".format(exc)
            self.logger.error("QrispGroverCircuit: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"grover.error": msg},
            )

        circuit = self._to_canonical_bit_order(qiskit_circuit, n)

        try:
            content = self._to_qasm2(circuit)
        except Exception as exc:
            msg = "OpenQASM 2.0 export failed: {}".format(exc)
            self.logger.error("QrispGroverCircuit: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"grover.error": msg},
            )

        diagram = str(circuit.draw("text"))
        ops = circuit.count_ops()
        attrs = {
            "circuit.format":         fmt,
            "circuit.svg":            "",  # blank stale Cirq SVG (NiFi merges attrs)
            "circuit.qasm3":          "",  # never emitted here; do not leave stale text
            "circuit.qasm2":          content.decode("utf-8"),
            "circuit.num_qubits":     str(n),
            "circuit.marked_state":   target,
            "circuit.num_iterations": str(num_iterations),
            "circuit.bit_order":      "canonical",
            "circuit.diagram":        diagram,
            "circuit.depth":          str(circuit.depth()),
            "circuit.gate_count":     str(sum(
                v for k, v in ops.items() if k not in ("barrier", "measure"))),
            "circuit.nonlocal_gates": str(circuit.num_nonlocal_gates()),
            "circuit.t_count":        str(ops.get("t", 0) + ops.get("tdg", 0)),
            "builder.component":      "QrispGrover",
            "builder.framework":      "qrisp",
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )

    @staticmethod
    def _to_canonical_bit_order(qiskit_circuit, n):
        """Reverse the data qubits so qubit 0 carries the leftmost target bit.

        Qrisp's compiled circuit may allocate ancillas beyond the ``n`` data
        qubits; only the data qubits are reversed, and the ancillas keep their
        positions above them.
        """
        from qiskit import QuantumCircuit

        width = qiskit_circuit.num_qubits
        if width < n:
            raise ValueError(
                "compiled circuit has {} qubits, fewer than the {} the marked "
                "state needs".format(width, n)
            )
        order = list(range(n - 1, -1, -1)) + list(range(n, width))
        out = QuantumCircuit(width)
        out.compose(qiskit_circuit, qubits=order, inplace=True)
        out.global_phase = qiskit_circuit.global_phase
        return out

    @staticmethod
    def _to_qasm2(circuit):
        """Serialise to OpenQASM 2.0 through a basis every engine parses."""
        from qiskit import qasm2, transpile

        tc = transpile(
            circuit,
            basis_gates=["h", "cx", "rz", "x"],
            optimization_level=0,
        )
        return qasm2.dumps(tc).encode("utf-8")
