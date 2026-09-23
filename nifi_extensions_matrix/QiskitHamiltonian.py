import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


def _parse_pauli_sum(expr, num_qubits_hint=0):
    """Parse a Hamiltonian spec (compact-indexed text or JSON) into
    ``(SparsePauliOp, num_qubits)`` using the shared framework-neutral
    :mod:`pauli_dsl` parser. See that module for the full syntax."""
    from qiskit.quantum_info import SparsePauliOp
    from pauli_dsl import parse_pauli_sum

    terms, n = parse_pauli_sum(expr, num_qubits_hint)
    return SparsePauliOp.from_sparse_list(terms, num_qubits=n), n


class QiskitHamiltonian(FlowFileTransform):
    """
    Builds a problem Hamiltonian (a SparsePauliOp) from a compact Pauli-sum DSL
    and writes it to the FlowFile as JSON.

    This is the *problem-definition* stage of the variational pipeline
    QiskitHamiltonian -> QiskitAnsatz -> QiskitVQE -> QuanifiReport. It carries
    no quantum execution and is reusable by any algorithm that needs an
    operator (VQE, QAOA, time evolution). The serialised operator is the only
    contract downstream stages rely on, advertised via the ``hamiltonian.format``
    attribute.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a problem Hamiltonian (SparsePauliOp) from a compact-indexed Pauli sum "
            "such as 'Z0 + Z1 + 0.5 X0 X1' (or JSON) and serialises it as JSON. "
            "Sets hamiltonian.format = 'sparse_pauli_op_json' so QiskitVQE (or any "
            "operator consumer) can pick it up. No quantum execution happens here."
        )
        tags = ["quantum", "qiskit", "vqe", "hamiltonian", "operator"]
        dependencies = ["qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.hamiltonian = PropertyDescriptor(
            name="Hamiltonian",
            description=(
                "Compact-indexed Pauli sum, e.g. 'Z0 + Z1 + 0.5 X0 X1'. Each term is "
                "an optional coefficient followed by Pauli-index factors; Pauli is one "
                "of I/X/Y/Z (case-insensitive), index is the qubit. Terms separated by "
                "+ or -; a bare number is an identity term; use plain decimals (not 1e-3). "
                "A JSON array like [{\"pauli\":\"ZZ\",\"qubits\":[0,1],\"coeff\":0.5}] is "
                "also accepted. This syntax is shared across all framework Hamiltonian "
                "processors."
            ),
            required=True,
            default_value="Z0 + Z1 + 0.5 X0 X1",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.num_qubits = PropertyDescriptor(
            name="Num Qubits",
            description=(
                "Number of qubits in the register. Use 0 to auto-derive from the "
                "highest qubit index used in the expression. Set explicitly when the "
                "operator should act on more qubits than the expression mentions."
            ),
            required=True,
            default_value="0",
            validators=[StandardValidators.NON_NEGATIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.hamiltonian, self.num_qubits]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        expr = get(self.hamiltonian)
        hint = int(get(self.num_qubits))

        try:
            op, n = _parse_pauli_sum(expr, hint)
        except Exception as exc:
            self.logger.error("QiskitHamiltonian failed to parse '{}': {}".format(expr, exc))
            return FlowFileTransformResult(
                relationship="failure",
                contents=bytes(flowFile.getContentsAsBytes() or b""),
                attributes={"hamiltonian.error": str(exc)},
            )

        # Serialise as dense Pauli labels + (real, imag) coefficients so any
        # consumer can rebuild it with SparsePauliOp.from_list.
        terms = [[label, float(coeff.real), float(coeff.imag)] for label, coeff in op.to_list()]
        payload = {"num_qubits": int(op.num_qubits), "terms": terms}
        content = json.dumps(payload, indent=2).encode("utf-8")

        self.logger.warn(
            "QiskitHamiltonian ({} qubits, {} terms):\n{}".format(n, len(terms), op)
        )

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes={
                "hamiltonian.format": "sparse_pauli_op_json",
                "hamiltonian.num_qubits": str(op.num_qubits),
                "hamiltonian.num_terms": str(len(terms)),
                "hamiltonian.framework": "qiskit",
                "hamiltonian.expression": expr,
            },
        )
