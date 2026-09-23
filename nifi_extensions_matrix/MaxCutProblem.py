import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class MaxCutProblem(FlowFileTransform):
    """
    Problem encoder: a weighted graph (edge list) becomes the MaxCut Ising
    Hamiltonian in the framework-neutral wire format, so the user never writes
    a Pauli string. Feed the output to any framework's QAOA (or VQE) solver.

    MaxCut maximizes the total weight of edges crossing a node partition.
    With cut(x) = sum_(i,j) w_ij * [x_i != x_j] and Z_i Z_j = -1 when i and j
    are on different sides, maximizing the cut is minimizing

        H = sum_(i,j) (w_ij / 2) * Z_i Z_j  -  (sum w_ij / 2) * I

    whose minimum eigenvalue is exactly -max_cut, so qaoa.exact_minimum and
    qaoa.approximation_ratio downstream read directly in cut units.

    This is a *problem-definition* stage like MoleculeHamiltonian: it emits
    hamiltonian.format = sparse_pauli_op_json (diagonal, QAOA-compatible) and
    is framework-agnostic. Edges come from the FlowFile content when it is a
    JSON edge list (data-driven flows), else from the Edges property.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Encodes a weighted graph (JSON edge list, e.g. [[0,1],[1,2,0.5]]) as "
            "the MaxCut Ising Hamiltonian in the framework-neutral wire format "
            "(hamiltonian.format = sparse_pauli_op_json, diagonal). The minimum "
            "eigenvalue equals -max_cut. Drop the output into QiskitQAOA, CirqQAOA, "
            "QrispQAOA or PennylaneQAOA to solve it — no Pauli strings required."
        )
        tags = ["quantum", "maxcut", "graph", "optimization", "hamiltonian", "qaoa"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.edges = PropertyDescriptor(
            name="Edges",
            description=(
                "JSON list of edges: [i, j] or [i, j, weight] with integer node "
                "indices and optional float weight (default 1.0). Ignored when the "
                "incoming FlowFile content is itself a JSON edge list."
            ),
            required=True,
            default_value="[[0, 1], [1, 2], [0, 2]]",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.num_nodes = PropertyDescriptor(
            name="Num Nodes",
            description="Number of graph nodes / qubits. 0 = derive from the highest edge index.",
            required=True,
            default_value="0",
            validators=[StandardValidators.NON_NEGATIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.edges, self.num_nodes]

    def getPropertyDescriptors(self):
        return self.descriptors

    @staticmethod
    def _parse_edges(spec):
        data = json.loads(spec)
        if isinstance(data, dict) and "edges" in data:
            data = data["edges"]
        if not isinstance(data, list) or not data:
            raise ValueError("expected a non-empty JSON list of edges")
        edges = []
        for k, e in enumerate(data):
            if not isinstance(e, list) or len(e) not in (2, 3):
                raise ValueError("edge {} must be [i, j] or [i, j, weight], got {!r}".format(k, e))
            i, j = int(e[0]), int(e[1])
            w = float(e[2]) if len(e) == 3 else 1.0
            if i < 0 or j < 0:
                raise ValueError("edge {} has a negative node index".format(k))
            if i == j:
                raise ValueError("edge {} is a self-loop ({}, {})".format(k, i, j))
            edges.append((i, j, w))
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
            max_idx = max(max(i, j) for i, j, _w in edges)
            if n == 0:
                n = max_idx + 1
            elif max_idx >= n:
                raise ValueError(
                    "edge index {} out of range for Num Nodes = {}".format(max_idx, n))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            msg = "invalid MaxCut edges: {}".format(exc)
            self.logger.error("MaxCutProblem: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"hamiltonian.error": msg},
            )

        # merge parallel edges, then H = sum w/2 ZiZj - (sum w)/2 I
        couplings = {}
        for i, j, w in edges:
            key = (min(i, j), max(i, j))
            couplings[key] = couplings.get(key, 0.0) + w
        total_w = sum(couplings.values())

        terms = [("", [], -total_w / 2.0)]
        terms += [("ZZ", [i, j], w / 2.0) for (i, j), w in sorted(couplings.items())]
        wire = terms_to_wire(terms, n)

        self.logger.warn(
            "MaxCutProblem: {} nodes, {} edges (total weight {}) -> {} Ising terms".format(
                n, len(couplings), total_w, len(wire["terms"])))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(wire, indent=2).encode("utf-8"),
            attributes={
                "hamiltonian.format": "sparse_pauli_op_json",
                "hamiltonian.num_qubits": str(n),
                "hamiltonian.num_terms": str(len(wire["terms"])),
                "hamiltonian.framework": "agnostic",
                "problem.type": "maxcut",
                "problem.num_nodes": str(n),
                "problem.num_edges": str(len(couplings)),
                "problem.total_weight": "{:g}".format(total_w),
            },
        )
