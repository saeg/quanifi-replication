"""Unit tests for the Grover N-M hardware matrix analysis (WP5).

This is the analysis PREREG-grover-hw-matrix.md exists to make possible: its
thresholds and tests must be provable BEFORE any hardware job is armed, on
data whose ground truth is known because it was planted. The synthetic
generator below draws counts from ``numpy`` binomial at KNOWN per-builder
success probabilities, with a planted post-routing gate-count ordering, a
planted readout ceiling below 1.0, and a planted mutant ladder (some deltas
above the 0.10 threshold, some below, one "no qualifying mutant"). Every test
either recovers a planted quantity or proves a degenerate input cannot crash
the script.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))

import grover_hw_matrix_analysis as A  # noqa: E402

SHOTS = 1024

#: Cases mirror PREREG §2's fixed table: two 2-qubit cases (not tested,
#: floor calibration only) and two 4-qubit cases (the primary hypothesis).
CASES = [
    {"case_id": "10", "qubits": 2, "partition": "asymmetric-2q"},
    {"case_id": "11", "qubits": 2, "partition": "palindrome-2q"},
    {"case_id": "0111", "qubits": 4, "partition": "asymmetric-4q"},
    {"case_id": "0110", "qubits": 4, "partition": "palindrome-4q"},
]

#: Planted post-routing two-qubit gate counts, ascending in the order the
#: script is expected to recover: qiskit < pennylane < cirq.
ISA_GATES = {"qiskit": 103, "pennylane": 125, "cirq": 148}

#: Planted 4-qubit success probabilities, decreasing with gate count so the
#: builder effect (PREREG §1) is real: qiskit(0.55) > pennylane(0.35) >
#: cirq(0.20), both adjacent gaps (0.20, 0.15) clear the 0.10 minimum.
P_4Q = {"qiskit": 0.55, "pennylane": 0.35, "cirq": 0.20}

#: 2-qubit success probabilities sit close together near the ceiling -- the
#: design has no power to separate builders there (PREREG §4/§7), which is
#: exactly why the 2-qubit cases are descriptive only.
P_2Q = {"qiskit": 0.93, "pennylane": 0.90, "cirq": 0.88}

#: Readout ceiling planted BELOW 1.0 for every case.
READOUT_P = {"10": 0.97, "11": 0.97, "0111": 0.95, "0110": 0.95}

#: The mutant ladder (Amendment A2, which supersedes PREREG §5's single
#: seed-searched mutant): three rungs per 4-qubit (builder, case), one rung
#: (``large``) per 2-qubit (builder, case). ``small`` is planted BELOW the
#: 0.10 minimum on purpose, so it must never be flagged; ``medium``/``large``
#: are planted comfortably above it, so both must always be flagged. One
#: cell has no qualifying mutant at all (a rotation.perturb search that never
#: reached a usable effect), and one rung is marked ``off_target`` to prove
#: that flag passes through the script verbatim.
#: Planted well clear of both sides of the 0.10 minimum so detection is
#: unambiguous regardless of binomial noise at 1024 shots -- unlike the real
#: ladder's achieved ``small`` (0.1001, deliberately borderline), these
#: values only need to exercise the script's rung logic, not mirror the real
#: numbers. Real schema, per ``experiments/grover_hw_mutants.json``
#: (top-level ``{"provenance": ..., "mutants": [...]}``): each entry carries
#: ``builder``, ``case``, ``rung``, ``operator``, ``locus``, ``delta_sim``,
#: ``target_delta``, ``epsilon`` and ``off_target`` -- the last three all
#: ``null`` for a ``large`` (``gate.remove``) entry, which has no target.
RUNG_DELTA = {"small": 0.03, "medium": 0.25, "large": 0.45}
RUNG_OPERATOR = {"small": "rotation.perturb", "medium": "rotation.perturb",
                 "large": "gate.remove"}

MUTANT_LADDER = {}
for _builder in ("qiskit", "pennylane", "cirq"):
    for _case_id in ("0111", "0110"):
        for _rung in ("small", "medium", "large"):
            MUTANT_LADDER[(_builder, _case_id, _rung)] = {
                "delta_sim": RUNG_DELTA[_rung],
                "target_delta": None if _rung == "large" else RUNG_DELTA[_rung],
                "epsilon": None if _rung == "large" else 0.1,
                "off_target": False if _rung != "large" else None,
                "operator": RUNG_OPERATOR[_rung], "locus": "middle",
            }
    for _case_id in ("10", "11"):
        MUTANT_LADDER[(_builder, _case_id, "large")] = {
            "delta_sim": RUNG_DELTA["large"], "target_delta": None,
            "epsilon": None, "off_target": None,
            "operator": "gate.remove", "locus": "middle",
        }
del _builder, _case_id, _rung

# One "no qualifying mutant" cell -- kept for defensive coverage even though
# the real ladder currently reports zero of these (every cell qualified).
MUTANT_LADDER[("cirq", "0110", "small")] = {"status": "no qualifying mutant"}
# One off_target=True flag, purely to prove the flag passes through the
# script verbatim -- the real ladder currently reports zero off_target
# entries too, so this exercises code the current data never exercises.
MUTANT_LADDER[("qiskit", "0111", "small")]["off_target"] = True


def _partition_of(case_id):
    return next(c["partition"] for c in CASES if c["case_id"] == case_id)


def _draw_counts(rng, p, shots, marked):
    """A binomial draw at the marked bitstring; the rest of the shots land on
    one other bitstring. The analysis only ever reads counts[marked] and
    sum(counts.values()), so the exact distribution of the "failure" shots
    is immaterial -- only their count matters."""
    successes = int(rng.binomial(shots, p))
    counts = {marked: successes}
    remainder = shots - successes
    if remainder:
        other = "0" * len(marked) if marked != "0" * len(marked) else "1" * len(marked)
        counts[other] = remainder
    return counts


def build_archive(rng, device, *, drop_readout_case=None, include_isa=True,
                  zero_shots_key=None, skip_mutant_hw_key=None,
                  mutant_ladder=MUTANT_LADDER):
    """One (manifest, polled) pair for one device, one replicate: readout +
    3-builder control + a qiskit null per case, plus one mutant PER RUNG
    (small/medium/large at 4 qubits, large only at 2 qubits) per
    ``mutant_ladder``. A mutant row's ``kind`` is its rung name -- this
    script's own manifest convention, since a (builder, case) cell now
    carries up to three mutant circuits rather than one.

    The degenerate-case knobs each isolate exactly one failure mode the
    script must tolerate: ``drop_readout_case`` omits the readout row for one
    case; ``include_isa=False`` omits ``isa_two_qubit_gates`` from every
    entry; ``zero_shots_key`` zeroes one row's counts/shots;
    ``skip_mutant_hw_key`` (a ``(device, builder, case, rung)`` tuple) omits
    one hardware mutant row even though the ladder has a qualifying entry.
    """
    job_id = "job-%s" % device
    manifest_entries, polled_entries = [], []

    def emit(label, kind, builder, case_id, qubits, p, row_key):
        expected = case_id
        counts = _draw_counts(rng, p, SHOTS, expected)
        shots_done = SHOTS
        if row_key == zero_shots_key:
            counts, shots_done = {}, 0
        attrs = {"grover.builder": builder, "grover.expected": expected,
                 "grover.case_id": case_id, "grover.partition": _partition_of(case_id)}
        entry = {"label": label, "kind": kind, "num_qubits": qubits, "attributes": attrs}
        if include_isa and builder in ISA_GATES:
            entry["isa_two_qubit_gates"] = ISA_GATES[builder]
            entry["isa_depth"] = ISA_GATES[builder] + 20
        manifest_entries.append(entry)
        polled_entries.append({"label": label, "counts": counts, "shots_done": shots_done})

    for case in CASES:
        case_id, qubits = case["case_id"], case["qubits"]
        p_table = P_4Q if qubits == 4 else P_2Q

        if case_id != drop_readout_case:
            emit("readout@%s" % case_id, "readout", "readout", case_id, qubits,
                 READOUT_P[case_id], ("readout", case_id))

        for builder in ("qiskit", "pennylane", "cirq"):
            emit("%s@%s@control" % (builder, case_id), "control", builder,
                 case_id, qubits, p_table[builder], ("control", builder, case_id))
            if builder == "qiskit":
                emit("qiskit@%s@null" % case_id, "null", "qiskit", case_id,
                     qubits, p_table[builder], ("null", "qiskit", case_id))

        rungs = ("small", "medium", "large") if qubits == 4 else ("large",)
        for builder in ("qiskit", "pennylane", "cirq"):
            for rung in rungs:
                ladder_row = mutant_ladder.get((builder, case_id, rung))
                if ladder_row is None or ladder_row.get("status") == "no qualifying mutant":
                    continue
                if (device, builder, case_id, rung) == skip_mutant_hw_key:
                    continue
                mutant_p = max(0.0, p_table[builder] - ladder_row["delta_sim"])
                emit("%s@%s@%s" % (builder, case_id, rung), rung, builder,
                     case_id, qubits, mutant_p, ("mutant", builder, case_id, rung))

    manifest = {"job_id": job_id, "device": device, "provider": "test",
                "shots": SHOTS, "entries": manifest_entries}
    polled = {"job_id": job_id, "device": device, "entries": polled_entries}
    return manifest, polled


def write_archive(tmp_path, manifest, polled):
    manifest_path = tmp_path / ("%s.manifest.json" % manifest["job_id"])
    polled_path = tmp_path / ("%s.polled.json" % manifest["job_id"])
    manifest_path.write_text(json.dumps(manifest))
    polled_path.write_text(json.dumps(polled))
    return manifest_path, polled_path


def mutants_as_list():
    """The archive shape: ``{"provenance": ..., "mutants": [...]}`` --
    ``load_mutants`` reads the ``mutants`` key (a plain list is also
    accepted, for a bare-list ladder file)."""
    out = []
    for (builder, case_id, rung), row in MUTANT_LADDER.items():
        record = {"builder": builder, "case": case_id, "rung": rung}
        record.update(row)
        out.append(record)
    return {"provenance": {"generated_by": "test fixture"}, "mutants": out}


def glob_for(tmp_path, suffix):
    return [str(tmp_path / ("*.%s.json" % suffix))]


# ---------------------------------------------------------------------------
# Statistics glue reused from the oracle
# ---------------------------------------------------------------------------

class TestStatisticsGlue:
    def test_wilson_matches_a_known_value(self):
        lo, hi = A.wilson(400, 512)
        assert (round(lo, 4), round(hi, 4)) == (0.7434, 0.8149)

    def test_wilson_of_an_empty_arm_is_maximally_uncertain(self):
        assert A.wilson(0, 0) == (0.0, 1.0)

    def test_one_sided_higher_favours_the_bigger_arm(self):
        _, p = A.one_sided_higher(400, 512, 300, 512)
        assert p < 0.001

    def test_one_sided_higher_of_a_worse_arm_is_not_significant(self):
        _, p = A.one_sided_higher(300, 512, 400, 512)
        assert p > 0.5

    def test_holm_is_not_bonferroni(self):
        adjusted = A.holm([0.01, 0.02, 0.03])
        assert adjusted == [pytest.approx(0.03), pytest.approx(0.04), pytest.approx(0.04)]

    def test_constants_match_prereg_section_4(self):
        assert A.CONFIDENCE == 0.95
        assert A.ALPHA == 0.05
        assert A.MIN_DIFFERENCE == 0.10
        assert A.MULTIPLICITY == "holm"


# ---------------------------------------------------------------------------
# End-to-end: the planted-data acceptance criteria
# ---------------------------------------------------------------------------

class TestPlantedRecovery:
    @pytest.fixture
    def happy_path(self, tmp_path):
        rng = np.random.default_rng(11)
        manifest, polled = build_archive(rng, "test-device")
        write_archive(tmp_path, manifest, polled)
        mutants_path = tmp_path / "mutants.json"
        mutants_path.write_text(json.dumps(mutants_as_list()))
        summary, cells = A.run_analysis(
            glob_for(tmp_path, "manifest"), glob_for(tmp_path, "polled"),
            str(mutants_path),
            # Deliberately the WRONG cli order, to prove the ranking comes
            # from isa_two_qubit_gates and not from this fallback.
            builder_order=("cirq", "pennylane", "qiskit"))
        return summary, cells

    def test_recovers_the_planted_builder_ordering(self, happy_path):
        summary, _ = happy_path
        effect = summary["builder_effect"]["test-device"]
        for case_id in ("0111", "0110"):
            case = effect["cases"][case_id]
            assert case["ranking"] == ["qiskit", "pennylane", "cirq"]
            assert case["ranking_source"] == "isa_two_qubit_gates"

    def test_holm_verdict_matches_the_planted_effect_sizes(self, happy_path):
        """Both 4q cases have adjacent gaps (0.20, 0.15) well clear of the
        0.10 minimum, so H1 must be supported in both."""
        summary, _ = happy_path
        effect = summary["builder_effect"]["test-device"]
        assert effect["h1_supported_cases"] == 2
        assert effect["h1_total_4q_cases"] == 2
        for case_id in ("0111", "0110"):
            case = effect["cases"][case_id]
            assert case["h1_supported"] is True
            for pair in case["pairs"]:
                assert pair["reject"] is True
                assert pair["p_holm"] < A.ALPHA
                assert pair["diff"] >= A.MIN_DIFFERENCE

    def test_flags_exactly_the_planted_mutants_above_threshold(self, happy_path):
        """small (delta_sim 0.03) is planted below the 0.10 minimum and must
        never be flagged; medium (0.25) and large (0.45) are planted well
        above it and must always be flagged -- across every builder and
        every 4-qubit case, plus large on the 2-qubit cases."""
        summary, _ = happy_path
        by_key = {(r["builder"], r["case_id"], r["rung"]): r
                  for r in summary["mutant_detectability"]}
        for builder in ("qiskit", "pennylane", "cirq"):
            for case_id in ("0111", "0110"):
                if (builder, case_id, "small") in by_key and \
                        by_key[(builder, case_id, "small")]["delta_sim"] is not None:
                    row = by_key[(builder, case_id, "small")]
                    assert row["detected"] is False, (builder, case_id, "small")
                    assert row["agrees_with_sim"] is True
                for rung in ("medium", "large"):
                    row = by_key[(builder, case_id, rung)]
                    assert row["detected"] is True, (builder, case_id, rung)
                    assert row["agrees_with_sim"] is True
            for case_id in ("10", "11"):
                row = by_key[(builder, case_id, "large")]
                assert row["detected"] is True, (builder, case_id, "large")

    def test_off_target_flag_passes_through_verbatim(self, happy_path):
        summary, _ = happy_path
        by_key = {(r["builder"], r["case_id"], r["rung"]): r
                  for r in summary["mutant_detectability"]}
        assert by_key[("qiskit", "0111", "small")]["off_target"] is True
        assert by_key[("qiskit", "0111", "medium")]["off_target"] is False
        # A `large` rung has no target in the real ladder, so off_target is
        # `null` there, not `false` -- must not be silently coerced.
        assert by_key[("qiskit", "0111", "large")]["off_target"] is None

    def test_no_qualifying_mutant_cell_is_reported_without_a_test(self, happy_path):
        summary, _ = happy_path
        row = next(r for r in summary["mutant_detectability"]
                  if r["builder"] == "cirq" and r["case_id"] == "0110"
                  and r["rung"] == "small")
        assert row["delta_sim"] is None
        assert row["detected"] is None
        assert "no qualifying mutant" in row["note"]

    def test_recovers_the_smallest_detectable_rung_per_device(self, happy_path):
        """medium and large are detectable everywhere they ran; small is
        planted below threshold for at least one cell -- so the device-wide
        answer must be "medium", the lowest rung detectable EVERYWHERE it
        was tested, not "small"."""
        summary, _ = happy_path
        result = summary["smallest_detectable_rung"]["test-device"]
        assert result["rung"] == "medium"
        assert result["delta_sim"] == pytest.approx(0.25)
        assert result["note"] == ""

    def test_readout_ceiling_and_corrected_value_are_descriptive(self, happy_path):
        _, cells = happy_path
        control = next(c for c in cells
                       if c["kind"] == "control" and c["builder"] == "qiskit"
                       and c["case_id"] == "0111")
        assert control["readout_ceiling"] == pytest.approx(0.95, abs=0.05)
        assert control["corrected_p"] == pytest.approx(
            control["p"] / control["readout_ceiling"], abs=1e-9)

    def test_within_batch_variance_is_labelled_not_a_stability_claim(self, happy_path):
        summary, _ = happy_path
        assert summary["within_batch_variance"]
        for row in summary["within_batch_variance"]:
            assert "not a stability claim" in row["note"]

    def test_2q_cases_carry_no_builder_effect_entries(self, happy_path):
        summary, _ = happy_path
        effect = summary["builder_effect"]["test-device"]
        assert "10" not in effect["cases"]
        assert "11" not in effect["cases"]

    def test_2q_floor_calibration_is_descriptive_only(self, happy_path):
        summary, _ = happy_path
        floor_rows = summary["floor_calibration_2q"]
        case_ids = {r["case_id"] for r in floor_rows}
        assert case_ids == {"10", "11"}
        for row in floor_rows:
            assert row["gap"] is not None
            assert "not tested" in row["note"]


# ---------------------------------------------------------------------------
# Degenerate inputs the script must tolerate without crashing
# ---------------------------------------------------------------------------

class TestDegenerateInputs:
    def test_missing_readout_row_does_not_crash(self, tmp_path):
        rng = np.random.default_rng(12)
        manifest, polled = build_archive(rng, "no-readout-device",
                                         drop_readout_case="0111")
        write_archive(tmp_path, manifest, polled)
        summary, cells = A.run_analysis(glob_for(tmp_path, "manifest"),
                                        glob_for(tmp_path, "polled"), None)
        affected = [c for c in cells if c["case_id"] == "0111"
                   and c["kind"] == "control"]
        assert affected
        for cell in affected:
            assert cell["readout_ceiling"] is None
            assert cell["corrected_p"] is None
        # The builder effect itself is unaffected -- it never needed readout.
        effect = summary["builder_effect"]["no-readout-device"]
        assert effect["cases"]["0111"]["h1_supported"] is True

    def test_missing_isa_two_qubit_gates_falls_back_to_cli_order(self, tmp_path):
        rng = np.random.default_rng(13)
        manifest, polled = build_archive(rng, "no-isa-device", include_isa=False)
        write_archive(tmp_path, manifest, polled)
        summary, _ = A.run_analysis(
            glob_for(tmp_path, "manifest"), glob_for(tmp_path, "polled"), None,
            builder_order=("qiskit", "pennylane", "cirq"))
        effect = summary["builder_effect"]["no-isa-device"]
        for case_id in ("0111", "0110"):
            case = effect["cases"][case_id]
            assert case["ranking"] == ["qiskit", "pennylane", "cirq"]
            assert case["ranking_source"].startswith("cli-order")
        # The verdict itself still comes out right from the CLI-order ranking.
        assert effect["h1_supported_cases"] == 2

    def test_zero_shot_cell_does_not_crash(self, tmp_path):
        rng = np.random.default_rng(14)
        zero_key = ("control", "cirq", "0110")
        manifest, polled = build_archive(rng, "zero-shots-device",
                                         zero_shots_key=zero_key)
        write_archive(tmp_path, manifest, polled)
        summary, cells = A.run_analysis(glob_for(tmp_path, "manifest"),
                                        glob_for(tmp_path, "polled"), None)
        zero_cell = next(c for c in cells if c["kind"] == "control"
                         and c["builder"] == "cirq" and c["case_id"] == "0110")
        assert zero_cell["shots"] == 0
        assert zero_cell["p"] is None
        assert zero_cell["wilson_lo"] is None and zero_cell["wilson_hi"] is None
        # That case's builder-effect pair touching cirq must be skipped, not crash.
        effect = summary["builder_effect"]["zero-shots-device"]
        case_0110 = effect["cases"]["0110"]
        assert case_0110["h1_supported"] is False
        assert len(case_0110["pairs"]) < 2

    def test_no_ladder_file_at_all_still_scores_hardware_but_carries_no_sim(self, tmp_path):
        """The ladder file is produced by a parallel work package and must be
        tolerated absent entirely: hardware mutant rows can still be scored
        (a control-vs-mutant test needs no simulator input), but delta_sim and
        the agreement column are unavailable."""
        rng = np.random.default_rng(15)
        manifest, polled = build_archive(rng, "no-ladder-device")
        write_archive(tmp_path, manifest, polled)
        summary, _ = A.run_analysis(glob_for(tmp_path, "manifest"),
                                    glob_for(tmp_path, "polled"),
                                    str(tmp_path / "does-not-exist.json"))
        assert summary["n_mutant_ladder_entries"] == 0
        by_key = {(r["builder"], r["case_id"], r["rung"]): r
                  for r in summary["mutant_detectability"]}
        for row in by_key.values():
            assert row["delta_sim"] is None
            assert row["agrees_with_sim"] is None
        # A cell whose hardware mutant DID run is still scored purely from
        # hardware -- delta_sim is not required for a control-vs-mutant test.
        assert by_key[("qiskit", "0111", "large")]["detected"] is True
        # The synthetic generator itself never submits a mutant circuit for
        # the "no qualifying mutant" cell/rung, and with no ladder file there
        # is no ladder-side row either -- the (builder, case, rung) simply
        # never appears.
        assert ("cirq", "0110", "small") not in by_key

    def test_hardware_mutant_missing_despite_qualifying_ladder_entry(self, tmp_path):
        rng = np.random.default_rng(16)
        key = ("missing-mutant-device", "qiskit", "0111", "large")
        manifest, polled = build_archive(rng, "missing-mutant-device",
                                         skip_mutant_hw_key=key)
        write_archive(tmp_path, manifest, polled)
        mutants_path = tmp_path / "mutants.json"
        mutants_path.write_text(json.dumps(mutants_as_list()))
        summary, _ = A.run_analysis(glob_for(tmp_path, "manifest"),
                                    glob_for(tmp_path, "polled"), str(mutants_path))
        row = next(r for r in summary["mutant_detectability"]
                  if r["builder"] == "qiskit" and r["case_id"] == "0111"
                  and r["rung"] == "large")
        assert row["delta_sim"] == pytest.approx(0.45)
        assert row["detected"] is None
        assert row["note"] == "no hardware mutant circuit for this cell/rung"


# ---------------------------------------------------------------------------
# CLI wiring: writes summary.json, cells.csv, report.html
# ---------------------------------------------------------------------------

class TestCli:
    def test_main_writes_all_three_outputs(self, tmp_path):
        rng = np.random.default_rng(17)
        manifest, polled = build_archive(rng, "cli-device")
        write_archive(tmp_path, manifest, polled)
        mutants_path = tmp_path / "mutants.json"
        mutants_path.write_text(json.dumps(mutants_as_list()))
        out_dir = tmp_path / "out"

        rc = A.main([
            "--manifests", str(tmp_path / "*.manifest.json"),
            "--polled", str(tmp_path / "*.polled.json"),
            "--mutants", str(mutants_path),
            "--out", str(out_dir),
        ])
        assert rc == 0
        assert (out_dir / "summary.json").exists()
        assert (out_dir / "cells.csv").exists()
        assert (out_dir / "report.html").exists()

        summary = json.loads((out_dir / "summary.json").read_text())
        assert summary["config"]["min_difference"] == 0.10
        assert summary["config"]["alpha"] == 0.05
        assert summary["smallest_detectable_rung"]["cli-device"]["rung"] == "medium"
        html_text = (out_dir / "report.html").read_text()
        assert "<title>Grover HW matrix analysis</title>" in html_text
        assert "cli-device" in html_text
        assert "Stage-3 headline" in html_text

    def test_main_requires_no_statistical_threshold_flags(self):
        """Hard rule: the only knobs are data-location and ranking-fallback,
        never alpha/min-difference/confidence -- those come only from the
        constants block."""
        source = Path(A.__file__).read_text()
        for forbidden in ("--alpha", "--min-diff", "--confidence", "--min-difference"):
            assert forbidden not in source
