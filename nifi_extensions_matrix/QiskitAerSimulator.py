import time
import io
import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitAerSimulator(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.2.0"
        description = (
            "Reads a quantum circuit from the FlowFile content (QPY or OpenQASM 3, "
            "determined by the 'circuit.format' attribute), adds measurement, runs it "
            "on Qiskit's AerSimulator, and writes the shot counts as JSON. Optionally "
            "applies a gate/readout noise model so the simulation is non-ideal."
        )
        tags = ["quantum", "qiskit", "aer", "simulation", "measurement", "noise"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-aer>=0.13.0", "qiskit-qasm3-import",
                        "qiskit-ibm-runtime>=0.20.0"]

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
                "Gate noise to apply during simulation. 'none' is an ideal "
                "simulator. 'depolarizing' applies depolarizing_error using the "
                "1-/2-qubit error rates. 'thermal_relaxation' applies "
                "thermal_relaxation_error using T1, T2 and gate time. "
                "'from_backend' derives a hardware-realistic model from a named "
                "fake backend's calibration data (see Backend Name)."
            ),
            required=True,
            default_value="none",
            allowable_values=["none", "depolarizing", "thermal_relaxation", "from_backend"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.noise_model_el = PropertyDescriptor(
            name="Noise Model (EL)",
            description=(
                "Optional Expression Language override for Noise Model, for data-driven "
                "/ mutation runs where the value comes from a FlowFile attribute (the "
                "Noise Model dropdown above cannot accept EL). When non-empty it WINS "
                "over the dropdown; leave blank to use the dropdown. Same allowed values: "
                "none, depolarizing, thermal_relaxation, from_backend. "
                "Example: ${sim.noise_model:replaceEmpty('none')}."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.backend_name = PropertyDescriptor(
            name="Backend Name",
            description=(
                "Name of the offline fake backend whose calibration drives the "
                "noise model when Noise Model = from_backend (e.g. 'fake_manila', "
                "'fake_sherbrooke'). Uses qiskit-ibm-runtime's fake_provider, so no "
                "IBM Quantum credentials or network access are required."
            ),
            required=False,
            default_value="fake_manila",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.error_1q = PropertyDescriptor(
            name="1-Qubit Error Rate",
            description="Depolarizing probability applied to single-qubit gates "
                        "(used when Noise Model = depolarizing).",
            required=False,
            default_value="0.001",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.error_2q = PropertyDescriptor(
            name="2-Qubit Error Rate",
            description="Depolarizing probability applied to two-qubit gates "
                        "(used when Noise Model = depolarizing).",
            required=False,
            default_value="0.01",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.t1_us = PropertyDescriptor(
            name="T1 (us)",
            description="Relaxation time T1 in microseconds "
                        "(used when Noise Model = thermal_relaxation).",
            required=False,
            default_value="50",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.t2_us = PropertyDescriptor(
            name="T2 (us)",
            description="Dephasing time T2 in microseconds; must be <= 2*T1 "
                        "(used when Noise Model = thermal_relaxation).",
            required=False,
            default_value="70",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.gate_time_ns = PropertyDescriptor(
            name="Gate Time (ns)",
            description="Gate duration in nanoseconds used to scale thermal "
                        "relaxation (used when Noise Model = thermal_relaxation).",
            required=False,
            default_value="100",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.readout_error = PropertyDescriptor(
            name="Readout Error Rate",
            description="Symmetric measurement bit-flip probability, applied on top "
                        "of any gate noise. 0 disables readout error.",
            required=False,
            default_value="0.0",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.random_seed = PropertyDescriptor(
            name="Random Seed",
            description=(
                "Seed for reproducible sampling. Empty = nondeterministic. Passed to Aer as seed_simulator."
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
            self.error_1q,
            self.error_2q,
            self.t1_us,
            self.t2_us,
            self.gate_time_ns,
            self.backend_name,
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

    def _build_noise_model(self, context, flowFile):
        """Return (noise_model_or_None, attributes) from the configured properties.

        The 1-/2-qubit errors are attached to the standard Aer basis gates so that
        ``transpile(circuit, backend)`` lands on gates the model actually covers.
        """
        from qiskit_aer.noise import (
            NoiseModel,
            depolarizing_error,
            thermal_relaxation_error,
            ReadoutError,
        )

        kind = self._resolve_noise_kind(context, flowFile)
        readout_p = float(self._prop(context, self.readout_error, flowFile) or 0.0)

        if kind == "none" and readout_p <= 0.0:
            return None, {"sim.noise_model": "none"}

        nm = NoiseModel()
        gates_1q = ["id", "rz", "sx", "x", "h", "u1", "u2", "u3"]
        gates_2q = ["cx", "cz", "ecr"]
        params = {}

        if kind == "depolarizing":
            p1 = float(self._prop(context, self.error_1q, flowFile) or 0.0)
            p2 = float(self._prop(context, self.error_2q, flowFile) or 0.0)
            if p1 > 0.0:
                nm.add_all_qubit_quantum_error(depolarizing_error(p1, 1), gates_1q)
            if p2 > 0.0:
                nm.add_all_qubit_quantum_error(depolarizing_error(p2, 2), gates_2q)
            params = {"error_1q": p1, "error_2q": p2}

        elif kind == "thermal_relaxation":
            # Convert T1/T2 to ns to match the gate time unit.
            t1 = float(self._prop(context, self.t1_us, flowFile) or 0.0) * 1000.0
            t2 = float(self._prop(context, self.t2_us, flowFile) or 0.0) * 1000.0
            t = float(self._prop(context, self.gate_time_ns, flowFile) or 0.0)
            err_1q = thermal_relaxation_error(t1, t2, t)
            err_2q = err_1q.tensor(thermal_relaxation_error(t1, t2, t))
            nm.add_all_qubit_quantum_error(err_1q, gates_1q)
            nm.add_all_qubit_quantum_error(err_2q, gates_2q)
            params = {"t1_us": t1 / 1000.0, "t2_us": t2 / 1000.0, "gate_time_ns": t}

        elif kind == "from_backend":
            from qiskit_ibm_runtime.fake_provider import FakeProviderForBackendV2

            name = (self._prop(context, self.backend_name, flowFile) or "").strip()
            backend = FakeProviderForBackendV2().backend(name)
            nm = NoiseModel.from_backend(backend)
            params = {"backend": name}

        if readout_p > 0.0:
            ro = ReadoutError([[1 - readout_p, readout_p], [readout_p, 1 - readout_p]])
            nm.add_all_qubit_readout_error(ro)
            params["readout_error"] = readout_p

        attrs = {
            "sim.noise_model": kind if kind != "none" else "readout_only",
            "sim.noise_params": json.dumps(params),
        }
        return nm, attrs

    def transform(self, context, flowFile):
        from qiskit import transpile
        from qiskit_aer import AerSimulator as Aer

        shots = int(self._prop(context, self.shots, flowFile))
        seed_raw = (self._prop(context, self.random_seed, flowFile) or "").strip()
        seed = int(seed_raw) if seed_raw else None

        fmt = flowFile.getAttribute("circuit.format") or "qasm3"
        raw = bytes(flowFile.getContentsAsBytes())

        # Guard: if content looks like JSON counts (no circuit.format set by upstream,
        # e.g. QiskitGroverSearch already ran simulation), pass results through unchanged.
        if not flowFile.getAttribute("circuit.format"):
            decoded = raw.decode("utf-8", errors="ignore").lstrip()
            if decoded.startswith("{") or decoded.startswith("["):
                return FlowFileTransformResult(
                    relationship="success",
                    contents=raw,
                    attributes={"sim.passthrough": "true"},
                )

        # Deserialize the circuit based on the format set by the upstream processor.
        if fmt == "qpy":
            from qiskit import qpy
            circuit = qpy.load(io.BytesIO(raw))[0]
        elif fmt == "qasm2":
            from qiskit import qasm2
            circuit = qasm2.loads(raw.decode("utf-8"),
                                  custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
        else:
            from qiskit import qasm3
            circuit = qasm3.loads(raw.decode("utf-8"))

        circuit.measure_all()

        nm, noise_attrs = self._build_noise_model(context, flowFile)
        backend = Aer(noise_model=nm) if nm is not None else Aer()
        run_kwargs = {"shots": shots}
        if seed is not None:
            run_kwargs["seed_simulator"] = seed
        t0 = time.time()
        result = backend.run(transpile(circuit, backend), **run_kwargs).result()
        elapsed = time.time() - t0
        counts = result.get_counts()

        # Canonical bit order: qubit 0 = leftmost char. Aer keys are
        # little-endian (clbit 0 = rightmost, register groups space-separated),
        # so strip separators and reverse.
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
                "sim.shots": str(shots),
                "sim.top_result": top_state,
                "sim.top_probability": f"{top_count / shots:.4f}",
                "sim.framework": "qiskit",
                "sim.component": "QiskitAerSimulator",
                "sim.bit_order": "q0_left",
                "report.type": "simulation",
                "perf.elapsed_seconds": "{:.4f}".format(elapsed),
                **({"run.seed": str(seed)} if seed is not None else {}),
                **noise_attrs,
            },
        )
