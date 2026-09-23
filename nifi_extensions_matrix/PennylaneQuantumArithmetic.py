import os
import re
import sys

# NiFi loads each processor in its own module context without the extensions
# directory on sys.path, so the sibling-module import below fails without this.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder

import arithmetic_spec as aspec


def _strip_measurements(qasm):
    """Drop the trailing measure/creg lines PennyLane's to_openqasm emits.

    Circuit builders in Quanifi are measurement-free — measurement belongs to
    the simulator or the hardware runner — so the exported QASM must contain
    only the encoding gates. Same helper as PennylaneFeatureEmbedding.
    """
    kept = [ln for ln in qasm.splitlines()
            if not re.match(r"\s*(measure|creg)\b", ln)]
    return "\n".join(kept) + "\n"


class PennylaneQuantumArithmetic(FlowFileTransform):
    """
    Builds a reversible integer addition circuit with PennyLane's arithmetic
    templates. ``SemiAdder`` is the confirmatory implementation; ``OutAdder``
    remains available only to reproduce the superseded pilot campaign.

    This is the third *framework* lane of the arithmetic differential
    experiment, alongside Qiskit and Cirq. It exists because the Qrisp lane
    cannot serve that role: Qrisp specialises its compiled circuit on the
    classical operand values, so every one of its five adders emits a
    different arithmetic body for different inputs (measured on the
    boundary:2 suite: ``default`` and ``fourier`` differ on all seven cases,
    ``qcla`` on two). A differential experiment that varies the input while
    claiming to hold the program fixed cannot use a builder that changes the
    program with the input.

    Both templates are built from *wires* rather than from operand values, so
    the body is identical for every operand pair by construction — verified
    across the whole suite by
    ``tests/test_fixed_locus_mutation.py::test_fixed_locus_is_operand_invariant``.

    ``SemiAdder`` writes the sum over an enlarged, ``n+1``-wire B register and
    uses ``n`` work wires, for ``3n+1`` wires total. The legacy ``OutAdder`` is
    out-of-place and uses a separate result register plus two work wires, for
    ``3n+3`` wires total.

    **Endianness.** PennyLane's arithmetic templates are big-endian: the first
    wire of each list is the most significant bit. Every other processor in
    this project is little-endian in wire space (bit *i* on wire *i*), and the
    ``q0_left`` counts convention, ``arithmetic_spec.describe`` and
    ``QuantumMutator``'s carry detection all assume it. Rather than special-case
    those, this builder allocates wires the project's way and passes each list
    to ``OutAdder`` reversed. Getting this wrong is silent: 0 and 3 are
    palindromes over two bits (``00``/``11``), so a little-endian encoding
    still produces the right answer for 0+0, 0+3, 3+0 and 3+3, and fails only
    on operands containing a 1.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a reversible integer adder with PennyLane's SemiAdder from "
            "Operation, Operand A, Operand B and Bit Width. Outputs qasm2 with "
            "no measurement, plus the classical expected result for a "
            "deterministic-output oracle."
        )
        tags = ["quantum", "pennylane", "arithmetic", "adder", "semi-in-place",
                "differential-testing", "circuit"]
        # PennyLane only. Deliberately NOT pennylane-qiskit: this builder does
        # not need it, and pulling qiskit in would drag the <2.5 pin (see
        # tests/test_processor_dependencies.py) into a lane that has no reason
        # to care about qiskit's version.
        dependencies = ["pennylane>=0.40"]

    IMPLEMENTATIONS = ("semiadder", "outadder")

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.implementation = PropertyDescriptor(
            name="Implementation",
            description=(
                "semiadder uses qml.SemiAdder and writes x+y over an enlarged "
                "Y register; it is the confirmatory implementation. outadder "
                "uses the superseded Fourier-basis qml.OutAdder lane and is "
                "retained only for replaying historical pilots."
            ),
            required=True,
            default_value="semiadder",
            allowable_values=list(self.IMPLEMENTATIONS),
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.operation = PropertyDescriptor(
            name="Operation",
            description=(
                "Arithmetic operation. Only 'add' is implemented here; the "
                "property exists so the FlowFile contract matches the Qiskit, "
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
            description="Second operand, encoded into the B register. SemiAdder "
                        "writes the sum over an enlarged B register; legacy "
                        "OutAdder writes it into a separate result register.",
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

    # -- register layout -----------------------------------------------------

    @staticmethod
    def _outadder_layout(n):
        """Wire assignment, little-endian, in the project's convention.

        a on 0..n-1, b on n..2n-1, the (n+1)-wide result on 2n..3n, and two
        work wires last. The carry-out is therefore the HIGHEST result index,
        which is what ``QuantumMutator._mut_break_carry`` assumes when it takes
        ``max(result_qubits)``; a big-endian allocation would silently point
        carry.break at the result's least significant bit instead.
        """
        a_qubits = tuple(range(n))
        b_qubits = tuple(range(n, 2 * n))
        result_qubits = tuple(range(2 * n, 3 * n + 1))
        work_qubits = (3 * n + 1, 3 * n + 2)
        return aspec.RegisterLayout(
            num_qubits=3 * n + 3,
            a_qubits=a_qubits,
            b_qubits=b_qubits,
            result_qubits=result_qubits,
            ancilla_qubits=work_qubits,
        )

    @staticmethod
    def _semiadder_layout(n):
        """Semi-in-place layout: n-bit A, (n+1)-bit B/result and n work wires."""
        a_qubits = tuple(range(n))
        result_qubits = tuple(range(n, 2 * n + 1))
        work_qubits = tuple(range(2 * n + 1, 3 * n + 1))
        return aspec.RegisterLayout(
            num_qubits=3 * n + 1,
            a_qubits=a_qubits,
            b_qubits=result_qubits[:n],
            result_qubits=result_qubits,
            ancilla_qubits=work_qubits,
        )

    @classmethod
    def _layout(cls, n, implementation="semiadder"):
        """Return the implementation-specific layout (compatibility helper)."""
        if implementation == "outadder":
            return cls._outadder_layout(n)
        return cls._semiadder_layout(n)

    def _fail(self, message):
        self.logger.error(message)
        return FlowFileTransformResult(
            relationship="failure",
            attributes={"arithmetic.error": message},
        )

    # -- transform -----------------------------------------------------------

    def transform(self, context, flowFile):
        import pennylane as qml

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        implementation = (get(self.implementation) or "").strip().lower()
        operation = (get(self.operation) or "add").strip().lower()

        if implementation not in self.IMPLEMENTATIONS:
            return self._fail(
                "PennylaneQuantumArithmetic: unknown implementation '{}'; expected "
                "one of {}".format(implementation, ", ".join(self.IMPLEMENTATIONS)))

        if operation != "add":
            return self._fail(
                "PennylaneQuantumArithmetic: operation '{}' is not implemented; "
                "only 'add' is available on this processor".format(operation))

        try:
            a = int(get(self.operand_a))
            b = int(get(self.operand_b))
            n = int(get(self.bit_width))
        except (TypeError, ValueError) as exc:
            return self._fail(
                "PennylaneQuantumArithmetic: non-integer operand or bit width "
                "({})".format(exc))

        try:
            aspec.validate_operands(operation, a, b, n)
        except aspec.ArithmeticSpecError as exc:
            return self._fail("PennylaneQuantumArithmetic: {}".format(exc))

        layout = self._layout(n, implementation)
        dev = qml.device("default.qubit", wires=layout.num_qubits)

        @qml.qnode(dev)
        def circuit():
            # Operand preparation: one X per set bit, on the project's wires.
            for i in range(n):
                if (a >> i) & 1:
                    qml.PauliX(wires=layout.a_qubits[i])
                if (b >> i) & 1:
                    qml.PauliX(wires=layout.b_qubits[i])
            # Reversed lists: PennyLane wants most-significant wire first.
            if implementation == "semiadder":
                qml.SemiAdder(
                    list(layout.a_qubits)[::-1],
                    list(layout.result_qubits)[::-1],
                    list(layout.ancilla_qubits)[::-1],
                )
            else:
                qml.OutAdder(
                    list(layout.a_qubits)[::-1],
                    list(layout.b_qubits)[::-1],
                    list(layout.result_qubits)[::-1],
                    mod=2 ** (n + 1),
                    work_wires=list(layout.ancilla_qubits),
                )
            return qml.expval(qml.PauliZ(0))

        try:
            # `wires=` is NOT optional here. Without it PennyLane numbers the
            # qreg by ORDER OF FIRST USE, so a circuit whose first gate lands on
            # wire 2 emits that wire as q[0] -- silently permuting the operand
            # registers relative to arithmetic_spec's layout. It is invisible in
            # PennyLane's own simulation (which keeps the wire labels) and only
            # appears once the exported qasm is read back: 1+0 and 0+1 produced
            # byte-identical circuits before this was passed.
            qasm = _strip_measurements(
                qml.to_openqasm(circuit, wires=list(range(layout.num_qubits)),
                                measure_all=False)())
        except Exception as exc:                      # pragma: no cover - defensive
            return self._fail(
                "PennylaneQuantumArithmetic: could not serialise to OpenQASM 2.0 "
                "({})".format(exc))

        try:
            diagram = qml.draw(circuit)()
        except Exception:                             # pragma: no cover - defensive
            diagram = ""

        specs = qml.specs(circuit, level="device")().resources
        nonlocal_gates = sum(c for size, c in specs.gate_sizes.items() if size >= 2)

        attrs = {
            "circuit.format":         "qasm2",
            "circuit.framework":      "pennylane",
            # PennyLane emits qasm2 only. Blank the other presentation attrs so
            # a cross-framework chain cannot render a stale upstream circuit
            # (NiFi merges attributes onto the FlowFile, it never drops them).
            "circuit.qasm2":          qasm,
            "circuit.qasm3":          "",
            "circuit.svg":            "",
            "circuit.num_qubits":     str(layout.num_qubits),
            "circuit.diagram":        diagram,
            "circuit.depth":          str(specs.depth),
            "circuit.gate_count":     str(specs.num_gates),
            "circuit.nonlocal_gates": str(nonlocal_gates),
            "circuit.t_count":        str(specs.gate_counts.get("T", 0)),
        }
        attrs.update(aspec.describe(
            operation, a, b, n, layout,
            implementation=implementation,
            framework="pennylane",
        ))

        return FlowFileTransformResult(
            relationship="success",
            contents=qasm.encode("utf-8"),
            attributes=attrs,
        )
