import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class MaxIndependentSetProblem(FlowFileTransform):
    """
    Problem encoder: converts a graph (edge list) into the Maximum Independent Set (MIS)
    Ising Hamiltonian in the framework-neutral wire format (hamiltonian.format = sparse_pauli_op_json).

    The Maximum Independent Set problem seeks the largest subset of vertices S subset V such that
    no two vertices in S share an edge.
    With binary variables x_i in {0, 1} indicating whether vertex i is in the independent set:
        minimize  - sum_i x_i  +  A * sum_{(u,v) in E, u < v} x_u x_v
    where A > 1 is the penalty coefficient for adjacent selected vertices.
    Substituting x_i = (I - Z_i)/2 yields a diagonal Ising Hamiltonian.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Encodes a graph (JSON edge list) into the Maximum Independent Set (MIS) Ising "
            "Hamiltonian in the framework-neutral wire format (hamiltonian.format = sparse_pauli_op_json). "
            "Drop the output directly into QrispQAOA, QiskitQAOA, or CirqQAOA."
        )
        tags = ["quantum", "mis", "independent_set", "graph", "optimization", "hamiltonian", "qaoa"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.edges = PropertyDescriptor(
            name="Edges",
            description=(
                "JSON list of edges: [[0, 1], [1, 2], ...] with integer node indices. "
                "Ignored when the incoming FlowFile content is a valid JSON edge list."
            ),
            required=True,
            default_value="[[0, 1], [1, 2], [2, 3]]",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.num_nodes = PropertyDescriptor(
            name="Num Nodes",
            description="Number of graph nodes / qubits. 0 = derive from the highest edge index + 1.",
            required=True,
            default_value="0",
            validators=[StandardValidators.NON_NEGATIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.penalty = PropertyDescriptor(
            name="Penalty Factor",
            description="Penalty factor A for edges between selected vertices (must be > 1.0, default: 2.0).",
            required=True,
            default_value="2.0",
            validators=[StandardValidators.NUMBER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.edges, self.num_nodes, self.penalty]

    def getPropertyDescriptors(self):
        return self.descriptors

    @staticmethod
    def _parse_edges(spec):
        data = json.loads(spec)
        if isinstance(data, dict) and "edges" in data:
            data = data["edges"]
        if not isinstance(data, list) or not data:
            raise ValueError("expected a non-empty JSON list of edges")
        edges = set()
        for k, e in enumerate(data):
            if not isinstance(e, list) or len(e) not in (2, 3):
                raise ValueError(f"edge {k} must be [u, v] or [u, v, weight], got {e!r}")
            u, v = int(e[0]), int(e[1])
            if u < 0 or v < 0:
                raise ValueError(f"edge {k} has a negative node index")
            if u == v:
                raise ValueError(f"edge {k} is a self-loop ({u}, {v})")
            edges.add((min(u, v), max(u, v)))
        return edges

    def transform(self, context, flowFile):
        from pauli_dsl import terms_to_wire

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        raw = bytes(flowFile.getContentsAsBytes() or b"").decode("utf-8", "replace").strip()
        spec = raw if raw.startswith("[") or raw.startswith("{") else get(self.edges)

        try:
            edges = self._parse_edges(spec)
            n = int(get(self.num_nodes))
            penalty = float(get(self.penalty))
            if penalty <= 1.0:
                raise ValueError("Penalty Factor must be > 1.0 to guarantee independent set validity")
            max_idx = max(max(u, v) for u, v in edges)
            if n == 0:
                n = max_idx + 1
            elif max_idx >= n:
                raise ValueError(f"edge index {max_idx} out of range for Num Nodes = {n}")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            msg = f"invalid MaxIndependentSet input: {exc}"
            self.logger.error("MaxIndependentSetProblem: " + msg)
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"hamiltonian.error": msg},
            )

        # Degrees in graph G
        degrees = [0] * n
        for u, v in edges:
            degrees[u] += 1
            degrees[v] += 1

        # Formulate terms:
        # C_0 = - n / 2 + (penalty / 4) * len(edges)
        const_val = -0.5 * n + (penalty / 4.0) * len(edges)
        terms = [("", [], const_val)]

        # Linear Z terms: c_i = 0.5 - (penalty / 4.0) * degrees[i]
        for i in range(n):
            c_i = 0.5 - (penalty / 4.0) * degrees[i]
            if abs(c_i) > 1e-9:
                terms.append(("Z", [i], c_i))

        # Quadratic ZZ terms for edges: c_uv = penalty / 4.0
        for u, v in edges:
            terms.append(("ZZ", [u, v], penalty / 4.0))

        wire = terms_to_wire(terms, n)
        content = json.dumps(wire).encode("utf-8")

        attrs = {
            "hamiltonian.format": "sparse_pauli_op_json",
            "hamiltonian.num_qubits": str(n),
            "hamiltonian.num_terms": str(len(terms)),
            "hamiltonian.problem_type": "max_independent_set",
            "hamiltonian.edges_count": str(len(edges)),
            "hamiltonian.penalty": str(penalty),
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
