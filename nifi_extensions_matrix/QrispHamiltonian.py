import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QrispHamiltonian(FlowFileTransform):
    """
    Builds a problem Hamiltonian from the shared compact-indexed Pauli syntax and
    writes it to the FlowFile in the framework-neutral wire format.

    Problem-definition stage of the Qrisp variational pipeline
    QrispHamiltonian -> QrispAnsatz -> QrispVQE -> QuanifiReport. The wire format
    is identical to QiskitHamiltonian's and CirqHamiltonian's
    (``hamiltonian.format = sparse_pauli_op_json``), so all three are
    interchangeable; only ``hamiltonian.framework`` differs. Parsing is delegated
    to the shared :mod:`pauli_dsl`, so the same input syntax works everywhere.
    QrispVQE turns these terms into a ``qrisp.operators.qubit`` QubitOperator.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a problem Hamiltonian from a compact-indexed Pauli sum such as "
            "'Z0 + Z1 + 0.5 X0 X1' (or JSON) and serialises it in the framework-neutral "
            "wire format shared with QiskitHamiltonian and CirqHamiltonian. Tags "
            "hamiltonian.framework=qrisp. No quantum execution."
        )
        tags = ["quantum", "qrisp", "vqe", "hamiltonian", "operator"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.hamiltonian = PropertyDescriptor(
            name="Hamiltonian",
            description=(
                "Compact-indexed Pauli sum, e.g. 'Z0 + Z1 + 0.5 X0 X1'. Each term is an "
                "optional coefficient followed by Pauli-index factors; Pauli is one of "
                "I/X/Y/Z, index is the qubit. Terms separated by + or -; a bare number is "
                "an identity term; plain decimals only. A JSON array "
                "[{\"pauli\":\"ZZ\",\"qubits\":[0,1],\"coeff\":0.5}] is also accepted. Same "
                "syntax across all framework Hamiltonian processors."
            ),
            required=True,
            default_value="Z0 + Z1 + 0.5 X0 X1",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.num_qubits = PropertyDescriptor(
            name="Num Qubits",
            description="Register size; 0 = auto-derive from the highest qubit index used.",
            required=True,
            default_value="0",
            validators=[StandardValidators.NON_NEGATIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.hamiltonian, self.num_qubits]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from pauli_dsl import parse_pauli_sum, terms_to_wire, PauliDSLError

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        expr = get(self.hamiltonian)
        try:
            hint = int(get(self.num_qubits))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QrispHamiltonian: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"hamiltonian.error": msg},
            )

        try:
            terms, n = parse_pauli_sum(expr, hint)
        except PauliDSLError as exc:
            self.logger.error("QrispHamiltonian failed to parse '{}': {}".format(expr, exc))
            return FlowFileTransformResult(
                relationship="failure",
                contents=bytes(flowFile.getContentsAsBytes() or b""),
                attributes={"hamiltonian.error": str(exc)},
            )

        content = json.dumps(terms_to_wire(terms, n), indent=2).encode("utf-8")
        self.logger.warn("QrispHamiltonian ({} qubits, {} terms): {}".format(n, len(terms), expr))

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes={
                "hamiltonian.format": "sparse_pauli_op_json",
                "hamiltonian.num_qubits": str(n),
                "hamiltonian.num_terms": str(len(terms)),
                "hamiltonian.framework": "qrisp",
                "hamiltonian.expression": expr,
            },
        )
