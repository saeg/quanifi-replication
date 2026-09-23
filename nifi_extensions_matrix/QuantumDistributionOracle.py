import json
import os
import sys
import html as html_lib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reporting import page_template, write_card, bar_rows, now  # noqa: E402
from QuantumDistributionComparison import (  # noqa: E402
    _chi2_two_sample, _hellinger, _normalize,
)
from multiple_comparisons import (  # noqa: E402
    CORRECTIONS, normalize_method, reject as _reject, uncorrected as _uncorrected,
)

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder

_EXTRA_CSS = """\
    .badge {
      display: inline-block;
      padding: 2px 10px;
      border-radius: 12px;
      font-size: 0.78rem;
      font-weight: 600;
      font-family: monospace;
    }
    .verdict-pass     { background: #dafbe1; color: #1a7f37; }
    .verdict-fail     { background: #ffebe9; color: #cf222e; }
    .verdict-disagree { background: #fff8c5; color: #9a6700; }
    .metric-val       { color: #1f2328 !important; }
    .dissent          { color: #cf222e; font-weight: 600; }
    .run-footer {
      display: grid !important;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)) !important;
      gap: 16px !important;
      padding: 20px !important;
      background: #f6f8fa !important;
      border-top: 1px solid #d0d7de !important;
    }
    .run-footer .panel {
      background: #ffffff !important;
      border: 1px solid #d0d7de !important;
      border-radius: 8px !important;
      padding: 16px !important;
      box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important;
      margin: 0 !important;
    }
    .panel p {
      word-break: break-word;
    }
    .pairwise-scroll {
      max-height: 280px;
      overflow-y: auto;
      border: 1px solid #d0d7de;
      border-radius: 6px;
      margin-top: 8px;
    }
    .pairwise-scroll table {
      margin: 0;
    }
    .pairwise-scroll th {
      position: sticky;
      top: 0;
      background: #eaeef2;
      z-index: 1;
    }"""


# ---------------------------------------------------------------------------
# Distributional consensus
#
# QuantumConsensusOracle votes on each branch's TOP-1 measured state. That is
# the right rule when the algorithm has a single correct answer (Grover, phase
# estimation), but it breaks on a degenerate output. GHZ is the canonical case:
# |0...0> and |1...1> each carry ~50%, so every branch's top-1 is a coin flip
# and three *correct* branches agree only ~25% of the time (measured: 29% over
# 24 Aer trials at n=16). The oracle then reports DISAGREE on correct code.
#
# This oracle asks the distributional question instead: do all branches sample
# the same distribution? It reuses the same two-gate rule the mutation study
# uses -- a chi-squared homogeneity test for significance, gated by a Hellinger
# effect size -- applied to every pair of branches.
#
# The two oracles are complementary, not competing. Top-1 is more interpretable
# when it applies; use this one when the output is degenerate, multi-peaked, or
# otherwise has no meaningful single winner.
# ---------------------------------------------------------------------------

def _pairwise(entries):
    """[(label_a, label_b, hellinger, p_value_or_None)] over all branch pairs."""
    out = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]
            hellinger = _hellinger(a["dist"], b["dist"])
            counts_a, counts_b = a.get("counts"), b.get("counts")
            test = (_chi2_two_sample(counts_a, counts_b)
                    if counts_a and counts_b else None)
            out.append((a["label"], b["label"], hellinger,
                        None if test is None else test[2]))
    return out


def _distribution_consensus(entries, threshold, alpha, expected_states=None,
                            correction="none"):
    """K-way verdict on distribution similarity.

    A pair *diverges* only when both gates fire: the chi-squared test rejects
    homogeneity (p < alpha, so the gap is not shot noise) AND the Hellinger
    distance exceeds ``threshold`` (so the gap is big enough to matter). When
    raw counts are unavailable the chi-squared gate cannot be evaluated, and
    the Hellinger gate decides alone.

    ``correction`` controls how the per-pair chi-squared p-values are
    corrected for multiplicity before the significance gate is applied. With
    K branches there are K(K-1)/2 pairs, so an uncorrected alpha makes false
    divergences near-certain once K is more than a handful. "none" (the
    default) reproduces the original, uncorrected p < alpha rule exactly --
    existing callers see no change. "holm" and "benjamini-hochberg" correct
    across the slot's pairs that have a p-value; pairs with no raw counts
    (p is None) still pass the significance gate regardless of correction.

    Severity mirrors QuantumConsensusOracle so the two are drop-in comparable:
      DISAGREE - at least one pair of branches diverges.
      FAIL     - branches agree with each other but their shared support does
                 not match ``expected_states`` (a whole-case fault: every
                 branch is wrong in the same way).
      PASS     - branches agree, and match expectation when one is given.

    ``expected_states`` is a set of bitstrings the agreed distribution should
    put its weight on -- for GHZ-n, {"0"*n, "1"*n}. Ground truth is checked as
    support rather than a single winner, which is the whole point here.
    """
    method = normalize_method(correction)
    if method not in CORRECTIONS:
        raise ValueError(f"unknown correction {method!r}; expected one of {CORRECTIONS}")
    if not entries:
        return {
            "verdict": "DISAGREE", "reason": "no branches", "branches": 0,
            "max_hellinger": 0.0, "min_p_value": None, "divergent_pairs": [],
            "dissenters": [], "pairs": [], "correction": method,
            "pairwise_tests": 0, "significant_pairs_raw": 0,
            "significant_pairs_corrected": 0,
        }
    if len(entries) == 1:
        return {
            "verdict": "DISAGREE", "reason": "only one branch; nothing to compare",
            "branches": 1, "max_hellinger": 0.0, "min_p_value": None,
            "divergent_pairs": [], "dissenters": [], "pairs": [], "correction": method,
            "pairwise_tests": 0, "significant_pairs_raw": 0,
            "significant_pairs_corrected": 0,
        }

    pairs = _pairwise(entries)
    max_hellinger = max(p[2] for p in pairs)
    p_values = [p[3] for p in pairs if p[3] is not None]
    min_p = min(p_values) if p_values else None
    m = len(p_values)

    p_list = [p[3] for p in pairs]
    raw_sig = _uncorrected(p_list, alpha)
    corrected_sig = raw_sig if method == "none" else _reject(p_list, alpha, method)

    divergent = []
    for (label_a, label_b, hellinger, p_value), sig in zip(pairs, corrected_sig):
        significant = True if p_value is None else sig
        if significant and hellinger > threshold:
            divergent.append((label_a, label_b, hellinger))

    # A branch is a dissenter when it diverges from every other branch: that
    # isolates the odd one out, which is the version to suspect.
    others = len(entries) - 1
    diverge_count = {e["label"]: 0 for e in entries}
    for label_a, label_b, _ in divergent:
        diverge_count[label_a] = diverge_count.get(label_a, 0) + 1
        diverge_count[label_b] = diverge_count.get(label_b, 0) + 1
    dissenters = sorted(label for label, n in diverge_count.items()
                        if others and n == others)

    if divergent:
        detail = ", ".join("{}~{} H={:.4f}".format(a, b, h) for a, b, h in divergent)
        if method == "none":
            reason = "{} of {} branch pairs diverge (H > {:g}, p < {:g}): {}".format(
                len(divergent), len(pairs), threshold, alpha, detail)
        else:
            reason = (
                "{} of {} branch pairs diverge (H > {:g}, p < {:g} after {} "
                "over {} tests): {}".format(
                    len(divergent), len(pairs), threshold, alpha, method, m, detail))
        if dissenters:
            reason += " | isolated: " + ", ".join(dissenters)
        verdict = "DISAGREE"
    elif expected_states:
        # Support check: the branches agree, but do they agree on the RIGHT
        # states? Compare against the union of each branch's significant mass.
        observed = set()
        for entry in entries:
            observed |= {state for state, prob in entry["dist"].items() if prob >= 0.01}
        missing = set(expected_states) - observed
        extra = observed - set(expected_states)
        if missing or extra:
            verdict = "FAIL"
            reason = ("all {} branches agree (max H={:.4f}) but the support is "
                      "wrong: missing {}, unexpected {}".format(
                          len(entries), max_hellinger,
                          sorted(missing) or "none", sorted(extra) or "none"))
        else:
            verdict = "PASS"
            reason = "all {} branches agree (max H={:.4f}) on the expected support".format(
                len(entries), max_hellinger)
    else:
        verdict = "PASS"
        reason = "all {} branches agree (max H={:.4f} <= {:g})".format(
            len(entries), max_hellinger, threshold)

    return {
        "verdict": verdict, "reason": reason, "branches": len(entries),
        "max_hellinger": max_hellinger, "min_p_value": min_p,
        "divergent_pairs": [(a, b, round(h, 6)) for a, b, h in divergent],
        "dissenters": dissenters,
        "pairs": [(a, b, round(h, 6), None if p is None else round(p, 8))
                  for a, b, h, p in pairs],
        "correction": method,
        "pairwise_tests": m,
        "significant_pairs_raw": sum(raw_sig),
        "significant_pairs_corrected": sum(corrected_sig),
    }


# ---------------------------------------------------------------------------

class QuantumDistributionOracle(FlowFileTransform):
    """
    K-way consensus oracle that votes on **distribution similarity** rather
    than on the single most-likely measured state.

    Use this instead of QuantumConsensusOracle whenever the circuit's output is
    degenerate or multi-peaked -- GHZ, W states, uniform superpositions, or any
    distribution with no meaningful single winner. For single-answer algorithms
    (Grover, phase estimation) QuantumConsensusOracle's top-1 vote remains the
    more interpretable choice; both are kept so the paper can compare them.

    Collection model
    ----------------
    Identical to QuantumConsensusOracle: branches are slotted by Consensus
    Label and buffered on disk until K have arrived, at which point the vote
    runs and the slot is cleared.

    Output relationships
    --------------------
    pass     - all branches sample the same distribution (and match the
               expected support, when given).
    fail     - branches diverge (DISAGREE) or agree on the wrong support (FAIL).
    waiting  - fewer than K branches collected; slot is buffering.
               Auto-terminate this relationship in NiFi.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "K-way consensus over branch measurement distributions, voting on "
            "distribution similarity (max pairwise Hellinger gated by a "
            "chi-squared homogeneity test) instead of the top-1 measured state. "
            "Use for degenerate or multi-peaked outputs such as GHZ, where a "
            "top-1 vote is a coin flip and reports DISAGREE on correct code. "
            "Ground truth is checked as expected support, not a single winner. "
            "Writes an HTML consensus card."
        )
        tags = ["quantum", "consensus", "oracle", "hellinger", "distribution",
                "n-version", "ghz", "report"]
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
            description="Report filename: {flow_name}-distribution-consensus.html.",
            required=True,
            default_value="quanifi",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.state_dir = PropertyDescriptor(
            name="State Directory",
            description=(
                "Directory where partial slots are persisted between FlowFile "
                "arrivals. Must be writable by NiFi."
            ),
            required=True,
            default_value="reports/tmp/quanifi_distoracle_state",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.consensus_label = PropertyDescriptor(
            name="Consensus Label",
            description=(
                "Slot key (Expression Language). All branches of one test case "
                "must produce the same key. Default gives one slot per case so "
                "concurrent cases do not collide."
            ),
            required=True,
            default_value="${test.run_id}-${test.case_id}",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.expected_branches = PropertyDescriptor(
            name="Expected Branches",
            description=(
                "K - how many framework branches feed one slot before the oracle "
                "votes (e.g. 3 for Qiskit/Cirq/Qrisp)."
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
        self.hellinger_threshold = PropertyDescriptor(
            name="Agreement Threshold",
            description=(
                "Maximum pairwise Hellinger distance still counted as agreement. "
                "On noiseless simulators 0.1 is appropriate. On real hardware it "
                "MUST be calibrated per device from control replicates - the "
                "device's own noise floor can exceed 0.1 on its own, which would "
                "flag every run as a disagreement. Supports Expression Language "
                "so a calibrated value can be injected per run."
            ),
            required=True,
            default_value="0.1",
            validators=[StandardValidators.NUMBER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.alpha = PropertyDescriptor(
            name="Significance Level",
            description=(
                "Alpha for the chi-squared homogeneity test on raw counts. A pair "
                "is only called divergent when p < alpha AND the Hellinger "
                "distance exceeds the Agreement Threshold. Ignored when the "
                "branches carry probabilities rather than integer counts."
            ),
            required=True,
            default_value="0.05",
            validators=[StandardValidators.NUMBER_VALIDATOR],
        )
        self.correction = PropertyDescriptor(
            name="Multiple Comparison Correction",
            description=(
                "How the per-pair chi-squared p-values are corrected for multiplicity. "
                "With K branches the oracle runs K(K-1)/2 pairwise tests (3 branches -> 3, "
                "20 branches -> 190), so an uncorrected alpha makes false divergences "
                "near-certain at large K. 'none': each pair tested at alpha (legacy "
                "behaviour). 'holm': Holm-Bonferroni step-down, controls the family-wise "
                "error rate across the slot's pairs. 'benjamini-hochberg': step-up, "
                "controls the false discovery rate. Only pairs with raw counts count "
                "toward the number of tests; a pair diverges only if its corrected test "
                "rejects AND its Hellinger distance exceeds the Agreement Threshold. "
                "No default: choose deliberately."),
            required=True,
            allowable_values=["none", "holm", "benjamini-hochberg"],
        )
        self.expected_support = PropertyDescriptor(
            name="Expected Support",
            description=(
                "Optional comma-separated bitstrings the agreed distribution "
                "should put its weight on - for GHZ-3, '000,111'. Checked as a "
                "SET, not a single winner, which is what makes ground truth "
                "meaningful for degenerate outputs. Reads ${test.expected_support} "
                "by default; leave the attribute unset to skip the check."
            ),
            required=False,
            default_value="${test.expected_support}",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.reports_dir,
            self.flow_name,
            self.state_dir,
            self.consensus_label,
            self.expected_branches,
            self.branch_label,
            self.hellinger_threshold,
            self.alpha,
            self.correction,
            self.expected_support,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def getRelationships(self):
        return [
            Relationship(name="pass", description="Verdict PASS.", auto_terminated=False),
            Relationship(name="fail", description="Verdict FAIL or DISAGREE.", auto_terminated=False),
            Relationship(name="waiting", description="Buffering; <K branches so far.", auto_terminated=False),
        ]

    # -- state sidecar --------------------------------------------------------

    def _slot_path(self, state_dir, label):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        return os.path.join(state_dir, f"{safe}.json")

    def _load_slot(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
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
        branch = (get(self.branch_label) or "").strip()
        try:
            threshold = float(get(self.hellinger_threshold))
        except (TypeError, ValueError):
            threshold = 0.1
        try:
            alpha = float(context.getProperty(self.alpha).getValue())
        except (TypeError, ValueError):
            alpha = 0.05
        correction = normalize_method(context.getProperty(self.correction).getValue())
        expected_states = [s.strip() for s in (get(self.expected_support) or "").split(",")
                           if s.strip()]

        attrs = {}
        try:
            attrs = dict(flowFile.getAttributes())
        except Exception:
            pass
        raw = bytes(flowFile.getContentsAsBytes())

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self.logger.error("QuantumDistributionOracle: JSON parse error: {}".format(exc))
            return FlowFileTransformResult(
                relationship="waiting", contents=raw,
                attributes={"consensus.error": "JSON parse error: {}".format(exc)})
        if not isinstance(parsed, dict) or not parsed:
            msg = "expected a non-empty {{state: count}} object, got {}".format(
                type(parsed).__name__)
            self.logger.error("QuantumDistributionOracle: {}".format(msg))
            return FlowFileTransformResult(
                relationship="waiting", contents=raw,
                attributes={"consensus.error": msg})
        if correction not in CORRECTIONS:
            msg = f"unknown Multiple Comparison Correction {correction!r}"
            self.logger.error("QuantumDistributionOracle: {}".format(msg))
            return FlowFileTransformResult(
                relationship="waiting", contents=raw,
                attributes={"consensus.error": msg})

        # Keep the raw counts alongside the normalised distribution: the
        # chi-squared gate needs integer shot counts, and normalising throws
        # away the shot information it depends on.
        counts = (parsed if all(isinstance(v, int) and not isinstance(v, bool)
                                for v in parsed.values()) else None)
        dist = _normalize(parsed)

        os.makedirs(state_dir, exist_ok=True)
        slot_path = self._slot_path(state_dir, label)
        slot = self._load_slot(slot_path)
        sim_fw = attrs.get("sim.framework", "")
        b_comp = attrs.get("builder.component") or attrs.get("builder.framework")
        s_comp = attrs.get("sim.component") or attrs.get("sim.framework") or sim_fw
        if b_comp and s_comp:
            branch = f"{b_comp} and {s_comp}"
        elif not branch:
            branch = s_comp or b_comp or f"branch-{len(slot['entries']) + 1}"
        slot["entries"].append({"label": branch, "dist": dist, "counts": counts})

        if len(slot["entries"]) < k:
            with open(slot_path, "w", encoding="utf-8") as f:
                json.dump(slot, f)
            self.logger.warn(
                "QuantumDistributionOracle [{}]: branch '{}' stored ({}/{}), waiting.".format(
                    label, branch, len(slot["entries"]), k))
            return FlowFileTransformResult(
                relationship="waiting", contents=raw,
                attributes={"consensus.status": "waiting", "consensus.label": label,
                            "consensus.have": str(len(slot["entries"])), "consensus.need": str(k)},
            )

        res = _distribution_consensus(slot["entries"], threshold, alpha, expected_states,
                                      correction)
        try:
            os.remove(slot_path)
        except OSError:
            pass

        case_id = attrs.get("test.case_id", attrs.get("assert.case_id", ""))
        run_id  = attrs.get("test.run_id", "")
        verdict = res["verdict"]

        out_attrs = {
            "report.type":                  "consensus",
            "assert.verdict":               verdict,
            "assert.case_id":               case_id,
            "assert.run_id":                run_id,
            "assert.reason":                res["reason"],
            "consensus.mode":               "distribution",
            "consensus.label":              label,
            "consensus.branches":           str(res["branches"]),
            "consensus.max_hellinger":      f"{res['max_hellinger']:.4f}",
            "consensus.threshold":          f"{threshold:g}",
            "consensus.alpha":              f"{alpha:g}",
            "consensus.divergent_pairs":    str(len(res["divergent_pairs"])),
            "consensus.dissenters":         ",".join(res["dissenters"]),
            "consensus.dissenter_count":    str(len(res["dissenters"])),
            "consensus.expected_support":   ",".join(expected_states),
            "consensus.correction":               res["correction"],
            "consensus.pairwise_tests":           str(res["pairwise_tests"]),
            "consensus.significant_pairs_raw":    str(res["significant_pairs_raw"]),
            "consensus.significant_pairs_corrected": str(res["significant_pairs_corrected"]),
        }
        if res["min_p_value"] is not None:
            out_attrs["consensus.min_p_value"] = f"{res['min_p_value']:.6g}"
        # Pass the mutation bookkeeping through so MutationScoreReport can score it.
        for key in ("mut.applied", "mut.operator", "mut.target_attr",
                    "mut.original_value", "mut.seed", "mut.base_case_id",
                    "test.run_id", "test.case_id", "test.partition", "test.expected"):
            if key in attrs:
                out_attrs[key] = attrs[key]

        self.logger.warn(
            "QuantumDistributionOracle [{}]: {} over {} branches, maxH={:.4f} "
            "(threshold {:g}), divergent pairs={}, dissenters=[{}] | {} | "
            "correction={} m={} sig(raw/corrected)={}/{}".format(
                label, verdict, res["branches"], res["max_hellinger"], threshold,
                len(res["divergent_pairs"]), ",".join(res["dissenters"]), res["reason"],
                res["correction"], res["pairwise_tests"], res["significant_pairs_raw"],
                res["significant_pairs_corrected"]))

        self._write_report(reports_dir, flow_name, label, run_id, case_id,
                           slot["entries"], res, threshold, alpha, expected_states)

        relationship = "pass" if verdict == "PASS" else "fail"
        return FlowFileTransformResult(relationship=relationship, contents=raw,
                                       attributes=out_attrs)

    # -- HTML report ----------------------------------------------------------

    def _write_report(self, reports_dir, flow_name, label, run_id, case_id,
                      entries, res, threshold, alpha, expected_states):
        os.makedirs(reports_dir, exist_ok=True)
        data_path = os.path.join(reports_dir, f"{flow_name}-data.json")
        output_path = os.path.join(reports_dir, f"{flow_name}-distribution-consensus.html")

        # Load existing cases
        runs = {}
        if os.path.exists(data_path):
            try:
                with open(data_path, "r", encoding="utf-8") as f:
                    runs = json.load(f)
            except Exception:
                runs = {}

        # Format current run entry
        pair_list = []
        for a, b, h, p in res["pairs"]:
            status = "diverges" if h > threshold else "agrees"
            pair_list.append({
                "pair": f"{a} <-> {b}",
                "hellinger": float(h),
                "p_val": "--" if p is None else f"{p:.4g}",
                "status": status
            })

        formatted_branches = []
        for e in entries:
            dist_items = sorted(e.get("dist", {}).items(), key=lambda kv: -kv[1])
            top_s = dist_items[0][0] if dist_items else "--"
            top_p = dist_items[0][1] if dist_items else 0.0
            formatted_branches.append({
                "framework": e.get("label", ""),
                "top_state": top_s,
                "top_pct": float(top_p),
                "dist": [{"state": s, "pct": float(pct), "label": f"{pct:.1f}%"}
                         for s, pct in dist_items[:8]]
            })

        runs[label] = {
            "id": label,
            "case_id": case_id,
            "run_id": run_id,
            "verdict": res["verdict"],
            "max_hellinger": float(res["max_hellinger"]),
            "branches": formatted_branches,
            "pairs": pair_list,
            "timestamp": now(),
            "correction": res["correction"],
            "pairwise_tests": res["pairwise_tests"],
            "significant_pairs_raw": res["significant_pairs_raw"],
            "significant_pairs_corrected": res["significant_pairs_corrected"],
        }

        try:
            with open(data_path, "w", encoding="utf-8") as f:
                json.dump(runs, f, indent=2)
        except Exception as exc:
            self.logger.warn(f"Failed to write {data_path}: {exc}")

        try:
            from dashboard_generator import render_matrix_dashboard
            render_matrix_dashboard(runs, output_path, flow_name=flow_name)
        except Exception as exc:
            self.logger.warn(f"Dashboard generator error: {exc}")
