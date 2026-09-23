import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class PortfolioRebalancingProblem(FlowFileTransform):
    """
    Problem encoder: converts mean-variance portfolio optimization with budget
    constraints into the framework-neutral Ising Hamiltonian wire format
    (hamiltonian.format = sparse_pauli_op_json).

    Given n assets with expected returns mu, covariance matrix Sigma, risk factor q >= 0,
    and budget K (select exactly K assets), minimizes:
        C(x) = q * x^T Sigma x - mu^T x + P * (sum_i x_i - K)^2
    where P > 0 is the constraint penalty factor.
    Substituting x_i = (I - Z_i)/2 maps the cost to a diagonal Ising Hamiltonian.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Encodes Markowitz portfolio rebalancing with cardinality constraint into "
            "the framework-neutral Ising Hamiltonian wire format. Drop the output into "
            "QrispQAOA (especially with Mixer Type = XY), QiskitQAOA, or CirqQAOA."
        )
        tags = ["quantum", "finance", "portfolio", "optimization", "hamiltonian", "qaoa"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.expected_returns = PropertyDescriptor(
            name="Expected Returns",
            description="JSON list of expected asset returns mu (e.g. [0.10, 0.20, 0.15]).",
            required=True,
            default_value="[0.10, 0.20, 0.15]",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.covariance_matrix = PropertyDescriptor(
            name="Covariance Matrix",
            description="JSON 2D list representing the asset covariance matrix Sigma (n x n).",
            required=True,
            default_value="[[0.05, 0.01, 0.02], [0.01, 0.08, 0.03], [0.02, 0.03, 0.06]]",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.risk_factor = PropertyDescriptor(
            name="Risk Factor",
            description="Risk aversion coefficient q >= 0 (default: 0.5).",
            required=True,
            default_value="0.5",
            validators=[StandardValidators.NUMBER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.budget = PropertyDescriptor(
            name="Budget",
            description="Number of assets K to select from the portfolio.",
            required=True,
            default_value="2",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.penalty = PropertyDescriptor(
            name="Penalty Factor",
            description="Constraint violation penalty coefficient P (default: 2.0).",
            required=True,
            default_value="2.0",
            validators=[StandardValidators.NUMBER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.expected_returns,
            self.covariance_matrix,
            self.risk_factor,
            self.budget,
            self.penalty,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from pauli_dsl import terms_to_wire

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        raw = bytes(flowFile.getContentsAsBytes() or b"").decode("utf-8", "replace").strip()
        data = None
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
            except Exception:
                pass

        try:
            if data and "expected_returns" in data:
                mu = [float(x) for x in data["expected_returns"]]
            else:
                mu = [float(x) for x in json.loads(get(self.expected_returns))]

            if data and "covariance_matrix" in data:
                sigma = [[float(v) for v in row] for row in data["covariance_matrix"]]
            else:
                sigma = [[float(v) for v in row] for row in json.loads(get(self.covariance_matrix))]

            n = len(mu)
            if len(sigma) != n or any(len(row) != n for row in sigma):
                raise ValueError(f"Covariance Matrix must be {n}x{n} matching Expected Returns length {n}")

            q = float(data.get("risk_factor", get(self.risk_factor)) if data else get(self.risk_factor))
            k_budget = int(data.get("budget", get(self.budget)) if data else get(self.budget))
            penalty = float(data.get("penalty", get(self.penalty)) if data else get(self.penalty))

            if k_budget > n or k_budget < 1:
                raise ValueError(f"Budget K ({k_budget}) must be between 1 and {n}")
        except Exception as exc:
            msg = f"Invalid Portfolio input: {exc}"
            self.logger.error("PortfolioRebalancingProblem: " + msg)
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"hamiltonian.error": msg},
            )

        # Build alpha_i and beta_ij
        alpha = [0.0] * n
        for i in range(n):
            alpha[i] = q * sigma[i][i] - mu[i] + penalty * (1.0 - 2.0 * k_budget)

        beta = {}
        for i in range(n):
            for j in range(i + 1, n):
                beta[(i, j)] = q * (sigma[i][j] + sigma[j][i]) + 2.0 * penalty

        # Constant term
        c_0 = (k_budget ** 2) * penalty + sum(alpha[i] / 2.0 for i in range(n))
        for (i, j), b_val in beta.items():
            c_0 += b_val / 4.0

        terms = [("", [], c_0)]

        # Linear Z_i terms
        for i in range(n):
            c_i = -alpha[i] / 2.0
            for j in range(n):
                if i != j:
                    pair = (min(i, j), max(i, j))
                    c_i -= beta[pair] / 4.0
            if abs(c_i) > 1e-9:
                terms.append(("Z", [i], c_i))

        # Quadratic Z_i Z_j terms
        for (i, j), b_val in beta.items():
            c_ij = b_val / 4.0
            if abs(c_ij) > 1e-9:
                terms.append(("ZZ", [i, j], c_ij))

        wire = terms_to_wire(terms, n)
        content = json.dumps(wire).encode("utf-8")

        attrs = {
            "hamiltonian.format": "sparse_pauli_op_json",
            "hamiltonian.num_qubits": str(n),
            "hamiltonian.num_terms": str(len(terms)),
            "hamiltonian.problem_type": "portfolio_rebalancing",
            "hamiltonian.budget": str(k_budget),
            "hamiltonian.risk_factor": str(q),
            "hamiltonian.penalty": str(penalty),
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
