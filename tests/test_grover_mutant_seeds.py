"""Tests for experiments/grover_mutant_seeds.py.

PREREG-grover-hw-matrix.md Amendment A2 replaced the original §5 seed search
with a mutant ladder (small/medium/large effect sizes per 4-qubit cell, large
only for 2-qubit cells) after two measurements made during §5's
implementation: QuantumMutator's Mutation Seed has no effect once a
Mutation Locus is pinned, and gate.remove/middle turned out to be
catastrophic (delta_sim 0.51-1.00) rather than marginal on every one of the
twelve original cells. These tests pin the new ladder's reproducibility, its
targeting accuracy, and keep the original locus/seed-independence finding as
documentation -- it is still true and it is why the ladder exists at all.

Building the full ladder runs 24 bisection-backed simulations, so most tests
share one module-scoped ``selection``/``output`` fixture rather than
recomputing it per test; only the determinism tests below legitimately need
to call build_selection()/build_output()/main() a second time.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))

import grover_mutant_seeds as G  # noqa: E402


def _strip_timestamp(output):
    provenance = dict(output["provenance"])
    provenance.pop("generated_utc", None)
    return {"provenance": provenance, "mutants": output["mutants"]}


@pytest.fixture(scope="module")
def selection():
    """The deterministic ladder, computed once and shared read-only."""
    return G.build_selection()


@pytest.fixture(scope="module")
def output(selection):
    """The full JSON payload (provenance + the shared selection above).

    Built independently of the ``selection`` fixture's *value* (build_output
    calls build_selection() itself), but declared to depend on it so the
    two expensive computations are not both paid for by tests that only
    need one of them.
    """
    return G.build_output()


class TestDeterminism:
    """These three tests are the only ones allowed to compute the ladder
    twice -- reproducibility is the property under test."""

    def test_build_selection_is_byte_identical_across_calls(self):
        first = G.build_selection()
        second = G.build_selection()
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_build_output_differs_only_in_timestamp(self):
        first = G.build_output()
        second = G.build_output()
        assert _strip_timestamp(first) == _strip_timestamp(second)
        assert (first["provenance"]["generated_utc"] != ""
                and second["provenance"]["generated_utc"] != "")

    def test_full_script_run_twice_is_identical_apart_from_timestamp(self, tmp_path, monkeypatch):
        """End-to-end: run main() twice against scratch paths and diff, the
        way the acceptance check does."""
        out1 = tmp_path / "run1.json"
        out2 = tmp_path / "run2.json"

        monkeypatch.setattr(G, "OUT_PATH", out1)
        G.main()
        monkeypatch.setattr(G, "OUT_PATH", out2)
        G.main()

        data1 = json.loads(out1.read_text())
        data2 = json.loads(out2.read_text())
        assert _strip_timestamp(data1) == _strip_timestamp(data2)


class TestLadderShape:
    def test_expected_total_of_24_mutants(self, selection):
        """6 four-qubit cells x 3 rungs + 6 two-qubit cells x 1 rung = 24
        (A2's "Batch size changes" section)."""
        assert len(selection["mutants"]) == 24

    def test_every_entry_carries_a_rung(self, selection):
        for entry in selection["mutants"]:
            assert entry["rung"] in {"small", "medium", "large"}

    def test_four_qubit_cells_get_all_three_rungs(self, selection):
        by_cell = {}
        for entry in selection["mutants"]:
            if entry["num_qubits"] == 4:
                by_cell.setdefault((entry["builder"], entry["case"]), set()).add(entry["rung"])
        assert len(by_cell) == 6  # 3 builders x 2 four-qubit cases
        for rungs in by_cell.values():
            assert rungs == {"small", "medium", "large"}

    def test_two_qubit_cells_get_only_the_large_rung(self, selection):
        for entry in selection["mutants"]:
            if entry["num_qubits"] == 2:
                assert entry["rung"] == "large"
                assert entry["operator"] == "gate.remove"

    def test_every_cell_and_rung_is_unique(self, selection):
        seen = set()
        for entry in selection["mutants"]:
            key = (entry["builder"], entry["case"], entry["rung"])
            assert key not in seen, f"{key} appears more than once"
            seen.add(key)

    def test_large_rung_uses_gate_remove_and_no_epsilon(self, selection):
        for entry in selection["mutants"]:
            if entry["rung"] == "large":
                assert entry["operator"] == "gate.remove"
                assert entry["epsilon"] is None
                assert entry["target_delta"] is None
                assert entry["off_target"] is None

    def test_small_and_medium_rungs_use_rotation_perturb_with_an_epsilon(self, selection):
        for entry in selection["mutants"]:
            if entry["rung"] in {"small", "medium"}:
                assert entry["operator"] == "rotation.perturb"
                assert entry["epsilon"] is not None
                assert G.EPSILON_BOUNDS[0] <= entry["epsilon"] <= G.EPSILON_BOUNDS[1]
                assert entry["bisection_bounds"] == list(G.EPSILON_BOUNDS)


class TestTargeting:
    def test_small_rung_targets_point_one(self, selection):
        for entry in selection["mutants"]:
            if entry["rung"] == "small":
                assert entry["target_delta"] == pytest.approx(0.10)

    def test_medium_rung_targets_point_two_five(self, selection):
        for entry in selection["mutants"]:
            if entry["rung"] == "medium":
                assert entry["target_delta"] == pytest.approx(0.25)

    def test_small_and_medium_rungs_land_within_tolerance_or_are_flagged(self, selection):
        """A rung either hits its target within OFF_TARGET_TOLERANCE, or is
        explicitly marked off_target=True and still kept with its achieved
        value (A2 point 4) -- never silently wrong."""
        for entry in selection["mutants"]:
            if entry["rung"] in {"small", "medium"}:
                miss = abs(entry["delta_sim"] - entry["target_delta"])
                if miss > G.OFF_TARGET_TOLERANCE:
                    assert entry["off_target"] is True
                else:
                    assert entry["off_target"] is False

    def test_no_four_qubit_rung_is_off_target_in_this_measured_run(self, selection):
        """Measured: every one of the 12 four-qubit small/medium rungs landed
        within ~0.0001-0.0002 of its target in this run -- the epsilon knob
        is well-behaved on these circuits across all three builders. If a
        future change makes some cell unreachable, this test will fail and
        that failure IS the design finding A2 asks to flag prominently, not
        a bug in the test."""
        four_q_targeted = [e for e in selection["mutants"]
                           if e["num_qubits"] == 4 and e["rung"] in {"small", "medium"}]
        assert len(four_q_targeted) == 12
        off_target = [e for e in four_q_targeted if e["off_target"]]
        assert off_target == [], (
            "4-qubit rung(s) could not be hit within tolerance -- a design "
            f"finding, not a script bug: {off_target}"
        )


class TestAchievedDeltaIsRecorded:
    def test_large_rung_delta_sim_matches_the_original_seed_11_measurement(self, selection):
        """The large rung is specified to keep the original §5 numbers
        exactly (gate.remove, locus middle, seed 11, 1024 shots). Pin two of
        the twelve measured in the original run."""
        by_key = {(e["builder"], e["case"]): e for e in selection["mutants"]
                  if e["rung"] == "large"}
        assert by_key[("qiskit", "10")]["delta_sim"] == pytest.approx(1.0)
        assert by_key[("qiskit", "0111")]["delta_sim"] == pytest.approx(0.568359375)

    def test_delta_sim_is_the_achieved_value_not_the_target(self, selection):
        """Achieved and target need not be bit-identical -- the bisection
        lands close, not necessarily exact -- so this only pins that the two
        fields are populated independently (a bug that just copied the
        target into delta_sim would still pass the tolerance test above)."""
        for entry in selection["mutants"]:
            if entry["rung"] in {"small", "medium"}:
                assert isinstance(entry["delta_sim"], float)
                assert isinstance(entry["target_delta"], float)


class TestProvenance:
    def test_provenance_records_the_ladder_design(self, output):
        provenance = output["provenance"]
        assert provenance["locus"] == "middle"
        assert provenance["seed"] == 11
        assert provenance["large_rung_shots"] == 1024
        assert provenance["epsilon_search_shots"] == 4096
        assert provenance["epsilon_bounds"] == list(G.EPSILON_BOUNDS)
        assert provenance["epsilon_max_iterations"] == 24
        assert provenance["off_target_tolerance"] == pytest.approx(0.03)
        assert provenance["rung_targets"] == {"small": 0.10, "medium": 0.25, "large": None}
        assert provenance["qiskit_version"]
        assert provenance["generated_utc"].endswith("Z")

    def test_generated_json_file_has_the_expected_top_level_shape(self, tmp_path, monkeypatch):
        out = tmp_path / "grover_hw_mutants.json"
        monkeypatch.setattr(G, "OUT_PATH", out)
        G.main()
        on_disk = json.loads(out.read_text())
        assert set(on_disk.keys()) == {"provenance", "mutants"}
        assert len(on_disk["mutants"]) == 24


class TestLocusSeedIndependenceFinding:
    """Kept from the original §5 test suite: this measurement is exactly why
    the ladder (this file) replaced the seed search in the first place, and
    it remains valuable documentation that a future change to QuantumMutator
    could silently invalidate. Cheap (two mutations, no bisection), so these
    do not need the shared fixture."""

    def test_locus_middle_makes_gate_remove_independent_of_seed(self):
        control = G.build_control("QiskitGroverCircuit", "0111", 2)
        mutant_11, _ = G.build_mutant(control, "gate.remove", seed=11)
        mutant_20, _ = G.build_mutant(control, "gate.remove", seed=20)
        assert mutant_11 == mutant_20

    def test_locus_middle_makes_rotation_perturb_candidate_choice_independent_of_seed(self):
        """Same finding for rotation.perturb: the *candidate rotation gate*
        chosen is seed-independent under a pinned locus (only Rotation
        Epsilon, a separate property, actually varies the mutant) -- this is
        the property the whole ladder design leans on."""
        control = G.build_control("QiskitGroverCircuit", "0111", 2)
        mutant_11, attrs_11 = G.build_mutant(
            control, "rotation.perturb", seed=11, epsilon=0.5
        )
        mutant_20, attrs_20 = G.build_mutant(
            control, "rotation.perturb", seed=20, epsilon=0.5
        )
        assert mutant_11 == mutant_20
        assert attrs_11["mut.gate"] == attrs_20["mut.gate"]
        assert attrs_11["mut.position"] == attrs_20["mut.position"]
