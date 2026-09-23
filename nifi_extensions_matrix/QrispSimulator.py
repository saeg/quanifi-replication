import time
import contextlib
import io
import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QrispSimulator(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Reads a quantum circuit from the FlowFile content (OpenQASM 2.0, "
            "determined by the 'circuit.format' attribute), adds measurement, runs it "
            "on Qrisp's native statevector simulator, and writes shot counts as JSON. "
            "Accepts qasm2 output from any Qiskit or Cirq circuit processor. "
            "Note: Qrisp's native simulator is an ideal (noiseless) statevector "
            "engine and has no noise-model support — for noisy simulation use "
            "QiskitAerSimulator or CirqSimulator, which expose a Noise Model property."
        )
        tags = ["quantum", "qrisp", "simulation", "measurement"]
        dependencies = ["qrisp==0.9.5", "qiskit>=2.0.0,<2.5"]

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
                "Seed for reproducible sampling. Empty = nondeterministic. Best-effort via the global NumPy seed: Qrisp's sampler exposes no seed API."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.shots, self.random_seed]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qrisp import QuantumCircuit, QrispSimulatorBackend

        try:
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
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QrispSimulator: " + msg)
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"sim.error": msg},
            )

        fmt = flowFile.getAttribute("circuit.format") or "qasm2"
        raw = bytes(flowFile.getContentsAsBytes())

        if fmt != "qasm2":
            msg = (
                f"Unsupported circuit.format '{fmt}'. "
                "QrispSimulator only accepts qasm2. "
                "Set Output Format = qasm2 on the upstream circuit processor."
            )
            # Log loudly: with the failure relationship auto-terminated on the
            # canvas, an unlogged rejection makes the FlowFile vanish silently.
            self.logger.error("QrispSimulator: " + msg)
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"sim.error": msg},
            )

        qc = QuantumCircuit.from_qasm_str(raw.decode("utf-8"))
        # Measure the qubit list, not range(n): Qrisp's measure() treats a
        # range as a single measurement spec and allocates only one clbit,
        # which silently truncates every counts key to the wrong width.
        # Skip if the circuit already carries measurements, or we'd append a
        # duplicate readout register and garble the counts keys.
        if not any(instr.op.name == "measure" for instr in qc.data):
            qc.measure(qc.qubits)

        if seed is not None:
            import numpy as np
            np.random.seed(seed)  # best-effort: Qrisp's sampler has no seed API
        t0 = time.time()
        # Suppress Qrisp's tqdm progress bar so it doesn't appear in NiFi logs.
        with contextlib.redirect_stdout(io.StringIO()):
            counts = QrispSimulatorBackend().run(qc, shots=shots)
        elapsed = time.time() - t0

        # Canonical bit order: qubit 0 = leftmost char. Qrisp returns
        # Qiskit-style little-endian keys (clbit 0 = rightmost), so reverse.
        normalised = {}
        for key, cnt in counts.items():
            k = key.replace(" ", "")[::-1]
            normalised[k] = normalised.get(k, 0) + cnt

        sorted_counts = dict(sorted(normalised.items(), key=lambda x: x[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes={
                "sim.shots":           str(shots),
                "sim.top_result":      top_state,
                "sim.top_probability": f"{top_count / shots:.4f}",
                "sim.framework":       "qrisp",
                "sim.component":       "QrispSimulator",
                "sim.bit_order":       "q0_left",
                "report.type":         "simulation",
                "perf.elapsed_seconds": "{:.4f}".format(elapsed),
                **({"run.seed": str(seed)} if seed is not None else {}),
            },
        )
