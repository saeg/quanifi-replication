import time
import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitRuntimeSampler(FlowFileTransform):
    """
    Executes a circuit on IBM Quantum hardware through Qiskit Runtime's
    SamplerV2, or on a local noisy fake backend (offline) when Backend starts
    with 'fake_'.

    Like BraketDevice, this is a hardware gateway for the shared circuit
    contract: any framework's qasm2/qasm3 output runs on a real IBM QPU
    through this one processor. The circuit is transpiled to the backend's
    ISA before submission, and the same SamplerV2 code path serves both modes
    (qiskit-ibm-runtime executes fake backends locally with their calibrated
    noise models, which is also what makes this processor testable offline).

    Backend values:
      fake_<name>   local noisy model, e.g. fake_manila, fake_sherbrooke
      least_busy    the least busy real operational QPU on the account
      <name>        a specific real backend, e.g. ibm_brisbane

    Real backends need an IBM Quantum token (property, or a saved account).
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Samples a circuit (circuit.format = qasm2 or qasm3, from any "
            "framework's builder) via IBM Qiskit Runtime SamplerV2. Backend "
            "'fake_<name>' runs the calibrated noisy fake backend locally "
            "(offline); 'least_busy' or a backend name submits to real IBM "
            "hardware (token required)."
        )
        tags = ["quantum", "qiskit", "ibm", "hardware", "qpu", "runtime"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-ibm-runtime>=0.20.0",
                        "qiskit-qasm3-import"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.backend = PropertyDescriptor(
            name="Backend",
            description=(
                "'fake_<name>' (local noisy model, offline), 'least_busy' "
                "(real QPU), or a real backend name like 'ibm_brisbane'."
            ),
            required=True,
            default_value="fake_manila",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.token = PropertyDescriptor(
            name="Token",
            description=(
                "IBM Quantum API token for real backends. Empty uses a saved "
                "account (QiskitRuntimeService.save_account) or environment."
            ),
            required=False,
            sensitive=True,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description="Number of shots.",
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.random_seed = PropertyDescriptor(
            name="Random Seed",
            description=(
                "Seed for reproducible runs: seeds the transpiler and, in "
                "fake-backend local mode, the simulator. Ignored on real "
                "hardware. Empty = nondeterministic."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.backend, self.token, self.shots, self.random_seed]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _circuit_from_flowfile(self, flowFile):
        fmt = flowFile.getAttribute("circuit.format")
        raw = bytes(flowFile.getContentsAsBytes())
        if fmt == "qasm2":
            from qiskit import qasm2
            circuit = qasm2.loads(raw.decode("utf-8"),
                                  custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
        elif fmt == "qasm3":
            from qiskit import qasm3
            circuit = qasm3.loads(raw.decode("utf-8"))
        else:
            raise ValueError(
                "Unsupported circuit.format '{}'. QiskitRuntimeSampler accepts "
                "qasm2 or qasm3; set Output Format on the upstream processor.".format(fmt))
        if not circuit.count_ops().get("measure"):
            circuit.measure_all()
        return circuit

    @staticmethod
    def _fake_backend(name):
        """'fake_manila' -> qiskit_ibm_runtime.fake_provider.FakeManilaV2()."""
        import qiskit_ibm_runtime.fake_provider as fp
        camel = "Fake" + "".join(p.capitalize() for p in name.split("_")[1:])
        for candidate in (camel + "V2", camel):
            cls = getattr(fp, candidate, None)
            if cls is not None:
                return cls()
        raise ValueError(
            "unknown fake backend '{}' (no {} in qiskit_ibm_runtime.fake_provider)".format(
                name, camel + "V2"))

    def transform(self, context, flowFile):
        from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
        from qiskit_ibm_runtime import SamplerV2

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        backend_spec = get(self.backend).strip()
        token = (get(self.token) or "").strip()
        shots = int(get(self.shots))
        seed_raw = (get(self.random_seed) or "").strip()
        seed = int(seed_raw) if seed_raw else None

        try:
            circuit = self._circuit_from_flowfile(flowFile)
        except Exception as exc:
            self.logger.error("QiskitRuntimeSampler: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"hw.error": str(exc)},
            )

        try:
            if backend_spec.startswith("fake_"):
                backend = self._fake_backend(backend_spec)
                mode = "local-fake"
            else:
                from qiskit_ibm_runtime import QiskitRuntimeService
                service = QiskitRuntimeService(token=token) if token \
                    else QiskitRuntimeService()
                backend = service.least_busy(operational=True, simulator=False) \
                    if backend_spec == "least_busy" else service.backend(backend_spec)
                mode = "cloud"

            isa_circuit = generate_preset_pass_manager(
                backend=backend, optimization_level=1,
                seed_transpiler=seed).run(circuit)
            sampler_options = {}
            if seed is not None and mode == "local-fake":
                sampler_options = {"simulator": {"seed_simulator": seed}}
            sampler = SamplerV2(mode=backend, options=sampler_options) \
                if sampler_options else SamplerV2(mode=backend)
            t0 = time.time()
            job = sampler.run([isa_circuit], shots=shots)
            pub = job.result()[0]
            elapsed = time.time() - t0
            try:
                counts = pub.join_data().get_counts()
            except Exception:  # older runtime versions: single register field
                counts = next(iter(pub.data.__dict__.values())).get_counts()
        except Exception as exc:
            msg = ("Runtime sampling on '{}' failed: {}. For real backends check "
                   "the Token property or a saved IBM account.".format(backend_spec, exc))
            self.logger.error("QiskitRuntimeSampler: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"hw.error": msg},
            )

        # canonical q0-left keys (runtime counts are little-endian)
        counts = {k.replace(" ", "")[::-1]: int(v) for k, v in counts.items()}
        sorted_counts = dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        self.logger.warn(
            "QiskitRuntimeSampler ({}, {}): top |{}> p={:.4f}".format(
                backend_spec, mode, top_state, top_count / shots))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes={
                "hw.provider": "ibm-runtime",
                "hw.backend": getattr(backend, "name", backend_spec) or backend_spec,
                "hw.mode": mode,
                "sim.shots": str(shots),
                "sim.top_result": top_state,
                "sim.top_probability": "{:.4f}".format(top_count / shots),
                "sim.framework": "qiskit",
                "sim.bit_order": "q0_left",
                "report.type": "simulation",
                "perf.elapsed_seconds": "{:.4f}".format(elapsed),
                **({"run.seed": str(seed)} if seed is not None else {}),
            },
        )
