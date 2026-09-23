import json
import os
import sys
import html as html_lib
import hashlib
import re
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reporting import page_template, write_card, now  # noqa: E402
from QuantumDistributionComparison import (  # noqa: E402
    _chi2_two_sample, _hellinger, _normalize,
)

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder

_EXTRA_CSS = """\
    .badge { display:inline-block; padding:2px 10px; border-radius:12px;
             font-size:0.78rem; font-weight:600; font-family:monospace; }
    .verdict-pass { background:#dafbe1; color:#1a7f37; }
    .verdict-fail { background:#ffebe9; color:#cf222e; }
    .killed   { color:#1a7f37; font-weight:600; }
    .survived { color:#9a6700; font-weight:600; }
    .alarm    { color:#cf222e; font-weight:600; }
    .metric-val { color:#1f2328 !important; }"""


# ---------------------------------------------------------------------------
# Calibrated scoring
#
# On a QPU the oracle's decision threshold cannot be a constant. Measured null
# floors for the same GHZ-16 circuit at 662 shots: 0.020 on Aer, 0.3309 on
# ibm_kingston, 0.4115 on iqm_emerald -- against the 0.1 the manuscript used for
# noiseless simulators. A fixed 0.1 flags all three *correct* implementations as
# defective on both devices.
#
# So the threshold is derived from control replicates that rode in the SAME job
# as the circuits being judged, which is also why the submitter batches them
# together: calibration taken from a different job does not describe this one,
# because device calibration drifts.
# ---------------------------------------------------------------------------

def _pairwise_hellinger(entries):
    out = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            out.append(_hellinger(_normalize(entries[i]["counts"]),
                                  _normalize(entries[j]["counts"])))
    return out


def _calibrate(nulls, quantile=0.95):
    """Threshold = `quantile` of pairwise Hellinger among control replicates."""
    distances = _pairwise_hellinger(nulls)
    if not distances:
        return None, []
    ordered = sorted(distances)
    return ordered[min(len(ordered) - 1, int(quantile * len(ordered)))], distances


def _judge(counts_a, counts_b, threshold, alpha):
    """Two-gate rule: chi-squared rejection AND effect size over threshold.

    Hellinger alone is not enough -- on ibm_kingston two `rotation.perturb`
    comparisons cleared the threshold by 0.003 with chi-squared p of 0.196 and
    0.710, i.e. pure noise, and a one-gate rule scored them as kills.
    """
    hellinger = _hellinger(_normalize(counts_a), _normalize(counts_b))
    chi = _chi2_two_sample(counts_a, counts_b)
    p_value = chi[2] if chi else None
    significant = p_value is not None and p_value < alpha
    return hellinger, p_value, (hellinger > threshold and significant)


def score_batch(entries, alpha=0.05, quantile=0.95, expected_support=None):
    """Calibrate on the nulls, then score controls and mutants.

    `entries`: [{"label", "kind", "counts"}]. kind is "null" for a calibration
    replicate, "control" for a correct framework version, anything else for a
    mutant (the string names the operator).
    """
    nulls = [e for e in entries if e.get("kind") == "null"]
    controls = [e for e in entries if e.get("kind") == "control"]
    mutants = [e for e in entries if e.get("kind") not in ("null", "control")]

    threshold, distances = _calibrate(nulls, quantile)
    if threshold is None:
        return {"verdict": "FAIL", "reason": "need at least 2 null replicates to "
                "calibrate a threshold; got %d" % len(nulls), "threshold": None,
                "rows": [], "false_alarms": 0, "killed": 0, "survived": 0,
                "null_median": None, "replicates": len(nulls)}

    rows = []
    false_alarms = 0
    for i in range(len(controls)):
        for j in range(i + 1, len(controls)):
            h, p, diverged = _judge(controls[i]["counts"], controls[j]["counts"],
                                    threshold, alpha)
            if diverged:
                false_alarms += 1
            rows.append({"comparison": "%s vs %s" % (controls[i]["label"],
                                                     controls[j]["label"]),
                         "kind": "control", "hellinger": round(h, 4),
                         "chi2_p": None if p is None else round(p, 6),
                         "verdict": "DISAGREE (false alarm)" if diverged else "AGREE"})

    killed = survived = 0
    for mutant in mutants:
        for control in controls:
            # A mutant derived from framework X is compared against the OTHER
            # frameworks: comparing it to its own control would be a
            # single-framework oracle, not the N-version one.
            if control["label"] and control["label"] in mutant["label"]:
                continue
            h, p, diverged = _judge(control["counts"], mutant["counts"],
                                    threshold, alpha)
            killed += diverged
            survived += not diverged
            rows.append({"comparison": "%s vs %s" % (control["label"], mutant["label"]),
                         "kind": mutant["kind"], "hellinger": round(h, 4),
                         "chi2_p": None if p is None else round(p, 6),
                         "verdict": "KILLED" if diverged else "SURVIVED"})

    support_ok = True
    if expected_support and controls:
        observed = set()
        for control in controls:
            dist = _normalize(control["counts"])
            observed |= {s for s, prob in dist.items() if prob >= 0.01}
        support_ok = not (set(expected_support) - observed)

    if false_alarms:
        verdict, reason = "FAIL", (
            "%d of %d control pairs diverge at the calibrated threshold %.4f -- "
            "the correct versions disagree, so the oracle is not trustworthy here"
            % (false_alarms, len(controls) * (len(controls) - 1) // 2, threshold))
    elif not support_ok:
        verdict, reason = "FAIL", (
            "controls agree but their support misses expected states %s"
            % sorted(set(expected_support) - observed))
    else:
        verdict, reason = "PASS", (
            "all %d control pairs agree at threshold %.4f; %d/%d mutant "
            "comparisons killed" % (len(controls) * (len(controls) - 1) // 2,
                                    threshold, killed, killed + survived))

    ordered = sorted(distances)
    return {"verdict": verdict, "reason": reason, "threshold": threshold,
            "null_median": ordered[len(ordered) // 2], "replicates": len(nulls),
            "null_pairs": len(distances), "rows": rows,
            "false_alarms": false_alarms, "killed": killed, "survived": survived}


# ---------------------------------------------------------------------------

class QuantumCalibratedOracle(FlowFileTransform):
    """
    Scores a whole hardware batch: derives the agreement threshold from control
    replicates carried in the same job, then judges every control and mutant
    comparison against it.

    Use this instead of a statically configured threshold whenever the branches
    executed on a QPU. Measured null floors for one GHZ-16 circuit at 662 shots
    were 0.020 (Aer), 0.3309 (ibm_kingston) and 0.4115 (iqm_emerald); the 0.1
    tuned on noiseless simulators flags every correct version as defective on
    both devices.

    Input
    -----
    One FlowFile whose body is ``{"entries": [{"label", "kind", "counts"}, ...]}``
    as emitted by a batch submitter/poller. ``kind`` is ``null`` for a
    calibration replicate, ``control`` for a correct version, or the mutation
    operator name for a mutant.

    Output relationships
    --------------------
    pass     - every control pair agrees at the calibrated threshold.
    fail     - a control pair diverges (the oracle cannot be trusted on this
               batch) or the agreed support is wrong.
    failure  - malformed input.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.2.0"
        description = (
            "Scores a hardware batch with a threshold calibrated from control "
            "replicates in the same job, instead of a fixed value. Applies the "
            "two-gate kill rule (chi-squared rejection AND effect size) to every "
            "control and mutant comparison, reports false alarms and the mutation "
            "score, persists the raw batch counts, and writes an HTML card. "
            "Pair with a batch submitter/poller."
        )
        tags = ["quantum", "oracle", "calibration", "hardware", "mutation",
                "n-version", "hellinger", "report"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.reports_dir = PropertyDescriptor(
            name="Reports Directory",
            description="Folder where the HTML report is written.",
            required=True,
            default_value="reports",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.flow_name = PropertyDescriptor(
            name="Flow Name",
            description="Report filename: {flow_name}-calibrated-oracle.html.",
            required=True,
            default_value="quanifi",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.raw_results_dir = PropertyDescriptor(
            name="Raw Results Directory",
            description=(
                "Folder where the validated poller payload is persisted before "
                "scoring. Each JSON artifact contains the untouched entries and "
                "batch metadata, so a hardware run can be replayed offline."
            ),
            required=True,
            default_value="reports/raw-hardware-batches",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.alpha = PropertyDescriptor(
            name="Significance Level",
            description=(
                "Alpha for the chi-squared homogeneity gate. A pair only counts "
                "as divergent when p < alpha AND the Hellinger distance exceeds "
                "the calibrated threshold."
            ),
            required=True,
            default_value="0.05",
            validators=[StandardValidators.NUMBER_VALIDATOR],
        )
        self.quantile = PropertyDescriptor(
            name="Threshold Quantile",
            description=(
                "Which quantile of the pairwise null distribution becomes the "
                "threshold. 0.95 gives roughly a 5% false-alarm rate."
            ),
            required=True,
            default_value="0.95",
            validators=[StandardValidators.NUMBER_VALIDATOR],
        )
        self.min_replicates = PropertyDescriptor(
            name="Minimum Replicates",
            description=(
                "Refuse to score a batch with fewer control replicates than this. "
                "Two is the bare minimum for one pairwise distance; ten gives 45 "
                "pairs and a stable quantile."
            ),
            required=True,
            default_value="4",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
        )
        self.expected_support = PropertyDescriptor(
            name="Expected Support",
            description=(
                "Optional comma-separated bitstrings the agreed distribution "
                "should cover -- for GHZ-3, '000,111'. Checked as a SET, which is "
                "what makes ground truth meaningful for degenerate outputs."
            ),
            required=False,
            default_value="${test.expected_support}",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.reports_dir, self.flow_name, self.raw_results_dir, self.alpha,
            self.quantile, self.min_replicates, self.expected_support,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def getRelationships(self):
        return [
            Relationship(name="pass", description="All control pairs agree.",
                         auto_terminated=False),
            Relationship(name="fail", description="A control pair diverged, or "
                         "the support is wrong.", auto_terminated=False),
            Relationship(name="failure", description="Malformed batch input.",
                         auto_terminated=False),
        ]

    def transform(self, context, flowFile):
        def get(prop):
            return context.getProperty(prop).evaluateAttributeExpressions(flowFile).getValue()

        reports_dir = context.getProperty(self.reports_dir).getValue()
        flow_name = context.getProperty(self.flow_name).getValue()
        raw_results_dir = get(self.raw_results_dir)
        try:
            alpha = float(context.getProperty(self.alpha).getValue())
        except (TypeError, ValueError):
            alpha = 0.05
        try:
            quantile = float(context.getProperty(self.quantile).getValue())
        except (TypeError, ValueError):
            quantile = 0.95
        try:
            min_reps = int(context.getProperty(self.min_replicates).getValue())
        except (TypeError, ValueError):
            min_reps = 4
        expected = [s.strip() for s in (get(self.expected_support) or "").split(",")
                    if s.strip()]

        raw = bytes(flowFile.getContentsAsBytes())
        try:
            payload = json.loads(raw.decode("utf-8"))
            entries = payload["entries"] if isinstance(payload, dict) else payload
            if not isinstance(entries, list) or not entries:
                raise ValueError("expected a non-empty 'entries' list")
            for e in entries:
                if "counts" not in e or "kind" not in e:
                    raise ValueError("each entry needs 'kind' and 'counts'")
        except Exception as exc:  # noqa: BLE001 - report any malformed shape
            self.logger.error("QuantumCalibratedOracle: bad batch input: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"oracle.error": "bad batch input: {}".format(exc)})

        attrs = dict(flowFile.getAttributes()) if flowFile.getAttributes() else {}
        try:
            raw_path = self._persist_raw_batch(
                raw_results_dir, payload, entries, attrs, raw)
        except Exception as exc:  # noqa: BLE001 - never silently lose QPU results
            msg = "could not persist raw batch counts: {}".format(exc)
            self.logger.error("QuantumCalibratedOracle: {}".format(msg))
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"oracle.error": msg})

        nulls = sum(1 for e in entries if e.get("kind") == "null")
        if nulls < min_reps:
            msg = ("batch has %d null replicates, Minimum Replicates is %d -- "
                   "refusing to calibrate on too few" % (nulls, min_reps))
            self.logger.error("QuantumCalibratedOracle: {}".format(msg))
            return FlowFileTransformResult(relationship="failure", contents=raw,
                                           attributes={"oracle.error": msg})

        res = score_batch(entries, alpha=alpha, quantile=quantile,
                          expected_support=expected or None)

        out_attrs = {
            "report.type": "consensus",
            "assert.verdict": res["verdict"],
            "assert.reason": res["reason"],
            "oracle.mode": "calibrated",
            "oracle.threshold": "%.4f" % res["threshold"],
            "oracle.null_median": "%.4f" % res["null_median"],
            "oracle.replicates": str(res["replicates"]),
            "oracle.null_pairs": str(res["null_pairs"]),
            "oracle.false_alarms": str(res["false_alarms"]),
            "oracle.killed": str(res["killed"]),
            "oracle.survived": str(res["survived"]),
            "oracle.mutation_score": ("%.4f" % (res["killed"] /
                                                (res["killed"] + res["survived"]))
                                      if res["killed"] + res["survived"] else ""),
            "oracle.alpha": "%g" % alpha,
            "oracle.raw_results_path": raw_path,
        }
        for key in ("batch.device", "batch.job_id", "batch.shots",
                    "test.run_id", "test.case_id"):
            if key in attrs:
                out_attrs[key] = attrs[key]

        self.logger.warn(
            "QuantumCalibratedOracle: {} threshold={:.4f} (null median {:.4f}, "
            "{} reps) false_alarms={} killed={} survived={}".format(
                res["verdict"], res["threshold"], res["null_median"],
                res["replicates"], res["false_alarms"], res["killed"],
                res["survived"]))

        self._write_report(reports_dir, flow_name, res, attrs, alpha)
        relationship = "pass" if res["verdict"] == "PASS" else "fail"
        return FlowFileTransformResult(
            relationship=relationship,
            contents=json.dumps(res, indent=2).encode("utf-8"),
            attributes=out_attrs)

    @staticmethod
    def _persist_raw_batch(raw_results_dir, payload, entries, attrs, raw):
        """Atomically preserve the exact counts consumed by the oracle.

        The content hash makes unnamed batches collision-resistant and makes
        retries idempotent. Vendor identifiers are sanitized before becoming a
        filename, so FlowFile attributes cannot escape the configured folder.
        """
        if not raw_results_dir:
            raise ValueError("Raw Results Directory is empty")
        os.makedirs(raw_results_dir, exist_ok=True)

        identity = (attrs.get("batch.job_id") or attrs.get("test.run_id") or
                    hashlib.sha256(raw).hexdigest()[:16])
        device = attrs.get("batch.device", "unknown-device")

        def safe(value):
            value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip(".-")
            return value[:100] or "unknown"

        digest = hashlib.sha256(raw).hexdigest()[:12]
        filename = "{}-{}-{}.json".format(
            safe(device), safe(identity), digest)
        path = os.path.join(raw_results_dir, filename)
        artifact = {
            "schema": "quanifi.hardware-batch.v1",
            "batch": {key: value for key, value in attrs.items()
                      if key.startswith("batch.") or key.startswith("test.")},
            "entries": entries,
        }
        if isinstance(payload, dict):
            artifact["poller"] = {key: value for key, value in payload.items()
                                  if key != "entries"}

        fd, temporary = tempfile.mkstemp(
            prefix=".{}-".format(filename), suffix=".tmp",
            dir=raw_results_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(artifact, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return path

    def _write_report(self, reports_dir, flow_name, res, attrs, alpha):
        os.makedirs(reports_dir, exist_ok=True)
        path = os.path.join(reports_dir, "%s-calibrated-oracle.html" % flow_name)
        css = "verdict-pass" if res["verdict"] == "PASS" else "verdict-fail"
        icon = "&#x2713;" if res["verdict"] == "PASS" else "&#x2717;"

        def row_class(r):
            return {"AGREE": "", "DISAGREE (false alarm)": "alarm",
                    "KILLED": "killed", "SURVIVED": "survived"}.get(r["verdict"], "")

        rows = "".join(
            "<tr><td>{}</td><td>{}</td><td class='metric-val'>{:.4f}</td>"
            "<td>{}</td><td class='{}'>{}</td></tr>".format(
                html_lib.escape(r["comparison"]), html_lib.escape(r["kind"]),
                r["hellinger"], "&mdash;" if r["chi2_p"] is None else
                "{:.4g}".format(r["chi2_p"]), row_class(r),
                html_lib.escape(r["verdict"]))
            for r in res["rows"])

        card = """\
<section class="run-card">
  <header class="run-header">
    <span class="run-title">
      <span class="badge {css}">{icon} {verdict}</span>
      &nbsp;&middot;&nbsp; {device}
      &nbsp;&middot;&nbsp; threshold {threshold:.4f}
    </span>
    <span class="run-time">{ts}</span>
  </header>
  <div class="run-body"><div class="panel">
    <p style="color:#8c959f;font-size:0.85rem;margin:0 0 12px">{reason}</p>
    <table>
      <tr><th>Setting</th><th>Value</th><th>Meaning</th></tr>
      <tr><td>Calibrated threshold</td><td class='metric-val'>{threshold:.4f}</td>
          <td>from {reps} replicates ({pairs} pairs) in this job</td></tr>
      <tr><td>Null median</td><td class='metric-val'>{nullmed:.4f}</td>
          <td>device noise floor; the manuscript's fixed value is 0.1</td></tr>
      <tr><td>Significance level</td><td class='metric-val'>{alpha:g}</td>
          <td>chi-squared gate; both gates must fire</td></tr>
      <tr><td>Mutation score</td><td class='metric-val'>{killed}/{total}</td>
          <td>two-gate kills</td></tr>
      <tr><td>False alarms</td><td class='metric-val'>{alarms}</td>
          <td>correct versions wrongly flagged</td></tr>
    </table>
    <h3 style="margin-top:18px">Comparisons</h3>
    <table>
      <tr><th>Pair</th><th>Kind</th><th>Hellinger</th><th>&chi;&sup2; p</th><th>Verdict</th></tr>
      {rows}
    </table>
  </div></div>
</section>""".format(
            css=css, icon=icon, verdict=html_lib.escape(res["verdict"]),
            device=html_lib.escape(attrs.get("batch.device", "&mdash;")),
            threshold=res["threshold"], ts=now(),
            reason=html_lib.escape(res["reason"]), reps=res["replicates"],
            pairs=res["null_pairs"], nullmed=res["null_median"], alpha=alpha,
            killed=res["killed"], total=res["killed"] + res["survived"],
            alarms=res["false_alarms"], rows=rows)

        write_card(path, card, page_template("%s — calibrated oracle" % flow_name,
                                             _EXTRA_CSS))
