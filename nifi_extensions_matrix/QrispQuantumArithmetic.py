import os
import sys

# NiFi loads each processor in its own module context without the extensions
# directory on sys.path, so the sibling-module import below fails without this.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder

import arithmetic_spec as aspec


class QrispQuantumArithmetic(FlowFileTransform):
    """
    Builds a quantum arithmetic circuit from Qrisp's typed QuantumFloat
    variables: it encodes two operands into quantum registers and computes
    A op B (add / subtract / multiply) as a reversible circuit, using Qrisp's
    automatic arithmetic and uncomputation.

    Quantum arithmetic on typed quantum variables is Qrisp's flagship
    high-level feature and has no equivalent circuit builder in the Qiskit or
    Cirq processor families, which is exactly Quanifi's reuse story: a
    physics-free "3 * 2 as a quantum circuit" box on the canvas.

    Emits qasm2 (Qrisp's neutral wire format) so the circuit can be run by
    QrispSimulator, QiskitAerSimulator, or CirqSimulator. No measurement is
    added — connect a simulator to run it. The classical answer is published
    as arithmetic.expected_result for the report card.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a quantum arithmetic circuit (add / subtract / multiply) on "
            "Qrisp QuantumFloat registers. Configure Operation, Operand A, "
            "Operand B, Bit Width, and Signed. Outputs qasm2 compatible with "
            "QrispSimulator, QiskitAerSimulator, and CirqSimulator; no measurement "
            "is added."
        )
        tags = ["quantum", "qrisp", "arithmetic", "quantumfloat", "adder", "multiplier", "circuit"]
        dependencies = ["qrisp==0.9.5", "qiskit>=2.0.0,<2.5"]

    IMPLEMENTATIONS = ("default", "cuccaro", "fourier", "gidney", "qcla")

    #: Implementation name -> the qrisp callable that performs qb += qa.
    #: All four are in-place and land on the same register layout as the
    #: Qiskit and Cirq arithmetic processors.
    _NAMED_ADDERS = {
        "cuccaro": "cuccaro_adder",
        "fourier": "fourier_adder",
        "gidney":  "gidney_adder",
        "qcla":    "qcla",
    }

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.operation = PropertyDescriptor(
            name="Operation",
            description=(
                "Arithmetic operation to compute on the two QuantumFloat operands: "
                "add (A+B), subtract (A-B), or multiply (A*B)."
            ),
            required=True,
            default_value="multiply",
            allowable_values=["add", "subtract", "multiply"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.operand_a = PropertyDescriptor(
            name="Operand A",
            description="First operand, encoded into the first QuantumFloat register.",
            required=True,
            default_value="3",
            validators=[StandardValidators.INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.operand_b = PropertyDescriptor(
            name="Operand B",
            description="Second operand, encoded into the second QuantumFloat register.",
            required=True,
            default_value="2",
            validators=[StandardValidators.INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.bit_width = PropertyDescriptor(
            name="Bit Width",
            description=(
                "Number of mantissa bits per operand register (exponent 0, i.e. "
                "integer arithmetic). Larger widths give a wider representable "
                "range but a bigger circuit."
            ),
            required=True,
            default_value="3",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.signed = PropertyDescriptor(
            name="Signed",
            description=(
                "Use signed operands (adds a sign qubit, allowing negative values). "
                "When false, operands must be non-negative."
            ),
            required=True,
            default_value="true",
            allowable_values=["true", "false"],
        )
        self.implementation = PropertyDescriptor(
            name="Implementation",
            description=(
                "Which adder to use. 'default' keeps Qrisp's high-level operator "
                "(out-of-place: both operands survive and the answer lands in a "
                "fresh register) and supports every operation. The four named "
                "adders are in-place (the answer overwrites operand B) and "
                "support addition only, but they share the register layout of "
                "the Qiskit and Cirq arithmetic processors, so a differential "
                "comparison against those is direct."
            ),
            required=True,
            default_value="default",
            allowable_values=["default", "cuccaro", "fourier", "gidney", "qcla"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.operation,
            self.operand_a,
            self.operand_b,
            self.bit_width,
            self.signed,
            self.implementation,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qrisp import QuantumFloat
        from qiskit import qasm2 as qiskit_qasm2
        from qiskit.compiler import transpile

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        operation = get(self.operation).strip().lower()
        signed = get(self.signed).lower() == "true"
        implementation = (get(self.implementation) or "default").strip().lower()

        if implementation not in self.IMPLEMENTATIONS:
            msg = "QrispQuantumArithmetic: unknown implementation '{}'".format(implementation)
            self.logger.error(msg)
            return FlowFileTransformResult(relationship="failure", attributes={"arithmetic.error": msg})

        if implementation != "default" and operation != "add":
            msg = (
                "QrispQuantumArithmetic: implementation '{}' is an in-place adder and "
                "supports only 'add', not '{}'; use Implementation=default for that "
                "operation".format(implementation, operation)
            )
            self.logger.error(msg)
            return FlowFileTransformResult(relationship="failure", attributes={"arithmetic.error": msg})

        if implementation != "default" and signed:
            msg = (
                "QrispQuantumArithmetic: implementation '{}' requires unsigned operands; "
                "set Signed=false".format(implementation)
            )
            self.logger.error(msg)
            return FlowFileTransformResult(relationship="failure", attributes={"arithmetic.error": msg})

        try:
            a = int(get(self.operand_a))
            b = int(get(self.operand_b))
            n = int(get(self.bit_width))
        except (TypeError, ValueError) as exc:
            msg = "QrispQuantumArithmetic: non-integer operand or bit width ({})".format(exc)
            self.logger.error(msg)
            return FlowFileTransformResult(relationship="failure", attributes={"arithmetic.error": msg})

        if operation not in ("add", "subtract", "multiply"):
            msg = "QrispQuantumArithmetic: unknown operation '{}'".format(operation)
            self.logger.error(msg)
            return FlowFileTransformResult(relationship="failure", attributes={"arithmetic.error": msg})

        # Representable integer range for a QuantumFloat(n, signed) with exponent 0.
        if signed:
            lo, hi = -(2 ** n), 2 ** n - 1
        else:
            lo, hi = 0, 2 ** n - 1
        for label, val in (("A", a), ("B", b)):
            if not (lo <= val <= hi):
                msg = (
                    "QrispQuantumArithmetic: operand {} = {} is outside the "
                    "representable range [{}, {}] for bit width {} (signed={})".format(
                        label, val, lo, hi, n, str(signed).lower()
                    )
                )
                self.logger.error(msg)
                return FlowFileTransformResult(relationship="failure", attributes={"arithmetic.error": msg})

        layout = None

        if implementation == "default":
            qf1 = QuantumFloat(n, signed=signed)
            qf2 = QuantumFloat(n, signed=signed)
            qf1[:] = a
            qf2[:] = b

            if operation == "add":
                qf_res = qf1 + qf2
                expected = a + b
            elif operation == "subtract":
                qf_res = qf1 - qf2
                expected = a - b
            else:
                qf_res = qf1 * qf2
                expected = a * b

            qk = qf1.qs.compile().to_qiskit()
        else:
            # In-place adder: the target is widened by one qubit so the carry-out
            # has somewhere to go, and the answer overwrites operand B. This is
            # the layout the Qiskit and Cirq arithmetic processors also use.
            import qrisp

            adder = self._NAMED_ADDERS[implementation]
            fn = getattr(qrisp, adder)
            qf1 = QuantumFloat(n)
            qf2 = QuantumFloat(n + 1)
            qf1[:] = a
            qf2[:] = b
            fn(qf1, qf2)
            qf_res = qf2
            expected = a + b
            qk = qf1.qs.compile().to_qiskit()

            layout = aspec.RegisterLayout(
                num_qubits=qk.num_qubits,
                a_qubits=tuple(range(n)),
                b_qubits=tuple(range(n, 2 * n + 1)),
                result_qubits=tuple(range(n, 2 * n + 1)),
                ancilla_qubits=tuple(range(2 * n + 1, qk.num_qubits)),
            )
        content = qiskit_qasm2.dumps(qk).encode("utf-8")

        # Decompose composite gates (Qrisp emits e.g. a QFT block for multiply) to
        # primitives so the reported metrics reflect the true gate-level circuit.
        qk_decomp = transpile(
            qk,
            basis_gates=['h', 'cx', 'p', 'cp', 'swap', 'x', 'z', 'rz', 's', 't', 'sdg', 'tdg', 'id'],
            optimization_level=0,
        )
        ops = qk_decomp.count_ops()
        depth = qk_decomp.depth()
        gate_count = sum(ops.values())
        two_qubit_names = {'cx', 'cz', 'cy', 'ch', 'cp', 'crz', 'crx', 'cry', 'cu', 'ccx', 'swap'}
        nonlocal_gates = sum(count for gate, count in ops.items() if gate in two_qubit_names)
        t_count = ops.get('t', 0) + ops.get('tdg', 0)

        diagram = str(qk.draw('text'))
        self.logger.warn("QrispQuantumArithmetic ({} {} {} = {}):\n{}".format(
            a, operation, b, expected, diagram
        ))

        attrs = {
            "circuit.format":            "qasm2",
            "circuit.svg": "",  # blank stale Cirq SVG (NiFi merges attrs)
            "circuit.num_qubits":        str(qk.num_qubits),
            "circuit.framework":         "qrisp",
            "circuit.diagram":           diagram,
            "circuit.depth":             str(depth),
            "circuit.gate_count":        str(gate_count),
            "circuit.nonlocal_gates":    str(nonlocal_gates),
            "circuit.t_count":           str(t_count),
            "arithmetic.operation":      operation,
            "arithmetic.operand_a":      str(a),
            "arithmetic.operand_b":      str(b),
            "arithmetic.bit_width":      str(n),
            "arithmetic.signed":         str(signed).lower(),
            "arithmetic.expected_result": str(expected),
            "arithmetic.result_size":    str(qf_res.size),
            "arithmetic.framework":      "qrisp",
            "arithmetic.implementation": implementation,
        }

        # The shared arithmetic contract (expected bitstring, result qubits) is
        # only defined for the in-place adders, whose layout matches the other
        # frameworks. The default operator is out-of-place and may be signed, so
        # it keeps the attribute set it has always emitted.
        if layout is not None:
            attrs.update(aspec.describe(
                operation, a, b, n, layout,
                implementation=implementation,
                framework="qrisp",
            ))

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
