import json
import math
import os
import sys
import time
import html as html_lib
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reporting import page_template, write_card, now  # noqa: E402

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder


# ---------------------------------------------------------------------------
# Pure consensus core (unit-tested directly, no NiFi / filesystem)
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


def _canonical(s):
    """Endian-agnostic canonical form of a measured bitstring: a string and its
    bit-reverse vote together, so Qiskit's little-endian readout and Cirq/Qrisp's
    MSB-first readout of the same state are not counted as a disagreement.

    (Limitation: this also merges a genuine state with its bit-reverse. For the
    marked-state / phase decodes the oracle scores, that is exactly the
    convention difference we want to neutralise; document if a future algorithm
    needs strict keys.)
    """
    return min(s, s[::-1]) if s else s


def _gt_match(top, expected):
    """Endian-agnostic ground-truth match."""
    if not expected:
        return True
    t, e = top.strip(), expected.strip()
    return t == e or t == e[::-1]


def _consensus(entries, expected="", check_gt=True):
    """K-way majority vote over branch distributions.

    entries: list of ``{"label": str, "dist": {state: prob}}`` (normalised).

    Severity order (mirrors QuantumAssertion, generalised to K branches):
      DISAGREE — at least one branch's top result dissents from the majority
                 (the differential / cross-framework check; most severe).
      FAIL     — branches agree but the majority answer is wrong vs test.expected
                 (catches whole-case mutants: all branches share the same fault).
      PASS     — branches agree and (if checked) the majority answer is correct.
    """
    if not entries:
        return {
            "verdict": "DISAGREE", "reason": "no branches",
            "majority_top": "", "majority_count": 0, "branches": 0,
            "dissenters": [], "max_hellinger": 0.0,
        }

    tops = [(e["label"], (max(e["dist"], key=e["dist"].get) if e["dist"] else "")) for e in entries]
    canon_entries = [(_canonical(t), label) for label, t in tops]
    counts = Counter(c for c, _ in canon_entries)
    majority_canon, majority_count = counts.most_common(1)[0]
    dissenters = sorted({label for c, label in canon_entries if c != majority_canon})
    # A representative *actual* (non-canonicalised) top from the majority cluster.
    rep_top = next(t for (label, t), (c, _) in zip(tops, canon_entries) if c == majority_canon)

    max_h = 0.0
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            max_h = max(max_h, _hellinger(entries[i]["dist"], entries[j]["dist"]))

    if dissenters:
        verdict = "DISAGREE"
        reason = "branches dissent from majority |{}⟩: {}".format(
            rep_top, ", ".join(dissenters))
    elif check_gt and expected and not _gt_match(rep_top, expected):
        verdict = "FAIL"
        reason = "all branches agree on |{}⟩ but expected |{}⟩".format(rep_top, expected)
    else:
        verdict = "PASS"
        reason = "all {} branches agree on |{}⟩".format(len(entries), rep_top)
        if check_gt and expected:
            reason += " (matches expected)"

    return {
        "verdict": verdict, "reason": reason,
        "majority_top": rep_top, "majority_count": majority_count,
        "branches": len(entries), "dissenters": dissenters,
        "max_hellinger": max_h,
    }


class QuantumConsensusOracle(FlowFileTransform):
    """
    K-way majority oracle for differential / mutation testing.

    Generalises the 2-way ``QuantumDistributionComparison`` + ``QuantumAssertion``
    chain to an arbitrary number of framework branches. Collects ``Expected
    Branches`` measurement distributions that share a slot key (``Consensus
    Label``, EL — default one slot per test case), then takes a majority vote and
    emits a single verdict per slot:

      * **DISAGREE** — a branch's top result dissents from the majority
        (cross-framework differential failure).
      * **FAIL** — branches agree but the majority answer is wrong vs
        ``test.expected`` (whole-case mutants land here: every branch makes the
        same mistake, so consensus alone cannot see it — ground truth does).
      * **PASS** — branches agree and (when ``test.expected`` is present) the
        answer is correct.

    The verdict is emitted as ``assert.verdict`` and the ``mut.*`` / ``test.*``
    bookkeeping is passed through, so this processor feeds ``MutationScoreReport``
    directly. Top results are compared endian-agnostically so Qiskit's
    little-endian readout and Cirq/Qrisp's MSB-first readout of the same state
    are not a false disagreement.

    Relationships
    -------------
    pass     — verdict PASS.
    fail     — verdict FAIL or DISAGREE (route on to MutationScoreReport / alerts).
    waiting  — fewer than K branches collected; slot is buffering. Auto-terminate.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "K-way majority oracle: collects K framework measurement distributions "
            "sharing a slot key, takes a majority vote (endian-agnostic), and emits "
            "assert.verdict = PASS / FAIL / DISAGREE plus consensus.* details. "
            "Generalises QuantumDistributionComparison+QuantumAssertion to K branches "
            "and feeds MutationScoreReport. FAIL catches whole-case mutants that all "
            "branches get equally wrong; DISAGREE catches single-branch dissent."
        )
        tags = ["quantum", "testing", "consensus", "oracle", "majority",
                "differential", "mutation", "k-way"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.reports_dir = PropertyDescriptor(
            name="Reports Directory",
            description="Folder where the HTML consensus report is written.",
            required=True,
            default_value="reports",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.flow_name = PropertyDescriptor(
            name="Flow Name",
            description="Report basename: {flow_name}-consensus.html.",
            required=True,
            default_value="datadriven-grover",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.state_dir = PropertyDescriptor(
            name="State Directory",
            description="Directory where partial slots are buffered between branch arrivals.",
            required=True,
            default_value="reports/tmp/quanifi_consensus_state",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.consensus_label = PropertyDescriptor(
            name="Consensus Label",
            description=(
                "Slot key (Expression Language). All branches of one test case must "
                "produce the same key. Default gives one slot per case so concurrent "
                "cases do not collide."
            ),
            required=True,
            default_value="${test.run_id}-${test.case_id}",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.expected_branches = PropertyDescriptor(
            name="Expected Branches",
            description=(
                "K — how many framework branches feed one slot before the oracle votes "
                "(e.g. 3 for Qiskit/Cirq/Qrisp)."
            ),
            required=True,
            default_value="3",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.branch_label = PropertyDescriptor(
            name="Branch Label",
            description=(
                "Identifies this FlowFile's branch in the vote/report (Expression "
                "Language). Default reads the simulator framework; falls back to a "
                "positional name if absent."
            ),
            required=False,
            default_value="${builder.component} and ${sim.component}",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.check_ground_truth = PropertyDescriptor(
            name="Check Ground Truth",
            description=(
                "When 'true', also FAILs a slot whose agreed majority answer differs "
                "from test.expected (endian-agnostic). Skipped when test.expected is "
                "absent."
            ),
            required=True,
            default_value="true",
            allowable_values=["true", "false"],
        )
        self.slot_ttl = PropertyDescriptor(
            name="Slot TTL Seconds",
            description=(
                "Discard a partially filled slot older than this and start "
                "fresh. Guards against a branch that never arrives, which would "
                "otherwise buffer forever and silently swallow every later run. "
                "0 disables the check."
            ),
            required=True,
            default_value="3600",
            validators=[StandardValidators.NON_NEGATIVE_INTEGER_VALIDATOR],
        )
        self.descriptors = [
            self.slot_ttl,
            self.reports_dir,
            self.flow_name,
            self.state_dir,
            self.consensus_label,
            self.expected_branches,
            self.branch_label,
            self.check_ground_truth,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def getRelationships(self):
        return [
            Relationship(name="pass", description="Verdict PASS.", auto_terminated=False),
            Relationship(name="fail", description="Verdict FAIL or DISAGREE.", auto_terminated=False),
            Relationship(name="waiting", description="Buffering; <K branches so far.", auto_terminated=False),
        ]

    # ── state sidecar ─────────────────────────────────────────────────────────

    def _slot_path(self, state_dir, label):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        return os.path.join(state_dir, f"{safe}.json")

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

    def transform(self, context, flowFile):
        def get(prop):
            return context.getProperty(prop).evaluateAttributeExpressions(flowFile).getValue()

        reports_dir = context.getProperty(self.reports_dir).getValue()
        flow_name   = context.getProperty(self.flow_name).getValue()
        state_dir   = context.getProperty(self.state_dir).getValue()
        label       = get(self.consensus_label)
        try:
            k = int(get(self.expected_branches))
        except (TypeError, ValueError):
            k = 3
        branch      = (get(self.branch_label) or "").strip()
        check_gt    = (context.getProperty(self.check_ground_truth).getValue() or "true").lower() == "true"

        attrs = {}
        try:
            attrs = dict(flowFile.getAttributes())
        except Exception:
            pass
        raw = bytes(flowFile.getContentsAsBytes())

        try:
            dist = _normalize(json.loads(raw.decode("utf-8")))
        except Exception as exc:
            self.logger.warn("QuantumConsensusOracle: JSON parse error: {}".format(exc))
            return FlowFileTransformResult(relationship="waiting", contents=raw,
                                           attributes={"consensus.error": str(exc)})

        os.makedirs(state_dir, exist_ok=True)
        slot_path = self._slot_path(state_dir, label)
        try:
            ttl = int(context.getProperty(self.slot_ttl).getValue())
        except (TypeError, ValueError):
            ttl = 3600
        slot = self._load_slot(slot_path, ttl_seconds=ttl)
        sim_fw = attrs.get("sim.framework", "")
        b_comp = attrs.get("builder.component") or attrs.get("builder.framework")
        s_comp = attrs.get("sim.component") or attrs.get("sim.framework") or sim_fw
        if b_comp and s_comp:
            branch = f"{b_comp} and {s_comp}"
        elif not branch:
            branch = s_comp or b_comp or f"branch-{len(slot['entries']) + 1}"
        slot["entries"].append({"label": branch, "dist": dist})

        if len(slot["entries"]) < k:
            with open(slot_path, "w", encoding="utf-8") as f:
                json.dump(slot, f)
            self.logger.warn(
                "QuantumConsensusOracle [{}]: branch '{}' stored ({}/{}), waiting.".format(
                    label, branch, len(slot["entries"]), k))
            return FlowFileTransformResult(
                relationship="waiting", contents=raw,
                attributes={"consensus.status": "waiting", "consensus.label": label,
                            "consensus.have": str(len(slot["entries"])), "consensus.need": str(k)},
            )

        # Kth branch — vote and clear the slot.
        expected = attrs.get("test.expected", "")
        res = _consensus(slot["entries"], expected=expected, check_gt=check_gt)
        try:
            os.remove(slot_path)
        except OSError:
            pass

        case_id = attrs.get("test.case_id", attrs.get("assert.case_id", ""))
        run_id  = attrs.get("test.run_id", "")
        verdict = res["verdict"]

        dissent_set = set(res["dissenters"])
        branch_details = []
        for e in slot["entries"]:
            top = max(e["dist"], key=e["dist"].get) if e["dist"] else ""
            branch_details.append({
                "label": e["label"],
                "top": top,
                "dissent": e["label"] in dissent_set,
            })

        out_attrs = {
            "report.type":                "consensus",
            "assert.verdict":             verdict,
            "assert.case_id":             case_id,
            "assert.run_id":              run_id,
            "assert.reason":              res["reason"],
            "consensus.label":            label,
            "consensus.branches":         str(res["branches"]),
            "consensus.majority_top":     res["majority_top"],
            "consensus.majority_count":   str(res["majority_count"]),
            "consensus.dissenters":       ",".join(res["dissenters"]),
            "consensus.dissenter_count":  str(len(res["dissenters"])),
            "consensus.max_hellinger":    f"{res['max_hellinger']:.4f}",
            "consensus.expected":         expected,
            "consensus.branches_json":    json.dumps(branch_details),
        }
        # Pass the mutation bookkeeping through so MutationScoreReport can score it.
        for key in ("mut.applied", "mut.operator", "mut.target_attr",
                    "mut.original_value", "mut.seed", "mut.base_case_id",
                    "test.run_id", "test.case_id", "test.partition", "test.expected"):
            if key in attrs:
                out_attrs[key] = attrs[key]

        self.logger.warn(
            "QuantumConsensusOracle [{}]: {} over {} branches, majority=|{}⟩, "
            "dissenters=[{}], maxH={:.4f} | {}".format(
                label, verdict, res["branches"], res["majority_top"],
                ",".join(res["dissenters"]), res["max_hellinger"], res["reason"]))

        self._write_report(reports_dir, flow_name, label, run_id, case_id,
                           slot["entries"], res, expected)

        relationship = "pass" if verdict == "PASS" else "fail"
        return FlowFileTransformResult(relationship=relationship, contents=raw, attributes=out_attrs)

    # ── HTML report ───────────────────────────────────────────────────────────

    def _write_report(self, reports_dir, flow_name, label, run_id, case_id, entries, res, expected):
        os.makedirs(reports_dir, exist_ok=True)
        output_path = os.path.join(reports_dir, f"{flow_name}-consensus.html")

        verdict = res["verdict"]
        css = {"PASS": "verdict-pass", "FAIL": "verdict-fail"}.get(verdict, "verdict-disagree")
        icon = {"PASS": "✓", "FAIL": "✗", "DISAGREE": "⚠"}.get(verdict, "?")
        dissent = set(res["dissenters"])

        rows = ""
        for e in entries:
            top = max(e["dist"], key=e["dist"].get) if e["dist"] else ""
            is_d = e["label"] in dissent
            rows += (
                f'<tr><td><code>{html_lib.escape(e["label"])}</code></td>'
                f'<td class="{"val-bad" if is_d else "val-good"}">|{html_lib.escape(top)}⟩</td>'
                f'<td>{"✗ dissents" if is_d else "✓ majority"}</td></tr>\n'
            )

        card = (
            f'<section class="run-card"><header class="run-header">'
            f'<span class="run-title">Consensus &mdash; case '
            f'<code>{html_lib.escape(case_id or label)}</code> '
            f'<span class="verdict-badge {css}">{icon} {verdict}</span></span>'
            f'<span class="run-time">{now()}</span></header>'
            f'<div class="run-body"><div class="panel">'
            f'<table class="assert-table"><tr><th>Branch</th><th>Top</th><th></th></tr>{rows}</table>'
            f'</div><div class="panel">'
            f'<div>majority |{html_lib.escape(res["majority_top"])}⟩ '
            f'({res["majority_count"]}/{res["branches"]})</div>'
            f'<div>max Hellinger {res["max_hellinger"]:.4f}</div>'
            + (f'<div>expected |{html_lib.escape(expected)}⟩</div>' if expected else '')
            + f'<div style="color:#656d76;margin-top:8px">{html_lib.escape(res["reason"])}</div>'
            f'</div></div></section>'
        )

        extra_css = """\
    .verdict-pass    { background:#dafbe1; color:#1a7f37; }
    .verdict-fail    { background:#ffebe9; color:#cf222e; }
    .verdict-disagree{ background:#fff8c5; color:#9a6700; }
    .verdict-badge { display:inline-block; padding:3px 14px; border-radius:12px;
      font-size:0.9rem; font-weight:700; font-family:monospace; }
    .assert-table td, .assert-table th { padding:4px 12px; text-align:left; }
    .assert-table th { color:#656d76; }
    .val-good { color:#1a7f37; font-family:monospace; }
    .val-bad  { color:#cf222e; font-family:monospace; }"""

        page = page_template(f"{flow_name} — consensus", extra_css)
        write_card(output_path, card, page)
