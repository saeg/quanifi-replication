import json
import os
import sys
import html as html_lib
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reporting import page_template, write_card, now  # noqa: E402

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder

_EXTRA_CSS = """\
    .verdict-pass    { background:#dafbe1; color:#1a7f37; }
    .verdict-fail    { background:#ffebe9; color:#cf222e; }
    .verdict-disagree{ background:#fff8c5; color:#9a6700; }
    .verdict-badge {
      display:inline-block; padding:3px 14px; border-radius:12px;
      font-size:0.9rem; font-weight:700; font-family:monospace;
    }
    .assert-table td { padding:4px 10px; }
    .assert-table th { padding:4px 10px; text-align:left; color:#656d76; }
    .val-good { color:#1a7f37; font-family:monospace; font-weight:600; }
    .val-bad  { color:#cf222e; font-family:monospace; font-weight:600; }
    .val-mono { font-family:monospace; color:#1f2328; }"""


def _verdict_badge(verdict):
    css = {"PASS": "verdict-pass", "FAIL": "verdict-fail"}.get(verdict, "verdict-disagree")
    icon = {"PASS": "✓", "FAIL": "✗", "DISAGREE": "⚠"}.get(verdict, "?")
    return f'<span class="verdict-badge {css}">{icon} {verdict}</span>'


class QuantumAssertion(FlowFileTransform):
    """
    Assertion gate for the data-driven differential testing pipeline.

    Sits after QuantumDistributionComparison and applies two independent checks
    to every comparison result:

    1. Differential check  — the two frameworks agree with each other.
       Pass criterion: compare.hellinger_distance ≤ Hellinger Threshold.

    2. Ground-truth check  — the winning state matches the known expected answer.
       Pass criterion: compare.top_result_a == test.expected (when test.expected
       is present on the FlowFile; skipped otherwise).

    Verdict
    -------
    PASS      — both applicable checks passed.
    FAIL      — ≥1 check failed (frameworks agree but wrong answer, or threshold
                exceeded, or ground-truth mismatch).
    DISAGREE  — frameworks disagree beyond threshold (differential check alone
                is the most severe failure; logged separately).

    Relationships
    -------------
    pass    — verdict is PASS; auto-terminate when you want results silently logged.
    fail    — verdict is FAIL or DISAGREE; route to an alert / log / email.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.2.0"
        description = (
            "Assertion gate for data-driven differential testing. Receives the output "
            "of QuantumDistributionComparison and applies a Hellinger threshold check "
            "(differential oracle) plus an optional ground-truth check against "
            "test.expected. Routes to 'pass' or 'fail'. Appends a row to an HTML "
            "test-results table showing per-case verdicts, reasons, and a pass rate."
        )
        tags = ["quantum", "testing", "assertion", "oracle", "differential",
                "mutation", "data-driven", "hellinger"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.hellinger_threshold = PropertyDescriptor(
            name="Hellinger Threshold",
            description=(
                "Maximum Hellinger distance between the two framework distributions "
                "that is still considered agreement. Range [0, 1]: 0 = identical, "
                "1 = completely disjoint. A value of 0.05 is tight (ideal simulators "
                "on the same circuit should be very close). Use 0.15–0.25 for noisy "
                "or low-shot runs. The differential check FAILS when H > threshold."
            ),
            required=True,
            default_value="0.10",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.check_ground_truth = PropertyDescriptor(
            name="Check Ground Truth",
            description=(
                "When 'true', also asserts that the winning measurement state "
                "(compare.top_result_a) equals the test.expected attribute. "
                "If test.expected is absent on the FlowFile the check is silently "
                "skipped (so rows without a known answer still pass through)."
            ),
            required=True,
            default_value="true",
            allowable_values=["true", "false"],
        )
        self.reports_dir = PropertyDescriptor(
            name="Reports Directory",
            description="Folder where the HTML assertion report is written.",
            required=True,
            default_value="reports",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.flow_name = PropertyDescriptor(
            name="Flow Name",
            description=(
                "Report filename: {flow_name}-assertion.html. "
                "Each run appends a row to this file."
            ),
            required=True,
            default_value="datadriven-grover",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.descriptors = [
            self.hellinger_threshold,
            self.check_ground_truth,
            self.reports_dir,
            self.flow_name,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def getRelationships(self):
        return [
            Relationship(
                name="pass",
                description="Verdict is PASS — both checks passed.",
                auto_terminated=False,
            ),
            Relationship(
                name="fail",
                description="Verdict is FAIL or DISAGREE — route to an alert / log / email.",
                auto_terminated=False,
            ),
        ]

    def transform(self, context, flowFile):
        def get(prop):
            return (
                context.getProperty(prop)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )

        try:
            h_threshold    = float(get(self.hellinger_threshold) or 0.10)
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QuantumAssertion: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"assertion.error": msg},
            )
        do_groundtruth = get(self.check_ground_truth).lower() == "true"
        reports_dir    = get(self.reports_dir)
        flow_name      = get(self.flow_name)

        attrs = {}
        try:
            attrs = dict(flowFile.getAttributes())
        except Exception:
            pass

        raw = bytes(flowFile.getContentsAsBytes())

        # ── Read comparison inputs ──────────────────────────────────────────
        h_dist    = float(attrs.get("compare.hellinger_distance", "1.0") or "1.0")
        top_a     = attrs.get("compare.top_result_a", "")
        top_b     = attrs.get("compare.top_result_b", "")
        fw_a      = attrs.get("compare.framework_a", "framework-A")
        fw_b      = attrs.get("compare.framework_b", "framework-B")
        expected  = attrs.get("test.expected", "")
        case_id   = attrs.get("test.case_id", "")
        run_id    = attrs.get("test.run_id", "")
        partition = attrs.get("test.partition", "")
        marked    = attrs.get("circuit.marked_state", attrs.get("grover.marked_state", ""))

        # ── Apply checks ────────────────────────────────────────────────────
        failures = []

        # 1. Differential check
        if h_dist > h_threshold:
            failures.append(
                f"Hellinger {h_dist:.4f} > threshold {h_threshold:.4f} "
                f"({fw_a} top=|{top_a}⟩, {fw_b} top=|{top_b}⟩)"
            )
            verdict = "DISAGREE"
        else:
            verdict = "PASS"

        # 2. Ground-truth check (only when test.expected is present)
        gt_checked = False
        if do_groundtruth and expected:
            gt_checked = True
            # Normalize: strip spaces; both Qiskit (little-endian) and Cirq may
            # produce reversed bitstrings relative to the marked state. Compare
            # the top result against expected AND its reverse so the check is
            # endian-agnostic for same-length strings.
            top_norm = top_a.strip()
            exp_norm = expected.strip()
            if top_norm != exp_norm and top_norm != exp_norm[::-1]:
                failures.append(
                    f"top result |{top_norm}⟩ ≠ expected |{exp_norm}⟩"
                )
                verdict = "FAIL"

        if not failures:
            verdict = "PASS"

        reason = "; ".join(failures) if failures else "all checks passed"

        # ── Emit output attributes ──────────────────────────────────────────
        out_attrs = {
            "assert.verdict":           verdict,
            "assert.case_id":           case_id,
            "assert.run_id":            run_id,
            "assert.reason":            reason,
            "assert.hellinger":         f"{h_dist:.4f}",
            "assert.threshold":         f"{h_threshold:.4f}",
            "assert.top_result_a":      top_a,
            "assert.top_result_b":      top_b,
            "assert.expected":          expected,
            "assert.gt_checked":        str(gt_checked).lower(),
            "assert.framework_a":       fw_a,
            "assert.framework_b":       fw_b,
        }

        self.logger.warn(
            "QuantumAssertion [{}] {} — H={:.4f} (τ={}) top=|{}⟩ expected=|{}⟩ | {}".format(
                case_id, verdict, h_dist, h_threshold, top_a, expected, reason
            )
        )

        self._append_report_row(
            reports_dir, flow_name,
            case_id, run_id, partition, marked,
            fw_a, fw_b, top_a, top_b, expected,
            h_dist, h_threshold, gt_checked,
            verdict, reason,
        )

        relationship = "pass" if verdict == "PASS" else "fail"
        return FlowFileTransformResult(
            relationship=relationship,
            contents=raw,
            attributes=out_attrs,
        )

    # ── HTML report ─────────────────────────────────────────────────────────

    def _append_report_row(
        self, reports_dir, flow_name,
        case_id, run_id, partition, marked,
        fw_a, fw_b, top_a, top_b, expected,
        h_dist, h_threshold, gt_checked,
        verdict, reason,
    ):
        os.makedirs(reports_dir, exist_ok=True)
        output_path = os.path.join(reports_dir, f"{flow_name}-assertion.html")

        badge = _verdict_badge(verdict)

        def val(v, good=None):
            if good is not None:
                css = "val-good" if v == good else "val-bad"
            else:
                css = "val-mono"
            return f'<span class="{css}">{html_lib.escape(str(v))}</span>'

        h_color = "val-good" if h_dist <= h_threshold else "val-bad"
        gt_cell = (
            val(top_a, good=expected) + f" / expected {val(expected)}"
            if gt_checked and expected
            else '<span style="color:#8c959f">—</span>'
        )

        row = (
            f'<tr>'
            f'<td>{html_lib.escape(case_id)}</td>'
            f'<td style="color:#656d76;font-size:0.8rem">{html_lib.escape(partition)}</td>'
            f'<td><code>{html_lib.escape(marked)}</code></td>'
            f'<td><span class="{h_color};font-family:monospace">{h_dist:.4f}</span>'
            f' <span style="color:#8c959f;font-size:0.78rem">(τ={h_threshold})</span></td>'
            f'<td>{gt_cell}</td>'
            f'<td>{badge}</td>'
            f'<td style="color:#656d76;font-size:0.8rem">{html_lib.escape(reason)}</td>'
            f'</tr>\n'
        )

        header = (
            f'<section class="run-card">'
            f'<header class="run-header">'
            f'<span class="run-title">Assertion results &mdash; run '
            f'<code>{html_lib.escape(run_id or "?")}</code></span>'
            f'<span class="run-time">{now()}</span>'
            f'</header>'
            f'<div class="run-body"><div class="panel">'
            f'<table class="assert-table">'
            f'<tr><th>Case</th><th>Partition</th><th>Target</th>'
            f'<th>Hellinger</th><th>Ground truth</th>'
            f'<th>Verdict</th><th>Reason</th></tr>'
        )
        footer = '</table></div></div></section>'

        page = page_template(f"{flow_name} — assertion", _EXTRA_CSS)

        # Append the row inside the existing table if file exists;
        # otherwise create a fresh card.
        if os.path.exists(output_path):
            with open(output_path, "r", encoding="utf-8") as f:
                content = f.read()
            # Insert before the closing </table> of the last card.
            close = content.rfind("</table>")
            if close >= 0:
                content = content[:close] + row + content[close:]
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(content)
                return

        # Fresh file: write the full card.
        write_card(output_path, header + row + footer, page)
