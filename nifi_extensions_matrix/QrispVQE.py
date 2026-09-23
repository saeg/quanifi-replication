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


class QrispVQE(FlowFileTransform):
    """
    Variational Quantum Eigensolver for Qrisp, built on Qrisp's high-level
    ``VQEProblem``.

    Unlike Cirq (no VQE class, fully hand-rolled), Qrisp ships a first-class
    ``VQEProblem`` that owns the hybrid loop — so this solver assembles the
    problem from the upstream stages and calls it, rather than re-implementing
    the optimization. Solver stage of QrispHamiltonian -> QrispAnsatz ->
    QrispVQE -> QuanifiReport.

    Strict solver: it requires an upstream Hamiltonian (content, the
    framework-neutral wire format) and an ansatz spec (``ansatz.format =
    qrisp_spec``), failing clearly otherwise. The Hamiltonian wire format is
    shared, so a QiskitHamiltonian or CirqHamiltonian can also feed it; the
    ansatz spec is rebuilt into a Qrisp ``ansatz_function`` via QrispAnsatz's
    shared builder.

    One optimization (``VQEProblem.train_function``) yields the trained
    state-prep; the ground-state energy and the measurement distribution are then
    read from that same trained state, so they are consistent. Qrisp returns
    probabilities (not shot counts), matching QrispGroverSearch.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Variational Quantum Eigensolver using Qrisp's VQEProblem. Reads a Hamiltonian "
            "(content) and an ansatz spec (ansatz.format=qrisp_spec), trains the ansatz with "
            "the chosen optimizer, and emits vqe.* results, the optimal-state probability "
            "distribution as JSON, and report.type=simulation. Strict solver - requires both "
            "upstream stages."
        )
        tags = ["quantum", "qrisp", "vqe", "variational", "eigensolver"]
        dependencies = ["qrisp==0.9.5", "qiskit>=2.0.0,<2.5", "qiskit-qasm3-import"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.optimizer = PropertyDescriptor(
            name="Optimizer",
            description="Classical optimizer forwarded to Qrisp's VQEProblem.run (scipy.optimize).",
            required=True,
            default_value="COBYLA",
            allowable_values=["COBYLA", "NELDER_MEAD", "POWELL"],
        )
        self.max_iterations = PropertyDescriptor(
            name="Max Iterations",
            description="Maximum optimizer iterations (VQEProblem max_iter).",
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
            description="'random', 'zeros', or a comma-separated list of floats matching the total parameter count (params-per-layer * reps).",
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
        self.descriptors = [self.optimizer, self.max_iterations, self.shots, self.initial_parameters, self.random_seed]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _resolve_hamiltonian(self, flowFile):
        """Build a qrisp QubitOperator from the neutral wire format (requires upstream Hamiltonian)."""
        from pauli_dsl import wire_to_terms
        from qrisp.operators.qubit import X, Y, Z

        if flowFile.getAttribute("hamiltonian.format") != "sparse_pauli_op_json":
            raise ValueError(
                "no Hamiltonian on the FlowFile (expected hamiltonian.format="
                "'sparse_pauli_op_json' from an upstream Hamiltonian processor)")
        raw = bytes(flowFile.getContentsAsBytes() or b"").decode("utf-8")
        terms, n = wire_to_terms(raw)
        pmap = {"X": X, "Y": Y, "Z": Z}
        operator = 0
        for pauli, indices, coeff in terms:
            if not pauli:
                operator = operator + coeff
            else:
                factor = None
                for p, idx in zip(pauli, indices):
                    factor = pmap[p](idx) if factor is None else factor * pmap[p](idx)
                operator = operator + coeff * factor
        return operator, n

    def _resolve_ansatz_spec(self, flowFile, n):
        """Rebuild a Qrisp ansatz_function from the upstream spec (requires upstream QrispAnsatz)."""
        from QrispAnsatz import _build_ansatz_function

        if flowFile.getAttribute("ansatz.format") != "qrisp_spec":
            raise ValueError("no ansatz on the FlowFile (expected ansatz.format='qrisp_spec' from an upstream QrispAnsatz)")
        atype = flowFile.getAttribute("ansatz.type") or "efficient_su2"
        reps = int(flowFile.getAttribute("ansatz.reps") or "2")
        entanglement = flowFile.getAttribute("ansatz.entanglement") or "full"
        ansatz_function, per_layer = _build_ansatz_function(atype, n, entanglement)
        return ansatz_function, per_layer, reps, atype

    def transform(self, context, flowFile):
        import numpy as np
        from qrisp import QuantumVariable
        from qrisp.vqe.vqe_problem import VQEProblem

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        opt_name = get(self.optimizer)
        init_raw = get(self.initial_parameters).strip()
        try:
            maxiter = int(get(self.max_iterations))
            shots = int(get(self.shots))
            seed_raw = (get(self.random_seed) or "").strip()
            seed = int(seed_raw) if seed_raw else None
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QrispVQE: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"vqe.error": msg})


        try:
            hamiltonian, n = self._resolve_hamiltonian(flowFile)
            ansatz_function, per_layer, reps, atype = self._resolve_ansatz_spec(flowFile, n)
        except Exception as exc:
            self.logger.error("QrispVQE could not resolve inputs: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"vqe.error": "input resolution failed: {}".format(exc)})

        total_params = per_layer * reps
        if total_params == 0:
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"vqe.error": "ansatz has no free parameters; nothing to optimize"})

        # Initial point
        spec = init_raw.lower()
        init_point = None
        if spec == "zeros":
            init_point = np.zeros(total_params)
        elif spec != "random":
            try:
                init_point = np.array([float(x) for x in init_raw.split(",")])
            except ValueError as exc:
                return FlowFileTransformResult(
                    relationship="failure", contents=b"",
                    attributes={"vqe.error": "bad Initial Parameters: {}".format(exc)})
            if len(init_point) != total_params:
                return FlowFileTransformResult(
                    relationship="failure", contents=b"",
                    attributes={"vqe.error": "Initial Parameters length {} != total parameters {}".format(
                        len(init_point), total_params)})

        if seed is not None:
            np.random.seed(seed)  # best-effort: Qrisp has no seed API
        vqe = VQEProblem(hamiltonian, ansatz_function, per_layer, callback=True)

        start = time.time()
        try:
            # Suppress Qrisp's tqdm progress bar: NiFi's py4j bridge uses stdout,
            # so printing there corrupts the channel ("null response" crash).
            with contextlib.redirect_stdout(io.StringIO()):
                circuit_generator = vqe.train_function(
                    QuantumVariable(n), depth=reps, max_iter=maxiter,
                    optimizer=_OPTIMIZER[opt_name],
                    init_type="random", init_point=init_point)
                # Energy and counts from the SAME trained state, so they are consistent.
                # qrisp >=0.9 replaced QubitOperator.get_measurement(qv) with
                # expectation_value(state_prep) -> callable, where state_prep is a
                # function returning the prepared QuantumVariable.
                def _trained_state():
                    qv_e = QuantumVariable(n)
                    circuit_generator(qv_e)
                    return qv_e
                optimal_value = float(np.real(
                    hamiltonian.expectation_value(_trained_state, precision=0.01)()))

                qv_c = QuantumVariable(n)
                circuit_generator(qv_c)
                probs = qv_c.get_measurement(shots=shots)
        except Exception as exc:
            self.logger.error("QrispVQE optimization failed: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"vqe.error": "optimization failed: {}".format(exc)})
        elapsed = time.time() - start

        # Canonical bit order (q0_left): get_measurement on a bare
        # QuantumVariable already decodes qv[0] at the LEFTMOST char, and the
        # QubitOperator expectation machinery (which trained this state) uses
        # the same indexing — so the keys are canonical as-is. Do NOT reverse
        # here: QrispGroverSearch's reversal only bridges tag_state's mirrored
        # target-string convention, it is not a property of the measurement.
        sorted_probs = dict(sorted(probs.items(), key=lambda kv: kv[1], reverse=True))
        top_state, top_prob = next(iter(sorted_probs.items()))
        evals = len(vqe.optimization_costs) if vqe.optimization_costs else maxiter

        # Best-effort QASM3 export of the trained circuit (mirrors QrispGroverSearch).
        # qv_c is the trained QuantumVariable in this scope; qv_e only exists
        # inside the _trained_state() closure above.
        circuit_qasm3, diagram = "", ""
        try:
            from qiskit import qasm3 as qasm3_module
            qc = qv_c.qs.compile().to_qiskit()
            circuit_qasm3 = qasm3_module.dumps(qc)
            diagram = str(qc.draw("text"))
        except Exception as exc:
            self.logger.warn("QrispVQE QASM3 export skipped: {}".format(exc))

        self.logger.warn(
            "QrispVQE ({}, {} qubits, optimizer={}): optimal_value={:.6f} in {} evals, {:.2f}s".format(
                atype, n, opt_name, optimal_value, evals, elapsed))

        attrs = {
            "vqe.framework": "qrisp",
            "vqe.optimal_value": "{:.8f}".format(optimal_value),
            "vqe.num_iterations": str(evals),
            "vqe.cost_function_evals": str(evals),
            "vqe.max_iterations": str(maxiter),
            "vqe.optimizer": opt_name,
            "vqe.ansatz_type": atype,
            "vqe.num_qubits": str(n),
            "vqe.shots": str(shots),
            "vqe.elapsed_seconds": "{:.4f}".format(elapsed),
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
        if vqe.optimization_params:
            attrs["vqe.optimal_parameters"] = json.dumps([round(float(x), 8) for x in vqe.optimization_params[-1]])
        if circuit_qasm3:
            attrs["circuit.format"] = "qasm3"
            attrs["circuit.qasm3"] = circuit_qasm3
            attrs["circuit.num_qubits"] = str(n)
            attrs["circuit.framework"] = "qrisp"
            attrs["circuit.algorithm"] = "vqe"
        if diagram:
            attrs["circuit.diagram"] = diagram

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_probs, indent=2).encode("utf-8"),
            attributes=attrs,
        )
