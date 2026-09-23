import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder

# scipy method per optimizer name
_SCIPY_METHOD = {
    "COBYLA": "COBYLA",
    "NELDER_MEAD": "Nelder-Mead",
    "POWELL": "Powell",
    "L_BFGS_B": "L-BFGS-B",
}


def _bitstring_energy(terms, bitstring):
    """Energy of a measured bitstring (leftmost char = wire 0, PennyLane's order)."""
    energy = 0.0
    for pauli, indices, coeff in terms:
        value = coeff
        for idx in indices:
            value *= 1.0 - 2.0 * int(bitstring[idx])
        energy += value
    return energy


class PennylaneQAOA(FlowFileTransform):
    """
    Quantum Approximate Optimization Algorithm solver for PennyLane, built on
    the ``qml.qaoa`` module (``cost_layer`` / ``mixer_layer`` / ``x_mixer``).

    Solver stage of <any>Hamiltonian -> PennylaneQAOA -> QuanifiReport.
    PennyLane provides the QAOA layer structure but not the classical loop,
    so the 2p parameters are driven by ``scipy.optimize.minimize`` with exact
    (analytic) expectation values from ``default.qubit``, mirroring CirqQAOA.

    Strict solver: it requires an upstream Hamiltonian (content, the
    framework-neutral wire format) and that Hamiltonian must be *diagonal*
    (I/Z only); anything else routes to ``failure`` with a clear
    ``qaoa.error``. Because the wire format is shared, any
    <Framework>Hamiltonian can feed it.

    Bitstring convention follows the existing PennyLane processors: leftmost
    character = wire 0 (MSB-first). ``Shots`` only controls the final counts
    histogram; the optimization itself is analytic.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "QAOA solver using PennyLane's qml.qaoa layers driven by scipy. Reads a "
            "diagonal cost Hamiltonian (hamiltonian.format=sparse_pauli_op_json from any "
            "upstream Hamiltonian processor), optimizes the p-layer QAOA ansatz with "
            "analytic expectations on default.qubit, and emits qaoa.* results (optimal "
            "value, best bitstring, approximation ratio), measurement counts as JSON, and "
            "report.type=simulation. Strict solver - requires the upstream Hamiltonian."
        )
        tags = ["quantum", "pennylane", "qaoa", "optimization", "variational"]
        dependencies = ["pennylane>=0.40", "scipy>=1.10"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.layers = PropertyDescriptor(
            name="Layers",
            description=(
                "Number of QAOA layers p (cost + mixer repetitions). The ansatz "
                "has 2p parameters; deeper is more expressive but harder to optimize."
            ),
            required=True,
            default_value="2",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.optimizer = PropertyDescriptor(
            name="Optimizer",
            description="Classical optimizer (scipy.optimize.minimize method). All are gradient-free except L_BFGS_B (finite-difference gradients).",
            required=True,
            default_value="COBYLA",
            allowable_values=["COBYLA", "NELDER_MEAD", "POWELL", "L_BFGS_B"],
        )
        self.max_iterations = PropertyDescriptor(
            name="Max Iterations",
            description="Maximum optimizer iterations.",
            required=True,
            default_value="100",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description="Shots used to sample the final optimal-parameter circuit for the counts histogram. Does not affect the (analytic) optimization.",
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.initial_parameters = PropertyDescriptor(
            name="Initial Parameters",
            description="'random', 'zeros', or a comma-separated list of 2p floats (betas then gammas).",
            required=True,
            default_value="random",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.random_seed = PropertyDescriptor(
            name="Random Seed",
            description=(
                "Seed for reproducible runs (initial parameters and sampling). "
                "Empty = nondeterministic. Seeds the random initial point and the default.qubit sampling."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.layers, self.optimizer, self.max_iterations, self.shots, self.initial_parameters,
            self.random_seed,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _fail(self, msg):
        self.logger.error("PennylaneQAOA: " + msg)
        return FlowFileTransformResult(
            relationship="failure", contents=b"", attributes={"qaoa.error": msg},
        )

    def _resolve_cost_terms(self, flowFile):
        """Neutral (pauli, indices, coeff) terms from the wire content (requires upstream Hamiltonian)."""
        from pauli_dsl import wire_to_terms

        if flowFile.getAttribute("hamiltonian.format") != "sparse_pauli_op_json":
            raise ValueError(
                "no Hamiltonian on the FlowFile (expected hamiltonian.format="
                "'sparse_pauli_op_json' from an upstream Hamiltonian processor)")
        raw = bytes(flowFile.getContentsAsBytes() or b"").decode("utf-8")
        return wire_to_terms(raw)

    def transform(self, context, flowFile):
        import numpy as np
        import pennylane as qml
        from scipy.optimize import minimize
        from pauli_dsl import is_diagonal, diagonal_values

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        opt_name = get(self.optimizer)
        init_raw = get(self.initial_parameters).strip()
        try:
            p = int(get(self.layers))
            maxiter = int(get(self.max_iterations))
            shots = int(get(self.shots))
            seed_raw = (get(self.random_seed) or "").strip()
            seed = int(seed_raw) if seed_raw else None
        except (TypeError, ValueError) as exc:
            return self._fail("bad numeric property value: {}".format(exc))


        try:
            terms, n = self._resolve_cost_terms(flowFile)
        except Exception as exc:
            return self._fail("input resolution failed: {}".format(exc))

        if not is_diagonal(terms):
            return self._fail(
                "cost Hamiltonian must be diagonal (I/Z terms only) for QAOA; "
                "got X/Y terms. Use a variational solver for general operators.")

        # Initial point: 2p parameters, betas then gammas.
        spec = init_raw.lower()
        if spec == "random":
            initial_point = np.random.default_rng(seed).uniform(-np.pi, np.pi, 2 * p)
        elif spec == "zeros":
            initial_point = np.zeros(2 * p)
        else:
            try:
                initial_point = np.array([float(x) for x in init_raw.split(",")])
            except ValueError as exc:
                return self._fail("bad Initial Parameters: {}".format(exc))
            if len(initial_point) != 2 * p:
                return self._fail(
                    "Initial Parameters length {} != 2p = {}".format(len(initial_point), 2 * p))

        # Cost Hamiltonian as a qml operator sum; identity terms shift the
        # energy and ride along as qml.Identity.
        observables, coeffs = [], []
        for pauli, indices, coeff in terms:
            if not pauli:
                observables.append(qml.Identity(0))
            else:
                obs = qml.Z(indices[0])
                for idx in indices[1:]:
                    obs = obs @ qml.Z(idx)
                observables.append(obs)
            coeffs.append(float(coeff))
        cost_h = qml.Hamiltonian(coeffs, observables)
        mixer_h = qml.qaoa.x_mixer(range(n))

        def ansatz(params):
            betas, gammas = params[:p], params[p:]
            for w in range(n):
                qml.Hadamard(wires=w)
            for l in range(p):
                qml.qaoa.cost_layer(gammas[l], cost_h)
                qml.qaoa.mixer_layer(betas[l], mixer_h)

        dev = qml.device("default.qubit", wires=n, seed=seed)

        @qml.qnode(dev)
        def energy_qnode(params):
            ansatz(params)
            return qml.expval(cost_h)

        def energy(point):
            return float(energy_qnode(point))

        start = time.time()
        try:
            result = minimize(energy, initial_point, method=_SCIPY_METHOD[opt_name],
                              options={"maxiter": maxiter})
        except Exception as exc:
            self.logger.error("PennylaneQAOA optimization failed: {}".format(exc))
            return self._fail("optimization failed: {}".format(exc))
        elapsed = time.time() - start

        optimal_point = np.real(np.asarray(result.x, dtype=float))
        optimal_value = float(result.fun)
        evals = int(getattr(result, "nfev", 0) or 0)
        converged = bool(getattr(result, "success", False))

        # Sample the optimal circuit for the histogram (counts need finite shots).
        @qml.qnode(dev)
        def counts_qnode(params):
            ansatz(params)
            return qml.counts(wires=range(n))

        try:
            raw_counts = qml.set_shots(counts_qnode, shots=shots)(optimal_point)
        except Exception as exc:
            self.logger.error("PennylaneQAOA sampling failed: {}".format(exc))
            return self._fail("sampling failed: {}".format(exc))
        counts = {str(k): int(v) for k, v in raw_counts.items()}
        sorted_counts = dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        best_bitstring = min(counts, key=lambda b: _bitstring_energy(terms, b))
        best_value = _bitstring_energy(terms, best_bitstring)
        values = diagonal_values(terms, n)
        exact_min, exact_max = float(values.min()), float(values.max())
        if exact_max > exact_min:
            approximation_ratio = (exact_max - best_value) / (exact_max - exact_min)
        else:
            approximation_ratio = 1.0

        diagram = str(qml.draw(energy_qnode, max_length=10000)(optimal_point))

        self.logger.warn(
            "PennylaneQAOA ({} qubits, p={}, optimizer={}): optimal_value={:.6f}, "
            "best={} ({:.6f}), ratio={:.4f} in {} evals, {:.2f}s".format(
                n, p, opt_name, optimal_value, best_bitstring, best_value,
                approximation_ratio, evals, elapsed))

        attrs = {
            "qaoa.framework": "pennylane",
            "qaoa.optimal_value": "{:.8f}".format(optimal_value),
            "qaoa.best_measurement": best_bitstring,
            "qaoa.best_value": "{:.8f}".format(best_value),
            "qaoa.exact_minimum": "{:.8f}".format(exact_min),
            "qaoa.approximation_ratio": "{:.6f}".format(approximation_ratio),
            "qaoa.layers": str(p),
            "qaoa.optimal_parameters": json.dumps([round(float(x), 8) for x in optimal_point]),
            "qaoa.num_iterations": str(evals),
            "qaoa.cost_function_evals": str(evals),
            "qaoa.max_iterations": str(maxiter),
            "qaoa.converged": "true" if converged else "false",
            "qaoa.optimizer": opt_name,
            "qaoa.num_qubits": str(n),
            "qaoa.shots": str(shots),
            "qaoa.elapsed_seconds": "{:.4f}".format(elapsed),
            "perf.elapsed_seconds": "{:.4f}".format(elapsed),
            **({"run.seed": str(seed)} if seed is not None else {}),
            # circuit.* (best-effort: PennyLane has no neutral circuit format here)
            "circuit.num_qubits": str(n),
            "circuit.framework": "pennylane",
            "circuit.algorithm": "qaoa",
            "circuit.diagram": diagram,
            # sim.* + report.type so QuanifiReport renders the simulation card
            "sim.shots": str(shots),
            "sim.top_result": top_state,
            "sim.top_probability": "{:.4f}".format(top_count / shots),
            "sim.framework": "pennylane",
            "sim.bit_order": "q0_left",
            "report.type": "simulation",
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes=attrs,
        )
