import os
import sys

# NiFi loads each processor in its own module context without the extensions
# directory on sys.path, so the sibling-module import below fails without this.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder

import arithmetic_spec as aspec


class QiskitQuantumArithmetic(FlowFileTransform):
    """
    Builds a reversible integer addition circuit using one of the three
    independent adder implementations Qiskit ships.

    The point of exposing all three is differential testing.  ``cdkm`` and
    ``vbe`` are ripple-carry constructions with an explicit carry chain;
    ``draper`` adds in the Fourier basis and has no carry chain at all.  At
    2 bits they transpile to 33, 34, and 8 two-qubit gates respectively, so
    they are genuinely different programs computing one specification rather
    than three spellings of the same circuit.  That is what makes an
    N-version comparison between them meaningful.

    All three are used with ``kind="half"``, which gives the uniform register
    layout this processor's contract depends on: operand A on the first n
    qubits, operand B on the next n, and the carry-out immediately after.  The
    sum lands in place over B plus the carry-out, so the result register is
    ``b + cout``.  The layout is read back from the adder's own registers at
    build time rather than hardcoded, so a Qiskit change to the ordering is
    caught by the equivalence tests instead of silently producing wrong
    expectations.

    Emits qasm2 with no measurement, so the circuit can be run by
    QiskitAerSimulator, CirqSimulator, or QrispSimulator.  The classical
    answer travels with it as arithmetic.expected_result and the full expected
    counts key as arithmetic.expected_bitstring, in the project's canonical
    q0-left order.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a reversible integer adder circuit with one of Qiskit's three "
            "independent implementations (CDKM ripple-carry, VBE ripple-carry, "
            "Draper QFT). Configure Implementation, Operand A, Operand B, and Bit "
            "Width. Outputs qasm2 with no measurement, plus the classical expected "
            "result and expected counts key for a deterministic-output oracle."
        )
        tags = ["quantum", "qiskit", "arithmetic", "adder", "ripple-carry",
                "draper", "differential-testing", "circuit"]
        dependencies = ["qiskit>=2.0.0,<2.5"]

    #: Implementation name -> (qiskit class name, human description)
    IMPLEMENTATIONS = {
        "cdkm":   ("CDKMRippleCarryAdder", "Cuccaro ripple-carry"),
        "vbe":    ("VBERippleCarryAdder",  "Vedral-Barenco-Ekert ripple-carry"),
        "draper": ("DraperQFTAdder",       "Draper QFT-basis adder"),
    }

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.implementation = PropertyDescriptor(
            name="Implementation",
            description=(
                "Which of Qiskit's independent adder constructions to use. "
                "cdkm and vbe are ripple-carry with an explicit carry chain; "
                "draper adds in the Fourier basis and uses far fewer two-qubit "
                "gates. All three compute the same function."
            ),
            required=True,
            default_value="cdkm",
            allowable_values=["cdkm", "vbe", "draper"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.operation = PropertyDescriptor(
            name="Operation",
            description=(
                "Arithmetic operation. Only 'add' is supported by Qiskit's adder "
                "family; the property exists so the FlowFile contract matches the "
                "Cirq and Qrisp arithmetic processors."
            ),
            required=True,
            default_value="add",
            allowable_values=["add"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.operand_a = PropertyDescriptor(
            name="Operand A",
            description="First operand, encoded into the A register.",
            required=True,
            default_value="2",
            validators=[StandardValidators.INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.operand_b = PropertyDescriptor(
            name="Operand B",
            description="Second operand, encoded into the B register. The sum "
                        "is written in place over this register plus the carry-out.",
            required=True,
            default_value="3",
            validators=[StandardValidators.INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.bit_width = PropertyDescriptor(
            name="Bit Width",
            description="Width of each operand register in qubits.",
            required=True,
            default_value="2",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )

        self.descriptors = [
            self.implementation,
            self.operation,
            self.operand_a,
            self.operand_b,
            self.bit_width,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _build_adder(implementation, n):
        """Return (circuit, RegisterLayout) for one implementation at width n."""
        from qiskit.circuit.library import (
            CDKMRippleCarryAdder, VBERippleCarryAdder, DraperQFTAdder,
        )
        classes = {
            "cdkm":   CDKMRippleCarryAdder,
            "vbe":    VBERippleCarryAdder,
            "draper": DraperQFTAdder,
        }
        adder = classes[implementation](n, kind="half")

        # Read the layout back from the adder rather than assuming it.
        index_of = {q: i for i, q in enumerate(adder.qubits)}
        regs = {r.name: tuple(index_of[q] for q in r) for r in adder.qregs}
        a_qubits = regs["a"]
        b_qubits = regs["b"]
        cout = regs.get("cout", ())
        ancilla = tuple(
            i for name, idx in regs.items() if name not in ("a", "b", "cout")
            for i in idx
        )
        layout = aspec.RegisterLayout(
            num_qubits=adder.num_qubits,
            a_qubits=a_qubits,
            b_qubits=b_qubits,
            result_qubits=b_qubits + cout,
            ancilla_qubits=ancilla,
        )
        return adder, layout

    def _fail(self, message):
        self.logger.error(message)
        return FlowFileTransformResult(
            relationship="failure",
            attributes={"arithmetic.error": message},
        )

    # -- transform -----------------------------------------------------------

    def transform(self, context, flowFile):
        from qiskit import QuantumCircuit
        from qiskit import qasm2 as qiskit_qasm2
        from qiskit.compiler import transpile

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        implementation = (get(self.implementation) or "").strip().lower()
        operation = (get(self.operation) or "add").strip().lower()

        if implementation not in self.IMPLEMENTATIONS:
            return self._fail(
                "QiskitQuantumArithmetic: unknown implementation '{}'; expected one "
                "of {}".format(implementation, ", ".join(sorted(self.IMPLEMENTATIONS))))

        if operation != "add":
            return self._fail(
                "QiskitQuantumArithmetic: operation '{}' is not supported by Qiskit's "
                "adder family; only 'add' is available on this processor".format(operation))

        try:
            a = int(get(self.operand_a))
            b = int(get(self.operand_b))
            n = int(get(self.bit_width))
        except (TypeError, ValueError) as exc:
            return self._fail(
                "QiskitQuantumArithmetic: non-integer operand or bit width ({})".format(exc))

        try:
            aspec.validate_operands(operation, a, b, n)
        except aspec.ArithmeticSpecError as exc:
            return self._fail("QiskitQuantumArithmetic: {}".format(exc))

        adder, layout = self._build_adder(implementation, n)

        # Encode the operands, then apply the adder. No measurement is added:
        # the runner owns that, per the shared circuit contract.
        circuit = QuantumCircuit(adder.num_qubits)
        for i in range(n):
            if (a >> i) & 1:
                circuit.x(layout.a_qubits[i])
            if (b >> i) & 1:
                circuit.x(layout.b_qubits[i])
        circuit.compose(adder, inplace=True)

        try:
            content = qiskit_qasm2.dumps(circuit.decompose(reps=4)).encode("utf-8")
        except Exception as exc:                      # pragma: no cover - defensive
            return self._fail(
                "QiskitQuantumArithmetic: could not serialise to OpenQASM 2.0 ({})".format(exc))

        decomposed = transpile(
            circuit,
            basis_gates=['h', 'cx', 'p', 'cp', 'swap', 'x', 'z', 'rz', 's', 't',
                         'sdg', 'tdg', 'id'],
            optimization_level=0,
        )
        ops = decomposed.count_ops()
        two_qubit_names = {'cx', 'cz', 'cy', 'ch', 'cp', 'crz', 'crx', 'cry',
                           'cu', 'ccx', 'swap'}

        attrs = {
            "circuit.format":         "qasm2",
            "circuit.svg": "",  # blank stale Cirq SVG (NiFi merges attrs)
            "circuit.framework":      "qiskit",
            "circuit.num_qubits":     str(circuit.num_qubits),
            "circuit.depth":          str(decomposed.depth()),
            "circuit.gate_count":     str(sum(ops.values())),
            "circuit.nonlocal_gates": str(sum(c for g, c in ops.items()
                                              if g in two_qubit_names)),
            "circuit.t_count":        str(ops.get('t', 0) + ops.get('tdg', 0)),
        }
        attrs.update(aspec.describe(
            operation, a, b, n, layout,
            implementation=implementation,
            framework="qiskit",
        ))

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
