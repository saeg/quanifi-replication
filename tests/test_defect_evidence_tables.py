"""Reference-value checks for the defect-evidence recomputation script.

Runs the real computation functions against the archived data under
experiments/results/ and checks a set of pinned reference values: the exact
per-row two-route-adder CSV a prior manual check produced, and a spread of
Grover / Cepheus / Toffoli / Rx-probe numbers quoted in the task brief.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))
import defect_evidence_tables as det  # noqa: E402


# ---------------------------------------------------------------------------
# Table 1: two-route adder + Toffoli comparison
#
# Ground truth: experiments/results/defect_evidence_two_route_adders.csv is
# byte-identical to a prior, independently-run check of the same archived
# data (QuanifiBKP/qse-2027/reviews/2026-09-25-verification/
# two-route-recomputed.csv). These rows are that CSV, inlined.
# ---------------------------------------------------------------------------

ADDER_REFERENCE = {
    "cirq_qft-bw1-a0-b0-decomposed": (0.794921875, "00", 0.267578125, "00", 0.267578125),
    "cirq_qft-bw1-a0-b1-decomposed": (0.77734375, "10", 0.248046875, "00", 0.255859375),
    "cirq_qft-bw1-a1-b0-decomposed": (0.802734375, "10", 0.3046875, "10", 0.3046875),
    "cirq_qft-bw1-a1-b1-decomposed": (0.78515625, "01", 0.22265625, "10", 0.296875),
    "pennylane_outadder-bw1-a0-b0-decomposed": (0.880859375, "00", 1.0, "00", 1.0),
    "pennylane_outadder-bw1-a0-b1-decomposed": (0.86328125, "10", 0.005859375, "00", 0.98828125),
    "pennylane_outadder-bw1-a1-b0-decomposed": (0.84375, "10", 0.00390625, "00", 0.990234375),
    "pennylane_outadder-bw1-a1-b1-decomposed": (0.82421875, "01", 0.0, "00", 0.998046875),
    "qiskit_cdkm-bw1-a0-b0-decomposed": (0.859375, "00", 0.931640625, "00", 0.931640625),
    "qiskit_cdkm-bw1-a0-b0-whole": (0.85546875, "00", 0.900390625, "00", 0.900390625),
    "qiskit_cdkm-bw1-a0-b1-decomposed": (0.833984375, "10", 0.927734375, "10", 0.927734375),
    "qiskit_cdkm-bw1-a0-b1-whole": (0.84765625, "10", 0.91796875, "10", 0.91796875),
    "qiskit_cdkm-bw1-a1-b0-decomposed": (0.80078125, "10", 0.060546875, "11", 0.859375),
    "qiskit_cdkm-bw1-a1-b0-whole": (0.814453125, "10", 0.849609375, "10", 0.849609375),
    "qiskit_cdkm-bw1-a1-b1-decomposed": (0.73828125, "01", 0.8828125, "01", 0.8828125),
    "qiskit_cdkm-bw1-a1-b1-whole": (0.73046875, "01", 0.703125, "01", 0.703125),
    "toffoli-decomposed": (0.740234375, "111", 0.001953125, "110", 0.970703125),
    "toffoli-raw": (0.78125, "111", 0.830078125, "111", 0.830078125),
}


def test_two_route_adders_matches_reference_exactly():
    rows = {r["circuit"]: r for r in det.table_two_route_adders()}
    assert set(rows) == set(ADDER_REFERENCE)
    for circuit, (dp, dtop, op, otop, otp) in ADDER_REFERENCE.items():
        row = rows[circuit]
        assert row["direct_p_correct"] == dp
        assert row["direct_top"] == dtop
        assert row["oq_p_correct"] == op
        assert row["oq_top"] == otop
        assert row["oq_p_top"] == otp


# ---------------------------------------------------------------------------
# Table 2: two-route Grover comparison
# ---------------------------------------------------------------------------

GROVER_REFERENCE = {
    ("cirq", "10"): (0.932, 0.016, "00", 0.982),
    ("cirq", "11"): (0.943, 0.000, "00", 0.990),
    ("qiskit", "10"): (0.930, 0.959, None, None),
    ("qiskit", "11"): (0.930, 0.936, None, None),
    ("pennylane", "10"): (0.912, 0.955, None, None),
    ("pennylane", "11"): (0.945, 0.912, None, None),
    ("readout", "10"): (0.945, 0.980, None, None),
    ("readout", "11"): (0.957, 0.924, None, None),
    ("readout", "0111"): (0.922, 0.932, None, None),
    ("readout", "0110"): (0.936, 0.920, None, None),
}


def test_two_route_grover_matches_reference_values():
    rows = {(r["circuit"].split("-")[0], r["marked_state"]): r for r in det.table_two_route_grover()}
    for key, (direct_p, oq_p, oq_top, oq_top_p) in GROVER_REFERENCE.items():
        row = rows[key]
        assert round(row["direct_p_correct"], 3) == pytest.approx(direct_p, abs=1e-9)
        assert round(row["oq_p_correct"], 3) == pytest.approx(oq_p, abs=1e-9)
        if oq_top is not None:
            assert row["oq_top"] == oq_top
            assert round(row["oq_p_top"], 3) == pytest.approx(oq_top_p, abs=1e-9)


def test_two_route_grover_direct_0111_values():
    rows = {r["circuit"]: r for r in det.table_two_route_grover()}
    assert round(rows["cirq-grover-hw-002-0111"]["direct_p_correct"], 3) == pytest.approx(0.098, abs=1e-9)
    assert round(rows["qiskit-grover-hw-002-0111"]["direct_p_correct"], 3) == pytest.approx(0.188, abs=1e-9)


# ---------------------------------------------------------------------------
# Table 3: Cepheus adder discovery run
# ---------------------------------------------------------------------------

def test_cepheus_qualification_cdkm_correct_on_5_of_7_cases():
    rows = det.table_cepheus_discovery()
    cdkm_qualification = [
        r for r in rows if r["campaign"] == "qualification" and r["version"] == "cdkm"
    ]
    assert len(cdkm_qualification) == 7
    assert sum(r["decoded_correct"] for r in cdkm_qualification) == 5


def test_cepheus_repeat_probe_cdkm_3plus0_matches_reference():
    rows = {
        r["label"]: r
        for r in det.table_cepheus_discovery()
        if r["campaign"] == "repeat_probe"
    }
    expected_counts = [3, 2, 0, 2]
    dominant_counts = [248, 223, 238, 243]
    for run, (exp_count, dom_count) in enumerate(
        zip(expected_counts, dominant_counts), start=1
    ):
        row = rows[f"cdkm@3+0@run{run}"]
        assert row["expected_full"] == "111100"
        assert row["expected_full_count"] == exp_count
        assert row["dominant"] == "111010"
        assert row["dominant_count"] == dom_count
        assert row["decoded_dominant"] == 5
        assert row["expected_result"] == 3


def test_cepheus_repeat_probe_cdkm_1plus3_matches_reference():
    rows = {
        r["label"]: r
        for r in det.table_cepheus_discovery()
        if r["campaign"] == "repeat_probe"
    }
    dominant_counts = [383, 406, 389, 394]
    expected_counts = [27, 22, 20, 23]
    for run, (dom_count, exp_count) in enumerate(
        zip(dominant_counts, expected_counts), start=1
    ):
        row = rows[f"cdkm@1+3@run{run}"]
        assert row["dominant"] == "100000"
        assert row["dominant_count"] == dom_count
        assert row["decoded_dominant"] == 0
        assert row["expected_result"] == 4
        assert row["expected_full"] == "100010"
        assert row["expected_full_count"] == exp_count


def test_cepheus_campaign_row_counts():
    rows = det.table_cepheus_discovery()
    counts = {}
    for r in rows:
        counts[r["campaign"]] = counts.get(r["campaign"], 0) + 1
    assert counts == {"qualification": 21, "repeat_probe": 12, "layout_probe": 9}


# ---------------------------------------------------------------------------
# Table 4: Toffoli probe
# ---------------------------------------------------------------------------

def test_toffoli_probe_matches_reference():
    rows = {r["name"]: r for r in det.table_toffoli_probe()}
    assert rows["B_toffoli_decomposed"]["top_q0_left"] == "110"
    assert round(rows["B_toffoli_decomposed"]["p_top"], 3) == pytest.approx(0.873, abs=1e-3)
    assert rows["F2_iqm_decomposed"]["top_q0_left"] == "110"
    assert round(rows["F2_iqm_decomposed"]["p_top"], 3) == pytest.approx(0.959, abs=1e-3)


def test_toffoli_probe_cross_checks_jobs_summary_without_raising():
    # table_toffoli_probe() raises internally if the recomputed top/p_top
    # disagree with jobs_summary.csv; simply calling it is the check.
    rows = det.table_toffoli_probe()
    assert len(rows) == 14


# ---------------------------------------------------------------------------
# Table 5: Quantum Inspire Rx-sign probe
# ---------------------------------------------------------------------------

def test_qi_rx_probe_matches_reference():
    rows = {r["circuit"]: r for r in det.table_qi_rx_probe()}
    probe = rows["rx_sign_probe"]
    assert probe["count_0"] == 67
    assert probe["count_1"] == 957
    assert probe["p_negated"] == pytest.approx(0.9346, abs=5e-5)

    neg = rows["rx_sign_probe_neg"]
    assert neg["count_0"] == 922
    assert neg["count_1"] == 102


# ---------------------------------------------------------------------------
# Tables 6-11: 25 Sep 2026 reruns, side by side with their 17 Sep originals
# ---------------------------------------------------------------------------

def test_original_tables_are_unaffected_by_the_new_path_parameters():
    """Parameterizing the table functions must not change their default output."""
    adders = {r["circuit"]: r for r in det.table_two_route_adders()}
    assert adders["toffoli-decomposed"]["oq_top"] == "110"
    assert adders["toffoli-decomposed"]["oq_p_top"] == pytest.approx(
        0.970703125, abs=1e-9
    )
    grover = {r["circuit"]: r for r in det.table_two_route_grover()}
    assert grover["cirq-grover-hw-000-10"]["oq_p_correct"] == pytest.approx(
        0.015625, abs=1e-9
    )


def test_adders_rerun_comparison_matches_reference_values():
    rows = {r["circuit"]: r for r in det.table_two_route_adders_rerun_comparison()}

    toffoli = rows["toffoli-decomposed"]
    assert toffoli["oq_top_25sep"] == "110"
    assert round(toffoli["oq_p_top_25sep"], 3) == pytest.approx(0.922, abs=1e-9)

    pennylane_a0b1 = rows["pennylane_outadder-bw1-a0-b1-decomposed"]
    assert pennylane_a0b1["oq_top_25sep"] == "00"
    assert round(pennylane_a0b1["oq_p_top_25sep"], 3) == pytest.approx(0.986, abs=1e-9)

    qiskit_a1b0 = rows["qiskit_cdkm-bw1-a1-b0-decomposed"]
    assert qiskit_a1b0["oq_top_25sep"] == "11"
    assert round(qiskit_a1b0["oq_p_top_25sep"], 3) == pytest.approx(0.842, abs=1e-9)

    # The 17 Sep side of the merge must still equal the original table.
    original = {r["circuit"]: r for r in det.table_two_route_adders()}
    for circuit, row in rows.items():
        assert row["oq_top_17sep"] == original[circuit]["oq_top"]
        assert row["oq_p_top_17sep"] == original[circuit]["oq_p_top"]


def test_grover_rerun_comparison_matches_reference_values():
    rows = {r["circuit"]: r for r in det.table_two_route_grover_rerun_comparison()}

    cirq_10 = rows["cirq-grover-hw-000-10"]
    assert cirq_10["oq_top_25sep"] == "00"
    assert round(cirq_10["oq_p_top_25sep"], 3) == pytest.approx(0.979, abs=1e-9)

    cirq_11 = rows["cirq-grover-hw-001-11"]
    assert cirq_11["oq_top_25sep"] == "00"
    assert round(cirq_11["oq_p_top_25sep"], 3) == pytest.approx(0.988, abs=1e-9)


def test_two_route_mismatched_circuit_sets_raise():
    with pytest.raises(ValueError):
        det.merge_two_route_by_key(
            [{"circuit": "a", "x": 1}],
            [{"circuit": "b", "x": 2}],
            "circuit",
            "left",
            "right",
            shared_cols=[],
            value_cols=["x"],
        )


def test_two_route_disagreeing_shared_column_raises():
    with pytest.raises(ValueError):
        det.merge_two_route_by_key(
            [{"circuit": "a", "marked_state": "10", "x": 1}],
            [{"circuit": "a", "marked_state": "11", "x": 2}],
            "circuit",
            "left",
            "right",
            shared_cols=["marked_state"],
            value_cols=["x"],
        )


def test_cepheus_rerun_comparison_matches_reference_values():
    rows = det.table_cepheus_rerun_comparison()
    by_case_era = {(r["circuit_case"], r["era"]): r for r in rows}

    # The original Cepheus runs (repeat_probe / toffoli_probe) predate 7 Sep
    # 2026 and are not the same as the 17 Sep two-route runs in Tables 6-7,
    # so their era labels are "original"/"original_runN", not "17sep".
    assert {r["era"] for r in rows} == {
        "original_run1",
        "original_run2",
        "original_run3",
        "original_run4",
        "original",
        "25sep",
    }

    cdkm_3p0 = by_case_era[("cdkm@3+0", "25sep")]
    assert cdkm_3p0["top"] == "111010"
    assert round(cdkm_3p0["p_top"], 3) == pytest.approx(0.654, abs=1e-9)
    assert cdkm_3p0["decoded_top"] == 5
    assert cdkm_3p0["expected_full_count"] == 0

    cdkm_1p3 = by_case_era[("cdkm@1+3", "25sep")]
    assert cdkm_1p3["top"] == "100000"
    assert round(cdkm_1p3["p_top"], 3) == pytest.approx(0.711, abs=1e-9)
    assert cdkm_1p3["decoded_top"] == 0
    assert cdkm_1p3["expected_full_count"] == 17

    toffoli_b = by_case_era[("B_toffoli_decomposed", "25sep")]
    assert toffoli_b["top"] == "110"
    assert round(toffoli_b["p_top"], 3) == pytest.approx(0.867, abs=1e-9)

    toffoli_a = by_case_era[("A_toffoli_raw_ccx", "25sep")]
    assert toffoli_a["top"] == "111"
    assert round(toffoli_a["p_top"], 3) == pytest.approx(0.594, abs=1e-9)

    # The original-era rows still carry the same values as before the rename.
    cdkm_3p0_run1 = by_case_era[("cdkm@3+0", "original_run1")]
    assert cdkm_3p0_run1["expected_full_count"] == 3
    assert cdkm_3p0_run1["top"] == "111010"
    toffoli_a_original = by_case_era[("A_toffoli_raw_ccx", "original")]
    assert toffoli_a_original["top"] == "111"
    assert round(toffoli_a_original["p_top"], 3) == pytest.approx(0.592, abs=1e-9)

    # 4 runs + 1 rerun per CDKM case, 1 original + 1 rerun per Toffoli job.
    assert sum(r["circuit_case"] == "cdkm@3+0" for r in rows) == 5
    assert sum(r["circuit_case"] == "cdkm@1+3" for r in rows) == 5
    assert sum(r["circuit_case"] == "A_toffoli_raw_ccx" for r in rows) == 2
    assert sum(r["circuit_case"] == "B_toffoli_decomposed" for r in rows) == 2


def test_qi_rx_probe_comparison_matches_reference():
    rows = {r["circuit"]: r for r in det.table_qi_rx_probe_comparison()}
    probe = rows["rx_sign_probe"]
    assert probe["count_0_843300"] == 67
    assert probe["count_1_843300"] == 957
    assert probe["count_0_848824"] == 19
    assert probe["count_1_848824"] == 1005
    assert round(probe["p_negated_848824"], 3) == pytest.approx(0.981, abs=1e-9)


def test_qi_native_probe_matches_reference():
    rows = {r["circuit"]: r for r in det.table_qi_native_probe()}
    assert rows["x90"]["count_0"] == 17
    assert rows["x90"]["count_1"] == 1007
    assert rows["mx90"]["count_0"] == 1015
    assert rows["mx90"]["count_1"] == 9


def test_qi_cirq_diagnostic_matches_reference():
    rows = {
        (r["job_id"], r["circuit"]): r for r in det.table_qi_cirq_diagnostic()
    }

    cirq_10 = rows[("848830", "cirq_10")]
    assert cirq_10["top"] == "01"
    assert round(cirq_10["p_top"], 3) == pytest.approx(0.939, abs=1e-3)

    cirq_10_negrx = rows[("848830", "cirq_10_negrx")]
    assert cirq_10_negrx["top"] == "10"
    assert round(cirq_10_negrx["p_top"], 3) == pytest.approx(0.962, abs=1e-9)

    qiskit_10 = rows[("848830", "qiskit_10")]
    assert qiskit_10["top"] == "10"
    assert round(qiskit_10["p_top"], 3) == pytest.approx(0.932, abs=1e-9)

    cirq_11 = rows[("848831", "cirq_11")]
    assert cirq_11["top"] == "00"
    assert round(cirq_11["p_top"], 3) == pytest.approx(0.943, abs=1e-9)

    cirq_11_negrx = rows[("848831", "cirq_11_negrx")]
    assert cirq_11_negrx["top"] == "11"
    assert round(cirq_11_negrx["p_top"], 3) == pytest.approx(0.923, abs=1e-9)

    qiskit_11 = rows[("848831", "qiskit_11")]
    assert qiskit_11["top"] == "11"
    assert round(qiskit_11["p_top"], 3) == pytest.approx(0.933, abs=1e-9)


# ---------------------------------------------------------------------------
# Report assembly sanity checks
# ---------------------------------------------------------------------------

def test_build_report_writes_expected_files(tmp_path):
    report = det.build_report(tmp_path)
    for name in (
        "two_route_adders",
        "two_route_grover",
        "cepheus_discovery",
        "toffoli_probe",
        "qi_rx_probe",
        "two_route_adders_rerun_comparison",
        "two_route_grover_rerun_comparison",
        "cepheus_rerun_comparison",
        "qi_rx_probe_comparison",
        "qi_native_probe",
        "qi_cirq_diagnostic",
        "qi_recovered_batches",
    ):
        assert (tmp_path / f"defect_evidence_{name}.csv").exists()
    assert "Table 1" in report
    assert "Table 12" in report


# ---------------------------------------------------------------------------
# Table 12: recovered 12-13 Sep 2026 Tuna-17 diagnostic batches
# ---------------------------------------------------------------------------

def test_simulator_x_then_measure_gives_1():
    ops, measures, qubits = det.parse_cqasm(
        "version 3.0\nqubit[17] q\nbit[1] b\nX q[1]\nb[0] = measure q[1]\n"
    )
    ideal, ideal_p = det.simulate_ideal(ops, measures, qubits)
    assert ideal == "1"
    assert ideal_p == pytest.approx(1.0)


def test_simulator_rx_rz_ry_program_order_gives_1():
    """Rx(pi/2) then Rz(pi/2) then Ry(pi/2), applied in that program order to
    |0>, gives outcome '1' with certainty -- this is job 1442930's own gate
    sequence, and its own reference in the task brief."""
    ops, measures, qubits = det.parse_cqasm(
        "version 3.0\nqubit[17] q\nbit[1] b\n"
        "Rx(1.5707963) q[1]\nRz(1.5707963) q[1]\nRy(1.5707963) q[1]\n"
        "b[0] = measure q[1]\n"
    )
    ideal, ideal_p = det.simulate_ideal(ops, measures, qubits)
    assert ideal == "1"
    assert ideal_p == pytest.approx(1.0, abs=1e-9)


def test_simulator_reverse_order_ry_rz_rx_gives_0():
    """The reverse program order (job 1442929's sequence) gives the opposite
    outcome, confirming gate order -- not just gate set -- matters."""
    ops, measures, qubits = det.parse_cqasm(
        "version 3.0\nqubit[17] q\nbit[1] b\n"
        "Ry(1.5707963) q[1]\nRz(1.5707963) q[1]\nRx(1.5707963) q[1]\n"
        "b[0] = measure q[1]\n"
    )
    ideal, ideal_p = det.simulate_ideal(ops, measures, qubits)
    assert ideal == "0"
    assert ideal_p == pytest.approx(1.0, abs=1e-9)


def test_parse_cqasm_raises_on_unknown_gate():
    with pytest.raises(ValueError):
        det.parse_cqasm(
            "version 3.0\nqubit[17] q\nbit[1] b\nSWAP q[0], q[1]\nb[0] = measure q[0]\n"
        )


def test_gate_summary_and_verdict_helpers():
    ops = [("Rz", 1.0, 0), ("Ry", 1.0, 0), ("Rx", 1.0, 0), ("Rx", 1.0, 1)]
    assert det.gate_summary(ops) == "Rx:2 Ry:1 Rz:1"
    assert det.verdict_for("01", "01") == "matches ideal"
    assert det.verdict_for("01", "10") == "complement of ideal"
    assert det.verdict_for("00", "01") == "other"


def _recovered_rows_by_job():
    return {r["job_id"]: r for r in det.table_qi_recovered_batches()}


def test_recovered_batches_key_order_convention_job_1441798():
    """job 1441798 is 'X q[1]; b[0]=measure q[1]; b[1]=measure q[0]', the
    coordinator's own evidence for the b[0]-rightmost convention: ideal and
    returned top must both be '01'."""
    row = _recovered_rows_by_job()[1441798]
    assert row["ideal"] == "01"
    assert row["top"] == "01"
    assert row["verdict"] == "matches ideal"
    assert row["role"] == "readout baseline X q[1]"


def test_recovered_batches_835568_reference_values():
    rows = _recovered_rows_by_job()

    j1442927 = rows[1442927]
    assert j1442927["top"] == "00"
    assert j1442927["shots"] == 1024
    assert j1442927["p_top"] == pytest.approx(967 / 1024, abs=1e-9)
    assert j1442927["verdict"] == "complement of ideal"
    assert j1442927["ideal"] == "11"
    assert j1442927["ideal_rx_negated"] == "00"

    j1442928 = rows[1442928]
    assert j1442928["top"] == "11"
    assert j1442928["p_top"] == pytest.approx(906 / 1024, abs=1e-9)
    assert j1442928["verdict"] == "matches ideal"

    j1442929 = rows[1442929]
    assert j1442929["top"] == "1"
    assert j1442929["p_top"] == pytest.approx(954 / 1024, abs=1e-9)
    assert j1442929["verdict"] == "complement of ideal"

    j1442930 = rows[1442930]
    assert j1442930["top"] == "0"
    assert j1442930["p_top"] == pytest.approx(1010 / 1024, abs=1e-9)
    assert j1442930["verdict"] == "complement of ideal"

    # 1442931's own ideal (its exact sent gates, both Rx angles negated
    # relative to 1442927) is '00', the complement of 1442927's ideal '11' --
    # confirmed correct (coordinator's earlier "matches ideal" reference for
    # this one job was itself mistaken and has been retracted). '11' is what
    # 1442927 (the un-negated program) is ideal for, i.e. what a device that
    # silently negates every Rx produces when handed the negated program.
    j1442931 = rows[1442931]
    assert j1442931["top"] == "11"
    assert j1442931["p_top"] == pytest.approx(952 / 1024, abs=1e-9)
    assert j1442931["ideal"] == "00"
    assert j1442931["verdict"] == "complement of ideal"
    assert j1442931["ideal_rx_negated"] == "11"
    assert j1442931["matches_rx_negated"] == "yes"
    assert j1442931["rx_sign_sensitive"] == "yes"


def test_1442931_cqasm_differs_from_1442927_only_in_rx_sign():
    data927 = det.read_json(det.QI_RECOVERED_DIR / "batch-835568.json")
    jobs = {j["job_id"]: j["cqasm"] for j in data927["jobs"]}
    lines_927 = jobs[1442927].splitlines()
    lines_931 = jobs[1442931].splitlines()
    assert len(lines_927) == len(lines_931)
    diffs = [
        (a, b) for a, b in zip(lines_927, lines_931) if a != b
    ]
    assert len(diffs) == 2
    for old, new in diffs:
        assert old.startswith("Rx(1.5707963)")
        assert new.startswith("Rx(-1.5707963)")
        # same qubit operand on both sides of the diff
        assert old.split(")", 1)[1] == new.split(")", 1)[1]


def test_write_recovered_circuits_is_idempotent(tmp_path):
    written_first = det.write_recovered_circuits(out_dir=tmp_path)
    contents_first = {p: p.read_text() for p in written_first}
    written_second = det.write_recovered_circuits(out_dir=tmp_path)
    contents_second = {p: p.read_text() for p in written_second}
    assert contents_first == contents_second
    assert len(written_first) == 22  # 5 + 4 + 5 + 3 + 5 jobs across 5 batches
    job_1441798 = tmp_path / "batch-835027" / "job-1441798.cq"
    assert job_1441798 in contents_first
    assert "X q[1]" in contents_first[job_1441798]


def test_negate_rx_ops_only_touches_rx():
    ops = [("Rz", 1.0, 0), ("Rx", 0.5, 0), ("H", 1), ("CNOT", 0, 1), ("Rx", -0.5, 1)]
    negated = det.negate_rx_ops(ops)
    assert negated == [
        ("Rz", 1.0, 0),
        ("Rx", -0.5, 0),
        ("H", 1),
        ("CNOT", 0, 1),
        ("Rx", 0.5, 1),
    ]
    # original is untouched
    assert ops[1] == ("Rx", 0.5, 0)


def test_recovered_batches_rx_sign_sensitivity_counts_by_hand():
    """Every one of the 22 recovered jobs, checked by hand against the
    printed Table 12, either doesn't depend on Rx's sign or, if it does,
    hardware matches the Rx-negated ideal -- both counts are 11/11, so all
    22 jobs return `top == ideal_rx_negated`."""
    rows = det.table_qi_recovered_batches()
    assert len(rows) == 22

    sensitive = [r for r in rows if r["rx_sign_sensitive"] == "yes"]
    insensitive = [r for r in rows if r["rx_sign_sensitive"] == "no"]
    assert len(sensitive) == 11
    assert len(insensitive) == 11
    assert {r["job_id"] for r in sensitive} == {
        1441797, 1442878, 1442879, 1442880, 1442881,
        1442896, 1442897, 1442927, 1442929, 1442930, 1442931,
    }
    assert {r["job_id"] for r in insensitive} == {
        1441798, 1441799, 1441800, 1441801,
        1442888, 1442889, 1442890, 1442891, 1442892, 1442898, 1442928,
    }

    # All 11 sign-sensitive jobs returned the Rx-negated ideal; all 11
    # sign-insensitive jobs matched their own (sign-independent) ideal.
    assert sum(r["matches_rx_negated"] == "yes" for r in sensitive) == 11
    assert sum(r["verdict"] == "matches ideal" for r in insensitive) == 11
    assert sum(r["matches_rx_negated"] == "yes" for r in rows) == 22


def test_qi_recovered_summary_lines_matches_hand_count():
    rows = det.table_qi_recovered_batches()
    lines = det.qi_recovered_summary_lines(rows)
    assert lines[0] == (
        "Of the 11 jobs whose ideal outcome depends on the sign of Rx, 11 "
        "returned the Rx-negated outcome; of the 11 jobs whose outcome does "
        "not depend on it, 11 matched the ideal."
    )
    assert any(line.startswith("22 of all 22 jobs") for line in lines)
    assert any("1442931" in line and "1442927" in line for line in lines)
