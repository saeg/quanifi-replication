"""Tests for QuantumDistributionOracle.

The processor exists because QuantumConsensusOracle votes on the top-1 measured
state, which is a coin flip for a degenerate output like GHZ. The regression
that matters most is TestGHZDegeneracy: three *correct* GHZ branches must agree
essentially always, where the top-1 oracle agrees only ~25% of the time.

Everything runs offline -- the oracle is framework-agnostic and consumes counts
JSON, so no simulator is needed.
"""
import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

from conftest import MockContext, MockFlowFile, result_to_flowfile

from QuantumDistributionOracle import QuantumDistributionOracle, _distribution_consensus


def counts_flowfile(counts, branch, tmp_path, label="run-case", **extra):
    attrs = {"sim.framework": branch, "test.run_id": "run", "test.case_id": "case"}
    attrs.update(extra)
    return MockFlowFile(content=json.dumps(counts).encode(), attributes=attrs)


def ctx(tmp_path, **overrides):
    props = {
        "Reports Directory": str(tmp_path / "reports"),
        "Flow Name": "test",
        "State Directory": str(tmp_path / "state"),
        "Consensus Label": "slot",
        "Expected Branches": "3",
        "Branch Label": "${sim.framework}",
        "Agreement Threshold": "0.1",
        "Significance Level": "0.05",
        "Expected Support": "",
    }
    props.update(overrides)
    return MockContext(**props)


def feed(branches, tmp_path, **overrides):
    """Push each (label, counts) through one oracle instance; return last result."""
    oracle = QuantumDistributionOracle()
    result = None
    for label, counts in branches:
        result = oracle.transform(ctx(tmp_path, **overrides),
                                  counts_flowfile(counts, label, tmp_path))
    return result


def ghz_counts(n, shots, rng):
    """A correct GHZ sample: shots split binomially between |0..0> and |1..1>."""
    zeros = sum(1 for _ in range(shots) if rng.random() < 0.5)
    return {"0" * n: zeros, "1" * n: shots - zeros}


# ---------------------------------------------------------------------------

class TestGHZDegeneracy:
    """The regression this processor was built for."""

    def test_three_correct_ghz_branches_agree(self, tmp_path):
        """Top-1 voting agrees ~25% of the time here; distributional must be ~100%.

        Each branch's most-likely state is a coin flip between |0..0> and
        |1..1>, so a top-1 oracle calls DISAGREE on correct code about three
        runs in four. The distributional oracle looks at the whole shape and
        must pass every time.
        """
        rng = random.Random(7)
        passes = 0
        trials = 25
        for trial in range(trials):
            branches = [(fw, ghz_counts(16, 662, rng))
                        for fw in ("qiskit", "cirq", "qrisp")]
            result = feed(branches, tmp_path / str(trial))
            if result.relationship == "pass":
                passes += 1
        assert passes == trials, (
            "expected all %d trials to pass, got %d" % (trials, passes))

    def test_top1_would_have_disagreed(self, tmp_path):
        """Sanity-check the premise: branches whose top-1 differs still agree.

        Two branches that are the same distribution but land on opposite sides
        of the 50/50 split -- exactly the case that breaks a top-1 vote.
        """
        branches = [
            ("qiskit", {"0000": 340, "1111": 322}),
            ("cirq", {"0000": 322, "1111": 340}),   # top-1 differs from qiskit
            ("qrisp", {"0000": 331, "1111": 331}),
        ]
        result = feed(branches, tmp_path)
        flow = result_to_flowfile(result)
        assert result.relationship == "pass"
        assert flow.getAttributes()["consensus.dissenter_count"] == "0"


class TestVerdicts:
    def test_buffers_until_k_branches(self, tmp_path):
        oracle = QuantumDistributionOracle()
        first = oracle.transform(ctx(tmp_path),
                                 counts_flowfile({"00": 500, "11": 500}, "qiskit", tmp_path))
        assert first.relationship == "waiting"
        attrs = result_to_flowfile(first).getAttributes()
        assert attrs["consensus.have"] == "1"
        assert attrs["consensus.need"] == "3"

    def test_divergent_branch_is_isolated(self, tmp_path):
        """One branch sampling a disjoint distribution must be named as dissenter."""
        branches = [
            ("qiskit", {"0000": 331, "1111": 331}),
            ("cirq", {"1000": 331, "0111": 331}),   # parity-flipped: disjoint support
            ("qrisp", {"0000": 340, "1111": 322}),
        ]
        result = feed(branches, tmp_path)
        flow = result_to_flowfile(result)
        attrs = flow.getAttributes()
        assert result.relationship == "fail"
        assert attrs["assert.verdict"] == "DISAGREE"
        assert attrs["consensus.dissenters"] == "cirq"
        assert float(attrs["consensus.max_hellinger"]) > 0.9

    def test_expected_support_catches_whole_case_fault(self, tmp_path):
        """All branches agree but on the wrong states -> FAIL, not DISAGREE."""
        branches = [("qiskit", {"1000": 331, "0111": 331}),
                    ("cirq", {"1000": 325, "0111": 337}),
                    ("qrisp", {"1000": 340, "0111": 322})]
        result = feed(branches, tmp_path, **{"Expected Support": "0000,1111"})
        attrs = result_to_flowfile(result).getAttributes()
        assert result.relationship == "fail"
        assert attrs["assert.verdict"] == "FAIL"

    def test_expected_support_passes_when_correct(self, tmp_path):
        branches = [("qiskit", {"0000": 331, "1111": 331}),
                    ("cirq", {"0000": 325, "1111": 337}),
                    ("qrisp", {"0000": 340, "1111": 322})]
        result = feed(branches, tmp_path, **{"Expected Support": "0000,1111"})
        assert result.relationship == "pass"
        assert result_to_flowfile(result).getAttributes()["assert.verdict"] == "PASS"

    def test_threshold_is_configurable(self, tmp_path):
        """A calibrated threshold must be able to absorb a device noise floor."""
        branches = [("a", {"0000": 400, "1111": 262}),
                    ("b", {"0000": 262, "1111": 400}),
                    ("c", {"0000": 331, "1111": 331})]
        strict = feed(branches, tmp_path / "strict", **{"Agreement Threshold": "0.01"})
        loose = feed(branches, tmp_path / "loose", **{"Agreement Threshold": "0.9"})
        assert strict.relationship == "fail"
        assert loose.relationship == "pass"


class TestContract:
    def test_emits_contract_attributes(self, tmp_path):
        branches = [("qiskit", {"00": 500, "11": 500}),
                    ("cirq", {"00": 495, "11": 505}),
                    ("qrisp", {"00": 502, "11": 498})]
        attrs = result_to_flowfile(feed(branches, tmp_path)).getAttributes()
        assert attrs["report.type"] == "consensus"
        assert attrs["consensus.mode"] == "distribution"
        assert attrs["consensus.branches"] == "3"
        assert attrs["consensus.threshold"] == "0.1"
        assert "consensus.max_hellinger" in attrs
        assert "consensus.min_p_value" in attrs
        assert attrs["assert.run_id"] == "run"
        assert attrs["assert.case_id"] == "case"

    def test_passes_mutation_bookkeeping_through(self, tmp_path):
        """MutationScoreReport downstream needs these to score a mutant."""
        oracle = QuantumDistributionOracle()
        result = None
        for label, counts in [("qiskit", {"00": 500, "11": 500}),
                              ("cirq", {"00": 495, "11": 505}),
                              ("qrisp", {"00": 502, "11": 498})]:
            flow = counts_flowfile(counts, label, tmp_path,
                                   **{"mut.applied": "true", "mut.operator": "gate.remove",
                                      "mut.seed": "42"})
            result = oracle.transform(ctx(tmp_path), flow)
        attrs = result_to_flowfile(result).getAttributes()
        assert attrs["mut.applied"] == "true"
        assert attrs["mut.operator"] == "gate.remove"
        assert attrs["mut.seed"] == "42"

    def test_writes_html_report(self, tmp_path):
        branches = [("qiskit", {"00": 500, "11": 500}),
                    ("cirq", {"00": 495, "11": 505}),
                    ("qrisp", {"00": 502, "11": 498})]
        feed(branches, tmp_path)
        report = tmp_path / "reports" / "test-distribution-consensus.html"
        assert report.exists()
        body = report.read_text()
        assert "PASS" in body and "Hellinger" in body


class TestErrorPaths:
    def test_malformed_json_routes_to_waiting_with_error(self, tmp_path):
        oracle = QuantumDistributionOracle()
        flow = MockFlowFile(content=b"not json", attributes={"sim.framework": "qiskit"})
        result = oracle.transform(ctx(tmp_path), flow)
        assert result.relationship == "waiting"
        assert "JSON parse error" in result_to_flowfile(result).getAttributes()["consensus.error"]

    def test_non_object_payload_routes_to_waiting_with_error(self, tmp_path):
        oracle = QuantumDistributionOracle()
        flow = MockFlowFile(content=b"[1, 2, 3]", attributes={"sim.framework": "qiskit"})
        result = oracle.transform(ctx(tmp_path), flow)
        assert result.relationship == "waiting"
        assert "non-empty" in result_to_flowfile(result).getAttributes()["consensus.error"]

    def test_probabilities_without_counts_still_vote(self, tmp_path):
        """Float inputs carry no shot information, so the chi2 gate is skipped
        and the Hellinger gate decides alone."""
        branches = [("a", {"00": 0.5, "11": 0.5}),
                    ("b", {"00": 0.51, "11": 0.49}),
                    ("c", {"00": 0.49, "11": 0.51})]
        result = feed(branches, tmp_path)
        assert result.relationship == "pass"
        assert "consensus.min_p_value" not in result_to_flowfile(result).getAttributes()

    def test_builder_and_sim_component_branch_naming(self, tmp_path):
        oracle = QuantumDistributionOracle()
        c = ctx(tmp_path)
        r1 = oracle.transform(c, counts_flowfile({"11": 100}, "qiskit", tmp_path,
            **{"builder.component": "QiskitGrover", "sim.component": "CirqSimulator"}))
        assert r1.relationship == "waiting"
        r2 = oracle.transform(c, counts_flowfile({"11": 100}, "cirq", tmp_path,
            **{"builder.component": "CirqGrover", "sim.component": "QrispSimulator"}))
        assert r2.relationship == "waiting"
        r3 = oracle.transform(c, counts_flowfile({"00": 100}, "braket", tmp_path,
            **{"builder.component": "PennylaneGrover", "sim.component": "BraketSimulator"}))
        assert r3.relationship == "fail"
        out = result_to_flowfile(r3).getAttributes()
        assert out["assert.verdict"] == "DISAGREE"
        assert "PennylaneGrover and BraketSimulator" in out["consensus.dissenters"]



class TestConsensusCore:
    """Direct tests of the pure verdict function."""

    def test_empty_and_single_branch(self):
        assert _distribution_consensus([], 0.1, 0.05)["verdict"] == "DISAGREE"
        one = _distribution_consensus([{"label": "a", "dist": {"0": 1.0}}], 0.1, 0.05)
        assert one["verdict"] == "DISAGREE"
        assert "one branch" in one["reason"]

    def test_dissenter_needs_to_diverge_from_all_others(self):
        """A branch that diverges from only some peers is not isolated."""
        entries = [
            {"label": "a", "dist": {"0": 1.0}, "counts": None},
            {"label": "b", "dist": {"0": 0.5, "1": 0.5}, "counts": None},
            {"label": "c", "dist": {"1": 1.0}, "counts": None},
        ]
        res = _distribution_consensus(entries, 0.1, 0.05)
        assert res["verdict"] == "DISAGREE"
        # a~c diverge, and each also diverges from b, so all three are isolated
        assert set(res["dissenters"]) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# Multiple Comparison Correction
# ---------------------------------------------------------------------------
#
# Fixture: a={"0":66,"1":34}; b,c,d={"0":50,"1":50}. Every a~x pair has
# H=0.1150, p=0.02189 (raw-significant at alpha=0.05, and H exceeds the
# default 0.1 threshold); every b/c/d pair has H=0, p=1.0. m=6 pairs total,
# 3 of them (all involving "a") raw-significant. Holm's alpha/(m-rank)
# schedule at rank 0 is alpha/6 = 0.00833, well under 0.02189, so Holm
# rejects nothing here -- correction alone flips the verdict from DISAGREE to
# PASS on data that never changed.

FOUR_BRANCH_MCC_FIXTURE = [
    ("a", {"0": 66, "1": 34}),
    ("b", {"0": 50, "1": 50}),
    ("c", {"0": 50, "1": 50}),
    ("d", {"0": 50, "1": 50}),
]


class TestMultipleComparisonCorrectionCore:
    """Direct tests of _distribution_consensus's correction handling."""

    def test_raw_flags_but_holm_does_not(self):
        entries = [{"label": label, "dist": {k: v / sum(counts.values())
                                             for k, v in counts.items()},
                    "counts": counts}
                  for label, counts in FOUR_BRANCH_MCC_FIXTURE]

        default = _distribution_consensus(entries, 0.1, 0.05)
        assert default["verdict"] == "DISAGREE"
        assert default["dissenters"] == ["a"]
        assert default["correction"] == "none"
        assert default["pairwise_tests"] == 6
        assert default["significant_pairs_raw"] == 3
        assert default["significant_pairs_corrected"] == 3

        holm = _distribution_consensus(entries, 0.1, 0.05, correction="holm")
        assert holm["verdict"] == "PASS"
        assert holm["significant_pairs_raw"] == 3
        assert holm["significant_pairs_corrected"] == 0
        assert holm["divergent_pairs"] == []

        bh = _distribution_consensus(entries, 0.1, 0.05, correction="benjamini-hochberg")
        assert bh["verdict"] == "DISAGREE"
        assert bh["significant_pairs_corrected"] == 3

    def test_none_pairs_keep_hellinger_gate(self):
        """No raw counts -> the chi2 gate can't run -> correction is moot:
        the Hellinger gate alone still decides, same as before this property
        existed."""
        entries = [
            {"label": "a", "dist": {"0": 1.0}, "counts": None},
            {"label": "b", "dist": {"0": 0.5, "1": 0.5}, "counts": None},
            {"label": "c", "dist": {"1": 1.0}, "counts": None},
        ]
        res = _distribution_consensus(entries, 0.1, 0.05, correction="holm")
        assert res["verdict"] == "DISAGREE"
        assert res["pairwise_tests"] == 0

    def test_unknown_correction_raises(self):
        entries = [{"label": "a", "dist": {"0": 1.0}, "counts": None},
                  {"label": "b", "dist": {"0": 1.0}, "counts": None}]
        with pytest.raises(ValueError):
            _distribution_consensus(entries, 0.1, 0.05, correction="bonferroni")


def mcc_ctx(tmp_path, expected_branches="3", **overrides):
    props = {
        "Reports Directory": str(tmp_path / "reports"),
        "Flow Name": "test",
        "State Directory": str(tmp_path / "state"),
        "Consensus Label": "slot",
        "Expected Branches": expected_branches,
        "Branch Label": "${sim.framework}",
        "Agreement Threshold": "0.1",
        "Significance Level": "0.05",
        "Expected Support": "",
    }
    props.update(overrides)
    return MockContext(**props)


class TestMultipleComparisonCorrectionProcessor:
    def test_unset_defaults_to_none(self, tmp_path):
        oracle = QuantumDistributionOracle()
        result = None
        for label, counts in [("qiskit", {"00": 500, "11": 500}),
                              ("cirq", {"00": 495, "11": 505}),
                              ("qrisp", {"00": 502, "11": 498})]:
            result = oracle.transform(mcc_ctx(tmp_path),
                                      counts_flowfile(counts, label, tmp_path))
        attrs = result_to_flowfile(result).getAttributes()
        assert attrs["consensus.correction"] == "none"
        assert attrs["consensus.pairwise_tests"] == "3"

    def test_holm_flips_verdict_relative_to_unset(self, tmp_path):
        def feed_four(path, **overrides):
            oracle = QuantumDistributionOracle()
            result = None
            for label, counts in FOUR_BRANCH_MCC_FIXTURE:
                result = oracle.transform(
                    mcc_ctx(path, expected_branches="4", **overrides),
                    counts_flowfile(counts, label, path))
            return result

        default_result = feed_four(tmp_path / "default")
        default_attrs = result_to_flowfile(default_result).getAttributes()
        assert default_result.relationship == "fail"
        assert default_attrs["consensus.pairwise_tests"] == "6"
        assert default_attrs["consensus.significant_pairs_raw"] == "3"
        assert default_attrs["consensus.significant_pairs_corrected"] == "3"

        holm_path = tmp_path / "holm"
        holm_result = feed_four(holm_path, **{"Multiple Comparison Correction": "holm"})
        holm_attrs = result_to_flowfile(holm_result).getAttributes()
        assert holm_result.relationship == "pass"
        assert holm_attrs["consensus.pairwise_tests"] == "6"
        assert holm_attrs["consensus.significant_pairs_raw"] == "3"
        assert holm_attrs["consensus.significant_pairs_corrected"] == "0"

        report = holm_path / "reports" / "test-distribution-consensus.html"
        body = report.read_text()
        assert "Multiple-comparison correction" in body
        assert "holm" in body

    def test_invalid_correction_routes_to_waiting_with_error(self, tmp_path):
        oracle = QuantumDistributionOracle()
        flow = counts_flowfile({"00": 500, "11": 500}, "qiskit", tmp_path)
        result = oracle.transform(
            mcc_ctx(tmp_path, **{"Multiple Comparison Correction": "bonferroni"}), flow)
        assert result.relationship == "waiting"
        error = result_to_flowfile(result).getAttributes()["consensus.error"]
        assert "Multiple Comparison Correction" in error


class TestMultipleComparisonCorrectionDescriptor:
    def test_descriptor_shape(self):
        oracle = QuantumDistributionOracle()
        d = oracle.correction
        assert d.name == "Multiple Comparison Correction"
        assert d.required is True
        assert d.default_value is None
        assert d.allowable_values == ["none", "holm", "benjamini-hochberg"]
        assert "K(K-1)/2" in d.description


class TestForkParity:
    """nifi_extensions_matrix/QuantumDistributionOracle.py must behave
    identically to the upstream copy on the correction feature, since the
    plan hand-syncs the two rather than importing one from the other."""

    def test_holm_matches_upstream(self):
        fork_path = str(Path(__file__).resolve().parent.parent
                        / "nifi_extensions_matrix" / "QuantumDistributionOracle.py")
        saved_path = list(sys.path)
        try:
            spec = importlib.util.spec_from_file_location("QDO_fork", fork_path)
            fork_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fork_mod)
        finally:
            # The fork module inserts its own directory onto sys.path as a
            # side effect of executing; undo that so it can't shadow the
            # upstream copy for any test that runs after this one. Leave
            # sys.modules["multiple_comparisons"] alone: the fork's copy is
            # byte-identical to upstream's, so reusing the cached module is
            # correct, not a shadowing bug.
            sys.path[:] = saved_path

        entries = [{"label": label, "dist": {k: v / sum(counts.values())
                                             for k, v in counts.items()},
                    "counts": counts}
                  for label, counts in FOUR_BRANCH_MCC_FIXTURE]

        upstream_res = _distribution_consensus(entries, 0.1, 0.05, correction="holm")
        fork_res = fork_mod._distribution_consensus(entries, 0.1, 0.05, correction="holm")
        assert fork_res["verdict"] == upstream_res["verdict"] == "PASS"
        assert fork_res["pairwise_tests"] == upstream_res["pairwise_tests"]
        assert fork_res["significant_pairs_corrected"] == upstream_res["significant_pairs_corrected"]

        fork_oracle = fork_mod.QuantumDistributionOracle()
        assert fork_oracle.correction.allowable_values == ["none", "holm", "benjamini-hochberg"]
        assert fork_oracle.correction.required is True
