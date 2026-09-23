import json
import os
import sys
import time
import contextlib
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder

# scipy optimizer name per the processor's enum (Qrisp's run() forwards these).
_OPTIMIZER = {"COBYLA": "COBYLA", "NELDER_MEAD": "Nelder-Mead", "POWELL": "Powell"}


def _bitstring_energy(terms, bitstring):
    """Energy of a measured bitstring (leftmost char = qv[0], Qrisp's order)."""
    energy = 0.0
    for pauli, indices, coeff in terms:
        value = coeff
        for idx in indices:
            value *= 1.0 - 2.0 * int(bitstring[idx])
        energy += value
    return energy


class QrispQAOA(FlowFileTransform):
    """
    Quantum Approximate Optimization Algorithm solver for Qrisp, built on
    Qrisp's first-class ``QAOAProblem``.

    Unlike Cirq (no QAOA class, fully hand-rolled), Qrisp ships ``QAOAProblem``
    which owns the hybrid loop — so this solver assembles the three pieces
    (cost operator, RX mixer, classical cost function) from the upstream
    Hamiltonian and calls it, rather than re-implementing the optimization.

    Solver stage of <any>Hamiltonian -> QrispQAOA -> QuanifiReport. Strict
    solver: it requires an upstream Hamiltonian (content, the framework-
    neutral wire format) and that Hamiltonian must be *diagonal* (I/Z only);
    anything else routes to ``failure`` with a clear ``qaoa.error``. Because
    the wire format is shared, any <Framework>Hamiltonian can feed it.

    Bitstring convention follows the existing Qrisp processors: leftmost
    character = qv[0]. One optimization (``QAOAProblem.train_function``)
    yields the trained state-prep; the energy, the best measurement, and the
    distribution are all read from that same trained state, so they are
    consistent. Qrisp returns probabilities (not shot counts), matching
    QrispVQE/QrispGroverSearch.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "QAOA solver using Qrisp's QAOAProblem. Reads a diagonal cost Hamiltonian "
            "(hamiltonian.format=sparse_pauli_op_json from any upstream Hamiltonian "
            "processor), trains the p-layer QAOA circuit, and emits qaoa.* results "
            "(optimal value, best bitstring, approximation ratio), the optimal-state "
            "probability distribution as JSON, and report.type=simulation. Strict "
            "solver - requires the upstream Hamiltonian."
        )
        tags = ["quantum", "qrisp", "qaoa", "optimization", "variational"]
        dependencies = ["qrisp==0.9.5", "qiskit>=2.0.0,<2.5", "qiskit-qasm3-import"]

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
            description="Classical optimizer forwarded to Qrisp's QAOAProblem (scipy.optimize).",
            required=True,
            default_value="COBYLA",
            allowable_values=["COBYLA", "NELDER_MEAD", "POWELL"],
        )
        self.max_iterations = PropertyDescriptor(
            name="Max Iterations",
            description="Maximum optimizer iterations (QAOAProblem max_iter).",
            required=True,
            default_value="100",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description="Shots passed to get_measurement() for the final probability distribution. Does not affect optimization.",
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.initial_parameters = PropertyDescriptor(
            name="Initial Parameters",
            description="'random', 'zeros', or a comma-separated list of 2p floats (Qrisp's internal gamma/beta ordering).",
            required=True,
            default_value="random",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.random_seed = PropertyDescriptor(
            name="Random Seed",
            description=(
                "Seed for reproducible runs (initial parameters and sampling). "
                "Empty = nondeterministic. Best-effort via the global NumPy seed: Qrisp draws inits and samples from it."
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
        self.logger.error("QrispQAOA: " + msg)
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
    def _make_cost_operator(terms):
        """Phase-separation function U_P(C, gamma): CNOT ladder + Rz per Z-string.

        Identity terms only shift the energy by a constant and are skipped.
        """
        from qrisp import cx, rz

        def cost_operator(qv, gamma):
            for pauli, indices, coeff in terms:
                if not pauli:
                    continue
                for a, b in zip(indices, indices[1:]):
                    cx(qv[a], qv[b])
                rz(2.0 * coeff * gamma, qv[indices[-1]])
                for a, b in reversed(list(zip(indices, indices[1:]))):
                    cx(qv[a], qv[b])

        return cost_operator

    def transform(self, context, flowFile):
        import numpy as np
        from qrisp import QuantumVariable
        from qrisp.qaoa import QAOAProblem, RX_mixer
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

        # Initial point
        spec = init_raw.lower()
        init_point = None
        if spec == "zeros":
            init_point = np.zeros(2 * p)
        elif spec != "random":
            try:
                init_point = np.array([float(x) for x in init_raw.split(",")])
            except ValueError as exc:
                return self._fail("bad Initial Parameters: {}".format(exc))
            if len(init_point) != 2 * p:
                return self._fail(
                    "Initial Parameters length {} != 2p = {}".format(len(init_point), 2 * p))

        def cl_cost_function(res_dict):
            # qv.get_measurement keys are little-endian (qv[0] = RIGHTMOST
            # char), but _bitstring_energy reads char i as qubit i. Reverse so
            # the classical training loss evaluates the state that was actually
            # sampled — without this the optimizer trains the bit-reversed
            # (mirror-image) Hamiltonian.
            return sum(prob * _bitstring_energy(terms, bits[::-1])
                       for bits, prob in res_dict.items())

        if seed is not None:
            np.random.seed(seed)  # best-effort: Qrisp has no seed API
        qaoa = QAOAProblem(
            cost_operator=self._make_cost_operator(terms),
            mixer=RX_mixer,
            cl_cost_function=cl_cost_function,
            callback=True,
        )

        start = time.time()
        try:
            # Suppress Qrisp's tqdm progress bar: NiFi's py4j bridge uses stdout,
            # so printing there corrupts the channel ("null response" crash).
            with contextlib.redirect_stdout(io.StringIO()):
                circuit_generator = qaoa.train_function(
                    QuantumVariable(n), depth=p, max_iter=maxiter,
                    optimizer=_OPTIMIZER[opt_name],
                    init_type="random", init_point=init_point)
                # Energy, best measurement, and counts from the SAME trained state.
                qv = QuantumVariable(n)
                circuit_generator(qv)
                probs = qv.get_measurement(shots=shots)
        except Exception as exc:
            self.logger.error("QrispQAOA optimization failed: {}".format(exc))
            return self._fail("optimization failed: {}".format(exc))
        elapsed = time.time() - start

        # Canonical bit order (q0_left): reverse the little-endian keys so
        # char i = qv[i] everywhere downstream (energies, best, counts, report).
        probs = {k[::-1]: v for k, v in probs.items()}
        optimal_value = float(sum(p_ * _bitstring_energy(terms, b)
                                  for b, p_ in probs.items()))
        sorted_probs = dict(sorted(probs.items(), key=lambda kv: kv[1], reverse=True))
        top_state, top_prob = next(iter(sorted_probs.items()))
        evals = len(qaoa.optimization_costs) if qaoa.optimization_costs else maxiter

        best_bitstring = min(probs, key=lambda b: _bitstring_energy(terms, b))
        best_value = _bitstring_energy(terms, best_bitstring)
        values = diagonal_values(terms, n)
        exact_min, exact_max = float(values.min()), float(values.max())
        if exact_max > exact_min:
            approximation_ratio = (exact_max - best_value) / (exact_max - exact_min)
        else:
            approximation_ratio = 1.0

        # Best-effort QASM3 export of the trained circuit (mirrors QrispVQE).
        circuit_qasm3, diagram = "", ""
        try:
            from qiskit import qasm3 as qasm3_module
            qc = qv.qs.compile().to_qiskit()
            circuit_qasm3 = qasm3_module.dumps(qc)
            diagram = str(qc.draw("text"))
        except Exception as exc:
            self.logger.warn("QrispQAOA QASM3 export skipped: {}".format(exc))

        self.logger.warn(
            "QrispQAOA ({} qubits, p={}, optimizer={}): optimal_value={:.6f}, "
            "best={} ({:.6f}), ratio={:.4f} in {} evals, {:.2f}s".format(
                n, p, opt_name, optimal_value, best_bitstring, best_value,
                approximation_ratio, evals, elapsed))

        attrs = {
            "qaoa.framework": "qrisp",
            "qaoa.optimal_value": "{:.8f}".format(optimal_value),
            "qaoa.best_measurement": best_bitstring,
            "qaoa.best_value": "{:.8f}".format(best_value),
            "qaoa.exact_minimum": "{:.8f}".format(exact_min),
            "qaoa.approximation_ratio": "{:.6f}".format(approximation_ratio),
            "qaoa.layers": str(p),
            "qaoa.num_iterations": str(evals),
            "qaoa.cost_function_evals": str(evals),
            "qaoa.max_iterations": str(maxiter),
            "qaoa.optimizer": opt_name,
            "qaoa.num_qubits": str(n),
            "qaoa.shots": str(shots),
            "qaoa.elapsed_seconds": "{:.4f}".format(elapsed),
            "perf.elapsed_seconds": "{:.4f}".format(elapsed),
            **({"run.seed": str(seed)} if seed is not None else {}),
            # sim.* + report.type so QuanifiReport renders the simulation card
            "sim.shots": str(shots),
            "sim.top_result": top_state,
            "sim.top_probability": "{:.4f}".format(top_prob),
            "sim.framework": "qrisp",
            "sim.bit_order": "q0_left",
            "report.type": "simulation",
        }
        if qaoa.optimization_params:
            attrs["qaoa.optimal_parameters"] = json.dumps(
                [round(float(x), 8) for x in qaoa.optimization_params[-1]])
        if circuit_qasm3:
            attrs["circuit.format"] = "qasm3"
            attrs["circuit.qasm3"] = circuit_qasm3
            attrs["circuit.num_qubits"] = str(n)
            attrs["circuit.framework"] = "qrisp"
            attrs["circuit.algorithm"] = "qaoa"
        if diagram:
            attrs["circuit.diagram"] = diagram

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_probs, indent=2).encode("utf-8"),
            attributes=attrs,
        )
