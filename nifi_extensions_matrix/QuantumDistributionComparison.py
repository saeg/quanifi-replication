import json
import math
import os
import sys
import html as html_lib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reporting import page_template, write_card, bar_rows, now  # noqa: E402

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder

_EXTRA_CSS = """\
    .panel + .panel { border-left: 1px solid #d0d7de; }
    .badge {
      display: inline-block;
      padding: 2px 10px;
      border-radius: 12px;
      font-size: 0.78rem;
      font-weight: 600;
      font-family: monospace;
    }
    .badge-agree  { background: #dafbe1; color: #1a7f37; }
    .badge-differ { background: #ffebe9; color: #cf222e; }
    .badge-warn   { background: #fff8c5; color: #9a6700; }
    .badge-na     { background: #d0d7de; color: #656d76; }
    .metric-val   { color: #1f2328 !important; }
    .tag-a { color: #0969da; }
    .tag-b { color: #1a7f37; }
    .bar-fill-a { background: #0969da; height: 100%; border-radius: 4px; }
    .bar-fill-b { background: #1a7f37; height: 100%; border-radius: 4px; }"""


# ---------------------------------------------------------------------------
# Distribution metrics
# ---------------------------------------------------------------------------

def _normalize(dist):
    total = sum(dist.values())
    if total == 0:
        return dict(dist)
    return {k: v / total for k, v in dist.items()}


def _hellinger(p, q):
    keys = set(p) | set(q)
    return math.sqrt(
        sum((math.sqrt(p.get(k, 0.0)) - math.sqrt(q.get(k, 0.0))) ** 2 for k in keys) / 2
    )


def _total_variation(p, q):
    keys = set(p) | set(q)
    return sum(abs(p.get(k, 0.0) - q.get(k, 0.0)) for k in keys) / 2


def _fidelity(p, q):
    keys = set(p) | set(q)
    bc = sum(math.sqrt(p.get(k, 0.0) * q.get(k, 0.0)) for k in keys)
    return bc ** 2


# ---------------------------------------------------------------------------
# Chi-squared two-sample homogeneity test (pure Python, no scipy)
#
# The distance metrics above are effect sizes: they say how far apart two
# empirical distributions are, but not whether that gap is explainable by
# shot noise. The chi-squared test supplies that inference on the RAW counts.
# ---------------------------------------------------------------------------

def _gamma_p_series(a, x, itmax=300, eps=3e-12):
    """Regularized lower incomplete gamma P(a,x) by series expansion (x < a+1)."""
    gln = math.lgamma(a)
    ap = a
    s = 1.0 / a
    delt = s
    for _ in range(itmax):
        ap += 1.0
        delt *= x / ap
        s += delt
        if abs(delt) < abs(s) * eps:
            break
    return s * math.exp(-x + a * math.log(x) - gln)


def _gamma_q_contfrac(a, x, itmax=300, eps=3e-12, fpmin=1e-300):
    """Regularized upper incomplete gamma Q(a,x) by Lentz continued fraction (x >= a+1)."""
    gln = math.lgamma(a)
    b = x + 1.0 - a
    c = 1.0 / fpmin
    d = 1.0 / b
    h = d
    for i in range(1, itmax + 1):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < fpmin:
            d = fpmin
        c = b + an / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delt = d * c
        h *= delt
        if abs(delt - 1.0) < eps:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


def _chi2_sf(x, dof):
    """Survival function of the chi-squared distribution: P(X >= x)."""
    if x <= 0.0:
        return 1.0
    a = dof / 2.0
    x2 = x / 2.0
    if x2 < a + 1.0:
        return max(0.0, min(1.0, 1.0 - _gamma_p_series(a, x2)))
    return max(0.0, min(1.0, _gamma_q_contfrac(a, x2)))


def _chi2_two_sample(counts_a, counts_b, min_expected=5.0):
    """Pearson chi-squared test of homogeneity on two raw count dicts.

    Returns (chi2, dof, p_value, pooled_cells) or None when the test is not
    applicable (empty counts, or fewer than two cells after pooling).
    Bitstrings whose expected count in either sample falls below
    ``min_expected`` are pooled into a single bucket, the standard remedy for
    the chi-squared small-cell approximation problem.
    """
    n_a = sum(counts_a.values())
    n_b = sum(counts_b.values())
    if n_a <= 0 or n_b <= 0:
        return None
    total = n_a + n_b

    def expected_ok(oa, ob):
        c = oa + ob
        return (n_a * c / total) >= min_expected and (n_b * c / total) >= min_expected

    cells = [(counts_a.get(k, 0), counts_b.get(k, 0))
             for k in set(counts_a) | set(counts_b)]
    kept = [c for c in cells if expected_ok(*c)]
    rest = [c for c in cells if not expected_ok(*c)]
    pooled_cells = len(rest)
    if rest:
        kept.append((sum(c[0] for c in rest), sum(c[1] for c in rest)))
    if len(kept) < 2:
        return None

    chi2 = 0.0
    for oa, ob in kept:
        c = oa + ob
        ea = n_a * c / total
        eb = n_b * c / total
        chi2 += (oa - ea) ** 2 / ea + (ob - eb) ** 2 / eb
    dof = len(kept) - 1
    return chi2, dof, _chi2_sf(chi2, dof), pooled_cells


# ---------------------------------------------------------------------------

class QuantumDistributionComparison(FlowFileTransform):
    """
    Pairs two quantum measurement distributions (from any two frameworks or
    algorithm configurations) and computes Hellinger distance, Total Variation,
    and classical Fidelity.

    Pairing model
    -------------
    All FlowFiles that arrive at this processor share a single comparison slot
    identified by the Comparison Label property.  The first FlowFile is stored
    to disk; the second triggers the comparison, the report is written, and the
    slot is cleared for the next pair.

    Because pairing is label-based (not attribute-based), the two FlowFiles can
    come from pipelines that target different states, use different qubit counts,
    or even run different algorithms entirely.  Each side is labelled by the
    grover.framework attribute if present, or by a configurable override.

    Output relationships
    --------------------
    success  — comparison complete; HTML card written; metrics in attributes.
    failure  — first of the pair stored; processor is waiting.  Auto-terminate
               this relationship in NiFi.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.2.0"
        description = (
            "Pairs two quantum measurement distributions and computes Hellinger "
            "distance, Total Variation, and Fidelity (effect sizes), plus a "
            "Pearson chi-squared homogeneity test on the raw counts (inference: "
            "is the gap explainable by shot noise?). Emits a two-gate verdict — "
            "consistent / negligible-difference / disagree — gated by the "
            "Significance Level and Practical Hellinger Threshold properties. "
            "Writes a side-by-side HTML comparison card.  Pairing is controlled "
            "by a static Comparison Label — no shared FlowFile attribute required."
        )
        tags = ["quantum", "comparison", "hellinger", "distribution", "qiskit", "qrisp", "report"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.reports_dir = PropertyDescriptor(
            name="Reports Directory",
            description="Folder where the HTML comparison report is written.",
            required=True,
            default_value="reports",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.flow_name = PropertyDescriptor(
            name="Flow Name",
            description=(
                "Report filename: {flow_name}-comparison.html. "
                "Also used as the page title."
            ),
            required=True,
            default_value="grover",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.state_dir = PropertyDescriptor(
            name="State Directory",
            description=(
                "Directory where the waiting distribution is persisted between "
                "FlowFile arrivals.  Must be writable by NiFi."
            ),
            required=True,
            default_value="reports/tmp/quanifi_compare_state",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.comparison_label = PropertyDescriptor(
            name="Comparison Label",
            description=(
                "Identifier for this comparison slot.  Both pipelines you want to "
                "compare must route into this processor instance.  The label is used "
                "as the state filename — set a unique value per processor if you have "
                "multiple comparison processors on the canvas.  "
                "Supports Expression Language so you can write "
                "'${test.run_id}-${test.case_id}' to get one slot per test case, "
                "which is required for data-driven runs where multiple cases are "
                "in-flight concurrently.  "
                "Example literal: 'grover-4q'.  Example dynamic: '${test.run_id}-${test.case_id}'."
            ),
            required=True,
            default_value="grover-comparison",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.framework_label = PropertyDescriptor(
            name="Framework Label",
            description=(
                "Label to identify this FlowFile's framework in the report.  "
                "Supports NiFi Expression Language so you can write "
                "'${grover.framework}' to read it from the FlowFile attribute, or "
                "type a literal like 'qiskit' to override it.  "
                "Leave blank to auto-detect from the grover.framework attribute "
                "(falls back to 'run-1' / 'run-2' if not set)."
            ),
            required=False,
            default_value="${grover.framework}",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.alpha = PropertyDescriptor(
            name="Significance Level",
            description=(
                "Alpha for the chi-squared homogeneity test on the raw counts. "
                "p-values at or above this are reported as 'consistent' — the "
                "observed difference is explainable by shot noise alone."
            ),
            required=True,
            default_value="0.01",
            validators=[StandardValidators.NUMBER_VALIDATOR],
        )
        self.hellinger_threshold = PropertyDescriptor(
            name="Practical Hellinger Threshold",
            description=(
                "Effect-size gate for the verdict. A statistically significant "
                "difference (p below Significance Level) is only reported as "
                "'disagree' when the Hellinger distance also exceeds this "
                "threshold; otherwise it is 'negligible-difference' (real but "
                "too small to matter, e.g. at very large shot counts)."
            ),
            required=True,
            default_value="0.1",
            validators=[StandardValidators.NUMBER_VALIDATOR],
        )
        self.descriptors = [
            self.reports_dir,
            self.flow_name,
            self.state_dir,
            self.comparison_label,
            self.framework_label,
            self.alpha,
            self.hellinger_threshold,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    # -----------------------------------------------------------------------

    def transform(self, context, flowFile):
        reports_dir = context.getProperty(self.reports_dir).getValue()
        flow_name   = context.getProperty(self.flow_name).getValue()
        state_dir   = context.getProperty(self.state_dir).getValue()
        label       = (
            context.getProperty(self.comparison_label)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        framework = (
            context.getProperty(self.framework_label)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        ) or ""

        meta = {
            "marked_state": flowFile.getAttribute("circuit.marked_state") or "",
            "num_qubits":   flowFile.getAttribute("circuit.num_qubits")   or "",
            "algorithm":    flowFile.getAttribute("circuit.algorithm")     or "",
        }

        raw = bytes(flowFile.getContentsAsBytes())
        try:
            raw_dist = json.loads(raw.decode("utf-8"))
            incoming_dist = _normalize(raw_dist)
        except Exception as exc:
            self.logger.warn("QuantumDistributionComparison: JSON parse error: {}".format(exc))
            return FlowFileTransformResult(relationship="failure", contents=raw)

        # Raw counts (integers from the shot-count simulators) feed the
        # chi-squared test; probability inputs (floats, e.g. from
        # PennylaneExpectation's analytic lane) carry no shot information,
        # so for those the test is skipped and only effect sizes apply.
        incoming_counts = (
            raw_dist
            if raw_dist and all(isinstance(v, int) and not isinstance(v, bool)
                                for v in raw_dist.values())
            else None
        )

        os.makedirs(state_dir, exist_ok=True)
        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        state_path = os.path.join(state_dir, f"{safe_label}.json")

        state = {}
        if os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    state = json.load(f)
            except Exception:
                state = {}

        if "entry_a" not in state:
            state["entry_a"] = {
                "dist":      incoming_dist,
                "counts":    incoming_counts,
                "framework": framework,
                "meta":      meta,
            }
            with open(state_path, "w") as f:
                json.dump(state, f)
            self.logger.warn(
                "QuantumDistributionComparison [{}]: stored first entry (framework='{}', "
                "marked_state='{}'), waiting for second.".format(label, framework, meta["marked_state"])
            )
            return FlowFileTransformResult(
                relationship="failure",
                contents=raw,
                attributes={"compare.status": "waiting", "compare.label": label},
            )

        entry_a = state["entry_a"]
        entry_b = {"dist": incoming_dist, "framework": framework, "meta": meta}

        fa = entry_a["framework"] or "run-1"
        fb = entry_b["framework"] or "run-2"
        if fa == fb:
            fa = fa + " (1)"
            fb = fb + " (2)"

        dist_a = dict(sorted(entry_a["dist"].items(), key=lambda x: x[1], reverse=True))
        dist_b = dict(sorted(entry_b["dist"].items(), key=lambda x: x[1], reverse=True))

        h_dist = _hellinger(dist_a, dist_b)
        tv     = _total_variation(dist_a, dist_b)
        fid    = _fidelity(dist_a, dist_b)
        top_a  = next(iter(dist_a))
        top_b  = next(iter(dist_b))
        agree  = top_a == top_b

        # Two-gate verdict: the chi-squared p-value decides whether the gap is
        # statistically explainable by shot noise; the Hellinger threshold
        # decides whether a significant gap is practically meaningful.
        alpha       = float(context.getProperty(self.alpha).getValue())
        h_threshold = float(context.getProperty(self.hellinger_threshold).getValue())
        counts_a = entry_a.get("counts")
        test = (_chi2_two_sample(counts_a, incoming_counts)
                if counts_a and incoming_counts else None)
        if test is None:
            chi2 = dof = p_value = pooled_cells = None
            verdict = "no-test"
        else:
            chi2, dof, p_value, pooled_cells = test
            if p_value >= alpha:
                verdict = "consistent"
            elif h_dist <= h_threshold:
                verdict = "negligible-difference"
            else:
                verdict = "disagree"

        try:
            os.remove(state_path)
        except OSError:
            pass

        comparison = {
            "label":              label,
            "framework_a":        fa,
            "framework_b":        fb,
            "meta_a":             entry_a["meta"],
            "meta_b":             meta,
            "hellinger_distance": round(h_dist, 6),
            "total_variation":    round(tv, 6),
            "fidelity":           round(fid, 6),
            "agreement":          agree,
            "top_result_a":       top_a,
            "top_result_b":       top_b,
            "chi_squared": {
                "statistic":    None if chi2 is None else round(chi2, 6),
                "dof":          dof,
                "p_value":      None if p_value is None else round(p_value, 8),
                "pooled_cells": pooled_cells,
                "alpha":        alpha,
                "shots_a":      sum(counts_a.values()) if counts_a else None,
                "shots_b":      sum(incoming_counts.values()) if incoming_counts else None,
            },
            "hellinger_threshold": h_threshold,
            "verdict":            verdict,
            "distributions":      {fa: dist_a, fb: dist_b},
        }

        out_attrs = {
            "report.type":                "comparison",
            "compare.status":             "complete",
            "compare.label":              label,
            "compare.framework_a":        fa,
            "compare.framework_b":        fb,
            "compare.hellinger_distance": f"{h_dist:.4f}",
            "compare.total_variation":    f"{tv:.4f}",
            "compare.fidelity":           f"{fid:.4f}",
            "compare.top_result_a":       top_a,
            "compare.top_result_b":       top_b,
            "compare.top_prob_a":         f"{dist_a[top_a]:.4f}",
            "compare.top_prob_b":         f"{dist_b[top_b]:.4f}",
            "compare.agreement":          str(agree).lower(),
            "compare.verdict":            verdict,
        }
        if chi2 is not None:
            out_attrs.update({
                "compare.chi2":    f"{chi2:.4f}",
                "compare.dof":     str(dof),
                "compare.p_value": f"{p_value:.6g}",
                "compare.alpha":   f"{alpha:g}",
            })

        self.logger.warn(
            "QuantumDistributionComparison [{}]: {} vs {}  "
            "H={:.4f}  TV={:.4f}  F={:.4f}  agree={}  verdict={}  "
            "top=|{}⟩ / |{}⟩".format(
                label, fa, fb, h_dist, tv, fid, agree, verdict, top_a, top_b
            )
        )

        self._write_report(
            reports_dir, flow_name, label,
            fa, fb, entry_a["meta"], meta,
            dist_a, dist_b,
            h_dist, tv, fid, agree, top_a, top_b,
            chi2, dof, p_value, alpha, h_threshold, verdict,
        )

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(comparison, indent=2).encode("utf-8"),
            attributes=out_attrs,
        )

    # -----------------------------------------------------------------------

    def _write_report(self, reports_dir, flow_name, label,
                      fa, fb, meta_a, meta_b,
                      dist_a, dist_b,
                      h_dist, tv, fid, agree, top_a, top_b,
                      chi2, dof, p_value, alpha, h_threshold, verdict):

        os.makedirs(reports_dir, exist_ok=True)
        output_path = os.path.join(reports_dir, f"{flow_name}-comparison.html")

        agree_badge = (
            '<span class="badge badge-agree">&#x2713; agree</span>'
            if agree
            else '<span class="badge badge-differ">&#x2717; differ</span>'
        )

        verdict_badge = {
            "consistent":
                '<span class="badge badge-agree">&#x2713; consistent</span>',
            "negligible-difference":
                '<span class="badge badge-warn">&#x2248; negligible difference</span>',
            "disagree":
                '<span class="badge badge-differ">&#x2717; disagree</span>',
            "no-test":
                '<span class="badge badge-na">no test (no raw counts)</span>',
        }[verdict]

        def meta_subtitle(meta, fw):
            parts = []
            if meta.get("marked_state"):
                parts.append(f"target |{html_lib.escape(meta['marked_state'])}&#x27E9;")
            if meta.get("num_qubits"):
                parts.append(f"{html_lib.escape(meta['num_qubits'])} qubits")
            if meta.get("algorithm"):
                parts.append(html_lib.escape(meta["algorithm"]))
            return " &middot; ".join(parts) if parts else html_lib.escape(fw)

        sub_a = meta_subtitle(meta_a, fa)
        sub_b = meta_subtitle(meta_b, fb)

        metric_rows = "".join([
            f"<tr><td>Hellinger Distance</td>"
            f"<td class='metric-val'>{h_dist:.4f}</td>"
            f"<td>0 = identical &nbsp;/&nbsp; 1 = disjoint</td></tr>",
            f"<tr><td>Total Variation</td>"
            f"<td class='metric-val'>{tv:.4f}</td>"
            f"<td>half the L1 distance between distributions</td></tr>",
            f"<tr><td>Fidelity</td>"
            f"<td class='metric-val'>{fid:.4f}</td>"
            f"<td>1 = identical &nbsp;/&nbsp; 0 = disjoint</td></tr>",
            f"<tr><td>Top result</td>"
            f"<td class='metric-val'>"
            f"<span class='tag-a'>|{html_lib.escape(top_a)}&#x27E9;</span>"
            f" / "
            f"<span class='tag-b'>|{html_lib.escape(top_b)}&#x27E9;</span>"
            f"</td>"
            f"<td>{agree_badge}</td></tr>",
            (
                f"<tr><td>&chi;&sup2; homogeneity</td>"
                f"<td class='metric-val'>{chi2:.4f} (dof {dof})</td>"
                f"<td>p = {p_value:.4g} &nbsp;vs&nbsp; &alpha; = {alpha:g}</td></tr>"
                if chi2 is not None else
                f"<tr><td>&chi;&sup2; homogeneity</td>"
                f"<td class='metric-val'>&mdash;</td>"
                f"<td>needs raw counts on both sides</td></tr>"
            ),
            f"<tr><td>Verdict</td>"
            f"<td class='metric-val'>{verdict_badge}</td>"
            f"<td>p &ge; &alpha; &rArr; shot noise; else H &gt; {h_threshold:g} "
            f"decides practical relevance</td></tr>",
        ])

        bars_a = bar_rows(dist_a, "bar-fill-a")
        bars_b = bar_rows(dist_b, "bar-fill-b")

        card = f"""\
<section class="run-card">
  <header class="run-header">
    <span class="run-title">
      <span class="tag-a">{html_lib.escape(fa)}</span>
      &nbsp;vs&nbsp;
      <span class="tag-b">{html_lib.escape(fb)}</span>
      &nbsp;&middot;&nbsp; H&nbsp;=&nbsp;{h_dist:.4f}
      &nbsp;&middot;&nbsp; {verdict_badge}
    </span>
    <span class="run-time">{now()}</span>
  </header>
  <div class="run-body">
    <div class="panel">
      <h3>Distance Metrics</h3>
      <table>
        <tr><th>Metric</th><th>Value</th><th>Interpretation</th></tr>
        {metric_rows}
      </table>
    </div>
  </div>
  <div class="run-footer">
    <div class="panel">
      <h3><span class="tag-a">{html_lib.escape(fa)}</span></h3>
      <p style="color:#8c959f;font-size:0.8rem;margin:0 0 12px">{sub_a}</p>
      <div class="bar-chart">{bars_a}</div>
    </div>
    <div class="panel">
      <h3><span class="tag-b">{html_lib.escape(fb)}</span></h3>
      <p style="color:#8c959f;font-size:0.8rem;margin:0 0 12px">{sub_b}</p>
      <div class="bar-chart">{bars_b}</div>
    </div>
  </div>
</section>"""

        title = f"{flow_name} — comparison"
        page  = page_template(title, _EXTRA_CSS)
        write_card(output_path, card, page)
