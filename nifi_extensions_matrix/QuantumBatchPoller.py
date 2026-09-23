import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder


def merge(manifest_entries, counts_payload):
    """Attach each circuit's histogram to its manifest entry, by index.

    The documented artifact shape is a batch -- a list of
    ``{"measurement_keys": [...], "counts": {bitstring: n}}`` in submission
    order, which is why the submitter records the manifest in that same order.
    A bare histogram is accepted for a one-circuit job so a schema change
    degrades into a clear error rather than a traceback.

    The manifest entry is the only place a circuit's ground truth survives the
    round trip, so anything the submitter recorded there (``attributes``,
    ``num_qubits``) is carried forward rather than dropped — same as
    QuantumIBMBatchPoller's extract_counts. Without it a polled result cannot
    be scored against the classical answer and the lane silently falls back to
    a distributional oracle.
    """
    if isinstance(counts_payload, dict):
        counts_payload = counts_payload.get("counts", counts_payload)
        if counts_payload and all(isinstance(v, int) for v in counts_payload.values()):
            counts_payload = [{"counts": counts_payload}]
    if not isinstance(counts_payload, list):
        raise ValueError("measurement_counts is neither a batch nor a histogram")
    if len(counts_payload) != len(manifest_entries):
        raise ValueError(
            "batch size mismatch: manifest has %d circuits, artifact has %d"
            % (len(manifest_entries), len(counts_payload)))
    merged = []
    for entry, item in zip(manifest_entries, counts_payload):
        counts = item.get("counts", item) if isinstance(item, dict) else item
        if not isinstance(counts, dict) or not counts:
            raise ValueError("circuit '%s' has no counts" % entry.get("label"))
        row = {"label": entry.get("label"), "kind": entry.get("kind"),
               "counts": counts}
        if entry.get("attributes"):
            row["attributes"] = entry["attributes"]
        if entry.get("num_qubits") is not None:
            row["num_qubits"] = entry["num_qubits"]
        merged.append(row)
    return merged


class QuantumBatchPoller(FlowFileTransform):
    """
    Polls an IQM Resonance batch job and emits every circuit's counts, merged
    with the submitter's manifest, in QuantumCalibratedOracle's input format.

    Companion to QuantumIQMBatchSubmitter. IQMJobPoller cannot be used here: it
    extracts a single circuit by index, because Quanifi previously submitted one
    circuit per FlowFile.

    Why polling rather than waiting: a synchronous submit-and-wait died after
    1h14m on a queued garnet job with a connection reset, losing results the QPU
    had already produced. Each invocation polls for a bounded time and then
    leaves on 'pending', which is wired back into this processor, so no NiFi
    thread is parked on a queue.

    Endpoints (Server API v1):
        GET /api/v1/jobs/{id}
        GET /api/v1/jobs/{id}/artifacts/measurement_counts

    Output relationships
    --------------------
    success  - job completed; body is {"entries": [{label, kind, counts}, ...]}.
    pending  - still queued or running; wire back into this processor.
    failure  - job failed or cancelled, or the API was unreachable.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Polls an IQM Resonance batch job and emits all circuits' measurement "
            "counts merged with the submitter's manifest, ready for "
            "QuantumCalibratedOracle. Routes to 'pending' while the job is queued "
            "— wire that back in to keep polling without blocking the canvas."
        )
        tags = ["quantum", "iqm", "resonance", "hardware", "batch", "polling", "rest"]
        dependencies = ["requests>=2.28"]

    TERMINAL = ("completed", "failed", "cancelled")

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.token = PropertyDescriptor(
            name="API Token",
            description="IQM Resonance API token. Use a sensitive parameter.",
            required=True, default_value="", sensitive=True,
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.job_id = PropertyDescriptor(
            name="Job ID",
            description="Expression Language; defaults to the submitter's attribute.",
            required=True, default_value="${batch.job_id}",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.server_url = PropertyDescriptor(
            name="Server URL",
            description="IQM server base URL for the REST API.",
            required=True, default_value="https://resonance.meetiqm.com",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.poll_timeout = PropertyDescriptor(
            name="Poll Timeout Seconds",
            description=("How long ONE invocation polls before giving up and "
                         "routing to 'pending'. Keep it short; the loopback does "
                         "the waiting, not a parked thread."),
            required=True, default_value="30",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
        )
        self.poll_interval = PropertyDescriptor(
            name="Poll Interval Seconds",
            description="Delay between polls within one invocation.",
            required=True, default_value="5",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
        )
        self.descriptors = [self.token, self.job_id, self.server_url,
                            self.poll_timeout, self.poll_interval]

    def getPropertyDescriptors(self):
        return self.descriptors

    def getRelationships(self):
        return [
            Relationship(name="success", description="Job completed; entries emitted.",
                         auto_terminated=False),
            Relationship(name="pending", description="Still queued or running; "
                         "wire back into this processor.", auto_terminated=False),
            Relationship(name="failure", description="Job failed/cancelled or API "
                         "unreachable.", auto_terminated=False),
        ]

    def _get(self, session, url, timeout=30):
        return session.get(url, timeout=timeout)

    def transform(self, context, flowFile):
        import requests  # noqa: PLC0415 - declared in ProcessorDetails.dependencies

        def get(prop):
            return context.getProperty(prop).evaluateAttributeExpressions(flowFile).getValue()

        raw = bytes(flowFile.getContentsAsBytes())
        try:
            manifest = json.loads(raw.decode("utf-8"))
            entries = manifest["entries"]
        except Exception as exc:  # noqa: BLE001
            msg = "body is not a batch manifest: {}".format(exc)
            self.logger.error("QuantumBatchPoller: {}".format(msg))
            return FlowFileTransformResult(relationship="failure", contents=raw,
                                           attributes={"batch.error": msg})

        job_id = (get(self.job_id) or manifest.get("job_id") or "").strip()
        server = (get(self.server_url) or manifest.get("server") or "").rstrip("/")
        token = get(self.token) or ""
        timeout = int(context.getProperty(self.poll_timeout).getValue())
        interval = int(context.getProperty(self.poll_interval).getValue())

        session = requests.Session()
        session.headers.update({"Authorization": "Bearer %s" % token,
                                "Accept": "application/json"})
        base = "%s/api/v1/jobs/%s" % (server, job_id)

        deadline = time.time() + timeout
        status = None
        while True:
            try:
                response = self._get(session, base)
            except Exception as exc:  # noqa: BLE001 - network flake is retryable
                self.logger.warn("QuantumBatchPoller: {} unreachable: {}".format(
                    base, exc))
                return FlowFileTransformResult(
                    relationship="pending", contents=raw,
                    attributes={"batch.status": "unreachable",
                                "batch.error": str(exc)})
            if response.status_code != 200:
                msg = "HTTP {} polling job {}: {}".format(
                    response.status_code, job_id, response.text[:300])
                self.logger.error("QuantumBatchPoller: {}".format(msg))
                return FlowFileTransformResult(
                    relationship="failure", contents=raw,
                    attributes={"batch.error": msg,
                                "batch.http_status": str(response.status_code)})
            body = response.json()
            status = str((body.get("status") if isinstance(body, dict) else "")
                         or "").lower()
            if status in self.TERMINAL or time.time() + interval >= deadline:
                break
            time.sleep(interval)

        if status not in self.TERMINAL:
            self.logger.warn("QuantumBatchPoller: job {} still '{}'; pending".format(
                job_id, status or "unknown"))
            return FlowFileTransformResult(
                relationship="pending", contents=raw,
                attributes={"batch.status": status or "unknown",
                            "batch.job_id": job_id})

        if status != "completed":
            msg = "job {} ended '{}'".format(job_id, status)
            self.logger.error("QuantumBatchPoller: {}".format(msg))
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": msg, "batch.status": status})

        artifact = self._get(session, base + "/artifacts/measurement_counts", 60)
        if artifact.status_code != 200:
            msg = "HTTP {} fetching measurement_counts for {}".format(
                artifact.status_code, job_id)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": msg})
        try:
            merged = merge(entries, artifact.json())
        except Exception as exc:  # noqa: BLE001
            msg = "could not merge counts onto the manifest: {}".format(exc)
            self.logger.error("QuantumBatchPoller: {}".format(msg))
            return FlowFileTransformResult(relationship="failure", contents=raw,
                                           attributes={"batch.error": msg})

        shots = sum(next(iter(merged))["counts"].values()) if merged else 0
        out = {"entries": merged, "job_id": job_id,
               "device": manifest.get("device", ""), "layout": manifest.get("layout"),
               # The IQM serializer already emits q0_left keys. Recorded so
               # QuantumBatchResultExpander does not fall back to its q0_right
               # default and mirror every key (which reads as 0% success).
               "bit_order": "q0_left"}
        attrs = {"batch.status": "completed", "batch.job_id": job_id,
                 "batch.size": str(len(merged)),
                 "batch.device": manifest.get("device", ""),
                 "batch.shots": str(shots),
                 "batch.bit_order": "q0_left"}
        self.logger.warn("QuantumBatchPoller: job {} completed; {} circuits".format(
            job_id, len(merged)))
        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(out, indent=2).encode("utf-8"), attributes=attrs)
