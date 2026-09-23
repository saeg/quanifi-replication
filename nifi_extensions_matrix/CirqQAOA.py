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


class CirqQAOA(FlowFileTransform):
    """
    Hand-rolled Quantum Approximate Optimization Algorithm solver for Cirq.

    Cirq has no high-level QAOA class, so the whole algorithm is built from
    primitives (the same "we provide the high-level component Cirq lacks"
    story as CirqVQE): a sympy-parameterised ansatz alternating cost layers
    exp(-i gamma H) (CNOT ladder + Rz per Z-string term) and Rx mixer layers,
    energies computed exactly from the state vector, and
    ``scipy.optimize.minimize`` driving the 2p parameters.

    Solver stage of <any>Hamiltonian -> CirqQAOA -> QuanifiReport. Strict
    solver: it requires an upstream Hamiltonian (content, the framework-
    neutral wire format) and that Hamiltonian must be *diagonal* (I/Z only);
    anything else routes to ``failure`` with a clear ``qaoa.error``. Because
    the wire format is shared, any <Framework>Hamiltonian can feed it.

    Bitstring convention follows the existing Cirq processors: leftmost
    character = qubit 0 (the opposite of the Qiskit solvers). The exact
    optimum of the diagonal cost is computed classically, so
    ``qaoa.approximation_ratio`` = (E_max - E_best) / (E_max - E_min).
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Hand-rolled QAOA for Cirq: alternating exp(-i gamma H) cost layers and Rx "
            "mixer layers optimized with scipy. Reads a diagonal cost Hamiltonian "
            "(hamiltonian.format=sparse_pauli_op_json from any upstream Hamiltonian "
            "processor) and emits qaoa.* results (optimal value, best bitstring, "
            "approximation ratio), measurement counts as JSON, and report.type=simulation. "
            "Strict solver - requires the upstream Hamiltonian."
        )
        tags = ["quantum", "cirq", "qaoa", "optimization", "variational"]
        dependencies = ["cirq-core>=1.0", "scipy>=1.10", "sympy"]

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
            description="Shots used to sample the final optimal-parameter circuit for the counts histogram. Does not affect the (exact) optimization.",
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
                "Empty = nondeterministic. Seeds the random initial point and cirq.Simulator sampling."
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
        self.logger.error("CirqQAOA: " + msg)
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

    @staticmethod
    def _build_ansatz(terms, qubits, p):
        """The QAOA circuit: H^n then p alternating cost/mixer layers.

        Returns (circuit, param_names) with parameters ordered betas then
        gammas, matching the Initial Parameters convention of the other
        QAOA solvers. Each Z-string cost term becomes a CNOT ladder onto its
        last qubit + Rz(2 gamma coeff) + reverse ladder; identity terms only
        shift the energy by a constant and are skipped in the circuit.
        """
        import cirq
        import sympy

        betas = [sympy.Symbol("beta_{}".format(l)) for l in range(p)]
        gammas = [sympy.Symbol("gamma_{}".format(l)) for l in range(p)]

        circuit = cirq.Circuit()
        circuit.append(cirq.H(q) for q in qubits)
        for l in range(p):
            for pauli, indices, coeff in terms:
                if not pauli:
                    continue
                targets = [qubits[i] for i in indices]
                for a, b in zip(targets, targets[1:]):
                    circuit.append(cirq.CNOT(a, b))
                circuit.append(cirq.rz(2.0 * coeff * gammas[l]).on(targets[-1]))
                for a, b in reversed(list(zip(targets, targets[1:]))):
                    circuit.append(cirq.CNOT(a, b))
            circuit.append(cirq.rx(2.0 * betas[l]).on(q) for q in qubits)

        param_names = [str(s) for s in betas] + [str(s) for s in gammas]
        return circuit, param_names

    @staticmethod
    def _bitstring_energy(terms, bitstring):
        """Energy of a measured bitstring (leftmost char = qubit 0)."""
        energy = 0.0
        for pauli, indices, coeff in terms:
            value = coeff
            for idx in indices:
                value *= 1.0 - 2.0 * int(bitstring[idx])
            energy += value
        return energy

    def transform(self, context, flowFile):
        import numpy as np
        import cirq
        from cirq.contrib.svg import circuit_to_svg
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
                "got X/Y terms. Use a *VQE solver for general operators.")

        qubits = cirq.LineQubit.range(n)
        circuit, param_names = self._build_ansatz(terms, qubits, p)
        num_params = 2 * p

        # Initial point
        spec = init_raw.lower()
        if spec == "random":
            initial_point = np.random.default_rng(seed).uniform(-np.pi, np.pi, num_params)
        elif spec == "zeros":
            initial_point = np.zeros(num_params)
        else:
            try:
                initial_point = np.array([float(x) for x in init_raw.split(",")])
            except ValueError as exc:
                return self._fail("bad Initial Parameters: {}".format(exc))
            if len(initial_point) != num_params:
                return self._fail(
                    "Initial Parameters length {} != 2p = {}".format(len(initial_point), num_params))

        # Exact diagonal in *qubit-i = bit-i* order; remapped to Cirq's
        # state-vector index order (qubit 0 = most significant bit) so the
        # energy is a plain dot product with the probabilities.
        values = diagonal_values(terms, n)
        states = np.arange(2 ** n)
        cirq_order = np.zeros(2 ** n, dtype=np.int64)
        for i in range(n):
            cirq_order |= ((states >> (n - 1 - i)) & 1) << i
        values_cirq = values[cirq_order]
        exact_min, exact_max = float(values.min()), float(values.max())

        simulator = cirq.Simulator(seed=seed)

        def energy(point):
            resolver = cirq.ParamResolver({name: float(v) for name, v in zip(param_names, point)})
            state = simulator.simulate(circuit, resolver).final_state_vector
            probs = np.abs(np.asarray(state, dtype=np.complex128)) ** 2
            return float(np.dot(probs, values_cirq))

        start = time.time()
        try:
            result = minimize(energy, initial_point, method=_SCIPY_METHOD[opt_name],
                              options={"maxiter": maxiter})
        except Exception as exc:
            self.logger.error("CirqQAOA optimization failed: {}".format(exc))
            return self._fail("optimization failed: {}".format(exc))
        elapsed = time.time() - start

        optimal_point = np.real(np.asarray(result.x, dtype=float))
        optimal_value = float(result.fun)
        evals = int(getattr(result, "nfev", 0) or 0)
        converged = bool(getattr(result, "success", False))

        # Bind optimal parameters -> trained circuit (no measurements).
        opt_resolver = cirq.ParamResolver({name: float(v) for name, v in zip(param_names, optimal_point)})
        optimal_circuit = cirq.resolve_parameters(circuit, opt_resolver)

        # Sample the optimal circuit for the histogram + best measurement.
        measured = optimal_circuit.copy()
        measured.append(cirq.measure(*qubits, key="m"))
        run = simulator.run(measured, repetitions=shots)
        hist = run.histogram(key="m")  # {int: count}, MSB = first qubit
        counts = {format(int(k), "0{}b".format(n)): int(v) for k, v in hist.items()}
        sorted_counts = dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        best_bitstring = min(counts, key=lambda b: self._bitstring_energy(terms, b))
        best_value = self._bitstring_energy(terms, best_bitstring)
        if exact_max > exact_min:
            approximation_ratio = (exact_max - best_value) / (exact_max - exact_min)
        else:
            approximation_ratio = 1.0

        try:
            svg = circuit_to_svg(optimal_circuit)
        except Exception:
            svg = ""

        self.logger.warn(
            "CirqQAOA ({} qubits, p={}, optimizer={}): optimal_value={:.6f}, "
            "best={} ({:.6f}), ratio={:.4f} in {} evals, {:.2f}s".format(
                n, p, opt_name, optimal_value, best_bitstring, best_value,
                approximation_ratio, evals, elapsed))

        attrs = {
            "qaoa.framework": "cirq",
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
            # circuit.* contract for the trained (bound) circuit
            "circuit.format": "cirq_json",
            "circuit.cirq_json": cirq.to_json(optimal_circuit),
            "circuit.num_qubits": str(n),
            "circuit.framework": "cirq",
            "circuit.algorithm": "qaoa",
            "circuit.diagram": str(optimal_circuit),
            # sim.* + report.type so QuanifiReport renders the simulation card
            "sim.shots": str(shots),
            "sim.top_result": top_state,
            "sim.top_probability": "{:.4f}".format(top_count / shots),
            "sim.framework": "cirq",
            "sim.bit_order": "q0_left",
            "report.type": "simulation",
        }
        if svg:
            attrs["circuit.svg"] = svg

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes=attrs,
        )
