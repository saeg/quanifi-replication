import time
import json
import re

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


def _qubits_from_qasm(qasm):
    return sum(int(m) for m in re.findall(r"qreg\s+\w+\s*\[\s*(\d+)\s*\]", qasm))


class PennylaneSimulator(FlowFileTransform):
    """Counts-emitting PennyLane simulator. Unlike PennylaneExpectation (the
    QML expectation-value lane), this conforms to the standard shot-count
    simulator contract, making PennyLane's default.qubit a fourth independent
    engine interchangeable with QiskitAerSimulator/CirqSimulator/QrispSimulator
    for cross-framework differential testing."""

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Reads a quantum circuit from the FlowFile content (OpenQASM 2.0, "
            "determined by the 'circuit.format' attribute), samples it on "
            "PennyLane's default.qubit device, and writes shot counts as JSON. "
            "Accepts qasm2 output from any Quanifi circuit builder. "
            "Note: default.qubit is an ideal (noiseless) statevector engine — "
            "for noisy simulation use QiskitAerSimulator or CirqSimulator, "
            "which expose a Noise Model property. For PennyLane's QML-native "
            "outputs (expectation values, analytic probabilities) use "
            "PennylaneExpectation instead."
        )
        tags = ["quantum", "pennylane", "simulation", "measurement"]
        dependencies = ["pennylane>=0.40", "pennylane-qiskit>=0.40",
                        "qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.shots = PropertyDescriptor(
            name="Shots",
            description="Number of times to sample the circuit.",
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.random_seed = PropertyDescriptor(
            name="Random Seed",
            description=(
                "Seed for reproducible sampling. Empty = nondeterministic. Passed to the default.qubit device."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.shots, self.random_seed]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        import pennylane as qml

        shots = int(
            context.getProperty(self.shots)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        seed_raw = (
            context.getProperty(self.random_seed)
            .evaluateAttributeExpressions(flowFile)
            .getValue() or ""
        ).strip()
        seed = int(seed_raw) if seed_raw else None

        fmt = flowFile.getAttribute("circuit.format")
        if fmt != "qasm2":
            self.logger.error(
                "PennylaneSimulator: needs circuit.format=qasm2, got '{}'".format(fmt))
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"sim.error": (
                    "Unsupported circuit.format '{}'. PennylaneSimulator only "
                    "accepts qasm2. Set Output Format = qasm2 on the upstream "
                    "circuit processor.".format(fmt))},
            )

        qasm = bytes(flowFile.getContentsAsBytes()).decode("utf-8")
        n = _qubits_from_qasm(qasm)
        if n <= 0:
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"sim.error": "could not determine qubit count from qasm2"},
            )

        # measurements=[] strips any mid-/end-circuit measurements so we control
        # the readout: counts over all wires (wire 0 = leftmost bit, MSB-first).
        try:
            loaded = qml.from_qasm(qasm, measurements=[])
            dev = qml.device("default.qubit", wires=n, seed=seed)

            @qml.qnode(dev)
            def sample_counts():
                loaded()
                return qml.counts(wires=range(n))

            # qml.counts requires finite shots (no analytic mode), applied
            # per-QNode via set_shots since device-level shots are deprecated.
            t0 = time.time()
            counts = qml.set_shots(sample_counts, shots=shots)()
            elapsed = time.time() - t0
        except Exception as exc:
            self.logger.error("PennylaneSimulator: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"sim.error": (
                    "PennyLane failed to load or execute the circuit: {}"
                    .format(exc))},
            )

        sorted_counts = dict(
            sorted(((k, int(v)) for k, v in counts.items()),
                   key=lambda x: x[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes={
                "sim.shots":           str(shots),
                "sim.top_result":      top_state,
                "sim.top_probability": "{:.4f}".format(top_count / shots),
                "sim.framework":       "pennylane",
                "sim.component":       "PennylaneSimulator",
                "sim.bit_order":       "q0_left",
                "report.type":         "simulation",
                "perf.elapsed_seconds": "{:.4f}".format(elapsed),
                **({"run.seed": str(seed)} if seed is not None else {}),
            },
        )
