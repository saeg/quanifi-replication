import email.utils
import json
import time

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder


class IQMJobPoller(FlowFileTransform):
    """
    Polls the IQM Resonance REST API for a job submitted earlier (typically by
    QrispIQMDevice with Wait For Results = false) and emits its measurement
    counts once the job completes.

    Two endpoints of the IQM Server API (v1) are used:

        GET /api/v1/jobs/{job_id}
            -> {"status": "waiting"|"processing"|"completed"|"failed"|
                          "cancelled", "queue_position": int|null, ...}
        GET /api/v1/jobs/{job_id}/artifacts/measurement_counts
            -> [{"measurement_keys": [...], "counts": {"010": 37, ...}}, ...]

    Each invocation polls for at most Poll Timeout Seconds, honouring the
    server's Retry-After response header (see the response-headers section of
    https://resonance.iqm.tech/docs/api-reference) and falling back to Poll
    Interval Seconds when the header is absent. That bounded budget is what
    makes this safe on a canvas: a job still queued when the budget runs out
    leaves on the 'pending' relationship, which is normally wired back into
    this processor, so a multi-hour QPU queue costs many cheap passes instead
    of one thread parked for hours.

    Relationships:
        success   job completed; content is the counts JSON, sim.* attributes set
        pending   job still waiting/processing; loop this back into the processor
        failure   job failed/cancelled, or the API could not be reached
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Polls the IQM Resonance REST API (GET /api/v1/jobs/{id}) for the job "
            "in hw.job_id, honouring the Retry-After header, and emits measurement "
            "counts when it completes. Routes to 'pending' while the job is still "
            "queued or running — wire that back into this processor to keep "
            "polling without blocking the canvas."
        )
        tags = ["quantum", "iqm", "resonance", "hardware", "qpu", "polling", "rest"]
        dependencies = ["requests>=2.28"]

    DEFAULT_SERVER_URL = "https://resonance.meetiqm.com"
    #: Statuses from which execution cannot continue (IQM Server JobStatus).
    TERMINAL = ("completed", "failed", "cancelled")

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.server_url = PropertyDescriptor(
            name="Server URL",
            description=(
                "IQM server root URL. Empty uses hw.server_url from the FlowFile, "
                "then IQM Resonance (https://resonance.meetiqm.com)."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.token = PropertyDescriptor(
            name="API Token",
            description="IQM Resonance API token, sent as 'Authorization: Bearer <token>'.",
            required=True,
            sensitive=True,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.job_id = PropertyDescriptor(
            name="Job ID",
            description="ID of the job to poll. Defaults to the hw.job_id attribute.",
            required=True,
            default_value="${hw.job_id}",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.poll_timeout = PropertyDescriptor(
            name="Poll Timeout Seconds",
            description=(
                "Per-invocation polling budget. When it runs out and the job is "
                "still not terminal, the FlowFile leaves on 'pending'."
            ),
            required=True,
            default_value="60",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.poll_interval = PropertyDescriptor(
            name="Poll Interval Seconds",
            description="Seconds between polls when the server sends no Retry-After header.",
            required=True,
            default_value="5",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.max_poll_interval = PropertyDescriptor(
            name="Max Poll Interval Seconds",
            description=(
                "Upper bound applied to a server-supplied Retry-After, so an "
                "unusually long value cannot park the NiFi thread."
            ),
            required=True,
            default_value="60",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.circuit_index = PropertyDescriptor(
            name="Circuit Index",
            description=(
                "Index of the circuit within the submitted batch whose counts to "
                "emit. Quanifi submits one circuit per FlowFile, so 0 is right."
            ),
            required=True,
            default_value="0",
            validators=[StandardValidators.NON_NEGATIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.reverse_bits = PropertyDescriptor(
            name="Reverse Bit Order",
            description=(
                "false (default): the API's bitstrings already put clbit 0 "
                "leftmost, matching Quanifi's canonical q0_left order — this is "
                "what the Qrisp/IQM serializer produces (one measurement key per "
                "classical bit, concatenated in ascending key order). Set true if "
                "a different submitter measured into a register whose bitstrings "
                "come back little-endian."
            ),
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
        )
        self.descriptors = [self.server_url, self.token, self.job_id,
                            self.poll_timeout, self.poll_interval,
                            self.max_poll_interval, self.circuit_index,
                            self.reverse_bits]

    def getPropertyDescriptors(self):
        return self.descriptors

    def getRelationships(self):
        return [
            Relationship(
                name="success",
                description="Job completed; content is the measurement counts JSON.",
                auto_terminated=False,
            ),
            Relationship(
                name="pending",
                description=(
                    "Job is still waiting or processing. Loop this back into the "
                    "processor to keep polling."
                ),
                auto_terminated=False,
            ),
            Relationship(
                name="failure",
                description="Job failed or was cancelled, or the IQM API was unreachable.",
                auto_terminated=False,
            ),
        ]

    # --- HTTP helpers -------------------------------------------------------

    @staticmethod
    def _retry_after_seconds(response, default, cap):
        """Seconds to wait before the next poll, per the Retry-After header.

        RFC 9110 allows either a delay in seconds or an HTTP-date; accept both,
        ignore anything unparseable, and clamp to [0, cap] so a hostile or
        mistaken value cannot park the NiFi thread.
        """
        raw = (response.headers.get("Retry-After") or "").strip()
        wait = default
        if raw:
            try:
                wait = float(raw)
            except ValueError:
                parsed = email.utils.parsedate_to_datetime(raw)
                if parsed is not None:
                    wait = parsed.timestamp() - time.time()
        # Floor at 0.1s so a "retry now" answer cannot turn into a hot loop
        # against the API; cap so an outsized one cannot park the NiFi thread.
        return max(0.1, min(float(wait), float(cap)))

    def _get(self, session, url, timeout=30):
        return session.get(url, timeout=timeout)

    @staticmethod
    def _extract_counts(payload, index):
        """Pull one circuit's histogram out of a measurement_counts artifact.

        The documented shape is a batch — a list of CircuitMeasurementCounts,
        each ``{"measurement_keys": [...], "counts": {bitstring: n}}``. Accept a
        bare object or a bare histogram too, so a schema tweak on IQM's side
        degrades into a clear error rather than a traceback.
        """
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list) or not payload:
            raise ValueError("measurement_counts artifact is empty or not a batch")
        if index >= len(payload):
            raise ValueError(
                "Circuit Index {} is out of range: the batch holds {} circuit(s)".format(
                    index, len(payload)))
        entry = payload[index]
        counts = entry.get("counts", entry) if isinstance(entry, dict) else None
        if not isinstance(counts, dict) or not counts:
            raise ValueError("no counts in measurement_counts entry {}".format(index))
        return counts

    def transform(self, context, flowFile):
        import requests

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        server = ((get(self.server_url) or "").strip()
                  or (flowFile.getAttribute("hw.server_url") or "").strip()
                  or self.DEFAULT_SERVER_URL).rstrip("/")
        token = (get(self.token) or "").strip()
        job_id = (get(self.job_id) or "").strip()
        try:
            budget = int(get(self.poll_timeout))
            interval = int(get(self.poll_interval))
            max_interval = int(get(self.max_poll_interval))
            index = int(get(self.circuit_index))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("IQMJobPoller: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"hw.error": msg},
            )
        reverse = (get(self.reverse_bits) or "false").lower() == "true"

        passthrough = bytes(flowFile.getContentsAsBytes())
        attempts = int(flowFile.getAttribute("hw.poll_attempts") or 0)

        if not job_id:
            msg = ("No job id to poll: set the Job ID property or make sure the "
                   "upstream processor emitted hw.job_id.")
            self.logger.error("IQMJobPoller: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=passthrough,
                attributes={"hw.error": msg},
            )

        base = "{}/api/v1/jobs/{}".format(server, job_id)
        session = requests.Session()
        session.headers.update({
            "Accept": "application/json",
            "Authorization": "Bearer {}".format(token),
        })

        deadline = time.time() + budget
        status, queue_position = None, None

        while True:
            attempts += 1
            try:
                response = self._get(session, base)
            except Exception as exc:
                msg = "Could not reach the IQM API at {}: {}".format(base, exc)
                self.logger.error("IQMJobPoller: " + msg)
                return FlowFileTransformResult(
                    relationship="failure", contents=passthrough,
                    attributes={"hw.error": msg, "hw.poll_attempts": str(attempts)},
                )

            # 429/503 are "ask again later", not failures: the API tells us when.
            if response.status_code in (429, 503):
                wait = self._retry_after_seconds(response, interval, max_interval)
            elif not response.ok:
                msg = "IQM API returned {} for job {}: {}".format(
                    response.status_code, job_id, response.text[:400])
                self.logger.error("IQMJobPoller: " + msg)
                return FlowFileTransformResult(
                    relationship="failure", contents=passthrough,
                    attributes={"hw.error": msg, "hw.http_status": str(response.status_code),
                                "hw.poll_attempts": str(attempts)},
                )
            else:
                try:
                    job = response.json()
                except ValueError as exc:
                    msg = "IQM API sent a non-JSON job response: {}".format(exc)
                    self.logger.error("IQMJobPoller: " + msg)
                    return FlowFileTransformResult(
                        relationship="failure", contents=passthrough,
                        attributes={"hw.error": msg, "hw.poll_attempts": str(attempts)},
                    )
                status = str(job.get("status") or "").lower()
                queue_position = job.get("queue_position")

                if status in self.TERMINAL:
                    break
                wait = self._retry_after_seconds(response, interval, max_interval)

            if time.time() + wait >= deadline:
                break
            time.sleep(wait)

        progress = {
            "hw.provider": "iqm-resonance",
            "hw.job_id": job_id,
            "hw.server_url": server,
            "hw.status": status or "unknown",
            "hw.poll_attempts": str(attempts),
            **({"hw.queue_position": str(queue_position)}
               if queue_position is not None else {}),
        }

        if status not in self.TERMINAL:
            self.logger.info(
                "IQMJobPoller: job {} still '{}' after {} poll(s); routing to pending".format(
                    job_id, status or "unknown", attempts))
            return FlowFileTransformResult(
                relationship="pending", contents=passthrough, attributes=progress,
            )

        if status != "completed":
            errors = job.get("errors") or []
            detail = "; ".join(str(e) for e in errors) if errors else "no detail given"
            msg = "IQM job {} ended '{}': {}".format(job_id, status, detail)
            self.logger.error("IQMJobPoller: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=passthrough,
                attributes={**progress, "hw.error": msg},
            )

        try:
            artifact = self._get(session, base + "/artifacts/measurement_counts")
            if not artifact.ok:
                raise ValueError("HTTP {}: {}".format(artifact.status_code,
                                                      artifact.text[:400]))
            counts = self._extract_counts(artifact.json(), index)
        except Exception as exc:
            msg = "Job {} completed but its counts could not be read: {}".format(job_id, exc)
            self.logger.error("IQMJobPoller: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=passthrough,
                attributes={**progress, "hw.error": msg},
            )

        merged = {}
        for key, count in counts.items():
            k = str(key).replace(" ", "")
            if reverse:
                k = k[::-1]
            merged[k] = merged.get(k, 0) + int(count)
        sorted_counts = dict(sorted(merged.items(), key=lambda kv: kv[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))
        shots = sum(sorted_counts.values())

        self.logger.warn(
            "IQMJobPoller: job {} completed after {} poll(s); top |{}> p={:.4f}".format(
                job_id, attempts, top_state, top_count / shots))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes={
                **progress,
                "sim.shots": str(shots),
                "sim.top_result": top_state,
                "sim.top_probability": "{:.4f}".format(top_count / shots),
                "sim.framework": "qrisp",
                "sim.bit_order": "q0_left",
                "report.type": "simulation",
            },
        )
