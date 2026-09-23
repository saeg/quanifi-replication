import json
import os
import sys
import time

# NiFi loads each processor in its own module context without the extensions
# directory on sys.path, so the sibling-module imports below fail without this.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder

import batch_prep
from batch_prep import persist_manifest
from QuantumSuccessProbabilityOracle import required_shots

#: Submit modes. `preflight` does everything except contact the run endpoint.
#: It is the default because a processor started by accident, or a flow restored
#: from a backup, must not be able to spend QPU time.
PREFLIGHT, ARMED = "preflight", "armed"


def _parse_layout(raw):
    """'122,144,124' -> [122, 144, 124]; blank/None -> None (search)."""
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if not parts:
        return None
    if not all(p.isdigit() for p in parts):
        raise ValueError("Fixed Layout must be comma-separated integers, got %r" % (raw,))
    return [int(p) for p in parts]


def _parse_qubits(raw):
    """A blank or comma-separated set of physical qubits."""
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if not all(p.isdigit() for p in parts):
        raise ValueError("Excluded Physical Qubits must be comma-separated integers")
    return sorted({int(p) for p in parts})


def _discard_slot(path):
    """Remove a completed/failed batch slot so a retry starts cleanly."""
    try:
        os.remove(path)
    except OSError:
        pass


def interleave(entries):
    buckets = {"null": [], "control": [], "mutant": []}
    for entry in entries:
        key = entry["kind"] if entry["kind"] in ("null", "control") else "mutant"
        buckets[key].append(entry)
    out = []
    while any(buckets.values()):
        for key in ("null", "control", "mutant"):
            if buckets[key]:
                out.append(buckets[key].pop(0))
    return out


class QuantumIBMBatchSubmitter(FlowFileTransform):
    """Buffer a complete N-version batch and submit one asynchronous IBM Sampler job."""

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Accumulates one calibrated hardware batch, searches and pins one "
            "layout, checks queue and estimated QPU usage caps, and submits one "
            "asynchronous IBM Runtime Sampler job."
        )
        tags = ["quantum", "ibm", "hardware", "batch", "sampler", "submit"]
        dependencies = ["qiskit>=2.2,<2.5", "qiskit-ibm-runtime>=0.40,<1"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        def prop(name, description, default, validator=None, el=False, sensitive=False):
            kwargs = {"name": name, "description": description, "required": True,
                      "default_value": default, "sensitive": sensitive}
            if validator is not None:
                kwargs["validators"] = [validator]
            if el:
                kwargs["expression_language_scope"] = ExpressionLanguageScope.FLOWFILE_ATTRIBUTES
            return PropertyDescriptor(**kwargs)

        self.batch_label = prop("Batch Label", "Device-qualified slot key.",
                                "${test.run_id}-${target.device}",
                                StandardValidators.NON_EMPTY_VALIDATOR, True)
        self.expected_circuits = prop("Expected Circuits", "Circuits in one job.", "34",
                                      StandardValidators.POSITIVE_INTEGER_VALIDATOR, True)
        self.device = prop("Device", "IBM backend name.", "${target.device}",
                           StandardValidators.NON_EMPTY_VALIDATOR, True)
        self.token = prop("API Token", "IBM Quantum token; use a sensitive parameter.",
                          "", None, True, True)
        self.instance = prop("Instance", "Optional IBM service instance/plan.", "test", None, True)
        self.shots = prop("Shots", "Shots per circuit.", "662",
                          StandardValidators.POSITIVE_INTEGER_VALIDATOR)
        self.circuit_kind = prop("Circuit Kind", "null, control, or mutant operator.",
                                 "${test.kind}", None, True)
        self.circuit_label = prop("Circuit Label", "Label used by the oracle.",
                                  "${test.framework}", None, True)
        self.layout_seeds = prop("Layout Search Seeds", "Transpiler layouts to search.", "8",
                                 StandardValidators.POSITIVE_INTEGER_VALIDATOR)
        self.max_pending = prop("Maximum Pending Jobs", "Refuse submission above this queue.", "10",
                                StandardValidators.NON_NEGATIVE_INTEGER_VALIDATOR)
        self.max_usage = prop(
            "Maximum Estimated Usage Seconds",
            "Hard cap on estimated circuit execution time for this job. The "
            "estimate is gate duration times shots; it omits reset, readout and "
            "queue overhead, so it is a FLOOR on what the provider will bill, "
            "not a ceiling.", "120",
            StandardValidators.POSITIVE_INTEGER_VALIDATOR)
        self.max_circuits = prop(
            "Maximum Circuits",
            "Refuse to submit a batch larger than this.", "256",
            StandardValidators.POSITIVE_INTEGER_VALIDATOR)
        self.max_shots = prop(
            "Maximum Shots Per Circuit",
            "Refuse to submit if Shots exceeds this.", "4096",
            StandardValidators.POSITIVE_INTEGER_VALIDATOR)
        self.submit_mode = prop(
            "Submit Mode",
            "preflight (default) transpiles, searches a layout, estimates usage "
            "and stops WITHOUT contacting the provider. armed submits. The safe "
            "value is the default so that spending is always a deliberate act.",
            PREFLIGHT, StandardValidators.NON_EMPTY_VALIDATOR, True)
        self.attribute_prefix = prop(
            "Attribute Prefix",
            "FlowFile attributes with this prefix are captured onto each batch "
            "entry and travel through to the polled result, so a returned "
            "circuit can be scored against its own ground truth. Blank captures "
            "nothing, which reproduces the pre-2026-08 manifest exactly.",
            "arithmetic.", None, True)
        self.control_success = prop(
            "Control Success Probability",
            "Expected success probability of a CORRECT circuit on this device, "
            "used by preflight to report the shots per arm the design needs. "
            "The default is a measured value (0.43 for a 2-bit adder on a "
            "calibrated Falcon-generation noise model), not an optimistic one.",
            "0.43", None, True)
        self.min_difference = prop(
            "Minimum Detectable Difference",
            "The smallest drop in success probability the run must be able to "
            "detect. Cost scales with the INVERSE SQUARE of this, so halving it "
            "quadruples the shots.", "0.10", None, True)
        self.state_dir = prop("State Directory", "Partial-batch storage.",
                              "reports/tmp/quanifi_ibm_batch_state",
                              StandardValidators.NON_EMPTY_VALIDATOR)
        self.manifest_dir = prop(
            "Manifest Directory",
            "Where a submitted job's manifest is written, as <job_id>.json. It "
            "is the only record of which circuit produced which result, so "
            "without it a job can only be re-read by solving the ordering back "
            "out of the counts. The archived copy also carries the submitted "
            "OpenQASM, which the FlowFile copy does not.",
            "reports/manifests",
            StandardValidators.NON_EMPTY_VALIDATOR)
        self.fixed_layout = prop(
            "Fixed Layout",
            "Comma-separated physical qubits to pin this batch to, e.g. "
            "'122,144,124,142,141,123,143,136'. Blank (the default) searches "
            "Layout Seeds transpiler seeds and keeps the best, which is the "
            "historical behaviour. Pin it when several jobs must be compared to "
            "each other: two jobs 12 minutes apart on independently searched "
            "layouts moved one implementation's control success from 0.78 to "
            "0.16, so an unpinned layout makes job-to-job variation "
            "uninterpretable. Must name exactly as many qubits as the padded "
            "batch width. Expression Language is enabled so a lane can pin the "
            "layout its vendor qualified, read from disk rather than pasted in "
            "by hand -- IQM's equivalent property has always allowed this.",
            "",
            None, True,
        )
        self.excluded_physical_qubits = prop(
            "Excluded Physical Qubits",
            "Physical qubits the final transpiled circuits must not touch. Blank "
            "by default. During a free layout search these qubits also exclude "
            "candidates. A fixed layout is still checked after routing, so a "
            "later campaign job cannot silently reintroduce an excluded qubit.",
            "", None, True,
        )
        self.slot_ttl = prop("Slot TTL Seconds", "Discard stale partial batches.", "3600",
                             StandardValidators.NON_NEGATIVE_INTEGER_VALIDATOR)
        self.descriptors = [self.batch_label, self.expected_circuits, self.device,
                            self.token, self.instance, self.shots, self.circuit_kind,
                            self.fixed_layout, self.excluded_physical_qubits,
                            self.circuit_label, self.layout_seeds, self.max_pending,
                            self.max_usage, self.max_circuits, self.max_shots,
                            self.submit_mode, self.attribute_prefix,
                            self.control_success, self.min_difference,
                            self.state_dir, self.slot_ttl, self.manifest_dir]

    def getPropertyDescriptors(self):
        return self.descriptors

    def getRelationships(self):
        return [Relationship(name="submitted", description="IBM job submitted.",
                             auto_terminated=False),
                Relationship(name="waiting", description="Batch is incomplete.",
                             auto_terminated=False),
                Relationship(name="preflight",
                             description="Batch costed but NOT submitted.",
                             auto_terminated=False),
                Relationship(name="failure", description="Input or submission failed.",
                             auto_terminated=False)]

    @staticmethod
    def _slot_path(state_dir, label):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        return os.path.join(state_dir, safe + ".json")

    def _load_slot(self, path, ttl):
        try:
            if ttl and os.path.exists(path) and time.time() - os.path.getmtime(path) > ttl:
                os.remove(path)
                return {"entries": []}
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (FileNotFoundError, ValueError, OSError):
            return {"entries": []}

    def backend_for(self, device, token, instance):
        """Resolve the backend. Reads calibration only; submits nothing."""
        from qiskit_ibm_runtime import QiskitRuntimeService  # noqa: PLC0415

        service_kwargs = {"channel": "ibm_quantum_platform", "token": token}
        if instance:
            service_kwargs["instance"] = instance
        return QiskitRuntimeService(**service_kwargs).backend(device)

    def prepare(self, qasms, shots, device, token, instance, seeds,
                max_pending, max_usage, fixed_layout=None, metadata=None,
                excluded_physical_qubits=(), prior_layouts=()):
        """Everything up to submission: pad, lay out, transpile, cost.

        Split out from :meth:`submit` so preflight can run the whole cost model
        without a run endpoint ever being called. Anything that could contact
        the provider to CHANGE state lives in :meth:`submit`, not here.
        """
        backend = self.backend_for(device, token, instance)
        pending = int(getattr(backend.status(), "pending_jobs", 0) or 0)
        if pending > max_pending:
            raise RuntimeError("backend queue has {} pending jobs; cap is {}".format(
                pending, max_pending))

        # Pad BEFORE layout search: the adders span 5 to 8 qubits and one
        # layout has to serve them all. See batch_prep for why padding beats
        # per-width jobs.
        circuits, widths, target = batch_prep.pad_batch(
            batch_prep.circuits_from_qasm(qasms))
        capacity = int(getattr(backend, "num_qubits", 0) or 0)
        if capacity and target > capacity:
            raise RuntimeError(
                "batch needs {} qubits after padding; {} has {}".format(
                    target, device, capacity))

        if fixed_layout:
            if len(fixed_layout) != target:
                raise RuntimeError(
                    "Fixed Layout names {} qubits but the padded batch is {} "
                    "wide".format(len(fixed_layout), target))
            if capacity and max(fixed_layout) >= capacity:
                raise RuntimeError(
                    "Fixed Layout names qubit {} but {} has {}".format(
                        max(fixed_layout), device, capacity))
            if len(set(fixed_layout)) != len(fixed_layout):
                raise RuntimeError("Fixed Layout repeats a physical qubit")
            layout = list(fixed_layout)
            # Count two-qubit gates the same way the search does, so the
            # reported metric means the same thing whether or not it was pinned.
            evidence = batch_prep.pinned_layout_evidence(
                circuits, backend, layout, widths, metadata=metadata)
            two_q = max(row["two_qubit_gates"]
                        for row in evidence["winning_representatives"])
        else:
            two_q, layout, evidence = batch_prep.balanced_layout_for(
                circuits, backend, seeds, widths, metadata=metadata,
                excluded_physical_qubits=excluded_physical_qubits,
                prior_layouts=prior_layouts)
        from qiskit import transpile  # noqa: PLC0415
        transpiled = transpile(circuits, backend=backend, optimization_level=3,
                               initial_layout=layout, seed_transpiler=11,
                               scheduling_method="asap")
        profile_metadata = (list(metadata) if metadata is not None
                            else [{} for _ in transpiled])
        profiles = [batch_prep._profile_transpiled(circuit, backend, attrs)
                    for circuit, attrs in zip(transpiled, profile_metadata)]
        execution_profile = batch_prep._result_profile(
            profiles, profile_metadata, excluded_physical_qubits)
        execution_profile["result_mapping_complete"] = (
            all(profile["result_mapping_complete"] for profile in profiles)
            if metadata is not None else None)
        execution_profile["source"] = "final-submission-transpilation"
        evidence.update(execution_profile)
        if execution_profile["excluded_active_hits"]:
            raise RuntimeError(
                "final transpiled circuits touch excluded physical qubit(s): {}"
                .format(execution_profile["excluded_active_hits"]))
        if metadata is not None and not execution_profile["result_mapping_complete"]:
            raise RuntimeError("final transpiled result measurement map is incomplete")
        dt = float(getattr(backend, "dt", 0) or 0)
        estimated_usage = sum(float(c.duration or 0) * dt * shots for c in transpiled)
        if estimated_usage > max_usage:
            raise RuntimeError("estimated QPU usage {:.2f}s exceeds {:.2f}s cap".format(
                estimated_usage, max_usage))
        return {"backend": backend, "transpiled": transpiled, "layout": layout,
                "two_qubit_gates": two_q, "pending": pending,
                "layout_evidence": evidence,
                "estimated_usage": estimated_usage, "widths": widths,
                "padded_width": target, "capacity": capacity}

    def run_job(self, plan, shots):
        """The one call that spends QPU time."""
        from qiskit_ibm_runtime import SamplerV2  # noqa: PLC0415

        job = SamplerV2(mode=plan["backend"]).run(plan["transpiled"], shots=shots)
        return str(job.job_id())

    def submit(self, qasms, shots, device, token, instance, seeds, max_pending,
               max_usage, fixed_layout=None, metadata=None,
               excluded_physical_qubits=(), prior_layouts=()):
        """Prepare and submit. Kept for callers that always intended to spend."""
        plan = self.prepare(qasms, shots, device, token, instance, seeds,
                            max_pending, max_usage, fixed_layout, metadata,
                            excluded_physical_qubits, prior_layouts)
        return (self.run_job(plan, shots), plan["two_qubit_gates"],
                [int(q) for q in plan["layout"]], plan["pending"],
                plan["estimated_usage"])

    def transform(self, context, flowFile):
        def get(prop):
            return context.getProperty(prop).evaluateAttributeExpressions(flowFile).getValue()

        raw = bytes(flowFile.getContentsAsBytes())
        try:
            qasm = raw.decode("utf-8")
            if "OPENQASM" not in qasm:
                raise ValueError("body is not OpenQASM")
            label = get(self.batch_label)
            k = int(get(self.expected_circuits))
            ttl = int(get(self.slot_ttl))
        except Exception as exc:
            return FlowFileTransformResult(relationship="failure", contents=raw,
                                           attributes={"batch.error": str(exc)})
        state_dir = get(self.state_dir)
        os.makedirs(state_dir, exist_ok=True)
        path = self._slot_path(state_dir, label)
        slot = self._load_slot(path, ttl)
        entry = {"label": (get(self.circuit_label) or
                           "circuit-{}".format(len(slot["entries"]) + 1)),
                 "kind": (get(self.circuit_kind) or "control"),
                 "qasm": qasm}
        # Ground truth rides with the circuit. A manifest that does not carry it
        # produces a polled result nobody can score, and the lane then falls back
        # to a distributional oracle without saying so.
        captured = batch_prep.captured_attributes(
            flowFile.getAttributes(), get(self.attribute_prefix) or "")
        if captured:
            entry["attributes"] = captured
        slot["entries"].append(entry)
        if len(slot["entries"]) < k:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(slot, handle)
            return FlowFileTransformResult(
                relationship="waiting", contents=raw,
                attributes={"batch.status": "waiting", "batch.label": label,
                            "batch.have": str(len(slot["entries"])), "batch.need": str(k)})
        if len(slot["entries"]) != k:
            _discard_slot(path)
            return FlowFileTransformResult(relationship="failure", contents=raw,
                                           attributes={"batch.error": "slot exceeded Expected Circuits"})

        # Interleave first: null / control / mutant must stay spread through the
        # job so calibration drift cannot land on one kind. Padding happens
        # inside prepare() and must not reorder anything.
        ordered = interleave(slot["entries"])
        device, shots = get(self.device), int(get(self.shots))
        mode = (get(self.submit_mode) or PREFLIGHT).strip().lower()

        guard = self._check_guards(get, ordered, shots)
        if guard is not None:
            _discard_slot(path)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": guard, "batch.label": label})

        try:
            plan = self.prepare(
                [e["qasm"] for e in ordered], shots, device,
                batch_prep.resolve_secret(get(self.token), "IBM_QUANTUM_TOKEN"),
                batch_prep.resolve_secret(get(self.instance), "IBM_QUANTUM_INSTANCE"),
                int(get(self.layout_seeds)),
                int(get(self.max_pending)), float(get(self.max_usage)),
                _parse_layout(get(self.fixed_layout)),
                metadata=[e.get("attributes") or {} for e in ordered],
                excluded_physical_qubits=_parse_qubits(
                    get(self.excluded_physical_qubits)))
        except Exception as exc:
            _discard_slot(path)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": "preparation failed: {}".format(exc),
                            "batch.label": label})

        try:
            batch_prep.attach_layout_attempt(
                plan.setdefault("layout_evidence", {}), ordered)
        except Exception as exc:
            _discard_slot(path)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": "layout provenance failed: {}".format(exc),
                            "batch.label": label})

        common = self._plan_attributes(plan, label, device, shots, ordered)
        common.update(self._sizing_attributes(get, shots))

        if mode != ARMED:
            # Nothing has contacted the run endpoint at this point, and nothing
            # will. A new trigger creates a new run id and therefore a new slot,
            # so retaining this one would only make a later retry look stale.
            report = self._manifest(None, device, shots, plan, ordered)
            report["submit_mode"] = PREFLIGHT
            common.update({"batch.status": "preflight", "batch.submitted": "false"})
            _discard_slot(path)
            return FlowFileTransformResult(
                relationship="preflight",
                contents=json.dumps(report, indent=2).encode(),
                attributes=common)

        try:
            job_id = self.run_job(plan, shots)
        except Exception as exc:
            _discard_slot(path)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": "submit failed: {}".format(exc),
                            "batch.label": label})
        _discard_slot(path)

        manifest = self._manifest(job_id, device, shots, plan, ordered)
        # The archived copy keeps the circuits; the FlowFile copy keeps digests.
        written, manifest_error = persist_manifest(
            get(self.manifest_dir), manifest, [e["qasm"] for e in ordered])
        common.update({"batch.status": "submitted", "batch.submitted": "true",
                       "batch.job_id": job_id, "hw.job_id": job_id,
                       "batch.manifest_path": written or "",
                       "batch.manifest_error": manifest_error or ""})
        return FlowFileTransformResult(relationship="submitted",
                                       contents=json.dumps(manifest, indent=2).encode(),
                                       attributes=common)

    # -- helpers -------------------------------------------------------------

    def _check_guards(self, get, ordered, shots):
        """Every budget guard, evaluated before anything touches the provider."""
        try:
            max_circuits = int(get(self.max_circuits))
            max_shots = int(get(self.max_shots))
        except (TypeError, ValueError) as exc:
            return "non-numeric guard setting: {}".format(exc)
        if len(ordered) > max_circuits:
            return "batch has {} circuits; Maximum Circuits is {}".format(
                len(ordered), max_circuits)
        if shots > max_shots:
            return "requested {} shots; Maximum Shots Per Circuit is {}".format(
                shots, max_shots)
        return None

    def _sizing_attributes(self, get, shots):
        """What the shot budget should have been, next to what it is."""
        try:
            p_ref = float(get(self.control_success))
            min_diff = float(get(self.min_difference))
            needed = required_shots(p_ref, min_diff)
        except (TypeError, ValueError):
            return {}
        return {"batch.control_success_probability": "{:.4f}".format(p_ref),
                "batch.minimum_detectable_difference": "{:.4f}".format(min_diff),
                "batch.required_shots_per_arm": str(needed),
                "batch.shots_are_sufficient": "true" if shots >= needed else "false"}

    @staticmethod
    def _plan_attributes(plan, label, device, shots, ordered):
        return {"batch.label": label, "batch.device": device,
                "batch.size": str(len(ordered)), "batch.shots": str(shots),
                "batch.two_qubit_gates": str(plan["two_qubit_gates"]),
                "batch.layout": ",".join(str(int(q)) for q in plan["layout"]),
                "batch.pending_jobs": str(plan["pending"]),
                "batch.padded_width": str(plan["padded_width"]),
                "batch.circuit_widths": ",".join(str(w) for w in plan["widths"]),
                "batch.device_qubits": str(plan["capacity"]),
                "batch.estimated_usage_seconds": "{:.6f}".format(plan["estimated_usage"]),
                "hw.provider": "ibm-quantum"}

    @staticmethod
    def _manifest(job_id, device, shots, plan, ordered):
        """Self-describing: an entry carries enough to be scored on its own."""
        entries = batch_prep.manifest_entries(ordered, plan["widths"])
        return {"job_id": job_id, "device": device, "shots": shots,
                "layout": [int(q) for q in plan["layout"]],
                "layout_evidence": plan.get("layout_evidence", {}),
                "padded_width": plan["padded_width"],
                "estimated_usage_seconds": plan["estimated_usage"],
                # Device-wide queue depth at submit time. It reaches the
                # FlowFile as batch.pending_jobs, but a FlowFile is consumed and
                # gone, so without this the number is unrecoverable afterwards.
                # The campaign preregisters queue depth as something that drifts
                # across a window and could confound operator with position, so
                # the run has to be able to show what it actually was.
                "pending_jobs": plan["pending"],
                "entries": entries}
