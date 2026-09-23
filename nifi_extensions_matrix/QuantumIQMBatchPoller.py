"""Poll one IQM Resonance *batch* job and emit the manifest the expander wants.

`IQMJobPoller` polls a single circuit: it reads the `measurement_counts`
artifact and picks one histogram out of it by index. That is the right shape for
the Grover lanes, and the wrong shape for the arithmetic batch, where one job
carries 96 circuits and every one of them has to come back joined to the ground
truth the submitter recorded.

This is the IQM twin of `QuantumIBMBatchPoller`. It consumes the submitter's
batch manifest, matches the job's histograms to the manifest entries **by
position**, and emits::

    {"job_id", "device", "layout", "padded_width", "bit_order",
     "estimated_usage_seconds",
     "entries": [{"label", "kind", "counts", "attributes", "num_qubits"}, ...]}

which is exactly what `QuantumBatchResultExpander` consumes.

Why this file exists: on 2026-08-24 an IQM arithmetic job (96 circuits,
`01a03317-d0da-74f1-97f1-857b8673ff33`) executed on `garnet` and could not be
scored, because the group had been built pairing `QuantumIQMBatchSubmitter`
with the single-circuit `IQMJobPoller`. The expander rejected its output with
"batch has no 'entries' list" and, since that relationship was auto-terminated,
the whole job's results vanished. The results were paid for and lost.

Bit order: the Qrisp/IQM serialiser already emits q0-left, which is the repo's
canonical counts order, so nothing is reversed here -- the value is recorded on
the output so a consumer never has to guess.
"""
import json
import time

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder

import os


def _histogram(item):
    """The counts dict inside one measurement_counts entry, or None.

    A bare histogram is accepted so a schema tweak on IQM's side degrades into
    a clear error rather than a traceback -- but only if it actually looks like
    one. The looser `item.get("counts", item)` fallback would happily treat
    ``{"measurement_keys": [...]}`` as a histogram and score garbage.
    """
    if not isinstance(item, dict) or not item:
        return None
    if "counts" in item:
        counts = item["counts"]
        return counts if isinstance(counts, dict) and counts else None
    if all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
           for k, v in item.items()):
        return item
    return None


def merge_counts(payload, entries):
    """Join a measurement_counts artifact onto the submitter's manifest.

    The manifest entry is the only place a circuit's ground truth survives the
    round trip, so ``attributes`` and ``num_qubits`` are carried forward rather
    than dropped -- without them the polled counts cannot be scored against the
    classical answer and the lane silently degrades to a distributional oracle.

    Matching is positional, which is safe only because the submitter serialises
    the batch in manifest order and IQM returns one histogram per circuit in the
    order submitted. A length mismatch is therefore a real misalignment, not a
    cosmetic one, and is refused rather than zipped short.
    """
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or not payload:
        raise ValueError("measurement_counts artifact is empty or not a batch")
    if len(payload) != len(entries):
        raise ValueError(
            "job returned {} circuit(s); the manifest holds {}. Refusing to "
            "align them, because a partial zip would attach the wrong ground "
            "truth to every row after the gap.".format(len(payload), len(entries)))

    merged = []
    for index, (item, entry) in enumerate(zip(payload, entries)):
        counts = _histogram(item)
        if counts is None:
            raise ValueError("no counts in measurement_counts entry %d" % index)
        row = {"label": entry.get("label"), "kind": entry.get("kind"),
               "counts": counts}
        if entry.get("attributes"):
            row["attributes"] = entry["attributes"]
        if entry.get("num_qubits") is not None:
            row["num_qubits"] = entry["num_qubits"]
        merged.append(row)
    return merged



def _secret(configured, *env_names):
    """Credential from the property, else the environment.

    Deliberately inlined rather than imported from batch_prep: NiFi does not put
    sibling extension modules on a *poller's* import path, and a module-level
    `import batch_prep` here fails with ModuleNotFoundError and marks the
    processor invalid. The submitters can import it; the pollers cannot.

    Blank is not an error. NiFi does not evaluate Expression Language on a
    sensitive property, so a canvas that stores `${IQM_TOKEN}` -- which is how a
    credential is kept out of flow.json.gz -- hands this an empty string, and
    the real value is in the process environment.
    """
    value = (configured or "").strip()
    if value:
        return value
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""

class QuantumIQMBatchPoller(FlowFileTransform):
    """Poll an IQM Resonance batch job without blocking and emit its manifest."""

    TERMINAL = ("completed", "failed", "cancelled")
    DEFAULT_SERVER_URL = "https://resonance.iqm.tech"

    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Polls one IQM Resonance BATCH job and merges its measurement counts "
            "back onto the submitter's manifest, preserving each circuit's ground "
            "truth, so QuantumBatchResultExpander can expand and score it. The "
            "batch twin of IQMJobPoller, which handles a single circuit."
        )
        tags = ["quantum", "iqm", "hardware", "batch", "poller"]
        dependencies = ["requests"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get("jvm")
        super().__init__()
        self.token = PropertyDescriptor(
            name="API Token",
            description=("IQM Resonance API token. Leave blank to fall back to "
                         "the IQM_TOKEN environment variable."),
            required=False, default_value="", sensitive=True,
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
        self.server_url = PropertyDescriptor(
            name="Server URL", description="IQM Resonance base URL.",
            required=False, default_value=self.DEFAULT_SERVER_URL,
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
        self.job_id = PropertyDescriptor(
            name="Job ID", description="IQM job identifier.",
            required=True, default_value="${batch.job_id}",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
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
        self.descriptors = [self.token, self.server_url, self.job_id,
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
                         description=("Job failed or was cancelled, the manifest was "
                                      "unreadable, or the IQM API was unreachable."),
                         auto_terminated=False),
        ]

    def transform(self, context, flowFile):
        import requests  # noqa: PLC0415

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

        job_id = ((get(self.job_id) or "").strip()
                  or str(manifest.get("job_id") or "").strip())
        if not job_id:
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": "no job id to poll"})

        server = ((get(self.server_url) or "").strip()
                  or self.DEFAULT_SERVER_URL).rstrip("/")
        # Assignment order matters: an explicit property beats the environment,
        # so a stale IQM_TOKEN left in this long-lived process cannot win.
        token = _secret(get(self.token), "IQM_TOKEN")
        try:
            budget = int(get(self.poll_timeout))
            interval = int(get(self.poll_interval))
        except (TypeError, ValueError) as exc:
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": "bad numeric property: %s" % exc})

        base = "%s/api/v1/jobs/%s" % (server, job_id)
        session = requests.Session()
        session.headers.update({"Accept": "application/json",
                                "Authorization": "Bearer %s" % token})

        deadline = time.time() + budget
        status = None
        while True:
            try:
                response = session.get(base, timeout=30)
                if not response.ok:
                    raise ValueError("HTTP %s: %s" % (response.status_code,
                                                      response.text[:300]))
                status = str((response.json() or {}).get("status") or "").lower()
            except Exception as exc:  # noqa: BLE001
                return FlowFileTransformResult(
                    relationship="failure", contents=raw,
                    attributes={"batch.job_id": job_id,
                                "batch.error": "IQM poll failed: %s" % exc})
            if status in self.TERMINAL or time.time() + interval >= deadline:
                break
            time.sleep(interval)

        if status not in self.TERMINAL:
            return FlowFileTransformResult(
                relationship="pending", contents=raw,
                attributes={"batch.status": status or "unknown",
                            "batch.job_id": job_id})
        if status != "completed":
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.status": status, "batch.job_id": job_id,
                            "batch.error": "IQM job %s ended %s" % (job_id, status)})

        try:
            artifact = session.get(base + "/artifacts/measurement_counts", timeout=60)
            if not artifact.ok:
                raise ValueError("HTTP %s: %s" % (artifact.status_code,
                                                  artifact.text[:300]))
            merged = merge_counts(artifact.json(), entries)
        except Exception as exc:  # noqa: BLE001
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.job_id": job_id,
                            "batch.error": "could not extract IQM counts: %s" % exc})

        out = {"entries": merged, "job_id": job_id,
               "device": manifest.get("device", ""),
               "layout": manifest.get("layout"),
               "padded_width": manifest.get("padded_width"),
               # The Qrisp/IQM serialiser already emits the repo's canonical
               # order, so nothing is reversed; recorded so nobody has to guess.
               "bit_order": "q0_left",
               "estimated_usage_seconds": manifest.get("estimated_usage_seconds")}
        shots = sum(merged[0]["counts"].values()) if merged else 0
        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(out, indent=2).encode(),
            attributes={"batch.status": "completed", "batch.job_id": job_id,
                        "batch.size": str(len(merged)),
                        "batch.device": manifest.get("device", ""),
                        "batch.shots": str(shots),
                        "batch.bit_order": "q0_left",
                        "batch.estimated_usage_seconds":
                            str(manifest.get("estimated_usage_seconds", ""))})
