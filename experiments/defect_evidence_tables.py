#!/usr/bin/env python3
"""Recompute the paper's defect-evidence numbers from archived hardware counts.

Every number here is derived straight from JSON/CSV already committed under
experiments/results/ (job archives, plan/collected pairs, probe results) --
nothing is copied from the paper text. Tables 1-5 are one per archived probe
or campaign; Tables 6-11 place the archived 25 Sep 2026 reruns (same circuits,
same submission code) side by side with their earlier counterparts -- the
17 Sep originals for Tables 6-7, the 20 Sep probe for Table 9, and the
pre-7-Sep original Cepheus runs for Table 8. Table 12 covers the recovered
12-13 Sep 2026 Tuna-17 diagnostic batches, read back from the Quantum Inspire
API after the fact, scored against a small stdlib statevector simulator
(see `simulate_ideal`) rather than an archived "expected" field. The source
files and bit-order convention for each table are named in its Markdown
heading.

No network access, no provider SDKs, no hardware submissions. Run with:

    .venv/bin/python experiments/defect_evidence_tables.py

Writes experiments/results/defect_evidence_tables.md (also printed) and one
CSV per table, experiments/results/defect_evidence_<name>.csv. Also writes
each recovered job's cQASM program to
experiments/results/quantum_inspire_rx/recovered_2026-09-12-13/circuits/
(derived from the batch JSON files, which remain the source of truth).
"""

import argparse
import cmath
import collections
import csv
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "experiments" / "results"


def read_json(path):
    return json.loads(path.read_text())


def relpath(path):
    return str(path.relative_to(ROOT))


def score(counts, qubits, expected):
    """P(expected), top outcome, and P(top) over `qubits` (or the full string).

    `counts` and `expected` are both in q0-left order. When `qubits` is None
    the full key is compared/reported as-is; otherwise the counts are
    marginalized onto those qubit indices first.
    """
    if qubits is None:
        marginal = counts
    else:
        marginal = collections.Counter()
        for bits, n in counts.items():
            marginal["".join(bits[i] for i in qubits)] += n
    total = sum(marginal.values())
    top = max(marginal, key=marginal.get)
    return marginal.get(expected, 0) / total, top, marginal[top] / total


def write_csv(path, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def render_markdown_table(headers, rows, float_cols=()):
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for row in rows:
        cells = []
        for h in headers:
            v = row[h]
            cells.append(f"{v:.3f}" if h in float_cols else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Table 1: two-route adder + Toffoli comparison (18 circuits, 512 shots)
# ---------------------------------------------------------------------------

ADDER_DIR = RESULTS / "open_quantum_route_comparison" / "adders"
ADDER_DIRECT_PATH = (
    ADDER_DIR
    / "direct_iqm"
    / "adder-defect-direct-01a0ae3a-fc21-74c2-87c0-dbc99d301de8.json"
)
ADDER_OQ_PATH = ADDER_DIR / "open_quantum" / "job_ids.json"
ADDER_CHECKS_PATH = ADDER_DIR / "checks" / "controlled_reproduction.json"


def load_adder_expected(checks_path=ADDER_CHECKS_PATH):
    """(builder, a, b) -> (expected q0-left sum bitstring, result-qubit indices).

    Sourced from checks/controlled_reproduction.json's per-controlled-form
    rows at bit_width=1 (the width used by this 18-circuit comparison); each
    row independently derives the correct sum and which qubits carry it, so
    this is not re-derived by hand here.
    """
    data = read_json(checks_path)
    table = {}
    for row in data["rows"]:
        if row["bit_width"] == 1:
            table[(row["builder"], row["a"], row["b"])] = (
                row["correct_bitstring"],
                row["result_qubits"],
            )
    return table


def parse_adder_circuit(name):
    """'qiskit_cdkm-bw1-a1-b0-decomposed' -> ('qiskit/cdkm', 1, 0); None for Toffoli."""
    match = re.match(r"([a-z]+)_([a-z]+)-bw1-a(\d+)-b(\d+)-", name)
    if match is None:
        return None
    framework, impl, a, b = match.groups()
    return f"{framework}/{impl}", int(a), int(b)


def table_two_route_adders(
    direct_path=ADDER_DIRECT_PATH, oq_path=ADDER_OQ_PATH, checks_path=ADDER_CHECKS_PATH
):
    direct = read_json(direct_path)
    oq = read_json(oq_path)
    expected_table = load_adder_expected(checks_path)
    rows = []
    for name, raw_counts in zip(direct["circuit_names"], direct["counts"]):
        # IQM's Qiskit get_counts() is q0-right; the OQ archive is explicitly
        # q0-left (verified against its 'bit_order' field below), so the
        # direct-route counts are reversed to match.
        direct_counts = {
            bits.replace(" ", "")[::-1]: n for bits, n in raw_counts.items()
        }
        if oq[name]["bit_order"] != "q0_left":
            raise ValueError(f"{name}: OQ archive is not q0-left")
        parsed = parse_adder_circuit(name)
        if parsed is None:
            qubits, expected = None, "111"  # Toffoli: full 3-qubit outcome
        else:
            expected, qubits = expected_table[parsed]
        dp, dtop, _ = score(direct_counts, qubits, expected)
        op, otop, otp = score(oq[name]["counts"], qubits, expected)
        rows.append(
            {
                "circuit": name,
                "direct_p_correct": dp,
                "direct_top": dtop,
                "oq_p_correct": op,
                "oq_top": otop,
                "oq_p_top": otp,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Table 2: two-route Grover comparison (16 circuits, 512 shots)
# ---------------------------------------------------------------------------

GROVER_DIR = RESULTS / "open_quantum_route_comparison" / "grover"
GROVER_DIRECT_PATH = (
    GROVER_DIR
    / "direct_iqm"
    / "adder-defect-direct-01a0ae53-c9a4-7800-b179-18bf154fcc45.json"
)
GROVER_OQ_PATH = GROVER_DIR / "open_quantum" / "job_ids.json"


def table_two_route_grover(direct_path=GROVER_DIRECT_PATH, oq_path=GROVER_OQ_PATH):
    direct = read_json(direct_path)
    oq = read_json(oq_path)
    rows = []
    for name, raw_counts in zip(direct["circuit_names"], direct["counts"]):
        marked = name.rsplit("-", 1)[1]  # last dash-separated token, q0-left
        direct_counts = {
            bits.replace(" ", "")[::-1]: n for bits, n in raw_counts.items()
        }
        if oq[name]["bit_order"] != "q0_left":
            raise ValueError(f"{name}: OQ archive is not q0-left")
        dp, dtop, dtp = score(direct_counts, None, marked)
        op, otop, otp = score(oq[name]["counts"], None, marked)
        rows.append(
            {
                "circuit": name,
                "marked_state": marked,
                "direct_p_correct": dp,
                "direct_top": dtop,
                "direct_p_top": dtp,
                "oq_p_correct": op,
                "oq_top": otop,
                "oq_p_top": otp,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Table 3: Cepheus adder discovery run (Rigetti via Open Quantum), 3 campaigns
# ---------------------------------------------------------------------------

CEPHEUS_DIR = RESULTS / "open_quantum_cepheus_discovery"
CEPHEUS_CAMPAIGNS = [
    (
        "qualification",
        CEPHEUS_DIR / "qualification_run" / "plan.json",
        CEPHEUS_DIR / "qualification_run" / "collected" / "oq-qualification.json",
    ),
    (
        "repeat_probe",
        CEPHEUS_DIR / "repeat_probe" / "plan.json",
        CEPHEUS_DIR / "repeat_probe" / "collected.json",
    ),
    (
        "layout_probe",
        CEPHEUS_DIR / "layout_probe" / "plan.json",
        CEPHEUS_DIR / "layout_probe" / "collected.json",
    ),
]


def plan_entries_by_label(plan):
    """Flatten a plan's entries (however staged) into a label -> entry map.

    'qualification_run/plan.json' nests entries under 'stages' (only stage 0
    was ever submitted); 'repeat_probe' and 'layout_probe' keep a flat
    'entries' list. Either way, only labels that also appear in the
    corresponding collected file are ever looked up, so unsubmitted plan
    entries are naturally ignored without special-casing them here.
    """
    if "stages" in plan:
        entries = [e for stage in plan["stages"] for e in stage["entries"]]
    else:
        entries = plan["entries"]
    return {e["label"]: e for e in entries}


def decode_result(bitstring, qubits):
    """Little-endian decode over `qubits`; string index == qubit index (q0-left)."""
    return sum(int(bitstring[q]) << k for k, q in enumerate(qubits))


def cepheus_campaign_rows(campaign, plan_path, collected_path):
    entries = plan_entries_by_label(read_json(plan_path))
    rows = []
    for result in read_json(collected_path):
        label = result["label"]
        attrs = entries[label]["attributes"]
        qubits = [int(q) for q in attrs["arithmetic.result_qubits"].split(",")]
        counts = result["counts"]
        total = sum(counts.values())
        expected_full = attrs["arithmetic.expected_bitstring"]
        expected_result = int(attrs["arithmetic.expected_result"])
        dominant = max(counts, key=counts.get)
        decoded_dominant = decode_result(dominant, qubits)
        p_decoded_correct = (
            sum(
                n
                for bits, n in counts.items()
                if decode_result(bits, qubits) == expected_result
            )
            / total
        )
        rows.append(
            {
                "campaign": campaign,
                "label": label,
                "version": attrs["generation2.version_id"],
                "case_id": attrs["generation2.case_id"],
                "expected_full": expected_full,
                "expected_full_count": counts.get(expected_full, 0),
                "expected_full_p": counts.get(expected_full, 0) / total,
                "p_decoded_correct": p_decoded_correct,
                "dominant": dominant,
                "dominant_count": counts[dominant],
                "dominant_p": counts[dominant] / total,
                "decoded_dominant": decoded_dominant,
                "expected_result": expected_result,
                "decoded_correct": decoded_dominant == expected_result,
            }
        )
    return rows


def table_cepheus_discovery(campaigns=CEPHEUS_CAMPAIGNS):
    rows = []
    for campaign, plan_path, collected_path in campaigns:
        rows.extend(cepheus_campaign_rows(campaign, plan_path, collected_path))
    return rows


# ---------------------------------------------------------------------------
# Table 4: Toffoli probe (Rigetti via Open Quantum + IQM direct)
# ---------------------------------------------------------------------------

TOFFOLI_DIR = CEPHEUS_DIR / "toffoli_probe" / "results"
TOFFOLI_RAW_PATH = TOFFOLI_DIR / "raw_counts.json"
TOFFOLI_SUMMARY_PATH = TOFFOLI_DIR / "jobs_summary.csv"


def table_toffoli_probe(raw_path=TOFFOLI_RAW_PATH, summary_path=TOFFOLI_SUMMARY_PATH):
    raw = read_json(raw_path)
    with summary_path.open(newline="") as handle:
        summary_by_name = {r["name"]: r for r in csv.DictReader(handle)}
    rows = []
    for name, summary in summary_by_name.items():
        counts = raw[name]["raw_counts_littleendian"]
        total = sum(counts.values())
        top_littleendian = max(counts, key=counts.get)
        top_q0_left = top_littleendian[::-1]
        p_top = counts[top_littleendian] / total
        if top_q0_left != summary["top_q0_left"] or abs(
            p_top - float(summary["p_top"])
        ) > 5e-4:
            raise ValueError(
                f"{name}: recomputed top/p_top disagrees with jobs_summary.csv"
            )
        rows.append(
            {
                "name": name,
                "job_id": raw[name]["job_id"],
                "expected": summary["expected"],
                "top_q0_left": top_q0_left,
                "p_top": p_top,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Table 5: Quantum Inspire Rx-sign probe
# ---------------------------------------------------------------------------

QI_PROBE_PATH = RESULTS / "quantum_inspire_rx" / "probe-843300.json"


def table_qi_rx_probe(path=QI_PROBE_PATH):
    data = read_json(path)
    rows = []
    for result in data["results"]:
        counts = result["counts"]
        total = sum(counts.values())
        standard = result["expected_standard_rx"]
        negated = result["expected_negated_rx"]
        rows.append(
            {
                "circuit": result["circuit"],
                "count_0": counts.get("0", 0),
                "count_1": counts.get("1", 0),
                "p_standard": counts.get(standard, 0) / total,
                "p_negated": counts.get(negated, 0) / total,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Tables 6-11: 25 Sep 2026 archived reruns (same circuits/submission code as
# the originals above) shown side by side with their earlier counterparts:
# 17 Sep (Tables 6-7), 20 Sep (Table 9), or the pre-7-Sep original Cepheus
# runs (Table 8).
# ---------------------------------------------------------------------------

ADDER_RERUN_DIR = (
    RESULTS / "open_quantum_route_comparison" / "rerun_2026-09-25" / "adders"
)
ADDER_RERUN_DIRECT_PATH = (
    ADDER_RERUN_DIR
    / "direct_iqm"
    / "adder-defect-direct-01a0da40-2323-7075-a91c-21587d178506.json"
)
ADDER_RERUN_OQ_PATH = ADDER_RERUN_DIR / "open_quantum" / "job_ids.json"

GROVER_RERUN_DIR = (
    RESULTS / "open_quantum_route_comparison" / "rerun_2026-09-25" / "grover"
)
GROVER_RERUN_DIRECT_PATH = (
    GROVER_RERUN_DIR
    / "direct_iqm"
    / "adder-defect-direct-01a0da40-744c-7570-9ec6-a1b5c154848f.json"
)
GROVER_RERUN_OQ_PATH = GROVER_RERUN_DIR / "open_quantum" / "job_ids.json"

CEPHEUS_RERUN_OQ_PATH = CEPHEUS_DIR / "rerun_2026-09-25" / "open_quantum" / "job_ids.json"
REPEAT_PROBE_PLAN_PATH = CEPHEUS_DIR / "repeat_probe" / "plan.json"
REPEAT_PROBE_COLLECTED_PATH = CEPHEUS_DIR / "repeat_probe" / "collected.json"

QI_PROBE_RERUN_PATH = RESULTS / "quantum_inspire_rx" / "probe-848824.json"
QI_NATIVE_PROBE_PATH = (
    RESULTS / "quantum_inspire_rx" / "followup_2026-09-25" / "native-1474757.json"
)
QI_CIRQ_DIAGNOSTIC_PATHS = [
    RESULTS
    / "quantum_inspire_rx"
    / "followup_2026-09-25"
    / "cirq-diagnostic-848830.json",
    RESULTS
    / "quantum_inspire_rx"
    / "followup_2026-09-25"
    / "cirq-diagnostic-848831.json",
]


def merge_two_route_by_key(rows_a, rows_b, key, label_a, label_b, shared_cols, value_cols):
    """Zip two same-shaped table outputs into one row per key.

    `shared_cols` are asserted equal between the two runs and kept unsuffixed
    (e.g. the marked state doesn't change between reruns); `value_cols` are
    kept from both runs, suffixed `_<label_a>` / `_<label_b>`. Raises if the
    two inputs don't cover the same set of keys, or a shared column disagrees.
    """
    by_a = {r[key]: r for r in rows_a}
    by_b = {r[key]: r for r in rows_b}
    if set(by_a) != set(by_b):
        raise ValueError(f"Mismatched circuit sets between {label_a} and {label_b}")
    rows = []
    for k in (r[key] for r in rows_a):  # preserve rows_a's order
        row = {key: k}
        for col in shared_cols:
            if by_a[k][col] != by_b[k][col]:
                raise ValueError(f"{k}: {col} differs between {label_a} and {label_b}")
            row[col] = by_a[k][col]
        for col in value_cols:
            row[f"{col}_{label_a}"] = by_a[k][col]
        for col in value_cols:
            row[f"{col}_{label_b}"] = by_b[k][col]
        rows.append(row)
    return rows


def table_two_route_adders_rerun_comparison():
    """17 Sep vs 25 Sep, same 18 adder/Toffoli circuits, side by side."""
    sep17 = table_two_route_adders()
    sep25 = table_two_route_adders(ADDER_RERUN_DIRECT_PATH, ADDER_RERUN_OQ_PATH)
    return merge_two_route_by_key(
        sep17,
        sep25,
        "circuit",
        "17sep",
        "25sep",
        shared_cols=[],
        value_cols=["direct_p_correct", "oq_p_correct", "oq_top", "oq_p_top"],
    )


def table_two_route_grover_rerun_comparison():
    """17 Sep vs 25 Sep, same 16 Grover circuits, side by side."""
    sep17 = table_two_route_grover()
    sep25 = table_two_route_grover(GROVER_RERUN_DIRECT_PATH, GROVER_RERUN_OQ_PATH)
    return merge_two_route_by_key(
        sep17,
        sep25,
        "circuit",
        "17sep",
        "25sep",
        shared_cols=["marked_state"],
        value_cols=["direct_p_correct", "oq_p_correct", "oq_top", "oq_p_top"],
    )


def table_cepheus_rerun_comparison(
    rerun_path=CEPHEUS_RERUN_OQ_PATH,
    repeat_plan_path=REPEAT_PROBE_PLAN_PATH,
    repeat_collected_path=REPEAT_PROBE_COLLECTED_PATH,
    toffoli_raw_path=TOFFOLI_RAW_PATH,
    toffoli_summary_path=TOFFOLI_SUMMARY_PATH,
):
    """25 Sep Cepheus rerun (2 CDKM adder cases + Toffoli jobs A/B) next to
    the original runs (before 7 Sep): repeat_probe's 4 runs per CDKM case,
    and toffoli_probe's jobs_summary.csv rows for A_toffoli_raw_ccx /
    B_toffoli_decomposed.
    """
    rerun = read_json(rerun_path)
    plan_entries = plan_entries_by_label(read_json(repeat_plan_path))
    repeat_rows = {
        r["label"]: r
        for r in cepheus_campaign_rows(
            "repeat_probe", repeat_plan_path, repeat_collected_path
        )
    }
    rows = []

    # CDKM adder cases: decode little-endian over arithmetic.result_qubits,
    # exactly as in Table 3 / repeat_probe.
    for case_label, rerun_key in (("cdkm@3+0", "cdkm-3p0"), ("cdkm@1+3", "cdkm-1p3")):
        for run in range(1, 5):
            r = repeat_rows[f"{case_label}@run{run}"]
            rows.append(
                {
                    "circuit_case": case_label,
                    "era": f"original_run{run}",
                    "expected_full": r["expected_full"],
                    "expected_full_count": r["expected_full_count"],
                    "top": r["dominant"],
                    "p_top": r["dominant_p"],
                    "decoded_top": r["decoded_dominant"],
                }
            )
        attrs = plan_entries[f"{case_label}@run1"]["attributes"]
        qubits = [int(q) for q in attrs["arithmetic.result_qubits"].split(",")]
        expected_full = attrs["arithmetic.expected_bitstring"]
        if rerun[rerun_key]["bit_order"] != "q0_left":
            raise ValueError(f"{rerun_key}: rerun archive is not q0-left")
        counts = rerun[rerun_key]["counts"]
        total = sum(counts.values())
        top = max(counts, key=counts.get)
        rows.append(
            {
                "circuit_case": case_label,
                "era": "25sep",
                "expected_full": expected_full,
                "expected_full_count": counts.get(expected_full, 0),
                "top": top,
                "p_top": counts[top] / total,
                "decoded_top": decode_result(top, qubits),
            }
        )

    # Toffoli jobs A/B: full 3-qubit outcome is the whole answer, no decode.
    # jobs_summary.csv (Table 4's source) doesn't carry per-outcome counts,
    # so the original-run expected-string count is recovered from the same
    # raw_counts.json Table 4 uses, reversing its littleendian keys to
    # q0-left exactly as table_toffoli_probe() does.
    raw = read_json(toffoli_raw_path)
    with toffoli_summary_path.open(newline="") as handle:
        summary_by_name = {r["name"]: r for r in csv.DictReader(handle)}
    for summary_name, rerun_key in (
        ("A_toffoli_raw_ccx", "toffoli-A-raw-ccx"),
        ("B_toffoli_decomposed", "toffoli-B-decomposed"),
    ):
        expected = summary_by_name[summary_name]["expected"]
        old_counts = {
            bits[::-1]: n
            for bits, n in raw[summary_name]["raw_counts_littleendian"].items()
        }
        old_total = sum(old_counts.values())
        old_top = max(old_counts, key=old_counts.get)
        rows.append(
            {
                "circuit_case": summary_name,
                "era": "original",
                "expected_full": expected,
                "expected_full_count": old_counts.get(expected, 0),
                "top": old_top,
                "p_top": old_counts[old_top] / old_total,
                "decoded_top": "",
            }
        )
        if rerun[rerun_key]["bit_order"] != "q0_left":
            raise ValueError(f"{rerun_key}: rerun archive is not q0-left")
        new_counts = rerun[rerun_key]["counts"]
        new_total = sum(new_counts.values())
        new_top = max(new_counts, key=new_counts.get)
        rows.append(
            {
                "circuit_case": summary_name,
                "era": "25sep",
                "expected_full": expected,
                "expected_full_count": new_counts.get(expected, 0),
                "top": new_top,
                "p_top": new_counts[new_top] / new_total,
                "decoded_top": "",
            }
        )
    return rows


def table_qi_rx_probe_comparison(path_a=QI_PROBE_PATH, path_b=QI_PROBE_RERUN_PATH):
    """probe-843300 (20 Sep) vs probe-848824 (25 Sep), same 5 circuits, side by side."""
    rows_a = table_qi_rx_probe(path_a)
    rows_b = table_qi_rx_probe(path_b)
    return merge_two_route_by_key(
        rows_a,
        rows_b,
        "circuit",
        "843300",
        "848824",
        shared_cols=[],
        value_cols=["count_0", "count_1", "p_standard", "p_negated"],
    )


def table_qi_native_probe(path=QI_NATIVE_PROBE_PATH):
    """Native-pulse cQASM programs (rx_pos, rx_neg, x90, mx90).

    native-1474757.json's `results` entries carry a `circuit` key directly
    (checked against `per_circuit_job_ids`/`cqasm`'s program names, which
    agree), so this is the same shape as Table 5 and reuses its function.
    """
    return table_qi_rx_probe(path)


def table_qi_cirq_diagnostic(paths=QI_CIRQ_DIAGNOSTIC_PATHS):
    """Cirq-original vs Cirq-with-Rx-negated vs Qiskit-control, per marked state.

    Each file's `results` entries carry a `circuit` key directly. `top`/`p_top`
    are recomputed from `counts` here rather than trusting the archived
    `top_outcome`/`p_top` fields (they agree, but this table derives its own).
    """
    rows = []
    for path in paths:
        data = read_json(path)
        job_id = data["job_id"]
        marked_state = data["marked_state"]
        for result in data["results"]:
            counts = result["counts"]
            total = sum(counts.values())
            standard = result["expected_standard_rx"]
            negated = result["expected_negated_rx"]
            top = max(counts, key=counts.get)
            rows.append(
                {
                    "job_id": job_id,
                    "marked_state": marked_state,
                    "circuit": result["circuit"],
                    "top": top,
                    "p_top": counts[top] / total,
                    "p_standard": counts.get(standard, 0) / total,
                    "p_negated": counts.get(negated, 0) / total,
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Table 12: recovered 12-13 September 2026 Tuna-17 diagnostic batches
#
# These were read back from the Quantum Inspire API after the fact (the
# `recovered_at` field in each file), so unlike Tables 1-11 there is no
# pre-existing "expected"/"correct_bitstring" field to compare against --
# each job's ideal outcome is computed here with a small stdlib statevector
# simulator over exactly the gates its own cQASM program uses.
# ---------------------------------------------------------------------------

QI_RECOVERED_DIR = RESULTS / "quantum_inspire_rx" / "recovered_2026-09-12-13"
QI_RECOVERED_BATCH_PATHS = [
    QI_RECOVERED_DIR / "batch-835027.json",
    QI_RECOVERED_DIR / "batch-835548.json",
    QI_RECOVERED_DIR / "batch-835555.json",
    QI_RECOVERED_DIR / "batch-835559.json",
    QI_RECOVERED_DIR / "batch-835568.json",
]
QI_RECOVERED_CIRCUITS_DIR = QI_RECOVERED_DIR / "circuits"

# Only roles the coordinator gave explicitly; everything else stays
# "not recorded" rather than guessed.
QI_RECOVERED_ROLES = {
    1441798: "readout baseline X q[1]",
    1442927: "Cirq, marked 11, as sent (contains Rx)",
    1442928: "Qiskit, marked 11 (no Rx)",
    1442929: "probe Ry(pi/2) Rz(pi/2) Rx(pi/2)",
    1442930: "probe Rx(pi/2) Rz(pi/2) Ry(pi/2)",
    1442931: "same program with both Rx angles negated",
}

_CQASM_MEASURE_RE = re.compile(r"^b\[(\d+)\]\s*=\s*measure\s+q\[(\d+)\]$")
_CQASM_PARAM_GATE_RE = re.compile(
    r"^(Rx|Ry|Rz|CR)\(([-+0-9.eE]+)\)\s+q\[(\d+)\](?:,\s*q\[(\d+)\])?$"
)
_CQASM_SINGLE_GATE_RE = re.compile(r"^(H|X|Z)\s+q\[(\d+)\]$")
_CQASM_TWO_QUBIT_GATE_RE = re.compile(r"^(CNOT|CZ)\s+q\[(\d+)\],\s*q\[(\d+)\]$")


def parse_cqasm(text):
    """Parse a cQASM 3.0 program into (ops, measures, qubits used).

    Supports exactly the gates seen in these recovered batches: Rx/Ry/Rz(theta),
    H, X, Z, CNOT, CZ, CR(theta); `version`/`qubit[]`/`bit[]`/`barrier` lines
    are ignored and `b[i] = measure q[j]` lines are recorded separately (not
    treated as ops, since none of these programs branch on a mid-circuit
    result). Raises ValueError on anything else, rather than skipping it.
    """
    ops = []
    measures = {}
    qubits = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("version", "qubit[", "bit[", "barrier")):
            continue
        m = _CQASM_MEASURE_RE.match(line)
        if m:
            bit_idx, qubit_idx = int(m.group(1)), int(m.group(2))
            measures[bit_idx] = qubit_idx
            qubits.add(qubit_idx)
            continue
        m = _CQASM_PARAM_GATE_RE.match(line)
        if m:
            name, angle, q1, q2 = m.groups()
            angle = float(angle)
            if q2 is None:
                ops.append((name, angle, int(q1)))
                qubits.add(int(q1))
            else:
                ops.append((name, angle, int(q1), int(q2)))
                qubits.update({int(q1), int(q2)})
            continue
        m = _CQASM_SINGLE_GATE_RE.match(line)
        if m:
            name, q1 = m.groups()
            ops.append((name, int(q1)))
            qubits.add(int(q1))
            continue
        m = _CQASM_TWO_QUBIT_GATE_RE.match(line)
        if m:
            name, q1, q2 = m.groups()
            ops.append((name, int(q1), int(q2)))
            qubits.update({int(q1), int(q2)})
            continue
        raise ValueError(f"Unrecognized cQASM line: {line!r}")
    return ops, measures, sorted(qubits)


def _single_qubit_matrix(name, angle=None):
    """(u00, u01, u10, u11) for H/X/Z, or Rx/Ry/Rz(angle) -- exp(-i*angle*P/2)."""
    if name == "H":
        s = 1 / math.sqrt(2)
        return (s, s, s, -s)
    if name == "X":
        return (0, 1, 1, 0)
    if name == "Z":
        return (1, 0, 0, -1)
    if name == "Rx":
        c, s = math.cos(angle / 2), math.sin(angle / 2)
        return (c, -1j * s, -1j * s, c)
    if name == "Ry":
        c, s = math.cos(angle / 2), math.sin(angle / 2)
        return (c, -s, s, c)
    if name == "Rz":
        return (cmath.exp(-1j * angle / 2), 0, 0, cmath.exp(1j * angle / 2))
    raise ValueError(f"Unknown single-qubit gate: {name}")


def _apply_single(state, pos, matrix):
    u00, u01, u10, u11 = matrix
    step = 1 << pos
    new_state = list(state)
    for i in range(len(state)):
        if i & step:
            continue
        j = i | step
        a, b = state[i], state[j]
        new_state[i] = u00 * a + u01 * b
        new_state[j] = u10 * a + u11 * b
    return new_state


def _apply_cnot(state, pos_control, pos_target):
    step = 1 << pos_target
    return [
        state[i ^ step] if (i >> pos_control) & 1 else state[i]
        for i in range(len(state))
    ]


def _apply_controlled_phase(state, pos_a, pos_b, phase):
    return [
        state[i] * phase if (i >> pos_a) & 1 and (i >> pos_b) & 1 else state[i]
        for i in range(len(state))
    ]


def simulate_ideal(ops, measures, qubits):
    """Statevector-simulate `ops` over exactly `qubits`, then read out `measures`.

    Pure-Python complex arithmetic, only the qubits actually used. Returns
    (ideal_key, ideal_p): the most likely outcome formatted the way the
    Quantum Inspire API returns it (b[0] rightmost), and its probability
    under ideal (noise-free) execution of exactly the gates given -- which,
    for a program with negated rotation angles, is *not* the same ideal
    outcome as the un-negated program (see Table 12's notes).
    """
    local = {q: i for i, q in enumerate(qubits)}
    dim = 1 << len(qubits)
    state = [0j] * dim
    state[0] = 1 + 0j
    for op in ops:
        name = op[0]
        if name in ("H", "X", "Z"):
            _, q = op
            state = _apply_single(state, local[q], _single_qubit_matrix(name))
        elif name in ("Rx", "Ry", "Rz"):
            _, angle, q = op
            state = _apply_single(state, local[q], _single_qubit_matrix(name, angle))
        elif name == "CNOT":
            _, qc, qt = op
            state = _apply_cnot(state, local[qc], local[qt])
        elif name == "CZ":
            _, qa, qb = op
            state = _apply_controlled_phase(state, local[qa], local[qb], -1)
        elif name == "CR":
            _, angle, qa, qb = op
            state = _apply_controlled_phase(
                state, local[qa], local[qb], cmath.exp(1j * angle)
            )
        else:
            raise ValueError(f"Unknown gate: {name}")
    probs = [abs(a) ** 2 for a in state]
    ideal_index = max(range(dim), key=lambda i: probs[i])
    ideal_p = probs[ideal_index]
    n_bits = (max(measures) + 1) if measures else 0
    bits = [None] * n_bits
    for bit_idx, qubit_idx in measures.items():
        bits[bit_idx] = str((ideal_index >> local[qubit_idx]) & 1)
    ideal_key = "".join(bits[i] for i in reversed(range(n_bits)))
    return ideal_key, ideal_p


_GATE_SUMMARY_ORDER = ["H", "X", "Z", "Rx", "Ry", "Rz", "CNOT", "CZ", "CR"]


def gate_summary(ops):
    """'Rx:2 Ry:4 Rz:6 CR:2' -- counts per gate name, only gates present, in a
    fixed canonical order; barrier/measure lines are never in `ops`."""
    counts = collections.Counter(op[0] for op in ops)
    return " ".join(
        f"{name}:{counts[name]}" for name in _GATE_SUMMARY_ORDER if counts[name]
    )


def _bitwise_complement(key):
    return "".join("1" if c == "0" else "0" for c in key)


def verdict_for(top, ideal):
    if top == ideal:
        return "matches ideal"
    if len(top) == len(ideal) and top == _bitwise_complement(ideal):
        return "complement of ideal"
    return "other"


def negate_rx_ops(ops):
    """Same ops, but with every Rx angle negated; all other gates unchanged."""
    return [("Rx", -op[1], op[2]) if op[0] == "Rx" else op for op in ops]


def write_recovered_circuits(
    paths=QI_RECOVERED_BATCH_PATHS, out_dir=QI_RECOVERED_CIRCUITS_DIR
):
    """Write each job's cQASM exactly as archived to circuits/batch-<id>/job-<id>.cq.

    The JSON stays the single source of truth; these .cq files are a
    derived, human-readable convenience. Idempotent: re-running overwrites
    each file with the same content byte-for-byte, so it's safe to call on
    every `build_report()`.
    """
    written = []
    for path in paths:
        data = read_json(path)
        batch_dir = out_dir / f"batch-{data['batch_job_id']}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        for job in data["jobs"]:
            out_path = batch_dir / f"job-{job['job_id']}.cq"
            out_path.write_text(job["cqasm"])
            written.append(out_path)
    return written


def table_qi_recovered_batches(paths=QI_RECOVERED_BATCH_PATHS, roles=QI_RECOVERED_ROLES):
    rows = []
    for path in paths:
        data = read_json(path)
        batch_job_id = data["batch_job_id"]
        # Minute precision, UTC (every timestamp here already carries +00:00).
        batch_created_on = data["batch_created_on"][:16].replace("T", " ") + " UTC"
        for job in data["jobs"]:
            job_id = job["job_id"]
            counts = job["counts_as_returned"]
            total = sum(counts.values())
            if total != job["shots"]:
                raise ValueError(
                    f"job {job_id}: counts sum to {total}, shots says {job['shots']}"
                )
            top = max(counts, key=counts.get)
            ops, measures, qubits = parse_cqasm(job["cqasm"])
            if len(qubits) > 2:
                raise ValueError(
                    f"job {job_id}: expected at most 2 qubits, used {qubits}"
                )
            ideal, ideal_p = simulate_ideal(ops, measures, qubits)
            ideal_rx_negated, _ = simulate_ideal(
                negate_rx_ops(ops), measures, qubits
            )
            rows.append(
                {
                    "batch_job_id": batch_job_id,
                    "batch_created_on": batch_created_on,
                    "job_id": job_id,
                    "role": roles.get(job_id, "not recorded"),
                    "gates": gate_summary(ops),
                    "shots": job["shots"],
                    "top": top,
                    "p_top": counts[top] / total,
                    "ideal": ideal,
                    "ideal_p": ideal_p,
                    "verdict": verdict_for(top, ideal),
                    "ideal_rx_negated": ideal_rx_negated,
                    "matches_rx_negated": "yes" if top == ideal_rx_negated else "no",
                    "rx_sign_sensitive": "yes" if ideal != ideal_rx_negated else "no",
                }
            )
    return rows


def qi_recovered_summary_lines(rows):
    """Markdown lines summarizing Table 12's Rx-sign sensitivity, computed
    from `rows` (never hardcoded)."""
    sensitive = [r for r in rows if r["rx_sign_sensitive"] == "yes"]
    insensitive = [r for r in rows if r["rx_sign_sensitive"] == "no"]
    negated_hits_among_sensitive = sum(
        r["matches_rx_negated"] == "yes" for r in sensitive
    )
    ideal_hits_among_insensitive = sum(
        r["verdict"] == "matches ideal" for r in insensitive
    )
    negated_hits_overall = sum(r["matches_rx_negated"] == "yes" for r in rows)
    return [
        f"Of the {len(sensitive)} jobs whose ideal outcome depends on the "
        f"sign of Rx, {negated_hits_among_sensitive} returned the "
        f"Rx-negated outcome; of the {len(insensitive)} jobs whose outcome "
        f"does not depend on it, {ideal_hits_among_insensitive} matched the "
        "ideal.",
        "",
        f"{negated_hits_overall} of all {len(rows)} jobs returned the "
        "Rx-negated ideal outcome (`top == ideal_rx_negated`).",
        "",
        "Job 1442931's program already has both Rx angles negated relative "
        "to 1442927, so its own ideal is '00'; the returned '11' is "
        "1442927's (the original Cirq circuit's) marked state -- what a "
        "device that negates Rx produces from the negated program.",
    ]


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

TABLES = [
    {
        "name": "two_route_adders",
        "title": "Table 1 -- Two-route adder + Toffoli comparison (18 circuits, 512 shots)",
        "sources": [ADDER_DIRECT_PATH, ADDER_OQ_PATH, ADDER_CHECKS_PATH],
        "bit_order": (
            "All counts shown q0-left. Direct-IQM counts are Qiskit order "
            "(q0-right) and are reversed before scoring; the OQ archive is "
            "already q0-left (checked against its `bit_order` field)."
        ),
        "compute": table_two_route_adders,
        "headers": [
            "circuit",
            "direct_p_correct",
            "direct_top",
            "oq_p_correct",
            "oq_top",
            "oq_p_top",
        ],
        "float_cols": {"direct_p_correct", "oq_p_correct", "oq_p_top"},
    },
    {
        "name": "two_route_grover",
        "title": "Table 2 -- Two-route Grover comparison (16 circuits, 512 shots)",
        "sources": [GROVER_DIRECT_PATH, GROVER_OQ_PATH],
        "bit_order": (
            "All counts shown q0-left. Direct-IQM counts are Qiskit order "
            "(q0-right) and are reversed before scoring; the OQ archive is "
            "already q0-left. The marked state is the circuit name's last "
            "dash-separated token, already q0-left."
        ),
        "compute": table_two_route_grover,
        "headers": [
            "circuit",
            "marked_state",
            "direct_p_correct",
            "direct_top",
            "direct_p_top",
            "oq_p_correct",
            "oq_top",
            "oq_p_top",
        ],
        "float_cols": {"direct_p_correct", "direct_p_top", "oq_p_correct", "oq_p_top"},
    },
    {
        "name": "cepheus_discovery",
        "title": (
            "Table 3 -- Cepheus adder discovery run via Open Quantum (Rigetti); "
            "qualification (21), repeat_probe (12), layout_probe (9 of 12 planned)"
        ),
        "sources": [p for _, plan, collected in CEPHEUS_CAMPAIGNS for p in (plan, collected)],
        "bit_order": (
            "Counts are q0_left (per each collected entry's `bit_order` field). "
            "`decoded_dominant`/`p_decoded_correct` decode little-endian over "
            "`arithmetic.result_qubits`, i.e. value = sum(bit[q_k] << k)."
        ),
        "compute": table_cepheus_discovery,
        "headers": [
            "campaign",
            "label",
            "version",
            "case_id",
            "expected_full",
            "expected_full_count",
            "expected_full_p",
            "p_decoded_correct",
            "dominant",
            "dominant_count",
            "dominant_p",
            "decoded_dominant",
            "expected_result",
            "decoded_correct",
        ],
        "float_cols": {"expected_full_p", "p_decoded_correct", "dominant_p"},
    },
    {
        "name": "toffoli_probe",
        "title": "Table 4 -- Toffoli probe (Rigetti Cepheus via Open Quantum + IQM direct)",
        "sources": [TOFFOLI_RAW_PATH, TOFFOLI_SUMMARY_PATH],
        "bit_order": (
            "raw_counts.json stores `raw_counts_littleendian`; the top outcome "
            "and its probability are reversed to q0-left here and cross-checked "
            "against jobs_summary.csv's `top_q0_left`/`p_top` columns (raises "
            "on disagreement)."
        ),
        "compute": table_toffoli_probe,
        "headers": ["name", "job_id", "expected", "top_q0_left", "p_top"],
        "float_cols": {"p_top"},
    },
    {
        "name": "qi_rx_probe",
        "title": "Table 5 -- Quantum Inspire Rx-sign probe",
        "sources": [QI_PROBE_PATH],
        "bit_order": (
            "Single-qubit outcomes ('0'/'1'); P(standard)/P(negated) are "
            "recomputed from `counts` against each row's "
            "`expected_standard_rx`/`expected_negated_rx` fields."
        ),
        "compute": table_qi_rx_probe,
        "headers": ["circuit", "count_0", "count_1", "p_standard", "p_negated"],
        "float_cols": {"p_standard", "p_negated"},
    },
    {
        "name": "two_route_adders_rerun_comparison",
        "title": (
            "Table 6 -- Two-route adder + Toffoli comparison, 17 Sep vs 25 Sep "
            "rerun (same 18 circuits, same submission code)"
        ),
        "sources": [
            ADDER_DIRECT_PATH,
            ADDER_OQ_PATH,
            ADDER_RERUN_DIRECT_PATH,
            ADDER_RERUN_OQ_PATH,
        ],
        "bit_order": (
            "Same convention as Table 1 for both runs (direct-IQM reversed "
            "q0-right -> q0-left; OQ archives already q0-left)."
        ),
        "compute": table_two_route_adders_rerun_comparison,
        "headers": [
            "circuit",
            "direct_p_correct_17sep",
            "oq_p_correct_17sep",
            "oq_top_17sep",
            "oq_p_top_17sep",
            "direct_p_correct_25sep",
            "oq_p_correct_25sep",
            "oq_top_25sep",
            "oq_p_top_25sep",
        ],
        "float_cols": {
            "direct_p_correct_17sep",
            "oq_p_correct_17sep",
            "oq_p_top_17sep",
            "direct_p_correct_25sep",
            "oq_p_correct_25sep",
            "oq_p_top_25sep",
        },
    },
    {
        "name": "two_route_grover_rerun_comparison",
        "title": (
            "Table 7 -- Two-route Grover comparison, 17 Sep vs 25 Sep rerun "
            "(same 16 circuits, same submission code)"
        ),
        "sources": [
            GROVER_DIRECT_PATH,
            GROVER_OQ_PATH,
            GROVER_RERUN_DIRECT_PATH,
            GROVER_RERUN_OQ_PATH,
        ],
        "bit_order": (
            "Same convention as Table 2 for both runs (direct-IQM reversed "
            "q0-right -> q0-left; OQ archives already q0-left; marked state "
            "is the circuit name's last dash-separated token)."
        ),
        "compute": table_two_route_grover_rerun_comparison,
        "headers": [
            "circuit",
            "marked_state",
            "direct_p_correct_17sep",
            "oq_p_correct_17sep",
            "oq_top_17sep",
            "oq_p_top_17sep",
            "direct_p_correct_25sep",
            "oq_p_correct_25sep",
            "oq_top_25sep",
            "oq_p_top_25sep",
        ],
        "float_cols": {
            "direct_p_correct_17sep",
            "oq_p_correct_17sep",
            "oq_p_top_17sep",
            "direct_p_correct_25sep",
            "oq_p_correct_25sep",
            "oq_p_top_25sep",
        },
    },
    {
        "name": "cepheus_rerun_comparison",
        "title": (
            "Table 8 -- Cepheus rerun (25 Sep): CDKM adder cases + Toffoli "
            "jobs A/B, next to the original runs (before 7 Sep)"
        ),
        "sources": [
            REPEAT_PROBE_PLAN_PATH,
            REPEAT_PROBE_COLLECTED_PATH,
            TOFFOLI_RAW_PATH,
            TOFFOLI_SUMMARY_PATH,
            CEPHEUS_RERUN_OQ_PATH,
        ],
        "bit_order": (
            "Counts are q0_left throughout (checked against each source's "
            "own `bit_order` field; toffoli_probe's raw_counts_littleendian "
            "is reversed, as in Table 4). `decoded_top` decodes little-endian "
            "over `arithmetic.result_qubits` for the CDKM cases only (as in "
            "Table 3); Toffoli rows leave it blank since the full 3-qubit "
            "outcome is the whole answer. The original-run CDKM rows are "
            "repeat_probe's 4 runs per case; the original-run Toffoli rows use "
            "toffoli_probe's raw_counts.json (jobs_summary.csv, Table 4's "
            "source, does not carry per-outcome counts). The original runs "
            "predate 7 Sep 2026."
        ),
        "compute": table_cepheus_rerun_comparison,
        "headers": [
            "circuit_case",
            "era",
            "expected_full",
            "expected_full_count",
            "top",
            "p_top",
            "decoded_top",
        ],
        "float_cols": {"p_top"},
    },
    {
        "name": "qi_rx_probe_comparison",
        "title": "Table 9 -- Quantum Inspire Rx-sign probe, 20 Sep (843300) vs 25 Sep (848824)",
        "sources": [QI_PROBE_PATH, QI_PROBE_RERUN_PATH],
        "bit_order": "Same convention as Table 5 for both runs.",
        "compute": table_qi_rx_probe_comparison,
        "headers": [
            "circuit",
            "count_0_843300",
            "count_1_843300",
            "p_standard_843300",
            "p_negated_843300",
            "count_0_848824",
            "count_1_848824",
            "p_standard_848824",
            "p_negated_848824",
        ],
        "float_cols": {
            "p_standard_843300",
            "p_negated_843300",
            "p_standard_848824",
            "p_negated_848824",
        },
    },
    {
        "name": "qi_native_probe",
        "title": "Table 10 -- Quantum Inspire native-pulse probe (25 Sep follow-up)",
        "sources": [QI_NATIVE_PROBE_PATH],
        "bit_order": "Same convention as Table 5 (single-qubit outcomes).",
        "compute": table_qi_native_probe,
        "headers": ["circuit", "count_0", "count_1", "p_standard", "p_negated"],
        "float_cols": {"p_standard", "p_negated"},
    },
    {
        "name": "qi_cirq_diagnostic",
        "title": "Table 11 -- Quantum Inspire Cirq-vs-Qiskit Rx diagnostic (25 Sep follow-up)",
        "sources": QI_CIRQ_DIAGNOSTIC_PATHS,
        "bit_order": (
            "2-qubit outcomes from each result's `counts` field (already "
            "q0-left; each file also carries a `raw_counts_q0_right` field, "
            "not used here). `expected_standard_rx`/`expected_negated_rx` "
            "are per-job, relative to that job's marked state."
        ),
        "compute": table_qi_cirq_diagnostic,
        "headers": [
            "job_id",
            "marked_state",
            "circuit",
            "top",
            "p_top",
            "p_standard",
            "p_negated",
        ],
        "float_cols": {"p_top", "p_standard", "p_negated"},
    },
    {
        "name": "qi_recovered_batches",
        "title": (
            "Table 12 -- Recovered 12-13 Sep 2026 Tuna-17 diagnostic batches "
            "(read back from the Quantum Inspire API)"
        ),
        "sources": QI_RECOVERED_BATCH_PATHS,
        "bit_order": (
            "`top` is `counts_as_returned` as-is (API convention: b[0] "
            "rightmost, verified against job 1441798's `X q[1]` readout "
            "baseline: ideal and returned top both '01'). `ideal`/`ideal_p` "
            "come from a stdlib statevector simulation of each job's own "
            "cQASM (see `simulate_ideal`), formatted the same way. `role` is "
            "only filled in where the coordinator specified it; everything "
            "else is \"not recorded\" rather than guessed. `verdict` compares "
            "`top` to that job's own `ideal` -- for a job with negated "
            "rotation angles, its own ideal is generally not the same "
            "bitstring as the un-negated version's ideal (see the report's "
            "notes on job 1442931). `ideal_rx_negated` re-simulates the same "
            "job with every Rx angle negated and everything else unchanged; "
            "`matches_rx_negated` is whether `top` equals that; "
            "`rx_sign_sensitive` is whether negating Rx would even change "
            "the ideal outcome for this particular job (it does not for "
            "e.g. two stacked Rx(pi/2) gates, which compose to Rx(pi) "
            "either way up to an unobservable global phase)."
        ),
        "compute": table_qi_recovered_batches,
        "headers": [
            "batch_job_id",
            "batch_created_on",
            "job_id",
            "role",
            "gates",
            "shots",
            "top",
            "p_top",
            "ideal",
            "ideal_p",
            "verdict",
            "ideal_rx_negated",
            "matches_rx_negated",
            "rx_sign_sensitive",
        ],
        "float_cols": {"p_top", "ideal_p"},
        "extra_markdown": qi_recovered_summary_lines,
    },
]


def build_report(out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Defect-evidence tables (recomputed from archived hardware counts)",
        "",
        "Recomputed directly from the archived counts under "
        "`experiments/results/`; see `experiments/defect_evidence_tables.py`. "
        "Probabilities are shown to 3 decimals here; the CSVs alongside this "
        "file keep full precision.",
        "",
    ]
    for table in TABLES:
        rows = table["compute"]()
        write_csv(out_dir / f"defect_evidence_{table['name']}.csv", rows)
        lines.append(f"## {table['title']}")
        lines.append("")
        sources = ", ".join(f"`{relpath(p)}`" for p in table["sources"])
        lines.append(f"Source: {sources}.")
        lines.append("")
        lines.append(f"Bit order: {table['bit_order']}")
        lines.append("")
        lines.append(
            render_markdown_table(table["headers"], rows, table["float_cols"])
        )
        lines.append("")
        extra = table.get("extra_markdown")
        if extra:
            lines.extend(extra(rows))
            lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=RESULTS,
        help="Directory to write defect_evidence_tables.md and its CSVs into "
        "(default: experiments/results).",
    )
    args = parser.parse_args()
    # Derived convenience files (Table 12's source cQASM, written out beside
    # the batch JSON); regenerating is idempotent, so this runs every time.
    write_recovered_circuits()
    report = build_report(args.out_dir)
    (args.out_dir / "defect_evidence_tables.md").write_text(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
