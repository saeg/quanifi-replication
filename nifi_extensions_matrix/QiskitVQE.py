import base64
import io
import json
import time

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitVQE(FlowFileTransform):
    """
    Runs the Variational Quantum Eigensolver: the hybrid quantum-classical loop
    that finds the minimum eigenvalue of a Hamiltonian by varying an ansatz's
    parameters.

    This is the *solver* stage of
    QiskitHamiltonian -> QiskitAnsatz -> QiskitVQE -> QuanifiReport. The
    variational loop is irreducible to a single processor: Qiskit's
    ``optimizer.minimize(fun, x0, ...)`` owns the iteration synchronously and
    keeps its state (trust region / momentum / perturbations) inside the Python
    object, so the loop cannot be split across FlowFiles with stock optimizers.
    What *is* split out are the reusable preparation stages (Hamiltonian, ansatz)
    and the reporting stage.

    This is a *strict* solver: it owns only the optimization concerns and
    requires both preparation inputs to arrive on the FlowFile, so it cannot be
    confused with — or accidentally diverge from — the upstream stages.

      * Operator: parsed from the FlowFile content; requires ``hamiltonian.format
        = sparse_pauli_op_json`` (from an upstream QiskitHamiltonian).
      * Ansatz: loaded from the ``ansatz.qpy_b64`` attribute (from an upstream
        QiskitAnsatz).

    If either input is missing the FlowFile is routed to ``failure`` with a clear
    ``vqe.error``. The processor's own properties are strictly the solver's:
    optimizer, iterations, shots, and initial point.

    VQE expectation values use exact statevector simulation (deterministic and
    well-converging); the ``Shots`` property controls only the final sampling of
    the optimal-parameter circuit, whose measurement counts become the FlowFile
    content so QuanifiReport and QuantumDistributionComparison work downstream.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Variational Quantum Eigensolver. Reads a Hamiltonian (from QiskitHamiltonian "
            "content or the Hamiltonian property) and an ansatz (from the ansatz.qpy_b64 "
            "attribute or built from properties), then minimises the energy with a classical "
            "optimizer. Emits vqe.* result attributes, the optimal-circuit measurement counts "
            "as JSON content, and report.type = simulation for QuanifiReport."
        )
        tags = ["quantum", "qiskit", "vqe", "variational", "eigensolver"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-aer>=0.13.0", "qiskit-algorithms>=0.3.0"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

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
                "Empty = nondeterministic. Seeds qiskit_algorithms (algorithm_globals) and the final Aer sampling."
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
            self.optimizer, self.max_iterations, self.shots, self.initial_parameters,
            self.random_seed,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _resolve_operator(self, flowFile):
        """Operator from the FlowFile content (requires an upstream QiskitHamiltonian)."""
        from qiskit.quantum_info import SparsePauliOp

        if flowFile.getAttribute("hamiltonian.format") != "sparse_pauli_op_json":
            raise ValueError(
                "no Hamiltonian on the FlowFile (expected hamiltonian.format="
                "'sparse_pauli_op_json' from an upstream QiskitHamiltonian)")
        raw = bytes(flowFile.getContentsAsBytes() or b"")
        data = json.loads(raw.decode("utf-8"))
        terms = [(t[0], complex(t[1], t[2])) for t in data["terms"]]
        return SparsePauliOp.from_list(terms, num_qubits=data["num_qubits"])

    def _resolve_ansatz(self, flowFile):
        """Ansatz from the ansatz.qpy_b64 attribute (requires an upstream QiskitAnsatz)."""
        b64 = flowFile.getAttribute("ansatz.qpy_b64")
        if not b64:
            raise ValueError(
                "no ansatz on the FlowFile (expected ansatz.qpy_b64 from an upstream QiskitAnsatz)")
        from qiskit import qpy
        return qpy.load(io.BytesIO(base64.b64decode(b64)))[0]

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
        from qiskit.primitives import StatevectorEstimator
        from qiskit_aer import AerSimulator
        from qiskit_algorithms import VQE

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        opt_name = get(self.optimizer)
        init_spec = get(self.initial_parameters).strip().lower()
        init_raw = get(self.initial_parameters).strip()
        try:
            maxiter = int(get(self.max_iterations))
            shots = int(get(self.shots))
            seed_raw = (get(self.random_seed) or "").strip()
            seed = int(seed_raw) if seed_raw else None
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QiskitVQE: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"vqe.error": msg},
            )
        if seed is not None:
            from qiskit_algorithms.utils import algorithm_globals
            algorithm_globals.random_seed = seed

        try:
            operator = self._resolve_operator(flowFile)
            ansatz = self._resolve_ansatz(flowFile)
        except Exception as exc:
            self.logger.error("QiskitVQE could not resolve inputs: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure",
                contents=b"",
                attributes={"vqe.error": "input resolution failed: {}".format(exc)},
            )

        if ansatz.num_parameters == 0:
            msg = "Ansatz has no free parameters; nothing to optimize."
            self.logger.error("QiskitVQE: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"vqe.error": msg},
            )
        if ansatz.num_qubits != operator.num_qubits:
            msg = "Ansatz qubits ({}) != operator qubits ({}).".format(
                ansatz.num_qubits, operator.num_qubits)
            self.logger.error("QiskitVQE: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"vqe.error": msg},
            )

        # Initial point
        if init_spec == "random":
            initial_point = None
        elif init_spec == "zeros":
            initial_point = np.zeros(ansatz.num_parameters)
        else:
            try:
                initial_point = np.array([float(x) for x in init_raw.split(",")])
            except ValueError as exc:
                return FlowFileTransformResult(
                    relationship="failure", contents=b"",
                    attributes={"vqe.error": "bad Initial Parameters: {}".format(exc)},
                )
            if len(initial_point) != ansatz.num_parameters:
                return FlowFileTransformResult(
                    relationship="failure", contents=b"",
                    attributes={"vqe.error": "Initial Parameters length {} != ansatz parameters {}".format(
                        len(initial_point), ansatz.num_parameters)},
                )

        # Run VQE with exact expectation values.
        estimator = StatevectorEstimator()
        optimizer = self._make_optimizer(opt_name, maxiter)
        vqe = VQE(estimator, ansatz, optimizer, initial_point=initial_point)

        start = time.time()
        try:
            result = vqe.compute_minimum_eigenvalue(operator)
        except Exception as exc:
            self.logger.error("QiskitVQE optimization failed: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"vqe.error": "optimization failed: {}".format(exc)},
            )
        elapsed = time.time() - start

        optimal_point = np.real(np.asarray(result.optimal_point, dtype=float))
        optimal_value = float(np.real(result.eigenvalue))
        evals = int(result.cost_function_evals) if result.cost_function_evals is not None else maxiter
        # Heuristic: scipy/COBYLA report fewer evals than the cap once their
        # internal tolerance is met. Not authoritative for SPSA.
        converged = evals < maxiter

        # Bind the optimal parameters -> the trained circuit (no measurements,
        # so it stays consumable by QuanifiUnitary).
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
        # register separators and reverse — same as QiskitAerSimulator/QiskitQAOA.
        normalised = {}
        for key, cnt in counts.items():
            k = key.replace(" ", "")[::-1]
            normalised[k] = normalised.get(k, 0) + cnt
        sorted_counts = dict(sorted(normalised.items(), key=lambda kv: kv[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        # Serialise the trained circuit for downstream circuit.* consumers.
        tc = transpile(optimal_circuit, basis_gates=["rz", "ry", "rx", "cx", "h", "x"], optimization_level=0)
        circuit_qasm3 = qasm3.dumps(tc)
        ops = optimal_circuit.count_ops()

        self.logger.warn(
            "QiskitVQE ({}, {} qubits, optimizer={}): optimal_value={:.6f} in {} evals, {:.2f}s".format(
                flowFile.getAttribute("ansatz.type") or "unknown",
                operator.num_qubits, opt_name, optimal_value, evals, elapsed,
            )
        )

        attrs = {
            "vqe.framework": "qiskit",
            "vqe.optimal_value": "{:.8f}".format(optimal_value),
            "vqe.optimal_parameters": json.dumps([round(float(x), 8) for x in optimal_point]),
            "vqe.num_iterations": str(evals),
            "vqe.cost_function_evals": str(evals),
            "vqe.max_iterations": str(maxiter),
            "vqe.converged": "true" if converged else "false",
            "vqe.optimizer": opt_name,
            "vqe.ansatz_type": flowFile.getAttribute("ansatz.type") or "unknown",
            "vqe.num_qubits": str(operator.num_qubits),
            "vqe.shots": str(shots),
            "vqe.elapsed_seconds": "{:.4f}".format(elapsed),
            "perf.elapsed_seconds": "{:.4f}".format(elapsed),
            **({"run.seed": str(seed)} if seed is not None else {}),
            # circuit.* contract for the trained (bound) circuit
            "circuit.format": "qasm3",
            "circuit.qasm3": circuit_qasm3,
            "circuit.num_qubits": str(operator.num_qubits),
            "circuit.framework": "qiskit",
            "circuit.algorithm": "vqe",
            "circuit.diagram": str(optimal_circuit.draw("text")),
            "circuit.depth": str(optimal_circuit.depth()),
            "circuit.gate_count": str(sum(v for k, v in ops.items() if k not in ("barrier", "measure"))),
            "circuit.nonlocal_gates": str(optimal_circuit.num_nonlocal_gates()),
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
