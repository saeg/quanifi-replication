import json
import os
import sys
import html as html_lib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reporting import page_template, write_card, now  # noqa: E402

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder


# ---------------------------------------------------------------------------
# Pure scoring core (unit-tested directly, no NiFi / filesystem)
# ---------------------------------------------------------------------------
# A *record* is one per-case verdict that reached the aggregator:
#   {"applied": bool, "operator": str|None, "verdict": str, "case_id": str}
# `applied` is the mutant flag (mut.applied); controls have applied=False.

def _classify(verdict):
    """A mutant SURVIVES iff the consensus oracle passed it. Anything else
    (FAIL / DISAGREE / missing) means the dissent caught it — it is KILLED."""
    return "survived" if str(verdict).strip().upper() == "PASS" else "killed"


def _score(records):
    """Aggregate verdict records into survival-rate statistics.

    Controls (``applied=False``) are EXCLUDED from the survival rate by
    construction — a dissenting control is not a kill but a real cross-framework
    discrepancy, reported separately (see MUTATION_TESTING.md §3 / DISSERTATION.md).
    """
    by_op = {}
    controls_total = 0
    controls_dissent = []
    mut_total = 0
    mut_survived = 0

    for r in records:
        verdict = r.get("verdict", "")
        case_id = r.get("case_id", "")
        if r.get("applied"):
            op = r.get("operator") or "(unspecified)"
            d = by_op.setdefault(op, {"total": 0, "survived": 0, "killed": 0})
            d["total"] += 1
            if _classify(verdict) == "survived":
                d["survived"] += 1
                mut_survived += 1
            else:
                d["killed"] += 1
            mut_total += 1
        else:
            controls_total += 1
            if _classify(verdict) != "survived":   # a control should never dissent
                controls_dissent.append(case_id)

    for d in by_op.values():
        d["survival_rate"] = (d["survived"] / d["total"]) if d["total"] else 0.0

    overall = {
        "mutants": mut_total,
        "survived": mut_survived,
        "killed": mut_total - mut_survived,
        "survival_rate": (mut_survived / mut_total) if mut_total else 0.0,
        "mutation_score": ((mut_total - mut_survived) / mut_total) if mut_total else 0.0,
    }
    controls = {
        "total": controls_total,
        "dissenting": len(controls_dissent),
        "dissent_case_ids": controls_dissent,
    }
    return {"by_operator": by_op, "overall": overall, "controls": controls}


class MutationScoreReport(FlowFileTransform):
    """
    Survival-rate aggregator for the Layer-B mutation pipeline.

    Terminal stage of the consensus-oracle flow::

        ... -> QuantumDistributionComparison -> QuantumAssertion -> MutationScoreReport

    Each incoming FlowFile is one per-case verdict carrying the ``assert.verdict``
    written by QuantumAssertion (PASS / FAIL / DISAGREE) plus the ``mut.*``
    bookkeeping stamped by QuantumTestCaseSource. The processor accumulates
    verdicts per ``test.run_id`` in a JSON state sidecar, recomputes the survival
    rate on every FlowFile, rewrites an HTML report, and emits summary attributes.

    Kill rule: ``PASS`` -> survived, anything else -> killed (the mutated branch
    dissented). **Controls (``mut.applied=false``) are excluded from the survival
    rate**; a dissenting control is surfaced separately as a real cross-framework
    discrepancy to investigate, never counted as a kill. See MUTATION_TESTING.md
    and DISSERTATION.md for the rationale.

    Verdicts are deduplicated by case id within a run, so reprocessing a FlowFile
    does not double-count.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Aggregates per-case verdicts (assert.verdict + mut.* attributes) into a "
            "mutation survival rate, grouped by mutation operator and keyed on "
            "test.run_id. Excludes control rows from the score and reports control "
            "dissents (real cross-framework discrepancies) separately. Writes an HTML "
            "survival-rate report and emits mutation.* summary attributes."
        )
        tags = ["quantum", "testing", "mutation", "survival-rate", "oracle",
                "differential", "data-driven"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.verdict_attr = PropertyDescriptor(
            name="Verdict Attribute",
            description=(
                "Name of the FlowFile attribute holding the per-case verdict. PASS means "
                "the mutant survived; any other value (FAIL/DISAGREE) means it was killed. "
                "Default matches QuantumAssertion's output."
            ),
            required=True,
            default_value="assert.verdict",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.reports_dir = PropertyDescriptor(
            name="Reports Directory",
            description="Folder where the HTML survival-rate report and JSON state sidecar are written.",
            required=True,
            default_value="reports",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.flow_name = PropertyDescriptor(
            name="Flow Name",
            description=(
                "Report basename: {flow_name}-mutation.html and {flow_name}-mutation-state.json. "
                "Accumulated across the FlowFiles of a run."
            ),
            required=True,
            default_value="datadriven-grover",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.descriptors = [
            self.verdict_attr,
            self.reports_dir,
            self.flow_name,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def getRelationships(self):
        return [
            Relationship(
                name="success",
                description="Verdict recorded; FlowFile carries the current mutation.* summary.",
                auto_terminated=False,
            ),
            Relationship(
                name="control_dissent",
                description=(
                    "This FlowFile is an unmutated control whose branch dissented — a real "
                    "cross-framework discrepancy to investigate, not a mutation kill."
                ),
                auto_terminated=False,
            ),
        ]

    # ── state sidecar ─────────────────────────────────────────────────────────

    def _state_path(self, reports_dir, flow_name):
        return os.path.join(reports_dir, f"{flow_name}-mutation-state.json")

    def _load_state(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}

    def _save_state(self, path, state):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    def transform(self, context, flowFile):
        def get(prop):
            return (
                context.getProperty(prop)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )

        verdict_attr = get(self.verdict_attr)
        reports_dir  = get(self.reports_dir)
        flow_name    = get(self.flow_name)

        attrs = {}
        try:
            attrs = dict(flowFile.getAttributes())
        except Exception:
            pass
        raw = bytes(flowFile.getContentsAsBytes())

        run_id   = attrs.get("test.run_id", attrs.get("assert.run_id", "")) or "no-run"
        case_id  = attrs.get("test.case_id", attrs.get("assert.case_id", "")) or "no-case"
        verdict  = attrs.get(verdict_attr, "")
        applied  = str(attrs.get("mut.applied", "")).strip().lower() == "true"
        operator = attrs.get("mut.operator", "")
        is_control = str(attrs.get("mut.applied", "")).strip().lower() == "false"

        record = {
            "applied": applied,
            "operator": operator,
            "verdict": verdict,
            "case_id": case_id,
        }

        # ── accumulate (dedup by case id within the run) ────────────────────
        os.makedirs(reports_dir, exist_ok=True)
        state_path = self._state_path(reports_dir, flow_name)
        state = self._load_state(state_path)
        run_records = state.setdefault(run_id, {})
        run_records[case_id] = record
        self._save_state(state_path, state)

        stats = _score(list(run_records.values()))
        self._write_report(reports_dir, flow_name, run_id, stats)

        overall = stats["overall"]
        controls = stats["controls"]
        out_attrs = {
            "mutation.run_id":             run_id,
            "mutation.survival_rate":      f"{overall['survival_rate']:.4f}",
            "mutation.mutation_score":     f"{overall['mutation_score']:.4f}",
            "mutation.mutants":            str(overall["mutants"]),
            "mutation.survived":           str(overall["survived"]),
            "mutation.killed":             str(overall["killed"]),
            "mutation.controls":           str(controls["total"]),
            "mutation.controls_dissenting": str(controls["dissenting"]),
            "mutation.operators":          str(len(stats["by_operator"])),
        }

        self.logger.warn(
            "MutationScoreReport [{}] case={} verdict={} applied={} | "
            "survival_rate={:.4f} ({}/{} survived), controls_dissenting={}".format(
                run_id, case_id, verdict or "?", applied,
                overall["survival_rate"], overall["survived"], overall["mutants"],
                controls["dissenting"],
            )
        )

        # A dissenting control is a discrepancy worth routing for investigation.
        relationship = "control_dissent" if (is_control and _classify(verdict) != "survived") else "success"
        return FlowFileTransformResult(
            relationship=relationship,
            contents=raw,
            attributes=out_attrs,
        )

    # ── HTML report ───────────────────────────────────────────────────────────

    def _write_report(self, reports_dir, flow_name, run_id, stats):
        output_path = os.path.join(reports_dir, f"{flow_name}-mutation.html")
        overall = stats["overall"]
        controls = stats["controls"]

        def pct(x):
            return f"{x * 100:.1f}%"

        op_rows = "".join(
            f'<tr><td><code>{html_lib.escape(op)}</code></td>'
            f'<td>{d["total"]}</td><td class="val-killed">{d["killed"]}</td>'
            f'<td class="val-survived">{d["survived"]}</td>'
            f'<td>{pct(d["survival_rate"])}</td></tr>\n'
            for op, d in sorted(stats["by_operator"].items())
        ) or '<tr><td colspan="5" style="color:#6e7681">no mutants yet</td></tr>'

        control_note = (
            f'<div class="panel control-warn">⚠ {controls["dissenting"]} of '
            f'{controls["total"]} control rows dissented (excluded from the survival '
            f'rate — investigate as cross-framework discrepancies): '
            f'<code>{html_lib.escape(", ".join(controls["dissent_case_ids"]))}</code></div>'
            if controls["dissenting"]
            else f'<div class="panel" style="color:#3fb950">✓ all {controls["total"]} '
                 f'control rows agreed</div>'
        )

        card = (
            f'<section class="run-card"><header class="run-header">'
            f'<span class="run-title">Mutation survival &mdash; run '
            f'<code>{html_lib.escape(run_id)}</code></span>'
            f'<span class="run-time">{now()}</span></header>'
            f'<div class="run-body">'
            f'<div class="panel"><div class="big-stat">{pct(overall["survival_rate"])}'
            f'<span class="big-label">survival rate</span></div>'
            f'<div class="sub-stat">mutation score {pct(overall["mutation_score"])} '
            f'&middot; {overall["survived"]} survived / {overall["killed"]} killed '
            f'of {overall["mutants"]} mutants</div></div>'
            f'<div class="panel"><table class="assert-table">'
            f'<tr><th>Operator</th><th>Mutants</th><th>Killed</th><th>Survived</th>'
            f'<th>Survival</th></tr>{op_rows}</table></div>'
            f'{control_note}'
            f'</div></section>'
        )

        extra_css = """\
    .assert-table td, .assert-table th { padding:4px 12px; text-align:left; }
    .assert-table th { color:#8b949e; }
    .val-killed   { color:#3fb950; font-family:monospace; }
    .val-survived { color:#f85149; font-family:monospace; }
    .big-stat { font-size:2.4rem; font-weight:700; color:#e6edf3; }
    .big-label { display:block; font-size:0.85rem; color:#8b949e; font-weight:400; }
    .sub-stat { color:#8b949e; font-size:0.9rem; margin-top:6px; }
    .control-warn { color:#e3b341; }"""

        page = page_template(f"{flow_name} — mutation survival", extra_css)
        write_card(output_path, card, page)
