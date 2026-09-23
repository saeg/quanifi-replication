import time
import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class CirqSimulator(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.2.0"
        description = (
            "Reads a quantum circuit from the FlowFile content (Cirq JSON or OpenQASM 2.0, "
            "determined by the 'circuit.format' attribute), adds measurement, runs it on "
            "Cirq's wave-function simulator, and writes shot counts as JSON. "
            "Accepts Cirq JSON from CirqGroverCircuit or OpenQASM 2.0 from any circuit processor "
            "(including Qiskit processors that output qasm2 format). Optionally applies a "
            "noise channel (depolarizing / bit-flip / amplitude-damping) and a readout error."
        )
        tags = ["quantum", "cirq", "simulation", "measurement", "noise"]
        dependencies = ["cirq>=1.0.0", "ply"]

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
        self.noise_model = PropertyDescriptor(
            name="Noise Model",
            description=(
                "Noise channel applied after every moment via Cirq's noise model. "
                "'none' is an ideal simulator. 'depolarizing' uses cirq.depolarize, "
                "'bit_flip' uses cirq.bit_flip (both sized by Error Probability), "
                "'amplitude_damp' uses cirq.amplitude_damp (sized by Damping Gamma, "
                "the T1-relaxation analog)."
            ),
            required=True,
            default_value="none",
            allowable_values=["none", "depolarizing", "bit_flip", "amplitude_damp"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.noise_model_el = PropertyDescriptor(
            name="Noise Model (EL)",
            description=(
                "Optional Expression Language override for Noise Model, for data-driven "
                "/ mutation runs where the value comes from a FlowFile attribute (the "
                "Noise Model dropdown above cannot accept EL). When non-empty it WINS "
                "over the dropdown; leave blank to use the dropdown. Same allowed values: "
                "none, depolarizing, bit_flip, amplitude_damp. "
                "Example: ${sim.noise_model:replaceEmpty('none')}."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.error_probability = PropertyDescriptor(
            name="Error Probability",
            description="Channel probability for depolarizing/bit_flip noise (0–1).",
            required=False,
            default_value="0.01",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.damping_gamma = PropertyDescriptor(
            name="Damping Gamma",
            description="Amplitude-damping probability gamma (0–1), used when "
                        "Noise Model = amplitude_damp.",
            required=False,
            default_value="0.05",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.readout_error = PropertyDescriptor(
            name="Readout Error Rate",
            description="Symmetric measurement bit-flip probability, applied via a "
                        "cirq.bit_flip channel on each qubit just before measurement. "
                        "0 disables readout error.",
            required=False,
            default_value="0.0",
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
        self.descriptors = [
            self.shots,
            self.random_seed,
            self.noise_model,
            self.noise_model_el,
            self.error_probability,
            self.damping_gamma,
            self.readout_error,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _prop(self, context, descriptor, flowFile):
        return (
            context.getProperty(descriptor)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

    def _resolve_noise_kind(self, context, flowFile):
        """Effective noise model: the EL override if set, else the dropdown."""
        override = (self._prop(context, self.noise_model_el, flowFile) or "").strip()
        if override:
            return override
        return (self._prop(context, self.noise_model, flowFile) or "none").strip()

    def _build_noise(self, cirq, context, flowFile):
        """Return (noise_channel_or_None, readout_p, attributes) from the config.

        The noise channel is a cirq.NOISE_MODEL_LIKE (a single-qubit gate becomes a
        ConstantQubitNoiseModel applied after every moment). Readout error is handled
        separately by the caller, which inserts a bit_flip channel before measurement.
        """
        kind = self._resolve_noise_kind(context, flowFile)
        readout_p = float(self._prop(context, self.readout_error, flowFile) or 0.0)

        channel = None
        params = {}
        if kind == "depolarizing":
            p = float(self._prop(context, self.error_probability, flowFile) or 0.0)
            channel = cirq.depolarize(p) if p > 0.0 else None
            params = {"probability": p}
        elif kind == "bit_flip":
            p = float(self._prop(context, self.error_probability, flowFile) or 0.0)
            channel = cirq.bit_flip(p) if p > 0.0 else None
            params = {"probability": p}
        elif kind == "amplitude_damp":
            g = float(self._prop(context, self.damping_gamma, flowFile) or 0.0)
            channel = cirq.amplitude_damp(g) if g > 0.0 else None
            params = {"gamma": g}

        if readout_p > 0.0:
            params["readout_error"] = readout_p

        if channel is None and readout_p <= 0.0:
            return None, 0.0, {"sim.noise_model": "none"}

        attrs = {
            "sim.noise_model": kind if channel is not None else "readout_only",
            "sim.noise_params": json.dumps(params),
        }
        return channel, readout_p, attrs

    @staticmethod
    def _full_register(cirq, circuit, fmt, raw, flowFile):
        """All qubits to measure, including idle ones the parsers drop.

        A Cirq circuit only contains qubits that appear in operations, so a
        qasm2 ``qreg q[3]`` with an untouched ``q[2]`` would silently shrink
        the counts keys from '010' to '01'. Recover the declared width from
        the qreg declarations (qasm2) or from the upstream circuit.num_qubits
        attribute (cirq_json) and pad with the missing qubits.
        """
        import re
        qubits = set(circuit.all_qubits())
        if fmt == "qasm2":
            # cirq's qasm importer names qubits "<reg>_<i>"
            for name, size in re.findall(r"qreg\s+([A-Za-z_]\w*)\s*\[\s*(\d+)\s*\]",
                                         raw.decode("utf-8", errors="ignore")):
                qubits |= {cirq.NamedQubit(f"{name}_{i}") for i in range(int(size))}
        else:
            try:
                declared = int(flowFile.getAttribute("circuit.num_qubits") or 0)
            except (TypeError, ValueError):
                declared = 0
            if declared > len(qubits) and all(isinstance(q, cirq.LineQubit) for q in qubits):
                qubits |= {cirq.LineQubit(i) for i in range(declared)}
        return sorted(qubits)

    def transform(self, context, flowFile):
        import cirq

        try:
            shots = int(self._prop(context, self.shots, flowFile))
            seed_raw = (self._prop(context, self.random_seed, flowFile) or "").strip()
            seed = int(seed_raw) if seed_raw else None
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("CirqSimulator: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"sim.error": msg},
            )

        fmt = flowFile.getAttribute("circuit.format") or "cirq_json"
        raw = bytes(flowFile.getContentsAsBytes())

        if fmt == "cirq_json":
            circuit = cirq.read_json(json_text=raw.decode("utf-8"))
        elif fmt == "qasm2":
            from cirq.contrib.qasm_import import circuit_from_qasm
            circuit = circuit_from_qasm(raw.decode("utf-8"))
        else:
            msg = (
                f"Unsupported circuit.format '{fmt}'. "
                "CirqSimulator accepts cirq_json or qasm2. "
                "For Qiskit circuits, set the upstream processor's Output Format to qasm2."
            )
            # Log loudly: with the failure relationship auto-terminated on the
            # canvas, an unlogged rejection makes the FlowFile vanish silently.
            self.logger.error("CirqSimulator: " + msg)
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"sim.error": msg},
            )

        qubits = self._full_register(cirq, circuit, fmt, raw, flowFile)

        channel, readout_p, noise_attrs = self._build_noise(cirq, context, flowFile)

        measured = circuit.copy()
        if readout_p > 0.0:
            # Model readout error as a bit-flip channel on each qubit just before
            # measurement (a distinct moment so gate noise doesn't subsume it).
            measured.append(cirq.bit_flip(readout_p).on_each(*qubits))
        measured.append(cirq.measure(*qubits, key='result'))

        sim = cirq.Simulator(noise=channel, seed=seed) if channel is not None \
            else cirq.Simulator(seed=seed)
        t0 = time.time()
        result = sim.run(measured, repetitions=shots)
        elapsed = time.time() - t0

        def bits_to_str(bits):
            return ''.join(str(int(b)) for b in bits)

        histogram = result.histogram(key='result', fold_func=bits_to_str)
        sorted_counts = dict(sorted(histogram.items(), key=lambda x: x[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes={
                "sim.shots":        str(shots),
                "sim.top_result":   top_state,
                "sim.top_probability": f"{top_count / shots:.4f}",
                "sim.framework":    "cirq",
                "sim.component":    "CirqSimulator",
                "sim.bit_order":    "q0_left",
                "report.type":      "simulation",
                "perf.elapsed_seconds": "{:.4f}".format(elapsed),
                **({"run.seed": str(seed)} if seed is not None else {}),
                **noise_attrs,
            },
        )
