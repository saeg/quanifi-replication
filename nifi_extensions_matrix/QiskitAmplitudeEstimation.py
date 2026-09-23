import time
import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitAmplitudeEstimation(FlowFileTransform):
    """
    Quantum Amplitude Estimation on a Bernoulli problem, using qiskit-algorithms.

    The Bernoulli state preparation A = Ry(theta) with theta = 2*asin(sqrt(p))
    puts the objective qubit in state sqrt(1-p)|0> + sqrt(p)|1>, so the
    amplitude to estimate is exactly the configured probability p. Its Grover
    operator is Q = Ry(2*theta), which lets every framework implement the same
    problem and makes the processors cross-comparable via ae.* attributes.

    Two methods, mirroring qiskit-algorithms:
      * canonical  - QPE-based AmplitudeEstimation with m evaluation qubits.
        The raw estimate lies on the grid sin^2(pi*y/2^m) (e.g. p=0.2, m=2
        gives 0.5); the maximum-likelihood post-processing (ae.mle) refines it
        off-grid (~0.2).
      * iterative  - IterativeAmplitudeEstimation(epsilon, alpha), no QPE and
        no evaluation register; converges to p within epsilon.

    Emits ae.* result attributes and report.type=simulation so QuanifiReport
    renders the Amplitude Estimation panel.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Quantum Amplitude Estimation (Bernoulli problem) using qiskit-algorithms. "
            "Estimates the amplitude p prepared by A = Ry(2*asin(sqrt(p))). Method "
            "'canonical' is QPE-based with a grid estimate plus MLE refinement; "
            "'iterative' converges within epsilon without a phase register. Emits "
            "ae.* attributes and report.type = simulation."
        )
        tags = ["quantum", "qiskit", "amplitude-estimation", "qae", "estimation"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-algorithms>=0.3.0"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.probability = PropertyDescriptor(
            name="Probability",
            description=(
                "True Bernoulli probability p in [0, 1] encoded by the state "
                "preparation A = Ry(2*asin(sqrt(p))). This is the value the "
                "algorithm estimates."
            ),
            required=True,
            default_value="0.2",
            validators=[StandardValidators.NUMBER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.eval_qubits = PropertyDescriptor(
            name="Evaluation Qubits",
            description=(
                "Size m of the QPE evaluation register (canonical method only). "
                "The raw estimate lies on the grid sin^2(pi*y/2^m)."
            ),
            required=True,
            default_value="3",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.method = PropertyDescriptor(
            name="Method",
            description=(
                "canonical: QPE-based AmplitudeEstimation (grid estimate + MLE). "
                "iterative: IterativeAmplitudeEstimation (epsilon/alpha, no QPE)."
            ),
            required=True,
            default_value="canonical",
            allowable_values=["canonical", "iterative"],
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description="Shots per circuit run of the sampler.",
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.epsilon = PropertyDescriptor(
            name="Epsilon Target",
            description="Target estimation error (iterative method only).",
            required=True,
            default_value="0.01",
            validators=[StandardValidators.NUMBER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.alpha = PropertyDescriptor(
            name="Alpha",
            description="Confidence level alpha of the iterative estimate (iterative method only).",
            required=True,
            default_value="0.05",
            validators=[StandardValidators.NUMBER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.random_seed = PropertyDescriptor(
            name="Random Seed",
            description=(
                "Seed for reproducible sampling. Empty = nondeterministic. Passed to the StatevectorSampler."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.probability, self.eval_qubits, self.method,
            self.shots, self.epsilon, self.alpha, self.random_seed,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        import numpy as np
        from qiskit import QuantumCircuit
        from qiskit.primitives import StatevectorSampler
        from qiskit_algorithms import (
            AmplitudeEstimation, EstimationProblem, IterativeAmplitudeEstimation,
        )

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        try:
            p = float(get(self.probability))
        except (TypeError, ValueError):
            p = -1.0
        if not 0.0 <= p <= 1.0:
            msg = "Probability must be a number in [0, 1], got {!r}".format(
                get(self.probability))
            self.logger.error("QiskitAmplitudeEstimation: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"ae.error": msg},
            )

        m      = int(get(self.eval_qubits))
        method = get(self.method)
        shots  = int(get(self.shots))
        eps    = float(get(self.epsilon))
        alpha  = float(get(self.alpha))
        seed_raw = (get(self.random_seed) or "").strip()
        seed = int(seed_raw) if seed_raw else None


        # Bernoulli problem: A = Ry(theta), Grover operator Q = Ry(2*theta).
        theta = 2.0 * np.arcsin(np.sqrt(p))
        a_circ = QuantumCircuit(1)
        a_circ.ry(theta, 0)
        q_circ = QuantumCircuit(1)
        q_circ.ry(2.0 * theta, 0)
        problem = EstimationProblem(a_circ, objective_qubits=[0], grover_operator=q_circ)

        sampler = StatevectorSampler(default_shots=shots, seed=seed)

        attrs = {
            "ae.framework":   "qiskit",
            "ae.method":      method,
            "ae.probability": "{:.8f}".format(p),
            "ae.shots":       str(shots),
            "sim.framework":  "qiskit",
            "sim.shots":      str(shots),
            "sim.bit_order":  "q0_left",
            "report.type":    "simulation",
        }

        t0 = time.time()
        try:
            if method == "iterative":
                iae = IterativeAmplitudeEstimation(
                    epsilon_target=eps, alpha=alpha, sampler=sampler)
                result = iae.estimate(problem)
                estimate = float(result.estimation)
                attrs["ae.estimate"] = "{:.8f}".format(estimate)
                attrs["ae.epsilon"]  = "{:.6f}".format(eps)
                attrs["ae.alpha"]    = "{:.6f}".format(alpha)
                content = {"{:.8f}".format(estimate): 1.0}
            else:
                ae = AmplitudeEstimation(num_eval_qubits=m, sampler=sampler)
                result = ae.estimate(problem)
                estimate = float(result.estimation)
                attrs["ae.estimate"]    = "{:.8f}".format(estimate)
                attrs["ae.mle"]         = "{:.8f}".format(float(result.mle))
                attrs["ae.eval_qubits"] = str(m)
                attrs["ae.num_qubits"]  = str(m + 1)
                # samples: gridded estimate -> probability, for the report chart
                content = {
                    "{:.6f}".format(a): round(float(pr), 8)
                    for a, pr in sorted(result.samples.items(), key=lambda kv: -kv[1])
                }
                try:
                    circuit = ae.construct_circuit(problem)
                    ops = circuit.count_ops()
                    attrs["circuit.num_qubits"] = str(circuit.num_qubits)
                    attrs["circuit.depth"]      = str(circuit.depth())
                    attrs["circuit.gate_count"] = str(sum(
                        v for k, v in ops.items() if k not in ("barrier", "measure")))
                    attrs["circuit.diagram"]    = str(circuit.draw("text"))
                except Exception as exc:  # metrics are best-effort
                    self.logger.warn("circuit metrics skipped: {}".format(exc))
        except Exception as exc:
            self.logger.error("QiskitAmplitudeEstimation failed: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"ae.error": "estimation failed: {}".format(exc)},
            )

        attrs["perf.elapsed_seconds"] = "{:.4f}".format(time.time() - t0)
        if seed is not None:
            attrs["run.seed"] = str(seed)
        top = next(iter(content))
        attrs["sim.top_result"]      = top
        attrs["sim.top_probability"] = "{:.4f}".format(float(content[top]))

        self.logger.warn(
            "QiskitAmplitudeEstimation ({}): p={} -> estimate={}{}".format(
                method, p, attrs["ae.estimate"],
                " mle=" + attrs["ae.mle"] if "ae.mle" in attrs else "",
            )
        )

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(content, indent=2).encode("utf-8"),
            attributes=attrs,
        )
