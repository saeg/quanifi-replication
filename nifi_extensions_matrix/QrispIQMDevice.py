import json
import time

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QrispIQMDevice(FlowFileTransform):
    """
    Executes a circuit on an IQM quantum computer (IQM Resonance) through
    Qrisp's Backend Interface, or on Qrisp's local statevector simulator when
    Device Instance = 'local' (offline, no token needed).

    Like QiskitRuntimeSampler (IBM) and BraketDevice (AWS), this is a hardware
    gateway for the shared circuit contract: any framework's qasm2 output runs
    on a real IQM QPU through this one processor. Qrisp's Backend Interface
    (https://qrisp.eu/reference/Backend%20Interface/index.html) is asynchronous
    — `run_async()` returns a Job handle immediately — which is what makes both
    execution modes below possible:

      Wait For Results = true   poll Job.status() every Poll Interval Seconds
                                until a terminal state (or Poll Timeout
                                Seconds), then emit counts.
      Wait For Results = false  submit and continue immediately with
                                hw.job_id + hw.status, to be picked up later by
                                IQMJobPoller. QPU queues can be long, and this
                                keeps the canvas free while the job waits.

    Backend resolution prefers IQM's own Qrisp adapter (`iqm.qrisp_iqm`, shipped
    with iqm-client >= 35) and falls back to Qrisp's deprecated stop-gap
    `qrisp.interface.IQMBackend`. Neither is needed for Device Instance =
    'local'.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Runs a circuit (circuit.format = qasm2, from any framework's builder) "
            "on an IQM quantum computer via Qrisp's Backend Interface. Device "
            "Instance 'local' uses Qrisp's offline statevector simulator; "
            "'garnet' / 'emerald' / ... submit to IQM Resonance (API token "
            "required). Supports fire-and-forget submission (Wait For Results = "
            "false -> hw.job_id) for long QPU queues; pair it with IQMJobPoller."
        )
        tags = ["quantum", "qrisp", "iqm", "resonance", "hardware", "qpu"]
        dependencies = ["qrisp==0.9.5", "iqm-client>=35.0.0", "qiskit>=2.0.0,<2.5"]

    #: IQM Resonance is the default server when only a device instance is given.
    DEFAULT_SERVER_URL = "https://resonance.meetiqm.com"

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.device_instance = PropertyDescriptor(
            name="Device Instance",
            description=(
                "'local' for Qrisp's offline statevector simulator, or an IQM "
                "quantum computer identifier such as 'garnet' or 'emerald'. See "
                "the IQM Resonance website for the current device list."
            ),
            required=True,
            default_value="local",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.server_url = PropertyDescriptor(
            name="Server URL",
            description=(
                "IQM server root URL. Empty uses IQM Resonance "
                "(https://resonance.meetiqm.com). Set this only for an on-premise "
                "IQM server."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.token = PropertyDescriptor(
            name="API Token",
            description=(
                "IQM Resonance API token. Required for real devices; ignored for "
                "Device Instance = 'local'. Empty falls back to the IQM_TOKEN / "
                "IQM_TOKENS_FILE environment configuration."
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
        self.wait = PropertyDescriptor(
            name="Wait For Results",
            description=(
                "true: poll the job until it reaches a terminal state (up to Poll "
                "Timeout Seconds) and emit counts. false: submit and continue "
                "immediately with hw.job_id, for IQMJobPoller to collect."
            ),
            required=True,
            default_value="true",
            allowable_values=["true", "false"],
        )
        self.poll_timeout = PropertyDescriptor(
            name="Poll Timeout Seconds",
            description="Maximum seconds to poll when Wait For Results = true.",
            required=True,
            default_value="300",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.poll_interval = PropertyDescriptor(
            name="Poll Interval Seconds",
            description="Seconds between job status queries when waiting.",
            required=True,
            default_value="5",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.random_seed = PropertyDescriptor(
            name="Random Seed",
            description=(
                "Seed for reproducible sampling in 'local' mode (best-effort via "
                "the global NumPy seed: Qrisp's sampler exposes no seed API). "
                "Ignored on real hardware. Empty = nondeterministic."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.device_instance, self.server_url, self.token,
                            self.shots, self.wait, self.poll_timeout,
                            self.poll_interval, self.random_seed]

    def getPropertyDescriptors(self):
        return self.descriptors

    # --- circuit handling ---------------------------------------------------

    @staticmethod
    def _circuit_from_flowfile(flowFile):
        """qasm2 bytes -> a measured Qrisp QuantumCircuit."""
        from qrisp import QuantumCircuit

        fmt = flowFile.getAttribute("circuit.format") or "qasm2"
        if fmt != "qasm2":
            raise ValueError(
                "Unsupported circuit.format '{}'. QrispIQMDevice accepts qasm2 "
                "(Qrisp's importer); set Output Format = qasm2 on the upstream "
                "circuit processor.".format(fmt))

        raw = bytes(flowFile.getContentsAsBytes())
        qc = QuantumCircuit.from_qasm_str(raw.decode("utf-8"))
        # Only measure when the QASM did not already: unlike a simulator, a QPU
        # would otherwise be asked for a second, redundant readout register.
        if not any(instr.op.name == "measure" for instr in qc.data):
            # The qubit list, not range(n) — Qrisp reads a range as a single
            # measurement spec and allocates one clbit, truncating every key.
            qc.measure(qc.qubits)
        return qc

    # --- backend resolution -------------------------------------------------

    def _iqm_backend(self, token, device_instance, server_url):
        """Build a Qrisp Backend for IQM, newest adapter first.

        iqm-client >= 35 ships `iqm.qrisp_iqm.IQMBackend` (the QCCSW release);
        `qrisp.interface.IQMBackend` is the older stop-gap that Qrisp itself
        documents as deprecated and slated for removal. Try them in that order
        so the processor keeps working across both.
        """
        try:
            from iqm.qrisp_iqm import IQMBackend as _NewIQMBackend
        except ImportError:
            _NewIQMBackend = None

        if _NewIQMBackend is not None:
            kwargs = {"device_instance": device_instance}
            if token:
                kwargs["token"] = token
            return _NewIQMBackend(server_url, **kwargs), "iqm.qrisp_iqm"

        try:
            from qrisp.interface import IQMBackend as _LegacyIQMBackend
        except ImportError as exc:
            raise ImportError(
                "No IQM backend available. Install IQM's Qrisp adapter with "
                "`pip install iqm-client` (>= 35), or Qrisp's legacy one with "
                "`pip install qrisp[iqm]`."
            ) from exc

        # The legacy backend rejects server_url and device_instance together,
        # and Resonance is its implicit default, so pass only one of them.
        if server_url and server_url != self.DEFAULT_SERVER_URL:
            return _LegacyIQMBackend(api_token=token, server_url=server_url), "qrisp.interface"
        return _LegacyIQMBackend(api_token=token, device_instance=device_instance), "qrisp.interface"

    # --- polling ------------------------------------------------------------

    @staticmethod
    def _status_value(status):
        """Qrisp JobStatus (a StrEnum) -> its plain string value."""
        return getattr(status, "value", str(status))

    def _await_terminal(self, job, timeout, interval):
        """Poll Job.status() until terminal or the budget runs out.

        Returns (status, timed_out). Qrisp's Job.status() is a live, non-blocking
        query, so this gives the same result as a blocking result() call while
        keeping the wait bounded and observable in the NiFi logs.
        """
        from qrisp.interface import JOB_FINAL_STATES

        deadline = time.time() + timeout
        status = job.status()
        while status not in JOB_FINAL_STATES:
            if time.time() >= deadline:
                return status, True
            time.sleep(min(interval, max(0.0, deadline - time.time())))
            status = job.status()
        return status, False

    # --- counts normalisation -----------------------------------------------

    @staticmethod
    def _normalise(counts):
        """Qrisp/IQM keys are little-endian (clbit 0 rightmost) -> q0-left."""
        merged = {}
        for key, count in counts.items():
            k = str(key).replace(" ", "")[::-1]
            merged[k] = merged.get(k, 0) + int(count)
        return dict(sorted(merged.items(), key=lambda kv: kv[1], reverse=True))

    def transform(self, context, flowFile):
        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        device = (get(self.device_instance) or "").strip()
        server_url = (get(self.server_url) or "").strip() or self.DEFAULT_SERVER_URL
        token = (get(self.token) or "").strip()
        try:
            shots = int(get(self.shots))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QrispIQMDevice: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"hw.error": msg},
            )
        wait = (get(self.wait) or "true").lower() == "true"
        try:
            poll_timeout = int(get(self.poll_timeout))
            poll_interval = int(get(self.poll_interval))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QrispIQMDevice: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"hw.error": msg},
            )
        try:
            seed_raw = (get(self.random_seed) or "").strip()
            seed = int(seed_raw) if seed_raw else None
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QrispIQMDevice: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"hw.error": msg},
            )

        try:
            circuit = self._circuit_from_flowfile(flowFile)
        except Exception as exc:
            self.logger.error("QrispIQMDevice: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"hw.error": str(exc)},
            )

        local = device.lower() == "local"
        base_attrs = {
            "hw.provider": "iqm-resonance",
            "hw.device": device,
            "hw.mode": "local-sim" if local else "cloud",
            "sim.shots": str(shots),
            "sim.framework": "qrisp",
            "sim.bit_order": "q0_left",
            **({} if local else {"hw.server_url": server_url}),
            **({"run.seed": str(seed)} if seed is not None and local else {}),
        }

        # --- submit ---------------------------------------------------------
        t0 = time.time()
        try:
            if local:
                from qrisp.interface import QrispSimulatorBackend
                if seed is not None:
                    import numpy as np
                    np.random.seed(seed)  # best-effort: Qrisp's sampler has no seed API
                backend, adapter = QrispSimulatorBackend(), "qrisp.interface"
            else:
                backend, adapter = self._iqm_backend(token, device, server_url)
            job = backend.run_async(circuit, shots=shots)
        except Exception as exc:
            msg = ("IQM submission to '{}' failed: {}. For real devices check the "
                   "API Token property and the device instance name.".format(device, exc))
            self.logger.error("QrispIQMDevice: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={**base_attrs, "hw.error": msg},
            )

        job_id = str(getattr(job, "job_id", "") or "")
        base_attrs["hw.adapter"] = adapter
        if job_id:
            base_attrs["hw.job_id"] = job_id

        # --- fire-and-forget ------------------------------------------------
        if not wait:
            try:
                status = self._status_value(job.status())
            except Exception:
                status = "queued"
            self.logger.warn(
                "QrispIQMDevice: submitted job {} to {} (not waiting, status={})".format(
                    job_id or "<local>", device, status))
            return FlowFileTransformResult(
                relationship="success",
                contents=json.dumps({"job_id": job_id, "status": status},
                                    indent=2).encode("utf-8"),
                attributes={**base_attrs, "hw.status": status,
                            "hw.submitted_at": "{:.0f}".format(t0)},
            )

        # --- wait by polling ------------------------------------------------
        try:
            status, timed_out = self._await_terminal(job, poll_timeout, poll_interval)
        except Exception as exc:
            msg = "Polling IQM job {} failed: {}".format(job_id or "<local>", exc)
            self.logger.error("QrispIQMDevice: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={**base_attrs, "hw.error": msg},
            )

        status_value = self._status_value(status)
        if timed_out:
            msg = ("IQM job {} still '{}' after {}s. Increase Poll Timeout Seconds, "
                   "or set Wait For Results = false and collect it with "
                   "IQMJobPoller.".format(job_id or "<local>", status_value, poll_timeout))
            self.logger.error("QrispIQMDevice: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={**base_attrs, "hw.status": status_value, "hw.error": msg},
            )

        try:
            counts = job.result().get_counts()
        except Exception as exc:
            msg = "IQM job {} ended '{}': {}".format(job_id or "<local>", status_value, exc)
            self.logger.error("QrispIQMDevice: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={**base_attrs, "hw.status": status_value, "hw.error": msg},
            )
        elapsed = time.time() - t0

        sorted_counts = self._normalise(counts)
        top_state, top_count = next(iter(sorted_counts.items()))
        total = sum(sorted_counts.values()) or shots

        self.logger.warn(
            "QrispIQMDevice ({}, {}): top |{}> p={:.4f}".format(
                device, base_attrs["hw.mode"], top_state, top_count / total))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes={
                **base_attrs,
                "hw.status": status_value,
                "sim.top_result": top_state,
                "sim.top_probability": "{:.4f}".format(top_count / total),
                "report.type": "simulation",
                "perf.elapsed_seconds": "{:.4f}".format(elapsed),
            },
        )
