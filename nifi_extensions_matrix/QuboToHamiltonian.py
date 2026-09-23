import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QuboToHamiltonian(FlowFileTransform):
    """
    Problem encoder: a QUBO matrix becomes the equivalent Ising Hamiltonian in
    the framework-neutral wire format. QUBO (minimize x^T Q x over binary x)
    is the lingua franca of combinatorial optimization — portfolio selection,
    scheduling, routing, knapsack all reduce to it — so this one processor
    makes every such problem solvable by any framework's QAOA or VQE.

    Substituting x_i = (1 - Z_i)/2 gives, with q_ij = Q_ij + Q_ji (i < j):

        constant   c    =  sum_i Q_ii / 2  +  sum_(i<j) q_ij / 4
        linear     h_i  = -Q_ii / 2  -  sum_(j != i) q_ij / 4
        quadratic  J_ij =  q_ij / 4

    The minimum eigenvalue of H = c*I + sum h_i Z_i + sum J_ij Z_i Z_j equals
    min_x x^T Q x, and the ground bitstring (q0-left) is the argmin, so
    qaoa.best_measurement downstream is directly the binary solution vector.

    Framework-agnostic problem-definition stage, like MoleculeHamiltonian. The
    matrix comes from the FlowFile content when it is a JSON 2D array
    (data-driven flows), else from the QUBO Matrix property.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Encodes a QUBO matrix (JSON 2D array; minimize x^T Q x over binary x) "
            "as the equivalent Ising Hamiltonian in the framework-neutral wire "
            "format (hamiltonian.format = sparse_pauli_op_json, diagonal). The "
            "minimum eigenvalue equals the QUBO optimum and the ground bitstring is "
            "the binary solution. Feed it to any *QAOA or *VQE solver."
        )
        tags = ["quantum", "qubo", "ising", "optimization", "hamiltonian", "qaoa"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.matrix = PropertyDescriptor(
            name="QUBO Matrix",
            description=(
                "Square JSON 2D array Q; the problem is minimize x^T Q x over "
                "x in {0,1}^n. Ignored when the incoming FlowFile content is "
                "itself a JSON 2D array."
            ),
            required=True,
            default_value="[[-1, 2], [0, -1]]",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.matrix]

    def getPropertyDescriptors(self):
        return self.descriptors

    @staticmethod
    def _parse_matrix(spec):
        data = json.loads(spec)
        if isinstance(data, dict) and "qubo" in data:
            data = data["qubo"]
        if not isinstance(data, list) or not data:
            raise ValueError("expected a non-empty JSON 2D array")
        n = len(data)
        matrix = []
        for r, row in enumerate(data):
            if not isinstance(row, list) or len(row) != n:
                raise ValueError(
                    "row {} has length {}, expected {} (matrix must be square)".format(
                        r, len(row) if isinstance(row, list) else "?", n))
            matrix.append([float(v) for v in row])
        return matrix

    def transform(self, context, flowFile):
        from pauli_dsl import terms_to_wire

        spec_prop = (
            context.getProperty(self.matrix)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )
        raw = bytes(flowFile.getContentsAsBytes() or b"").decode("utf-8", "replace").strip()
        spec = raw if raw.startswith("[") or raw.startswith("{") else spec_prop

        try:
            q = self._parse_matrix(spec)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            msg = "invalid QUBO matrix: {}".format(exc)
            self.logger.error("QuboToHamiltonian: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"hamiltonian.error": msg},
            )

        n = len(q)
        tol = 1e-12

        constant = sum(q[i][i] for i in range(n)) / 2.0
        linear = [-q[i][i] / 2.0 for i in range(n)]
        quadratic = {}
        for i in range(n):
            for j in range(i + 1, n):
                qij = q[i][j] + q[j][i]
                if abs(qij) > tol:
                    constant += qij / 4.0
                    linear[i] -= qij / 4.0
                    linear[j] -= qij / 4.0
                    quadratic[(i, j)] = qij / 4.0

        terms = []
        if abs(constant) > tol:
            terms.append(("", [], constant))
        terms += [("Z", [i], h) for i, h in enumerate(linear) if abs(h) > tol]
        terms += [("ZZ", [i, j], v) for (i, j), v in sorted(quadratic.items())]
        if not terms:
            terms = [("", [], 0.0)]
        wire = terms_to_wire(terms, n)

        self.logger.warn(
            "QuboToHamiltonian: {}x{} QUBO -> {} Ising terms (constant {:g})".format(
                n, n, len(wire["terms"]), constant))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(wire, indent=2).encode("utf-8"),
            attributes={
                "hamiltonian.format": "sparse_pauli_op_json",
                "hamiltonian.num_qubits": str(n),
                "hamiltonian.num_terms": str(len(wire["terms"])),
                "hamiltonian.framework": "agnostic",
                "problem.type": "qubo",
                "problem.size": str(n),
            },
        )
