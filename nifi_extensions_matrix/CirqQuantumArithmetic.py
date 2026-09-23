import os
import sys

# NiFi loads each processor in its own module context without the extensions
# directory on sys.path, so the sibling-module import below fails without this.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder

import arithmetic_spec as aspec


class CirqQuantumArithmetic(FlowFileTransform):
    """
    Builds a reversible integer addition circuit in Cirq.

    Cirq ships no high-level adder.  It offers ``cirq.ArithmeticGate``, a
    primitive for defining arithmetic on classical values, but nothing
    equivalent to Qiskit's ``CDKMRippleCarryAdder`` or Qrisp's typed
    ``QuantumFloat`` arithmetic.  Building an adder in Cirq means writing the
    carry logic gate by gate.  This processor packages that work as a
    configurable canvas component, in exactly the sense that
    ``CirqGroverOperator`` packages the Grover operator Cirq also lacks: a
    missing high-level routine turned into a reusable flow artifact.

    Two implementations are offered because they are structurally unlike each
    other, which is what makes them useful as independent versions:

    ``ripple_carry``
        The Cuccaro MAJ/UMA construction.  Carries propagate through a chain
        of Toffoli gates; one ancilla holds the incoming carry.

    ``qft``
        Draper's Fourier-basis adder.  The sum register is transformed, the
        addend contributes controlled phase rotations, and the register is
        transformed back.  There is no carry chain and no Toffoli at all.

    The register layout matches the Qiskit and Qrisp arithmetic processors:
    operand A on the first n qubits, operand B on the next n, carry-out
    immediately after, and (for ``ripple_carry``) one ancilla last.  The sum
    is written in place over B, so the result register is B plus the
    carry-out.

    Emits qasm2 with no measurement.  The classical answer travels alongside
    as arithmetic.expected_result, with the full expected counts key as
    arithmetic.expected_bitstring in the project's canonical q0-left order.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a reversible integer adder circuit in Cirq, which ships no "
            "high-level adder of its own. Choose the Cuccaro ripple-carry "
            "construction or Draper's Fourier-basis adder. Configure Operand A, "
            "Operand B, and Bit Width. Outputs qasm2 with no measurement, plus the "
            "classical expected result for a deterministic-output oracle."
        )
        tags = ["quantum", "cirq", "arithmetic", "adder", "ripple-carry",
                "draper", "reuse", "differential-testing", "circuit"]
        dependencies = ["cirq>=1.6.0"]

    IMPLEMENTATIONS = ("ripple_carry", "qft")

    #: extra qubits beyond the 2n operand qubits, per implementation
    _EXTRA_QUBITS = {"ripple_carry": 2, "qft": 1}

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.implementation = PropertyDescriptor(
            name="Implementation",
            description=(
                "ripple_carry uses the Cuccaro MAJ/UMA carry chain (Toffoli "
                "based); qft uses Draper's Fourier-basis addition (no carry "
                "chain, no Toffoli). Both compute the same function."
            ),
            required=True,
            default_value="ripple_carry",
            allowable_values=list(self.IMPLEMENTATIONS),
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.operation = PropertyDescriptor(
            name="Operation",
            description=(
                "Arithmetic operation. Only 'add' is implemented here; the "
                "property exists so the FlowFile contract matches the Qiskit and "
                "Qrisp arithmetic processors."
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
            description="Second operand, encoded into the B register. The sum is "
                        "written in place over this register plus the carry-out.",
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

    # -- circuit construction ------------------------------------------------

    @staticmethod
    def _maj(cirq, x, y, z):
        """Majority: writes the carry of (x, y, z) into z, in place."""
        yield cirq.CNOT(z, y)
        yield cirq.CNOT(z, x)
        yield cirq.TOFFOLI(x, y, z)

    @staticmethod
    def _uma(cirq, x, y, z):
        """Un-majority and add: the inverse bookkeeping of _maj, leaving the sum."""
        yield cirq.TOFFOLI(x, y, z)
        yield cirq.CNOT(z, x)
        yield cirq.CNOT(x, y)

    @classmethod
    def _ripple_carry(cls, cirq, q, n):
        """Cuccaro ripple-carry adder: b += a, carry-out into q[2n]."""
        a = [q[i] for i in range(n)]
        b = [q[n + i] for i in range(n)]
        cout, ancilla = q[2 * n], q[2 * n + 1]
        # carry into stage i: the ancilla for stage 0, then a_{i-1}, which by
        # then holds the carry the previous MAJ left behind.
        carries = [ancilla] + a[:-1]
        for i in range(n):
            yield from cls._maj(cirq, carries[i], b[i], a[i])
        yield cirq.CNOT(a[n - 1], cout)
        for i in reversed(range(n)):
            yield from cls._uma(cirq, carries[i], b[i], a[i])

    @classmethod
    def _qft_adder(cls, cirq, q, n):
        """Draper Fourier-basis adder: b += a, carry-out into q[2n]."""
        a = [q[i] for i in range(n)]
        total = [q[n + i] for i in range(n)] + [q[2 * n]]   # LSB first
        m = len(total)
        order = list(reversed(range(m)))                    # MSB first

        # QFT over the sum register, swaps omitted (the phase kicks below and
        # the inverse transform use the same ordering, so they cancel out).
        for pos, target in enumerate(order):
            yield cirq.H(total[target])
            for step, control in enumerate(order[pos + 1:], start=1):
                yield cirq.CZ(total[control], total[target]) ** (1 / 2 ** step)

        # In the Fourier basis, adding a is a product of controlled phases:
        # bit a_i contributes 2**i, so it rotates the qubit holding weight
        # 2**p by 2*pi / 2**(p - i + 1).
        for target in order:
            for i in range(n):
                step = target - i + 1
                if step >= 1:
                    yield cirq.CZ(a[i], total[target]) ** (1 / 2 ** (step - 1))

        for pos in reversed(range(m)):
            target = order[pos]
            for step, control in enumerate(order[pos + 1:], start=1):
                yield cirq.CZ(total[control], total[target]) ** (-1 / 2 ** step)
            yield cirq.H(total[target])

    @classmethod
    def _layout(cls, implementation, n):
        extra = cls._EXTRA_QUBITS[implementation]
        return aspec.RegisterLayout(
            num_qubits=2 * n + extra,
            a_qubits=tuple(range(n)),
            b_qubits=tuple(range(n, 2 * n)),
            result_qubits=tuple(range(n, 2 * n)) + (2 * n,),
            ancilla_qubits=(2 * n + 1,) if extra == 2 else (),
        )

    def _fail(self, message):
        self.logger.error(message)
        return FlowFileTransformResult(
            relationship="failure",
            attributes={"arithmetic.error": message},
        )

    # -- transform -----------------------------------------------------------

    def transform(self, context, flowFile):
        import cirq

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        implementation = (get(self.implementation) or "").strip().lower()
        operation = (get(self.operation) or "add").strip().lower()

        if implementation not in self.IMPLEMENTATIONS:
            return self._fail(
                "CirqQuantumArithmetic: unknown implementation '{}'; expected one "
                "of {}".format(implementation, ", ".join(self.IMPLEMENTATIONS)))

        if operation != "add":
            return self._fail(
                "CirqQuantumArithmetic: operation '{}' is not implemented; only "
                "'add' is available on this processor".format(operation))

        try:
            a = int(get(self.operand_a))
            b = int(get(self.operand_b))
            n = int(get(self.bit_width))
        except (TypeError, ValueError) as exc:
            return self._fail(
                "CirqQuantumArithmetic: non-integer operand or bit width ({})".format(exc))

        try:
            aspec.validate_operands(operation, a, b, n)
        except aspec.ArithmeticSpecError as exc:
            return self._fail("CirqQuantumArithmetic: {}".format(exc))

        layout = self._layout(implementation, n)
        qubits = cirq.LineQubit.range(layout.num_qubits)
        circuit = cirq.Circuit()

        for i in range(n):
            if (a >> i) & 1:
                circuit.append(cirq.X(qubits[layout.a_qubits[i]]))
            if (b >> i) & 1:
                circuit.append(cirq.X(qubits[layout.b_qubits[i]]))

        if implementation == "ripple_carry":
            circuit.append(self._ripple_carry(cirq, qubits, n))
        else:
            circuit.append(self._qft_adder(cirq, qubits, n))

        try:
            content = cirq.qasm(circuit).encode("utf-8")
        except Exception as exc:                      # pragma: no cover - defensive
            return self._fail(
                "CirqQuantumArithmetic: could not serialise to OpenQASM 2.0 ({})".format(exc))

        ops = list(circuit.all_operations())
        two_qubit = sum(1 for op in ops if len(op.qubits) == 2)
        three_qubit = sum(1 for op in ops if len(op.qubits) == 3)

        # SVG + text diagram for the report card; fall back gracefully.
        try:
            from cirq.contrib.svg import circuit_to_svg
            svg = circuit_to_svg(circuit)
        except Exception as exc:                      # pragma: no cover - defensive
            self.logger.warn("circuit_to_svg failed: {}".format(exc))
            svg = ""
        diagram = str(circuit)

        attrs = {
            "circuit.format":         "qasm2",
            "circuit.framework":      "cirq",
            # Cirq emits svg + qasm2, never qasm3 — blank any stale upstream qasm3.
            "circuit.qasm3":          "",
            "circuit.num_qubits":     str(layout.num_qubits),
            "circuit.diagram":        diagram,
            "circuit.svg":            svg,
            "circuit.depth":          str(len(cirq.Circuit(circuit).moments)),
            "circuit.gate_count":     str(len(ops)),
            # Toffolis are counted here too: they are non-local, and a
            # ripple-carry adder is mostly Toffolis.
            "circuit.nonlocal_gates": str(two_qubit + three_qubit),
            "circuit.toffoli_count":  str(three_qubit),
        }
        attrs.update(aspec.describe(
            operation, a, b, n, layout,
            implementation=implementation,
            framework="cirq",
        ))

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
