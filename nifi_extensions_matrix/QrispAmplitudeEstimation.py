import time
import json
import contextlib
import io

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QrispAmplitudeEstimation(FlowFileTransform):
    """
    Quantum Amplitude Estimation on a Bernoulli problem via Qrisp's native
    IQAE (iterative, QPE-free amplitude estimation).

    The Bernoulli state preparation A = Ry(theta) with theta = 2*asin(sqrt(p))
    puts a QuantumBool in state sqrt(1-p)|0> + sqrt(p)|1>; IQAE estimates the
    probability of measuring |1>, i.e. exactly the configured p, to within
    epsilon at confidence alpha. This mirrors QiskitAmplitudeEstimation's
    'iterative' method (Qrisp has no canonical QPE-based estimator), so the
    two are cross-comparable through the shared ae.* attributes.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Quantum Amplitude Estimation (Bernoulli problem) using Qrisp's native "
            "IQAE. Estimates the amplitude p prepared by A = Ry(2*asin(sqrt(p))) to "
            "within Epsilon Target at confidence Alpha. Emits ae.* attributes and "
            "report.type = simulation."
        )
        tags = ["quantum", "qrisp", "amplitude-estimation", "iqae", "estimation"]
        dependencies = ["qrisp==0.9.5", "qiskit>=2.0.0,<2.5"]

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
        self.epsilon = PropertyDescriptor(
            name="Epsilon Target",
            description="Target estimation error of IQAE.",
            required=True,
            default_value="0.01",
            validators=[StandardValidators.NUMBER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.alpha = PropertyDescriptor(
            name="Alpha",
            description="Confidence level alpha of the IQAE estimate.",
            required=True,
            default_value="0.05",
            validators=[StandardValidators.NUMBER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description="Shots per IQAE round (passed through mes_kwargs).",
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.random_seed = PropertyDescriptor(
            name="Random Seed",
            description=(
                "Seed for reproducible sampling. Empty = nondeterministic. Best-effort via the global NumPy seed: IQAE has no seed API."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.probability, self.epsilon, self.alpha, self.shots, self.random_seed]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        import numpy as np
        from qrisp import IQAE, QuantumBool, ry

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
            self.logger.error("QrispAmplitudeEstimation: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"ae.error": msg},
            )

        try:
            eps   = float(get(self.epsilon))
            alpha = float(get(self.alpha))
            shots = int(get(self.shots))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QrispAmplitudeEstimation: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"ae.error": msg},
            )
        try:
            seed_raw = (get(self.random_seed) or "").strip()
            seed = int(seed_raw) if seed_raw else None
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QrispAmplitudeEstimation: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"ae.error": msg},
            )

        theta = 2.0 * float(np.arcsin(np.sqrt(p)))

        def state_function(qb):
            ry(theta, qb)

        if seed is not None:
            np.random.seed(seed)  # best-effort: IQAE has no seed API
        t0 = time.time()
        try:
            # Suppress Qrisp's tqdm progress bar: NiFi's py4j bridge uses stdout,
            # so printing there corrupts the channel ("null response" crash).
            qb = QuantumBool()
            with contextlib.redirect_stdout(io.StringIO()):
                estimate = float(IQAE(
                    [qb], state_function, eps=eps, alpha=alpha,
                    mes_kwargs={"shots": shots},
                ))
        except Exception as exc:
            self.logger.error("QrispAmplitudeEstimation failed: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"ae.error": "estimation failed: {}".format(exc)},
            )

        attrs = {
            "ae.framework":   "qrisp",
            "ae.method":      "iterative",
            "ae.probability": "{:.8f}".format(p),
            "ae.estimate":    "{:.8f}".format(estimate),
            "ae.epsilon":     "{:.6f}".format(eps),
            "ae.alpha":       "{:.6f}".format(alpha),
            "ae.num_qubits":  "1",
            "ae.shots":       str(shots),
            "sim.framework":  "qrisp",
            "sim.shots":      str(shots),
            "sim.top_result": "{:.6f}".format(estimate),
            "sim.top_probability": "1.0000",
            "sim.bit_order":  "q0_left",
            "report.type":    "simulation",
            "perf.elapsed_seconds": "{:.4f}".format(time.time() - t0),
            **({"run.seed": str(seed)} if seed is not None else {}),
        }

        self.logger.warn(
            "QrispAmplitudeEstimation (IQAE): p={} eps={} -> estimate={:.6f}".format(
                p, eps, estimate)
        )

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps({"{:.8f}".format(estimate): 1.0}, indent=2).encode("utf-8"),
            attributes=attrs,
        )
