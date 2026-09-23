import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder

import batch_prep
from batch_prep import persist_manifest
from QuantumSuccessProbabilityOracle import required_shots

#: Submit modes. See QuantumIBMBatchSubmitter for why preflight is the default.
PREFLIGHT, ARMED = "preflight", "armed"

#: Ranking used by ``case_major_order``. ``readout`` (the X-prepare baseline)
#: comes first, then ``control``, then ``null`` (the calibration replicate);
#: everything else -- mutants -- is not a named calibration kind and falls
#: through to rank 3, same convention as the IQM/IBM submitters' ``_KIND_CYCLE``.
_KIND_RANK = {"readout": 0, "control": 1, "null": 2}


def case_major_order(entries):
    """Order a batch by test case first, builder second, kind third.

    The IQM/IBM submitters interleave null/control/mutant round-robin so device
    calibration drift cannot bias one kind. That is the right answer when the
    comparison of interest is control-vs-mutant. Here it is not: the Grover
    N-M matrix's primary comparison is builder-vs-builder *within one test
    case* (does QiskitGroverCircuit disagree with PennylaneGroverCircuit on
    the same marked state?), so those circuits must be adjacent in the batch.
    Interleaving by kind alone would, with every cell the same kind
    ("control"), degenerate to arrival order -- and arrival order is
    non-deterministic because the three builder branches run in parallel.

    Sorting on declared keys (the submitter-stamped ``group``/``member``,
    which the tool sets to the test case id and the builder name) rather than
    on arrival order is what makes the layout reproducible: the same batch
    sorts the same way regardless of which branch happened to finish first.
    """
    def key(entry):
        kind = entry.get("kind") or ""
        return (entry.get("group") or "", entry.get("member") or "",
               _KIND_RANK.get(kind, 3), kind, entry.get("label") or "")
    return sorted(entries, key=key)


def _parse_layout(raw):
    """'122,144,124' -> [122, 144, 124]; blank/None -> None (search).

    Deliberately duplicated from QuantumIBMBatchSubmitter/QuantumIQMBatchSubmitter
    rather than imported. NiFi loads each processor module by file path, so a
    plain sibling import raises ModuleNotFoundError at load time and the
    processor is skipped with no UI feedback -- which is exactly what happened
    to QuantumMutator on 2026-08-23.
    """
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if not parts:
        return None
    if not all(p.isdigit() for p in parts):
        raise ValueError("Fixed Layout must be comma-separated integers, got %r" % (raw,))
    return [int(p) for p in parts]


def _discard_slot(path):
    """Remove a completed/failed batch slot so a retry starts cleanly."""
    try:
        os.remove(path)
    except OSError:
        pass


def _credentials_info(path):
    """Existence and refresh-token expiry of the QI credentials file.

    Read-only, offline, and deliberately does not round-trip through
    ``qi2_shared``'s pydantic settings model -- the raw JSON shape
    (``{"auths": {host: {"tokens": {...}}}, "default_host": host}``) is stable
    and a plain parse is enough for a preflight summary. Any problem reading
    or parsing the file is reported as "absent", never raised: a missing or
    unreadable credentials file is exactly the condition preflight exists to
    surface, not a reason to fail the FlowFile.
    """
    info = {"present": False, "refresh_expires_at": ""}
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as handle:
            doc = json.load(handle)
        info["present"] = True
        host = doc.get("default_host")
        tokens = ((doc.get("auths") or {}).get(host) or {}).get("tokens") or {}
        generated_at = tokens.get("generated_at")
        refresh_in = tokens.get("refresh_expires_in")
        if generated_at is not None and refresh_in is not None:
            import datetime  # noqa: PLC0415
            info["refresh_expires_at"] = datetime.datetime.fromtimestamp(
                float(generated_at) + float(refresh_in),
                tz=datetime.timezone.utc).isoformat()
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return info


class QuantumInspireBatchSubmitter(FlowFileTransform):
    """
    Accumulates circuits and submits them to Quantum Inspire as a SINGLE batch
    job, without waiting for the result.

    Modelled line-for-line on QuantumIQMBatchSubmitter -- same collection
    model, same budget guards, same preflight-by-default posture -- with the
    provider-specific parts replaced:

    * QI authenticates from ``~/.quantuminspire/config.json``, not a token
      property, so there is no ``API Token`` / ``Server URL`` here.
    * ``QIJob.job_id()`` is always the empty string (``JobV1.__init__``
      hardcodes it); the real identity is ``job.batch_job_id``, captured by
      ``run_job`` below.
    * A submitted job cannot be recovered from the batch id alone: the
      per-circuit job ids needed by ``job.result()`` live only on the in-memory
      job object, and ``QIJob.serialize()``/``deserialize()`` (a QPY file) is
      the only public way to recover them across a process boundary. So this
      submitter writes that handle to disk immediately after submission and
      records its path -- QuantumInspireBatchPoller deserialises it rather
      than reconstructing the job from the manifest.
    * QI's backend target carries no gate-duration data, so the usual
      duration-based usage estimate is always 0.0 here -- structurally, not by
      omission. ``Maximum Circuits`` and ``Maximum Shots Per Circuit`` are the
      real budget guards on this lane, plus preflight-by-default.

    Output relationships
    --------------------
    submitted - job launched; body is the manifest, hw.job_id is set.
    waiting   - buffering; fewer than K circuits so far. Auto-terminate.
    preflight - batch costed but NOT submitted.
    failure   - submission failed or the input was malformed.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Accumulates OpenQASM circuits and submits them to Quantum Inspire "
            "as a single batch job with one shared, searched qubit layout. Does "
            "not block: emits the batch job id and a manifest for "
            "QuantumInspireBatchPoller to collect."
        )
        tags = ["quantum", "quantum-inspire", "qutech", "hardware", "qpu",
                "batch", "submit"]
        # The literal "<2.5" substring is asserted by
        # tests/test_processor_dependencies.py. The effective ceiling is
        # tighter than what is written here: qiskit-quantuminspire 0.18.2
        # itself requires qiskit>=2.0.0,<2.4.0; that narrower pin is left to
        # pip/uv to resolve rather than duplicated here, so this file does not
        # need editing every time the plugin's own pin moves.
        dependencies = ["qiskit-quantuminspire>=0.18.2,<0.19", "qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.batch_label = PropertyDescriptor(
            name="Batch Label",
            description=("Slot key (Expression Language). Every circuit of one "
                         "batch must produce the same key."),
            required=True, default_value="${test.run_id}",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.expected_circuits = PropertyDescriptor(
            name="Expected Circuits",
            description="How many circuits form one batch before it is submitted.",
            required=True, default_value="12",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.device = PropertyDescriptor(
            name="Device",
            description=("Quantum Inspire backend name, e.g. 'Tuna-17', "
                         "'Tuna-9', 'Tuna-5', or 'QX emulator' (free, "
                         "noiseless -- validates transport only)."),
            required=True, default_value="Tuna-17",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description="Shots per circuit.",
            required=True, default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
        )
        self.circuit_kind = PropertyDescriptor(
            name="Circuit Kind",
            description=("Expression Language yielding 'null' for a calibration "
                         "replicate, 'control' for a correct version, or the "
                         "mutation operator name for a mutant."),
            required=True, default_value="${test.kind}",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.circuit_label = PropertyDescriptor(
            name="Circuit Label",
            description="Expression Language naming this circuit in the report.",
            required=False, default_value="${circuit.framework}",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.batch_group_key = PropertyDescriptor(
            name="Batch Group Key",
            description=(
                "Expression Language giving each circuit's case-major GROUP -- "
                "the primary sort key for case_major_order, so every builder's "
                "circuit for the same test case lands adjacent in the batch."
            ),
            required=True, default_value="${test.case_id}",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.batch_member_key = PropertyDescriptor(
            name="Batch Member Key",
            description=(
                "Expression Language giving each circuit's case-major MEMBER -- "
                "the secondary sort key, typically the builder identity."
            ),
            required=True, default_value="${grover.builder}",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.fixed_layout = PropertyDescriptor(
            name="Fixed Layout",
            description=(
                "Comma-separated physical qubits to pin this batch to. Blank "
                "(the default) searches Layout Search Seeds transpiler seeds "
                "and keeps the best. Pin it whenever several jobs are to be "
                "compared with each other. Must name exactly as many qubits as "
                "the padded batch width."
            ),
            required=False, default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.layout_seeds = PropertyDescriptor(
            name="Layout Search Seeds",
            description=("How many transpiler seeds to try when searching for a "
                         "layout. The best is reused for the whole batch."),
            required=True, default_value="8",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
        )
        self.max_usage = PropertyDescriptor(
            name="Maximum Estimated Usage Seconds",
            description=(
                "Hard cap on estimated circuit execution time for this job. "
                "INERT on Quantum Inspire: QI's backend target publishes no "
                "gate-duration model, so the estimate this guards is always "
                "0.0 and this cap can never trigger. Maximum Circuits and "
                "Maximum Shots Per Circuit are the real budget guards on this "
                "lane; batch.usage_cap_effective on the emitted attributes "
                "records that this cap is not effective."
            ),
            required=True, default_value="120",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
        )
        self.max_circuits = PropertyDescriptor(
            name="Maximum Circuits",
            description="Refuse to submit a batch larger than this.",
            required=True, default_value="50",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
        )
        self.max_shots = PropertyDescriptor(
            name="Maximum Shots Per Circuit",
            description="Refuse to submit if Shots exceeds this.",
            required=True, default_value="4096",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
        )
        self.submit_mode = PropertyDescriptor(
            name="Submit Mode",
            description=(
                "preflight (default) transpiles, searches a layout, estimates "
                "usage and stops WITHOUT contacting the provider. armed submits "
                "and spends QI credits. The safe value is the default."
            ),
            required=True, default_value=PREFLIGHT,
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.attribute_prefix = PropertyDescriptor(
            name="Attribute Prefix",
            description=(
                "FlowFile attributes with this prefix are captured onto each "
                "batch entry and travel through to the polled result, so a "
                "returned circuit can be scored against its own ground truth. "
                "Blank captures nothing."
            ),
            required=False, default_value="grover.",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.control_success = PropertyDescriptor(
            name="Control Success Probability",
            description=(
                "Expected success probability of a CORRECT circuit on this "
                "device, used by preflight to report the shots per arm the "
                "design needs."
            ),
            required=False, default_value="0.30",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.min_difference = PropertyDescriptor(
            name="Minimum Detectable Difference",
            description=(
                "Smallest drop in success probability the run must detect. Cost "
                "scales with the inverse square of this."
            ),
            required=False, default_value="0.10",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.state_dir = PropertyDescriptor(
            name="State Directory",
            description="Where partial batches are buffered between FlowFiles.",
            required=True,
            default_value="reports/tmp/quanifi_grover_batch_state",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.manifest_dir = PropertyDescriptor(
            name="Manifest Directory",
            description=("Where a submitted job's manifest (and its QPY job "
                         "handle) are written. The manifest is the only record "
                         "of which circuit produced which result; the handle "
                         "is the only way to recover an in-flight job."),
            required=True,
            default_value="experiments/results/grover_hw_matrix/manifests",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.credentials_file = PropertyDescriptor(
            name="Credentials File",
            description=(
                "Path to the Quantum Inspire CLI's persistent config file. NOT "
                "read for authentication -- the plugin itself hard-codes this "
                "path -- only to report whether it exists and when its refresh "
                "token expires, in preflight."
            ),
            required=False,
            default_value="~/.quantuminspire/config.json",
        )
        self.slot_ttl = PropertyDescriptor(
            name="Slot TTL Seconds",
            description=(
                "Discard a partially filled slot older than this and start "
                "fresh. Guards against a branch that never arrives. 0 disables "
                "the check."
            ),
            required=True,
            default_value="3600",
            validators=[StandardValidators.NON_NEGATIVE_INTEGER_VALIDATOR],
        )
        self.descriptors = [
            self.slot_ttl,
            self.batch_label, self.expected_circuits, self.device,
            self.shots, self.circuit_kind, self.circuit_label,
            self.batch_group_key, self.batch_member_key,
            self.fixed_layout,
            self.layout_seeds, self.max_usage, self.max_circuits, self.max_shots,
            self.submit_mode, self.attribute_prefix, self.control_success,
            self.min_difference, self.state_dir, self.manifest_dir,
            self.credentials_file,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def getRelationships(self):
        return [
            Relationship(name="submitted", description="Job launched; manifest emitted.",
                         auto_terminated=False),
            Relationship(name="waiting", description="Buffering; <K circuits so far.",
                         auto_terminated=False),
            Relationship(name="preflight",
                         description="Batch costed but NOT submitted.",
                         auto_terminated=False),
            Relationship(name="failure", description="Submission failed or bad input.",
                         auto_terminated=False),
        ]

    def _slot_path(self, state_dir, label):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        return os.path.join(state_dir, "%s.json" % safe)

    def _load_slot(self, path, ttl_seconds=0):
        """Load a partial slot, discarding it if it has gone stale.

        Without this a slot buffers forever: if one branch fails or is never
        sent, the Kth FlowFile never arrives, the batch never fires, and every
        later run joins the same orphaned slot and is silently swallowed. A TTL
        makes the failure self-healing -- a stale slot is dropped and the next
        arrival starts a fresh one.
        """
        try:
            if ttl_seconds > 0 and os.path.exists(path):
                age = time.time() - os.path.getmtime(path)
                if age > ttl_seconds:
                    self.logger.warn(
                        "{}: discarding slot {} -- {:.0f}s old, past the {}s TTL; "
                        "a branch never arrived".format(
                            type(self).__name__, os.path.basename(path),
                            age, ttl_seconds))
                    os.remove(path)
                    return {"entries": []}
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (FileNotFoundError, ValueError, OSError):
            return {"entries": []}

    def backend_for(self, device):
        """Resolve the QI backend. Reads backend metadata only; submits nothing."""
        from qiskit_quantuminspire.qi_provider import QIProvider  # noqa: PLC0415

        return QIProvider().get_backend(device)

    def prepare(self, qasms, shots, device, seeds, max_usage,
                fixed_layout=None, metadata=None, excluded_physical_qubits=(),
                prior_layouts=()):
        """Everything up to submission: pad, lay out, transpile, cost.

        Copied from QuantumIQMBatchSubmitter.prepare with ``server``/``token``
        dropped from the signature (QI has neither) and three QI-specific,
        offline metadata reads added: the backend type (for
        ``max_jobs_per_batch_job``), and ``max_shots`` -- both checked here so
        a limit violation raises in preflight rather than at submit time.
        """
        from qiskit import transpile  # noqa: PLC0415

        backend = self.backend_for(device)

        # Pad before layout search: one layout must serve the whole batch.
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
            evidence = batch_prep.pinned_layout_evidence(
                circuits, backend, layout, widths, metadata=metadata)
            two_q = max(row["two_qubit_gates"]
                        for row in evidence["winning_representatives"])
        else:
            two_q, layout, evidence = batch_prep.balanced_layout_for(
                circuits, backend, seeds, widths, metadata=metadata,
                excluded_physical_qubits=excluded_physical_qubits,
                prior_layouts=prior_layouts)
        transpiled = transpile(circuits, backend=backend, optimization_level=3,
                               initial_layout=layout, seed_transpiler=11)
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

        # QI's target sets no `dt` (no gate-duration model), so this estimate
        # is always 0.0 -- structurally, not by omission. See the Maximum
        # Estimated Usage Seconds docstring.
        dt = float(getattr(backend, "dt", 0) or 0)
        estimated_usage = sum(float(getattr(c, "duration", 0) or 0) * dt * shots
                              for c in transpiled)
        if max_usage and estimated_usage > max_usage:
            raise RuntimeError("estimated QPU usage {:.2f}s exceeds {:.2f}s cap".format(
                estimated_usage, max_usage))

        # Free metadata reads: raise here, in preflight, rather than at submit
        # time in QIJob._check_backendtype_job_limits / backend.run().
        backend_type = backend.get_backend_type()
        max_jobs_per_batch_job = int(backend_type.max_jobs_per_batch_job)
        if len(transpiled) > max_jobs_per_batch_job:
            raise RuntimeError(
                "batch has {} circuits; {} allows a maximum of {} jobs per "
                "batch job".format(len(transpiled), device, max_jobs_per_batch_job))
        device_max_shots = int(getattr(backend, "max_shots", 0) or 0)
        if device_max_shots and shots > device_max_shots:
            raise RuntimeError(
                "requested {} shots; {} allows a maximum of {}".format(
                    shots, device, device_max_shots))

        # Per-builder two-qubit count is this study's independent variable, so
        # it is recorded per circuit, not just as the batch-wide worst case.
        isa_profile = batch_prep.isa_profile(transpiled)

        return {"backend": backend, "transpiled": transpiled, "layout": layout,
                "two_qubit_gates": two_q, "estimated_usage": estimated_usage,
                "layout_evidence": evidence,
                "widths": widths, "padded_width": target, "capacity": capacity,
                "isa_profile": isa_profile,
                "provenance": batch_prep.transpiler_provenance(
                    backend, seed_transpiler=11, optimization_level=3),
                "provider_backend_type": backend_type.model_dump(mode="json"),
                "provider_max_batch_jobs": max_jobs_per_batch_job}

    def run_job(self, plan, shots):
        """The one call that spends. NOT ``job.job_id()`` -- QIJob hardcodes
        that to "" (JobV1.__init__(backend, "")); the real identity is
        ``batch_job_id``, populated by ``job.submit()`` inside ``backend.run``.
        """
        job = plan["backend"].run(plan["transpiled"], shots=shots)
        plan["job"] = job
        return str(job.batch_job_id)

    @staticmethod
    def persist_handle(manifest_dir, job):
        """Serialize the QPY job handle, BEFORE the manifest is written.

        ``QIJob.result()`` needs the per-circuit job ids held on the job
        object in memory; the only public way to recover them across a NiFi
        restart, or in a poller running in a separate process invocation, is
        ``QIJob.serialize()`` / ``QIJob.deserialize()``. Without this file the
        submitted job is unpollable.

        Never fatal: the QPU time is already spent by the time this is
        called, so a disk problem here must not turn a successful submission
        into a ``failure``. Returns ``(path, None)`` on success and
        ``(None, error)`` on failure, for the caller to record either way.
        """
        try:
            os.makedirs(manifest_dir, exist_ok=True)
            path = os.path.join(manifest_dir, "%s.qijob.qpy" % job.batch_job_id)
            job.serialize(path)
            return path, None
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)

    @staticmethod
    def _manifest_entries(ordered, plan):
        """Self-describing entries, plus the case-major group/member keys and
        the per-circuit ISA profile -- additive on top of the shared shape
        every hardware submitter emits."""
        rows = batch_prep.manifest_entries(ordered, plan["widths"])
        for index, row in enumerate(rows):
            row["group"] = ordered[index].get("group", "")
            row["member"] = ordered[index].get("member", "")
        return batch_prep.merge_isa_profile(rows, plan.get("isa_profile"))

    def transform(self, context, flowFile):
        def get(prop):
            return context.getProperty(prop).evaluateAttributeExpressions(flowFile).getValue()

        state_dir = context.getProperty(self.state_dir).getValue()
        label = get(self.batch_label)
        try:
            k = int(get(self.expected_circuits))
        except (TypeError, ValueError):
            k = 12
        kind = (get(self.circuit_kind) or "").strip() or "control"
        name = (get(self.circuit_label) or "").strip()
        group_key = (get(self.batch_group_key) or "").strip()
        member_key = (get(self.batch_member_key) or "").strip()

        raw = bytes(flowFile.getContentsAsBytes())
        try:
            qasm = raw.decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            return FlowFileTransformResult(relationship="failure", contents=raw,
                                           attributes={"batch.error": str(exc)})
        if "OPENQASM" not in qasm:
            msg = "body is not OpenQASM; this processor consumes circuits"
            self.logger.error("QuantumInspireBatchSubmitter: {}".format(msg))
            return FlowFileTransformResult(relationship="failure", contents=raw,
                                           attributes={"batch.error": msg})

        os.makedirs(state_dir, exist_ok=True)
        slot_path = self._slot_path(state_dir, label)
        try:
            ttl = int(context.getProperty(self.slot_ttl).getValue())
        except (TypeError, ValueError):
            ttl = 3600
        slot = self._load_slot(slot_path, ttl_seconds=ttl)
        if not name:
            name = "circuit-%d" % (len(slot["entries"]) + 1)
        entry = {"label": name, "kind": kind, "qasm": qasm,
                 "group": group_key, "member": member_key}
        captured = batch_prep.captured_attributes(
            flowFile.getAttributes(), get(self.attribute_prefix) or "")
        if captured:
            entry["attributes"] = captured
        slot["entries"].append(entry)

        if len(slot["entries"]) < k:
            with open(slot_path, "w", encoding="utf-8") as handle:
                json.dump(slot, handle)
            self.logger.warn(
                "QuantumInspireBatchSubmitter [{}]: buffered '{}' ({}/{}).".format(
                    label, name, len(slot["entries"]), k))
            return FlowFileTransformResult(
                relationship="waiting", contents=raw,
                attributes={"batch.status": "waiting", "batch.label": label,
                            "batch.have": str(len(slot["entries"])),
                            "batch.need": str(k)})
        if len(slot["entries"]) != k:
            _discard_slot(slot_path)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": "slot exceeded Expected Circuits",
                            "batch.label": label})

        ordered = case_major_order(slot["entries"])
        device = get(self.device)
        shots = int(context.getProperty(self.shots).getValue())
        seeds = int(context.getProperty(self.layout_seeds).getValue())

        mode = (get(self.submit_mode) or PREFLIGHT).strip().lower()

        guard = self._check_guards(context, get, ordered, shots)
        if guard is not None:
            self.logger.error("QuantumInspireBatchSubmitter: {}".format(guard))
            _discard_slot(slot_path)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": guard, "batch.label": label})

        try:
            max_usage = float(context.getProperty(self.max_usage).getValue())
        except (TypeError, ValueError):
            max_usage = 0.0
        try:
            plan = self.prepare([e["qasm"] for e in ordered], shots, device,
                                seeds, max_usage,
                                _parse_layout(get(self.fixed_layout)))
        except Exception as exc:  # noqa: BLE001 - surface any SDK/API failure
            self.logger.error("QuantumInspireBatchSubmitter: preparation failed: {}".format(exc))
            _discard_slot(slot_path)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": "preparation failed: {}".format(exc),
                            "batch.label": label})

        two_q, layout = plan["two_qubit_gates"], [int(q) for q in plan["layout"]]
        sizing = self._sizing_attributes(get, shots)
        entries_out = self._manifest_entries(ordered, plan)
        provenance = plan.get("provenance") or {}
        # Per-builder routed count, submission order, as a JSON list on the
        # relationship itself -- Checkpoint 3 reads this to decide whether a
        # 4-qubit builder is already at the noise floor before arming, without
        # having to parse the manifest body.
        isa_two_qubit_gates_json = json.dumps(
            [row.get("isa_two_qubit_gates") for row in entries_out])

        if mode != ARMED:
            creds = _credentials_info(context.getProperty(self.credentials_file).getValue())
            report = dict(provenance,
                          device=device, shots=shots, layout=layout,
                          layout_evidence=plan.get("layout_evidence", {}),
                          padded_width=plan["padded_width"],
                          estimated_usage_seconds=plan["estimated_usage"],
                          submit_mode=PREFLIGHT,
                          provider="quantum-inspire",
                          provider_backend_type=plan.get("provider_backend_type"),
                          entries=entries_out)
            attrs = {"batch.status": "preflight", "batch.submitted": "false",
                     "batch.label": label, "batch.device": device,
                     "batch.size": str(len(ordered)), "batch.shots": str(shots),
                     "batch.two_qubit_gates": str(two_q),
                     "batch.isa_two_qubit_gates": isa_two_qubit_gates_json,
                     "batch.layout": ",".join(str(q) for q in layout),
                     "batch.padded_width": str(plan["padded_width"]),
                     "batch.circuit_widths": ",".join(str(w) for w in plan["widths"]),
                     "batch.device_qubits": str(plan["capacity"]),
                     "hw.provider": "quantum-inspire",
                     "batch.credentials_present": "true" if creds["present"] else "false",
                     "batch.credentials_refresh_expires_at": creds["refresh_expires_at"],
                     "batch.provider_max_batch_jobs": str(plan["provider_max_batch_jobs"]),
                     "batch.estimated_usage_seconds": "0.000000",
                     "batch.usage_cap_effective": "false"}
            attrs.update(sizing)
            _discard_slot(slot_path)
            return FlowFileTransformResult(
                relationship="preflight",
                contents=json.dumps(report, indent=2).encode("utf-8"),
                attributes=attrs)

        try:
            job_id = self.run_job(plan, shots)
        except Exception as exc:  # noqa: BLE001 - surface any SDK/API failure
            self.logger.error("QuantumInspireBatchSubmitter: submit failed: {}".format(exc))
            _discard_slot(slot_path)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": "submit failed: {}".format(exc),
                            "batch.label": label})
        _discard_slot(slot_path)

        job = plan["job"]
        for index, row in enumerate(entries_out):
            try:
                row["provider_circuit_job_id"] = job.circuits_run_data[index].job_id
            except (IndexError, AttributeError):
                pass

        # The handle is written FIRST, before the manifest: the QPU time is
        # already spent, and without this file the job is unpollable.
        handle_path, handle_error = self.persist_handle(
            context.getProperty(self.manifest_dir).getValue(), job)
        handle_sha256 = ""
        if handle_path:
            try:
                with open(handle_path, "rb") as handle:
                    handle_sha256 = hashlib.sha256(handle.read()).hexdigest()
            except OSError as exc:
                handle_error = handle_error or str(exc)
        if handle_error:
            self.logger.error(
                "QuantumInspireBatchSubmitter: could not persist job handle "
                "for {}: {} -- without it this job is UNPOLLABLE; recover it "
                "manually via provider_circuit_job_id.".format(job_id, handle_error))

        import datetime  # noqa: PLC0415
        submitted_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        manifest = dict(provenance,
                        job_id=job_id, device=device, shots=shots,
                        layout=layout,
                        layout_evidence=plan.get("layout_evidence", {}),
                        padded_width=plan["padded_width"],
                        estimated_usage_seconds=plan["estimated_usage"],
                        provider="quantum-inspire",
                        provider_handle_path=handle_path or "",
                        provider_handle_sha256=handle_sha256,
                        provider_backend_type=plan.get("provider_backend_type"),
                        submitted_at=submitted_at,
                        entries=entries_out)
        written, manifest_error = persist_manifest(
            context.getProperty(self.manifest_dir).getValue(), manifest,
            [e["qasm"] for e in ordered])
        attrs = {
            "batch.manifest_path": written or "",
            "batch.manifest_error": manifest_error or "",
            "batch.handle_path": handle_path or "",
            "batch.handle_error": handle_error or "",
            "batch.status": "submitted", "batch.submitted": "true",
            "batch.label": label,
            "batch.size": str(len(ordered)), "batch.device": device,
            "batch.job_id": job_id, "batch.shots": str(shots),
            "batch.two_qubit_gates": str(two_q),
            "batch.isa_two_qubit_gates": isa_two_qubit_gates_json,
            "batch.layout": ",".join(str(q) for q in layout),
            "batch.padded_width": str(plan["padded_width"]),
            "batch.circuit_widths": ",".join(str(w) for w in plan["widths"]),
            "batch.device_qubits": str(plan["capacity"]),
            "batch.estimated_usage_seconds": "{:.6f}".format(plan["estimated_usage"]),
            "batch.provider_max_batch_jobs": str(plan["provider_max_batch_jobs"]),
            "hw.job_id": job_id, "hw.provider": "quantum-inspire",
        }
        attrs.update(sizing)
        self.logger.warn(
            "QuantumInspireBatchSubmitter [{}]: submitted {} circuits to {} as "
            "batch job {} ({} two-qubit gates, layout {})".format(
                label, len(ordered), device, job_id, two_q, layout))
        return FlowFileTransformResult(
            relationship="submitted",
            contents=json.dumps(manifest, indent=2).encode("utf-8"),
            attributes=attrs)

    # -- helpers -------------------------------------------------------------

    def _check_guards(self, context, get, ordered, shots):
        """Budget guards, evaluated before anything touches Quantum Inspire."""
        try:
            max_circuits = int(context.getProperty(self.max_circuits).getValue())
            max_shots = int(context.getProperty(self.max_shots).getValue())
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
