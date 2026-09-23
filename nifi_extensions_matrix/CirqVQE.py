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


class CirqVQE(FlowFileTransform):
    """
    Hand-rolled Variational Quantum Eigensolver for Cirq.

    Cirq has no high-level VQE class, so the hybrid loop is built from primitives
    (this is the "we provide the high-level component Cirq lacks" story): a
    ``sympy``-parameterised ansatz is simulated with ``cirq.Simulator``, the
    energy is the Hamiltonian's expectation value on the resulting state vector,
    and a classical ``scipy.optimize.minimize`` drives the parameters. The loop
    stays in one processor for the same reason as QiskitVQE — scipy owns the
    iteration and its state is not serialisable across FlowFiles.

    Solver stage of CirqHamiltonian -> CirqAnsatz -> CirqVQE -> QuanifiReport.
    It is a **strict solver**: it requires an upstream Hamiltonian (content, the
    framework-neutral wire format) and ansatz (``ansatz.cirq_json``), failing
    clearly otherwise. Because the Hamiltonian wire format is shared, a
    QiskitHamiltonian can also feed this solver.

    Expectation values are computed exactly from the state vector; ``Shots`` only
    controls the final sampling of the optimal circuit for the report histogram.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Hand-rolled VQE for Cirq: simulates a sympy-parameterised ansatz, computes the "
            "Hamiltonian expectation from the state vector, and minimises it with "
            "scipy.optimize. Strict solver - requires an upstream Hamiltonian (content) and "
            "ansatz (ansatz.cirq_json). Emits vqe.* results, optimal-circuit counts as JSON, "
            "and report.type=simulation."
        )
        tags = ["quantum", "cirq", "vqe", "variational", "eigensolver"]
        dependencies = ["cirq-core>=1.0", "scipy>=1.10"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

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
            description="'random', 'zeros', or a comma-separated list of floats matching the ansatz parameter count.",
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
        self.descriptors = [self.optimizer, self.max_iterations, self.shots, self.initial_parameters, self.random_seed]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _resolve_hamiltonian(self, flowFile):
        """Build a cirq.PauliSum from the neutral wire format (requires upstream Hamiltonian)."""
        import cirq
        from pauli_dsl import wire_to_terms

        if flowFile.getAttribute("hamiltonian.format") != "sparse_pauli_op_json":
            raise ValueError(
                "no Hamiltonian on the FlowFile (expected hamiltonian.format="
                "'sparse_pauli_op_json' from an upstream Hamiltonian processor)")
        raw = bytes(flowFile.getContentsAsBytes() or b"").decode("utf-8")
        terms, n = wire_to_terms(raw)
        qubits = cirq.LineQubit.range(n)
        pmap = {"X": cirq.X, "Y": cirq.Y, "Z": cirq.Z}
        psum = cirq.PauliSum()
        for pauli, indices, coeff in terms:
            if not pauli:
                psum += coeff * cirq.PauliString()
            else:
                psum += coeff * cirq.PauliString({qubits[i]: pmap[p] for p, i in zip(pauli, indices)})
        return psum, qubits

    def _resolve_ansatz(self, flowFile):
        """Load the parameterised circuit + ordered param names (requires upstream CirqAnsatz)."""
        import cirq

        cj = flowFile.getAttribute("ansatz.cirq_json")
        if not cj:
            raise ValueError("no ansatz on the FlowFile (expected ansatz.cirq_json from an upstream CirqAnsatz)")
        circuit = cirq.read_json(json_text=cj)
        names_attr = flowFile.getAttribute("ansatz.param_names")
        if names_attr:
            param_names = json.loads(names_attr)
        else:
            param_names = sorted(cirq.parameter_names(circuit))
        return circuit, param_names

    def transform(self, context, flowFile):
        import numpy as np
        import cirq
        from cirq.contrib.svg import circuit_to_svg
        from scipy.optimize import minimize

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        opt_name = get(self.optimizer)
        try:
            maxiter = int(get(self.max_iterations))
            shots = int(get(self.shots))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("CirqVQE: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"vqe.error": msg},
            )
        init_raw = get(self.initial_parameters).strip()
        seed_raw = (get(self.random_seed) or "").strip()
        seed = int(seed_raw) if seed_raw else None


        try:
            hamiltonian, qubits = self._resolve_hamiltonian(flowFile)
            circuit, param_names = self._resolve_ansatz(flowFile)
        except Exception as exc:
            self.logger.error("CirqVQE could not resolve inputs: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"vqe.error": "input resolution failed: {}".format(exc)},
            )

        num_params = len(param_names)
        n = len(qubits)
        if num_params == 0:
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"vqe.error": "ansatz has no free parameters; nothing to optimize"},
            )

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
                return FlowFileTransformResult(
                    relationship="failure", contents=b"",
                    attributes={"vqe.error": "bad Initial Parameters: {}".format(exc)})
            if len(initial_point) != num_params:
                return FlowFileTransformResult(
                    relationship="failure", contents=b"",
                    attributes={"vqe.error": "Initial Parameters length {} != ansatz parameters {}".format(
                        len(initial_point), num_params)})

        simulator = cirq.Simulator(seed=seed)
        qubit_map = {q: i for i, q in enumerate(qubits)}

        def energy(values):
            resolver = cirq.ParamResolver({name: float(v) for name, v in zip(param_names, values)})
            state = simulator.simulate(circuit, resolver).final_state_vector
            ev = hamiltonian.expectation_from_state_vector(
                np.asarray(state, dtype=np.complex128), qubit_map)
            return float(np.real(ev))

        start = time.time()
        try:
            result = minimize(energy, initial_point, method=_SCIPY_METHOD[opt_name],
                              options={"maxiter": maxiter})
        except Exception as exc:
            self.logger.error("CirqVQE optimization failed: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"vqe.error": "optimization failed: {}".format(exc)})
        elapsed = time.time() - start

        optimal_point = np.real(np.asarray(result.x, dtype=float))
        optimal_value = float(result.fun)
        evals = int(getattr(result, "nfev", 0) or 0)
        converged = bool(getattr(result, "success", False))

        # Bind optimal parameters -> trained circuit (no measurements).
        opt_resolver = cirq.ParamResolver({name: float(v) for name, v in zip(param_names, optimal_point)})
        optimal_circuit = cirq.resolve_parameters(circuit, opt_resolver)

        # Sample the optimal circuit for the report histogram.
        measured = optimal_circuit.copy()
        measured.append(cirq.measure(*qubits, key="m"))
        run = simulator.run(measured, repetitions=shots)
        hist = run.histogram(key="m")  # {int: count}, MSB = first qubit
        counts = {format(int(k), "0{}b".format(n)): int(v) for k, v in hist.items()}
        sorted_counts = dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        try:
            svg = circuit_to_svg(optimal_circuit)
        except Exception:
            svg = ""

        self.logger.warn(
            "CirqVQE ({}, {} qubits, optimizer={}): optimal_value={:.6f} in {} evals, {:.2f}s".format(
                flowFile.getAttribute("ansatz.type") or "unknown", n, opt_name, optimal_value, evals, elapsed))

        attrs = {
            "vqe.framework": "cirq",
            "vqe.optimal_value": "{:.8f}".format(optimal_value),
            "vqe.optimal_parameters": json.dumps([round(float(x), 8) for x in optimal_point]),
            "vqe.num_iterations": str(evals),
            "vqe.cost_function_evals": str(evals),
            "vqe.max_iterations": str(maxiter),
            "vqe.converged": "true" if converged else "false",
            "vqe.optimizer": opt_name,
            "vqe.ansatz_type": flowFile.getAttribute("ansatz.type") or "unknown",
            "vqe.num_qubits": str(n),
            "vqe.shots": str(shots),
            "vqe.elapsed_seconds": "{:.4f}".format(elapsed),
            "perf.elapsed_seconds": "{:.4f}".format(elapsed),
            **({"run.seed": str(seed)} if seed is not None else {}),
            # circuit.* contract for the trained (bound) circuit
            "circuit.format": "cirq_json",
            "circuit.cirq_json": cirq.to_json(optimal_circuit),
            "circuit.num_qubits": str(n),
            "circuit.framework": "cirq",
            "circuit.algorithm": "vqe",
            "circuit.diagram": str(optimal_circuit),
            # sim.* + report.type so QuanifiReport renders the simulation card
            "sim.shots": str(shots),
            "sim.top_result": top_state,
            "sim.top_probability": "{:.4f}".format(top_count / shots),
            "sim.framework": "cirq",
            "report.type": "simulation",
        }
        if svg:
            attrs["circuit.svg"] = svg

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes=attrs,
        )
