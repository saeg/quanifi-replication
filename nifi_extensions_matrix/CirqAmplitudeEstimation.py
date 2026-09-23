import time
import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class CirqAmplitudeEstimation(FlowFileTransform):
    """
    Canonical (QPE-based) Quantum Amplitude Estimation on a Bernoulli problem,
    hand-rolled in Cirq. Cirq has no high-level amplitude-estimation routine,
    so this processor implements it from primitives, mirroring the circuit
    conventions of CirqPhaseEstimation (phase register = LineQubit 0..m-1,
    controlled powers with qubit k controlling Q^(2^k), inverse QFT with
    without_reverse=True so qubit 0 reads out as the MSB).

    Bernoulli problem: A = Ry(theta) with theta = 2*asin(sqrt(p)) prepares
    sqrt(1-p)|0> + sqrt(p)|1> on the target qubit; its Grover operator is
    exactly Q = Ry(2*theta), whose powers are pure rotations
    Q^(2^k) = Ry(2^k * 2*theta) - no matrix exponentiation needed.

    The measured phase-register integer y gives the estimate
    a = sin^2(pi * y / 2^m), on the same grid as Qiskit's canonical
    AmplitudeEstimation, so the two processors are directly comparable.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Canonical QPE-based Quantum Amplitude Estimation implemented from "
            "primitives in Cirq (Bernoulli problem, A = Ry(2*asin(sqrt(p))), "
            "Grover operator Ry(2*theta)). Measures the m-qubit phase register and "
            "estimates a = sin^2(pi*y/2^m). Emits ae.* attributes and "
            "report.type = simulation."
        )
        tags = ["quantum", "cirq", "amplitude-estimation", "qae", "estimation"]
        dependencies = ["cirq>=1.0.0", "ply", "numpy"]

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
                "Size m of the QPE evaluation (phase) register. "
                "The estimate lies on the grid sin^2(pi*y/2^m)."
            ),
            required=True,
            default_value="3",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description="Number of samples of the phase register.",
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.random_seed = PropertyDescriptor(
            name="Random Seed",
            description=(
                "Seed for reproducible sampling. Empty = nondeterministic. Passed to cirq.Simulator(seed=...)."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.probability, self.eval_qubits, self.shots, self.random_seed]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        import numpy as np
        import cirq
        from cirq.contrib.svg import circuit_to_svg

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
            self.logger.error("CirqAmplitudeEstimation: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"ae.error": msg},
            )

        try:
            m     = int(get(self.eval_qubits))
            shots = int(get(self.shots))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("CirqAmplitudeEstimation: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"ae.error": msg},
            )
        try:
            seed_raw = (get(self.random_seed) or "").strip()
            seed = int(seed_raw) if seed_raw else None
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("CirqAmplitudeEstimation: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"ae.error": msg},
            )


        theta = 2.0 * float(np.arcsin(np.sqrt(p)))

        # Phase register first (LineQubit 0..m-1), target after — the same
        # ordering CirqPhaseEstimation uses.
        phase_qubits = cirq.LineQubit.range(m)
        target = cirq.LineQubit(m)

        circuit = cirq.Circuit()
        circuit.append(cirq.ry(theta).on(target))            # A|0>
        circuit.append(cirq.H.on_each(*phase_qubits))
        for k, ctrl in enumerate(phase_qubits):
            # Q^(2^k) = Ry(2^k * 2*theta): rotations compose additively.
            circuit.append(
                cirq.ry((2 ** k) * 2.0 * theta).on(target).controlled_by(ctrl))
        circuit.append(cirq.qft(*phase_qubits, without_reverse=True) ** -1)
        circuit.append(cirq.measure(*phase_qubits, key="phase"))

        t0 = time.time()
        result = cirq.Simulator(seed=seed).run(circuit, repetitions=shots)
        elapsed = time.time() - t0
        counts = {}
        for row in result.measurements["phase"]:
            # bits arrive in qubit order q0..q(m-1); q0 is the readout MSB
            # (without_reverse convention), so the q0-left string reads MSB->LSB.
            bits = "".join(str(int(b)) for b in row)
            counts[bits] = counts.get(bits, 0) + 1
        sorted_counts = dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        y = int(top_state, 2)
        estimate = float(np.sin(np.pi * y / (2 ** m)) ** 2)

        # Circuit metrics on the primitive-gate expansion.
        expanded = cirq.Circuit(cirq.decompose(circuit))
        all_ops = list(expanded.all_operations())
        try:
            svg = circuit_to_svg(circuit)
        except Exception as exc:
            self.logger.warn("circuit_to_svg failed: {}".format(exc))
            svg = ""

        attrs = {
            "ae.framework":     "cirq",
            "ae.method":        "canonical",
            "ae.probability":   "{:.8f}".format(p),
            "ae.estimate":      "{:.8f}".format(estimate),
            "ae.eval_qubits":   str(m),
            "ae.num_qubits":    str(m + 1),
            "ae.shots":         str(shots),
            "circuit.num_qubits":     str(m + 1),
            "circuit.depth":          str(len(expanded)),
            "circuit.gate_count":     str(len(all_ops)),
            "circuit.nonlocal_gates": str(sum(1 for op in all_ops if len(op.qubits) >= 2)),
            "circuit.t_count":        "0",
            "circuit.diagram":        str(circuit),
            "circuit.svg":            svg,
            "sim.framework":     "cirq",
            "sim.shots":         str(shots),
            "sim.top_result":    top_state,
            "sim.top_probability": "{:.4f}".format(top_count / shots),
            "sim.bit_order":     "q0_left",
            "report.type":       "simulation",
            "perf.elapsed_seconds": "{:.4f}".format(elapsed),
            **({"run.seed": str(seed)} if seed is not None else {}),
        }

        self.logger.warn(
            "CirqAmplitudeEstimation: p={} m={} -> y={} estimate={:.6f}".format(
                p, m, y, estimate)
        )

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes=attrs,
        )
