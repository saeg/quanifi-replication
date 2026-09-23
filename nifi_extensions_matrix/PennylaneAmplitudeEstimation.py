import time
import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class PennylaneAmplitudeEstimation(FlowFileTransform):
    """
    Canonical (QPE-based) Quantum Amplitude Estimation on a Bernoulli problem,
    hand-rolled in PennyLane. PennyLane has no high-level amplitude-estimation
    routine, so this processor composes it from the QuantumPhaseEstimation
    template applied to the Bernoulli Grover operator Q = Ry(2*theta), with the
    target prepared by A = Ry(theta), theta = 2*asin(sqrt(p)).

    The analytic probability distribution over the m estimation wires is read
    (PennyLane's QML-native output, matching the PennylaneExpectation lane's
    probability style), the most likely index y is decoded as
    a = sin^2(pi * y / 2^m) - the same estimate grid as the Qiskit and Cirq
    canonical processors, so all three are directly comparable.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Canonical QPE-based Quantum Amplitude Estimation composed from "
            "PennyLane's QuantumPhaseEstimation template (Bernoulli problem, "
            "A = Ry(2*asin(sqrt(p))), Grover operator Ry(2*theta)). Reads the "
            "analytic probabilities of the estimation wires and estimates "
            "a = sin^2(pi*y/2^m). Emits ae.* attributes and report.type = simulation."
        )
        tags = ["quantum", "pennylane", "amplitude-estimation", "qae", "estimation"]
        dependencies = ["pennylane>=0.40"]

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
                "Size m of the QPE estimation register. "
                "The estimate lies on the grid sin^2(pi*y/2^m)."
            ),
            required=True,
            default_value="3",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description=(
                "Used only to scale the analytic probabilities into pseudo-counts "
                "for the report histogram; the estimate itself is analytic "
                "(deterministic)."
            ),
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.probability, self.eval_qubits, self.shots]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        import numpy as np
        import pennylane as qml

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
            self.logger.error("PennylaneAmplitudeEstimation: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"ae.error": msg},
            )

        try:
            m     = int(get(self.eval_qubits))
            shots = int(get(self.shots))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("PennylaneAmplitudeEstimation: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"ae.error": msg},
            )

        theta = 2.0 * float(np.arcsin(np.sqrt(p)))
        est_wires = list(range(1, m + 1))  # wire 0 is the Bernoulli target

        dev = qml.device("default.qubit", wires=m + 1)

        @qml.qnode(dev)
        def qpe():
            qml.RY(theta, wires=0)  # A|0>
            qml.QuantumPhaseEstimation(
                qml.RY(2.0 * theta, wires=0), estimation_wires=est_wires)
            return qml.probs(wires=est_wires)

        t0 = time.time()
        try:
            probs = np.asarray(qpe(), dtype=float)
        except Exception as exc:
            self.logger.error("PennylaneAmplitudeEstimation failed: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"ae.error": "estimation failed: {}".format(exc)},
            )

        y = int(np.argmax(probs))
        estimate = float(np.sin(np.pi * y / (2 ** m)) ** 2)

        # Pseudo-counts over the estimation register (first estimation wire is
        # the MSB of the probs index, emitted leftmost: q0-left readout order,
        # matching CirqAmplitudeEstimation).
        counts = {
            format(idx, "0{}b".format(m)): int(round(float(pr) * shots))
            for idx, pr in enumerate(probs) if pr > 1e-12
        }
        sorted_counts = dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))
        top_state = format(y, "0{}b".format(m))

        try:
            specs = qml.specs(qpe)()
            resources = specs["resources"]
            depth = str(resources.depth)
            gate_count = str(resources.num_gates)
        except Exception:
            depth, gate_count = "", ""

        attrs = {
            "ae.framework":   "pennylane",
            "ae.method":      "canonical",
            "ae.probability": "{:.8f}".format(p),
            "ae.estimate":    "{:.8f}".format(estimate),
            "ae.eval_qubits": str(m),
            "ae.num_qubits":  str(m + 1),
            "ae.shots":       str(shots),
            "circuit.num_qubits": str(m + 1),
            "sim.framework":  "pennylane",
            "sim.shots":      str(shots),
            "sim.top_result": top_state,
            "sim.top_probability": "{:.4f}".format(float(probs[y])),
            "sim.bit_order":  "q0_left",
            "report.type":    "simulation",
            "perf.elapsed_seconds": "{:.4f}".format(time.time() - t0),
        }
        if depth:
            attrs["circuit.depth"] = depth
        if gate_count:
            attrs["circuit.gate_count"] = gate_count

        self.logger.warn(
            "PennylaneAmplitudeEstimation: p={} m={} -> y={} estimate={:.6f}".format(
                p, m, y, estimate)
        )

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes=attrs,
        )
