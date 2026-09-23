from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class ReadoutBaselineCircuit(FlowFileTransform):
    """Readout-error baseline for the Grover N-M hardware matrix.

    Emits the simplest possible circuit that prepares a target bitstring: an
    `x` on every qubit whose target bit is 1, then measurement -- no
    superposition, no entanglement, no two-qubit gates. On ideal hardware the
    target comes back on every shot, so any departure is pure
    state-preparation-and-measurement (SPAM) error for those specific
    physical qubits, isolated from the gate error the Grover builders
    (QiskitGroverCircuit / CirqGroverCircuit / PennylaneGroverCircuit)
    measure. Built with Qiskit regardless of which builder's cell it
    accompanies in a batch: it is a reference circuit, not a version under
    test, so there is no independence requirement here.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a readout-error baseline circuit: prepares a target bitstring "
            "with X gates only (no superposition, no entanglement, no two-qubit "
            "gates) and measures it. Used as the calibration reference in the "
            "Grover N-M hardware matrix so gate error (builder-dependent) can be "
            "separated from readout error (builder-independent) on the same "
            "physical qubits, in the same batch."
        )
        tags = ["quantum", "qiskit", "readout", "calibration", "baseline", "spam"]
        dependencies = ["qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.marked_state = PropertyDescriptor(
            name="Marked State",
            description=(
                "Target bitstring to prepare and measure, e.g. '0111'. Length "
                "sets the number of qubits. Bit order is left-to-right (qubit 0 "
                "= leftmost character), matching every builder in this repo. "
                "Must contain only '0' and '1'."
            ),
            required=True,
            default_value="10",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "Format used to serialize the circuit into the FlowFile content. "
                "Only 'qasm2' (OpenQASM 2.0) is supported -- this is a reference "
                "circuit meant to drop directly into the same hardware batch as "
                "the qasm2 Grover builders."
            ),
            required=True,
            default_value="qasm2",
            allowable_values=["qasm2"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.marked_state,
            self.output_format,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qiskit import QuantumCircuit, qasm2

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        target = get(self.marked_state) or ""
        fmt = get(self.output_format)

        if not target or any(bit not in ("0", "1") for bit in target):
            msg = (
                "Marked State must be a non-empty binary string (only '0'/'1' "
                "characters); got {!r}".format(target)
            )
            self.logger.error("ReadoutBaselineCircuit: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"grover.error": msg},
            )

        n = len(target)

        # Prepare exactly |target>: an X on every qubit whose target bit is 1.
        # Bit order is q0-left: target[0] is qubit 0.
        circuit = QuantumCircuit(n)
        for i, bit in enumerate(target):
            if bit == "1":
                circuit.x(i)
        circuit.measure_all()

        content = qasm2.dumps(circuit).encode("utf-8")

        diagram = str(circuit.draw("text"))
        self.logger.warn("Readout baseline circuit (target={}):\n{}".format(target, diagram))

        ops = circuit.count_ops()
        gate_count = sum(v for k, v in ops.items() if k not in ("barrier", "measure"))

        attrs = {
            "circuit.format":         fmt,
            "circuit.svg":            "",  # blank stale Cirq SVG (NiFi merges attrs)
            "circuit.num_qubits":     str(n),
            "circuit.marked_state":   target,
            "circuit.diagram":        diagram,
            "circuit.depth":          str(circuit.depth()),
            "circuit.gate_count":     str(gate_count),
            "circuit.nonlocal_gates": str(circuit.num_nonlocal_gates()),
            "circuit.t_count":        str(ops.get("t", 0) + ops.get("tdg", 0)),
            "circuit.bit_order":      "q0_left",
            "circuit.qasm2":          content.decode("utf-8"),
            "builder.component":      "ReadoutBaseline",
            "builder.framework":      "qiskit",
            "grover.kind":            "readout",
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
