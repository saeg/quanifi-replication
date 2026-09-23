import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitQAOA(FlowFileTransform):
    """
    Quantum Approximate Optimization Algorithm solver for Qiskit, built on
    ``qiskit_algorithms.QAOA``.

    Solver stage of <any>Hamiltonian -> QiskitQAOA -> QuanifiReport. Unlike
    VQE, QAOA derives its ansatz from the problem itself (alternating
    cost-layer exp(-i gamma H) and mixer-layer exp(-i beta X) blocks), so
    there is no separate ansatz stage: the cost Hamiltonian on the FlowFile
    is the whole problem definition.

    Strict solver: it requires an upstream Hamiltonian (content, the
    framework-neutral wire format with ``hamiltonian.format =
    sparse_pauli_op_json``) and that Hamiltonian must be *diagonal* (I/Z
    terms only — a classical cost function); anything else routes to
    ``failure`` with a clear ``qaoa.error``. Because the wire format is
    shared, any <Framework>Hamiltonian can feed this solver.

    The exact optimum of the (diagonal) cost is computed classically for the
    report, so ``qaoa.approximation_ratio`` is the normalized quality
    (E_max - E) / (E_max - E_min): 1.0 means the sampled optimum is the true
    minimum. Expectations during optimization use exact statevector sampling;
    ``Shots`` only controls the final histogram.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "QAOA solver using qiskit_algorithms.QAOA. Reads a diagonal cost "
            "Hamiltonian (hamiltonian.format=sparse_pauli_op_json from any upstream "
            "Hamiltonian processor), optimizes the p-layer QAOA ansatz, and emits "
            "qaoa.* results (optimal value, best bitstring, approximation ratio vs "
            "the exact optimum), measurement counts as JSON content, and "
            "report.type=simulation. Strict solver - requires the upstream Hamiltonian."
        )
        tags = ["quantum", "qiskit", "qaoa", "optimization", "variational"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-aer>=0.13.0", "qiskit-algorithms>=0.4.0"]

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
            description=(
                "Classical optimizer driving the parameter search. COBYLA and NELDER_MEAD are "
                "gradient-free; SPSA is robust to noise; L_BFGS_B is gradient-based (uses finite "
                "differences here)."
            ),
            required=True,
            default_value="COBYLA",
            allowable_values=["COBYLA", "SPSA", "NELDER_MEAD", "L_BFGS_B"],
        )
        self.max_iterations = PropertyDescriptor(
            name="Max Iterations",
            description="Maximum optimizer iterations (stop criterion for the classical loop).",
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
                "Empty = nondeterministic. Seeds qiskit_algorithms (algorithm_globals), the sampler, and the final Aer sampling."
            ),
            required=False,
            # No default: an empty string is not a valid non-negative integer, so
            # declaring default_value="" made the processor invalid on the canvas
            # before anyone configured it. Unset means nondeterministic, which the
            # transform already handles.
            validators=[StandardValidators.NON_NEGATIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.layers, self.optimizer, self.max_iterations, self.shots, self.initial_parameters,
            self.random_seed,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _fail(self, msg):
        self.logger.error("QiskitQAOA: " + msg)
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

    def _make_optimizer(self, name, maxiter):
        from qiskit_algorithms.optimizers import COBYLA, SPSA, NELDER_MEAD, L_BFGS_B
        if name == "SPSA":
            return SPSA(maxiter=maxiter)
        if name == "NELDER_MEAD":
            return NELDER_MEAD(maxiter=maxiter)
        if name == "L_BFGS_B":
            return L_BFGS_B(maxiter=maxiter)
        return COBYLA(maxiter=maxiter)

    def transform(self, context, flowFile):
        import numpy as np
        from qiskit import transpile, qasm3
        from qiskit.circuit.library import QAOAAnsatz
        from qiskit.primitives import StatevectorSampler
        from qiskit.quantum_info import SparsePauliOp
        from qiskit_aer import AerSimulator
        from qiskit_algorithms.minimum_eigensolvers import QAOA
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
        if seed is not None:
            from qiskit_algorithms.utils import algorithm_globals
            algorithm_globals.random_seed = seed

        try:
            terms, n = self._resolve_cost_terms(flowFile)
        except Exception as exc:
            return self._fail("input resolution failed: {}".format(exc))

        if not is_diagonal(terms):
            return self._fail(
                "cost Hamiltonian must be diagonal (I/Z terms only) for QAOA; "
                "got X/Y terms. Use a *VQE solver for general operators.")

        operator = SparsePauliOp.from_sparse_list(
            [(pauli, idx, coeff) for pauli, idx, coeff in terms], num_qubits=n)

        # Initial point: 2p parameters (betas then gammas, QAOAAnsatz order).
        spec = init_raw.lower()
        if spec == "random":
            initial_point = None
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

        qaoa = QAOA(
            StatevectorSampler(seed=seed),
            self._make_optimizer(opt_name, maxiter),
            reps=p,
            initial_point=initial_point,
        )

        start = time.time()
        try:
            result = qaoa.compute_minimum_eigenvalue(operator)
        except Exception as exc:
            self.logger.error("QiskitQAOA optimization failed: {}".format(exc))
            return self._fail("optimization failed: {}".format(exc))
        elapsed = time.time() - start

        optimal_point = np.real(np.asarray(result.optimal_point, dtype=float))
        optimal_value = float(np.real(result.eigenvalue))
        evals = int(result.cost_function_evals) if result.cost_function_evals is not None else maxiter
        converged = evals < maxiter
        best = result.best_measurement or {}
        # qiskit_algorithms returns Qiskit-ordered strings (qubit 0 = rightmost);
        # reverse to the canonical q0_left order used by every Quanifi emitter.
        best_bitstring = str(best.get("bitstring", ""))[::-1]
        best_value = float(np.real(best.get("value", optimal_value)))

        # Exact optimum of the diagonal cost -> normalized approximation ratio
        # (1.0 = the sampled best state is a true optimum).
        values = diagonal_values(terms, n)
        exact_min, exact_max = float(values.min()), float(values.max())
        if exact_max > exact_min:
            approximation_ratio = (exact_max - best_value) / (exact_max - exact_min)
        else:
            approximation_ratio = 1.0

        # Bind the optimal parameters -> trained circuit (same construction as
        # QAOA's internal ansatz, so the parameter order matches optimal_point).
        ansatz = QAOAAnsatz(cost_operator=operator, reps=p)
        optimal_circuit = ansatz.assign_parameters(optimal_point)

        # Sample the optimal circuit for the report histogram.
        measured = optimal_circuit.copy()
        measured.measure_all()
        sim = AerSimulator()
        run_kwargs = {"shots": shots}
        if seed is not None:
            run_kwargs["seed_simulator"] = seed
        counts = sim.run(transpile(measured, sim), **run_kwargs).result().get_counts()
        # Canonical bit order (q0_left): Aer keys are little-endian, so strip
        # register separators and reverse — same as QiskitAerSimulator.
        normalised = {}
        for key, cnt in counts.items():
            k = key.replace(" ", "")[::-1]
            normalised[k] = normalised.get(k, 0) + cnt
        sorted_counts = dict(sorted(normalised.items(), key=lambda kv: kv[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        # Serialise the trained circuit for downstream circuit.* consumers.
        decomposed = optimal_circuit.decompose(reps=3)
        tc = transpile(decomposed, basis_gates=["rz", "ry", "rx", "cx", "h", "x"], optimization_level=0)
        circuit_qasm3 = qasm3.dumps(tc)
        ops = tc.count_ops()

        self.logger.warn(
            "QiskitQAOA ({} qubits, p={}, optimizer={}): optimal_value={:.6f}, "
            "best={} ({:.6f}), ratio={:.4f} in {} evals, {:.2f}s".format(
                n, p, opt_name, optimal_value, best_bitstring, best_value,
                approximation_ratio, evals, elapsed))

        attrs = {
            "qaoa.framework": "qiskit",
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
            "circuit.format": "qasm3",
            "circuit.qasm3": circuit_qasm3,
            "circuit.num_qubits": str(n),
            "circuit.framework": "qiskit",
            "circuit.algorithm": "qaoa",
            "circuit.diagram": str(tc.draw("text")),
            "circuit.depth": str(tc.depth()),
            "circuit.gate_count": str(sum(v for k, v in ops.items() if k not in ("barrier", "measure"))),
            "circuit.nonlocal_gates": str(tc.num_nonlocal_gates()),
            "circuit.t_count": str(ops.get("t", 0) + ops.get("tdg", 0)),
            # sim.* + report.type so QuanifiReport renders the simulation card
            "sim.shots": str(shots),
            "sim.top_result": top_state,
            "sim.top_probability": "{:.4f}".format(top_count / shots),
            "sim.framework": "qiskit",
            "sim.bit_order": "q0_left",
            "report.type": "simulation",
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes=attrs,
        )
