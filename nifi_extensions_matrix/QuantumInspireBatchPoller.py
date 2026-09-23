"""Poll one Quantum Inspire *batch* job and emit the manifest the expander wants.

The Quantum Inspire twin of QuantumIQMBatchPoller / QuantumIBMBatchPoller. It
consumes the submitter's manifest, recovers the in-flight ``QIJob`` from the
QPY handle QuantumInspireBatchSubmitter wrote next to that manifest, polls it
to completion, and emits::

    {"entries": [{"label", "kind", "counts", "attributes", "num_qubits",
                  "shots_done"}, ...],
     "job_id", "device", "layout", "padded_width", "bit_order",
     "estimated_usage_seconds", "actual_usage_seconds", "submitted_at",
     "completed_at", "calibration_set_id", "provider_job_document"}

which is exactly what ``QuantumBatchResultExpander`` consumes.

Why a QPY handle, not the batch id alone: ``QIJob.result()`` needs the
per-circuit job ids that ``job.submit()`` populated on the in-memory job
object. Those ids are not recoverable from ``batch_job_id`` through any public
API; ``QIJob.serialize()``/``deserialize()`` (a QPY file carrying them in each
circuit's ``metadata``) is the only public recovery path, and this is why the
submitter writes that file before it writes the manifest.

Why partial failure gets its OWN branch, not a raise: Quantum Inspire's
``BatchJobStatus`` has exactly five values --
planned/queued/reserved/running/finished -- and NONE of them means "failed".
A batch every one of whose circuits errored out on the provider side still
reports ``finished``, and ``qiskit.result.Result.get_counts()`` (called with
no index) raises the INSTANT any one experiment in the batch is unsuccessful,
which would otherwise take every other, perfectly good circuit's counts down
with it. This poller reads counts per-index instead, so one failed circuit
cannot erase the rest of the batch. When some but not all circuits come back,
this routes to ``failure`` carrying the COMPLETE polled document -- every good
row, plus which labels failed and the provider's own system messages -- rather
than the IQM poller's ``contents=raw`` on failure, which discards paid results.
The 2026-08-24 lesson in that file's own header is that a paid job must never
vanish; the same principle applies here even though the concrete failure mode
(all-successful ``finished`` status hiding partial data loss) is different.

No sibling imports at all: NiFi loads a poller module without the extensions
directory on `sys.path`, only the submitter (which runs in the same discovery
pass as its FlowFileTransform siblings) gets that.

A Quantum Inspire batch that needed more circuits than the device's
``max_jobs_per_batch_job`` may now arrive as SEVERAL chunk manifests, one per
provider batch job, produced by QuantumInspireBatchSubmitter's partitioning
and fanned out upstream by a plain SplitJson -- this poller always handles
exactly ONE of them, polling and merging that one chunk's own batch job
against that chunk's own entries; it carries no notion of the other chunks.

Bit order: Quantum Inspire's counts, like Qiskit's and IBM's, come back
q0-right (qubit 0 is the RIGHTMOST character of the key) -- verified against a
real completed Tuna-17 job, where marked state '10' (q0-left) came back under
key '01'. Recorded on the output rather than silently normalised here, so a
consumer never has to guess; ``QuantumBatchResultExpander`` does the actual
flip.

Structurally unavailable evidence: Quantum Inspire publishes no QPU-usage
metric and no calibration-set identity comparable to IQM's job timeline, so
``actual_usage_seconds`` is always the empty string (never 0 -- a zero reads
as a free job and corrupts budget totals) and ``calibration_set_id`` is always
null for this provider. The substitutes recorded instead are
``provider_backend_type`` (on the submitter's manifest) and per-experiment
``shots_done`` here. Any completion validator applied across providers must
accept an empty ``actual_usage_seconds`` and a null ``calibration_set_id`` for
the ``quantum-inspire`` arm.
"""
import datetime
import json
import time

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder

#: Quantum Inspire, like Qiskit/IBM, returns counts MSB-first (qubit 0 is the
#: rightmost character). Verified against a real completed Tuna-17 job.
Q0_RIGHT = "q0_right"


def merge_counts(counts_list, results_list, entries):
    """Join per-circuit counts back onto the submitter's manifest entries.

    ``counts_list`` (one counts dict per circuit, in submission order) and
    ``results_list`` (the matching ``qiskit.result.models.ExperimentResult``
    objects, used only for ``shots_done``) must both align with ``entries`` by
    POSITION -- the same "Refusing to align" guard as
    ``QuantumIQMBatchPoller.merge_counts``, and for the same reason: a partial
    zip would attach the wrong ground truth to every row after the gap.

    Unlike the IQM poller, an individual EMPTY counts dict -- an experiment
    Quantum Inspire itself reports as failed -- is not fatal to the whole call.
    It is collected into the second return value instead of raising, so the
    caller can route a partially-failed batch to `failure` WITHOUT discarding
    the entries that DID come back.

    Returns ``(merged, failed_labels)``.
    """
    if isinstance(counts_list, dict):
        counts_list = [counts_list]
    if not isinstance(counts_list, list) or not counts_list:
        raise ValueError("result carries no counts")
    if len(counts_list) != len(entries):
        raise ValueError(
            "job returned {} circuit(s); the manifest holds {}. Refusing to "
            "align them, because a partial zip would attach the wrong ground "
            "truth to every row after the gap.".format(len(counts_list), len(entries)))
    if results_list is not None and len(results_list) != len(entries):
        raise ValueError(
            "job returned {} experiment result(s); the manifest holds {}. "
            "Refusing to align them, because a partial zip would attach the "
            "wrong ground truth to every row after the gap.".format(
                len(results_list), len(entries)))

    merged, failed_labels = [], []
    for index, (counts, entry) in enumerate(zip(counts_list, entries)):
        row = {"label": entry.get("label"), "kind": entry.get("kind")}
        if entry.get("attributes"):
            row["attributes"] = entry["attributes"]
        if entry.get("num_qubits") is not None:
            row["num_qubits"] = entry["num_qubits"]
        if entry.get("batch_index") is not None:
            row["batch_index"] = entry["batch_index"]
        if results_list is not None:
            try:
                row["shots_done"] = int(results_list[index].shots)
            except (AttributeError, TypeError, ValueError):
                pass
        if not isinstance(counts, dict) or not counts:
            failed_labels.append(entry.get("label"))
            continue
        row["counts"] = {str(k).replace(" ", ""): v for k, v in counts.items()}
        merged.append(row)
    return merged, failed_labels


class QuantumInspireBatchPoller(FlowFileTransform):
    """Poll a Quantum Inspire batch job without blocking and emit its manifest."""

    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Polls one Quantum Inspire BATCH job -- recovered from the QPY "
            "handle QuantumInspireBatchSubmitter wrote alongside its manifest "
            "-- and merges its measurement counts back onto that manifest, "
            "preserving each circuit's ground truth, so "
            "QuantumBatchResultExpander can expand and score it."
        )
        tags = ["quantum", "quantum-inspire", "qutech", "hardware", "batch", "poller"]
        dependencies = ["qiskit-quantuminspire>=0.18.2,<0.19", "qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get("jvm")
        super().__init__()
        self.job_id = PropertyDescriptor(
            name="Job ID", description="Quantum Inspire batch job identifier.",
            required=True, default_value="${batch.job_id}",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
        self.job_handle_path = PropertyDescriptor(
            name="Job Handle Path",
            description=(
                "Path to the QPY job handle QuantumInspireBatchSubmitter "
                "wrote. Blank (the default) reads it from the manifest's "
                "'provider_handle_path' field instead -- the normal case."),
            required=False, default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
        self.poll_timeout = PropertyDescriptor(
            name="Poll Timeout Seconds",
            description=("How long one onTrigger may wait before giving up and "
                         "routing to 'pending'. The FlowFile loops back, so this "
                         "bounds a NiFi thread, not the job."),
            required=False, default_value="60",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
        self.poll_interval = PropertyDescriptor(
            name="Poll Interval Seconds", description="Seconds between polls.",
            required=False, default_value="10",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
        self.descriptors = [self.job_id, self.job_handle_path,
                            self.poll_timeout, self.poll_interval]

    def getPropertyDescriptors(self):
        return self.descriptors

    def getRelationships(self):
        return [
            Relationship(name="success",
                         description="Job finished; manifest carries every circuit's counts.",
                         auto_terminated=False),
            Relationship(name="pending",
                         description=("Job not terminal yet. Loop this back into the "
                                      "processor to keep polling."),
                         auto_terminated=False),
            Relationship(name="failure",
                         description=("Job failed, the manifest or handle was "
                                      "unreadable, or some (but not necessarily "
                                      "all) experiments in the batch returned no "
                                      "counts -- see batch.partial."),
                         auto_terminated=False),
        ]

    def transform(self, context, flowFile):
        from qiskit.providers.jobstatus import JobStatus  # noqa: PLC0415
        from qiskit_quantuminspire.qi_jobs import QIJob  # noqa: PLC0415
        from qiskit_quantuminspire.qi_provider import QIProvider  # noqa: PLC0415

        def get(prop):
            return (context.getProperty(prop)
                    .evaluateAttributeExpressions(flowFile).getValue())

        raw = bytes(flowFile.getContentsAsBytes())
        try:
            manifest = json.loads(raw.decode("utf-8"))
            entries = manifest["entries"]
            if not isinstance(entries, list) or not entries:
                raise ValueError("manifest has no 'entries' list")
        except Exception as exc:  # noqa: BLE001
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": "unreadable batch manifest: %s" % exc})

        manifest_job_id = str(manifest.get("job_id") or "").strip()
        handle_path = ((get(self.job_handle_path) or "").strip()
                      or str(manifest.get("provider_handle_path") or "").strip())
        if not handle_path:
            msg = ("no job handle: 'provider_handle_path' is blank in the "
                  "manifest and Job Handle Path is unset. The batch job {} "
                  "likely ran and IS recoverable, but only manually, via the "
                  "'provider_circuit_job_id' recorded on each manifest entry."
                  .format(manifest_job_id or "?"))
            self.logger.error("QuantumInspireBatchPoller: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.job_id": manifest_job_id, "batch.error": msg})

        try:
            provider = QIProvider()
            job = QIJob.deserialize(provider, handle_path)
        except Exception as exc:  # noqa: BLE001
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.job_id": manifest_job_id,
                            "batch.error": "could not recover job from handle "
                                          "{}: {}".format(handle_path, exc)})

        job_id = str(job.batch_job_id)

        try:
            budget = int(get(self.poll_timeout))
            interval = int(get(self.poll_interval))
        except (TypeError, ValueError) as exc:
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.job_id": job_id,
                            "batch.error": "bad numeric property: %s" % exc})

        deadline = time.time() + budget
        status = None
        while True:
            try:
                status = job.status()
            except Exception as exc:  # noqa: BLE001
                # BatchJobStatus has no FAILURE value at all -- planned, queued,
                # reserved, running, finished. Reaching here means the provider
                # returned something QIJob.status() could not map, which is the
                # closest thing to a batch-level failure this API can produce.
                return FlowFileTransformResult(
                    relationship="failure", contents=raw,
                    attributes={"batch.job_id": job_id,
                                "batch.error": "unrecognised Quantum Inspire "
                                              "batch status: {}".format(exc)})
            if status == JobStatus.DONE or time.time() + interval >= deadline:
                break
            time.sleep(interval)

        if status != JobStatus.DONE:
            return FlowFileTransformResult(
                relationship="pending", contents=raw,
                attributes={"batch.status": str(status), "batch.job_id": job_id})

        try:
            result = job.result(wait_for_results=False)
        except Exception as exc:  # noqa: BLE001
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.job_id": job_id,
                            "batch.error": "could not fetch Quantum Inspire "
                                          "result: {}".format(exc)})

        # Per-index, NOT result.get_counts() with no argument: that call
        # raises the instant ANY experiment in the batch is unsuccessful,
        # which would take every other circuit's counts down with it -- the
        # exact all-or-nothing failure this poller exists to avoid.
        counts_list = []
        for index in range(len(result.results)):
            try:
                counts = result.get_counts(index)
            except Exception:  # noqa: BLE001 - a per-experiment failure, not fatal
                counts = {}
            counts_list.append(counts if isinstance(counts, dict) else {})

        try:
            merged, failed_labels = merge_counts(counts_list, result.results, entries)
        except ValueError as exc:
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.job_id": job_id, "batch.error": str(exc)})

        completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        system_messages = dict(getattr(result, "system_messages", {}) or {})
        provider_job_document = {
            "batch_job_id": job.batch_job_id,
            "backend_name": getattr(result, "backend_name", None),
            "success": bool(getattr(result, "success", False)),
            "system_messages": system_messages,
        }
        device = manifest.get("device", "")
        layout = manifest.get("layout")
        shared = {
            "job_id": job_id, "device": device, "layout": layout,
            "padded_width": manifest.get("padded_width"),
            "bit_order": Q0_RIGHT,
            "estimated_usage_seconds": manifest.get("estimated_usage_seconds"),
            # Never 0: a zero reads as a free job and corrupts budget totals.
            # Quantum Inspire publishes no QPU-usage metric to put here.
            "actual_usage_seconds": "",
            "submitted_at": manifest.get("submitted_at"),
            "completed_at": completed_at,
            # Structurally unavailable: no calibration-set identity is
            # published for this provider. provider_backend_type (on the
            # manifest) and per-experiment shots_done are the substitutes.
            "calibration_set_id": None,
            "provider_job_document": provider_job_document,
        }
        # Chunk provenance, when the manifest carries it -- absent on
        # IBM/IQM-style manifests and on a QI manifest that was never
        # chunked, so nothing is emitted for either of those.
        for key in ("chunk_index", "chunk_count", "chunk_offset", "chunk_size",
                   "batch_label", "batch_entry_count"):
            if manifest.get(key) is not None:
                shared[key] = manifest[key]

        if failed_labels:
            # A deliberate improvement over QuantumIQMBatchPoller: that poller
            # returns contents=raw on failure, discarding every already-paid
            # result. Here the good rows survive on the failure relationship
            # too, alongside which labels did not.
            doc = dict(shared, entries=merged, **{
                "qi.failed_labels": failed_labels,
                "system_messages": system_messages})
            msg = "{} of {} experiment(s) in batch job {} returned no counts: {}".format(
                len(failed_labels), len(entries), job_id, failed_labels)
            self.logger.error("QuantumInspireBatchPoller: " + msg)
            return FlowFileTransformResult(
                relationship="failure",
                contents=json.dumps(doc, indent=2).encode("utf-8"),
                attributes={"batch.job_id": job_id, "batch.partial": "true",
                            "batch.device": device, "batch.error": msg,
                            "batch.failed_labels": ",".join(
                                str(label) for label in failed_labels)})

        out = dict(shared, entries=merged)
        shots_done = [row["shots_done"] for row in merged if "shots_done" in row]
        shots = sum(merged[0]["counts"].values()) if merged else 0
        attrs = {"batch.status": "completed", "batch.job_id": job_id,
                "batch.size": str(len(merged)),
                "batch.device": device,
                "batch.shots": str(shots),
                "batch.bit_order": Q0_RIGHT,
                "batch.actual_usage_seconds": "",
                "batch.calibration_set_id": "",
                "batch.estimated_usage_seconds":
                    str(manifest.get("estimated_usage_seconds", "")),
                "batch.submitted_at": str(manifest.get("submitted_at") or ""),
                "batch.completed_at": str(completed_at),
                "batch.layout": ",".join(str(q) for q in (layout or [])),
                "batch.shots_done_min": str(min(shots_done)) if shots_done else "",
                "batch.shots_done_max": str(max(shots_done)) if shots_done else ""}
        if manifest.get("chunk_index") is not None:
            attrs["batch.chunk_index"] = str(manifest["chunk_index"])
        if manifest.get("chunk_count") is not None:
            attrs["batch.chunk_count"] = str(manifest["chunk_count"])
        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(out, indent=2).encode("utf-8"),
            attributes=attrs)
