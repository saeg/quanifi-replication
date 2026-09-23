#!/usr/bin/env python3
"""Analysis for the Grover N-M hardware version matrix (WP5).

Reads archived manifests + polled documents under
``experiments/results/grover_hw_matrix/`` and answers the questions
preregistered in ``docs/planning/PREREG-grover-hw-matrix.md`` (companion:
``docs/planning/PLAN-grover-hw-matrix-improvements.md``, WP5):

  1. Per cell: successes, shots, success probability, 95% Wilson interval,
     the readout ceiling for that case, and the descriptive corrected value
     ``p / readout_ceiling``. The statistical test in (3) below is on RAW p;
     the corrected value is descriptive only (PREREG §4).
  2. Within-batch variance: the distance between each ``null`` row and its
     matching ``control`` row. With one replicate this is the only variance
     estimate available -- it is labelled within-batch and is never a
     stability claim.
  3. Builder effect (primary hypothesis, PREREG §1/§4): within each device
     and each 4-qubit case, builders are ranked by post-routing two-qubit
     gate count ascending, then each adjacent pair in that ranking is tested
     one-sided that the lower-count builder has the higher success
     probability, requiring a difference >= 0.10. Holm-corrected across the
     4-qubit cases, separately per device and per adjacent-pair family. H1 is
     supported for a case when both adjacent pairs reject after Holm.
  4. The 2-qubit cases are NOT tested (PREREG §4 -- underpowered by design).
     Reported descriptively only, as the gap between the readout baseline
     and the control (floor calibration).
  5. Mutant detectability: per cell AND per rung of the 3-rung mutant ladder
     (Amendment A2, which supersedes §5 -- ``small``/``medium``/``large``,
     target Delta_sim 0.10/0.25/uncapped; 2-qubit cases carry ``large`` only),
     the paired control-vs-mutant comparison (one-sided ``worse``, minimum
     difference 0.10), the simulated effect size ``delta_sim``, ``epsilon``
     and ``off_target`` flag (from ``experiments/grover_hw_mutants.json``,
     produced by a parallel work package -- tolerated absent), the observed
     hardware delta, and whether they agree. Per device, the LOWEST rung
     whose paired test still rejects on every cell that ran it is the
     stage-3 headline (A2: "the smallest simulator-measured fault that
     remains detectable under each device's noise").

Statistics (Wilson interval, one-sided two-proportion test) are reused from
``nifi_extensions/QuantumSuccessProbabilityOracle.py`` rather than
reimplemented -- imported the way ``experiments/version_matrix_study.py``
imports processor modules, via ``_harness`` (installs the nifiapi stubs and
puts ``nifi_extensions/`` on ``sys.path``).

Nothing here reaches a provider: this is pure offline analysis over archived
JSON.

Usage:
  .venv/bin/python experiments/grover_hw_matrix_analysis.py \\
      --manifests "experiments/results/grover_hw_matrix/**/*.manifest.json" \\
      --polled "experiments/results/grover_hw_matrix/**/*.polled.json"

  .venv/bin/python experiments/grover_hw_matrix_analysis.py \\
      --manifests ... --polled ... --mutants experiments/grover_hw_mutants.json \\
      --builder-order qiskit,pennylane,cirq --out experiments/out/grover_hw_matrix/latest
"""
import argparse
import collections
import csv
import datetime
import glob
import html
import json
import sys
from pathlib import Path

from _harness import OUT_ROOT  # noqa: E402  (side effect: nifiapi stubs on import)

from QuantumSuccessProbabilityOracle import (  # noqa: E402
    two_proportion_test, wilson_interval)

# ---------------------------------------------------------------------------
# Constants -- quoted verbatim from PREREG §4
# (docs/planning/PREREG-grover-hw-matrix.md). This script reads its
# parameters from this block ONLY and carries no other defaults for anything
# that is a statistical threshold.
# ---------------------------------------------------------------------------
#: "Confidence level for every Wilson interval | 0.95"
CONFIDENCE = 0.95
#: "α | 0.05"
ALPHA = 0.05
#: "Minimum difference that counts (builder vs builder, control vs mutant) |
#: 0.10 in success probability"
MIN_DIFFERENCE = 0.10
#: "Alternative | one-sided, in the direction of H1 (builder) or `worse`
#: (mutant)"
ALTERNATIVE = "worse"  # QuantumSuccessProbabilityOracle's one-sided mode;
                       # "arm 1 exceeds arm 2" is exactly H1's direction for
                       # a builder pair and the mutant-kill direction for a
                       # control-vs-mutant pair.
#: "Multiplicity | Holm, across the 4-qubit cases only, separately per
#: device and per test family"
MULTIPLICITY = "holm"

#: "readout" / "control" / "null" are the three fixed kinds; anything else in
#: a manifest entry's "kind" is a mutant kind (PREREG §5:
#: ``grover.kind = "<operator>#<seed>"``).
FIXED_KINDS = ("readout", "control", "null")

#: PREREG's expected ranking (§1), used only as the fallback ordering when a
#: device/case has no isa_two_qubit_gates recorded (older manifests, or a
#: provider whose submitter has not landed WP2's per-circuit ISA profile
#: yet). The task input note is explicit that this must be tolerated.
DEFAULT_BUILDER_ORDER = ("qiskit", "pennylane", "cirq")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _load_json_glob(patterns):
    """job_id -> parsed JSON, for every file matched by any of ``patterns``."""
    out = {}
    for pattern in patterns:
        for path in sorted(glob.glob(pattern, recursive=True)):
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            job_id = payload.get("job_id") or Path(path).stem
            out[job_id] = payload
    return out


def load_manifests(patterns):
    """job_id -> manifest document (device, provider, entries[...])."""
    return _load_json_glob(patterns)


def load_polled(patterns):
    """job_id -> polled document (entries carrying counts + shots_done)."""
    return _load_json_glob(patterns)


def load_mutants(path):
    """The Δ_sim ladder (PREREG §5): a list of
    ``{builder, case, operator, seed, delta_sim}``, or ``{..., "status":
    "no qualifying mutant"}`` when no seed up to 30 reached 0.10 on the
    simulator. Tolerated absent -- produced by a parallel work package."""
    if not path:
        return []
    file_path = Path(path)
    if not file_path.exists():
        return []
    with file_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, list) else payload.get("mutants", [])


# ---------------------------------------------------------------------------
# Row construction -- join manifest entries to polled counts by (job_id, label)
# ---------------------------------------------------------------------------

def build_rows(manifests, polled):
    """One row per circuit that both submitted AND came back polled.

    A manifest entry with no matching polled entry (job never finished, or
    that one circuit failed on the provider) is silently dropped here --
    there is nothing to score. Every field the rest of this script depends on
    is read defensively: ``isa_two_qubit_gates``/``isa_depth`` may be absent
    (older manifests, or a submitter whose per-circuit ISA profile has not
    landed -- see the module docstring), and ``shots``/``successes`` may be
    zero (a circuit that came back but scored no shots, or a degenerate test
    fixture).
    """
    rows = []
    for job_id, manifest in manifests.items():
        poll = polled.get(job_id)
        if poll is None:
            continue
        device = manifest.get("device")
        counts_by_label, shots_by_label = {}, {}
        for pe in poll.get("entries") or []:
            label = pe.get("label")
            counts_by_label[label] = pe.get("counts") or {}
            shots_by_label[label] = pe.get("shots_done")

        order = (poll.get("bit_order") or "q0_left").strip().lower()
        for entry in manifest.get("entries") or []:
            label = entry.get("label")
            if label not in counts_by_label:
                continue
            raw_counts = counts_by_label[label]
            if order == "q0_right":
                counts = {str(k).replace(" ", "")[::-1]: int(v) for k, v in raw_counts.items()}
            else:
                counts = {str(k).replace(" ", ""): int(v) for k, v in raw_counts.items()}
            shots_done = shots_by_label.get(label)
            shots = int(shots_done) if shots_done is not None else sum(counts.values())
            attributes = entry.get("attributes") or {}
            expected = attributes.get("grover.expected")
            successes = int(counts.get(expected, 0)) if expected else 0
            case_id = attributes.get("grover.case_id")
            num_qubits = entry.get("num_qubits")
            if num_qubits is None and case_id:
                num_qubits = len(case_id)
            builder = attributes.get("grover.builder")
            if builder:
                builder = builder.strip().lower()
            rows.append({
                "device": device, "job_id": job_id, "label": label,
                "kind": entry.get("kind"),
                "builder": builder,
                "case_id": case_id,
                "partition": attributes.get("grover.partition"),
                "expected": expected,
                "num_qubits": num_qubits,
                "isa_two_qubit_gates": entry.get("isa_two_qubit_gates"),
                "isa_depth": entry.get("isa_depth"),
                "successes": successes, "shots": shots,
                "p": (successes / shots) if shots else None,
            })
    return rows


# ---------------------------------------------------------------------------
# Statistics glue
# ---------------------------------------------------------------------------

def wilson(successes, shots):
    return wilson_interval(successes, shots, confidence=CONFIDENCE)


def one_sided_higher(s1, n1, s2, n2):
    """p-value that arm 1 has the higher success probability than arm 2.

    ``QuantumSuccessProbabilityOracle.two_proportion_test``'s ``"worse"``
    alternative is exactly this shape ("arm 1 exceeds arm 2"), whichever pair
    of arms is being compared -- a builder pair in ranked order, or a
    control-vs-mutant pair.
    """
    return two_proportion_test(s1, n1, s2, n2, alternative=ALTERNATIVE)


def holm(pvalues):
    """Holm-Bonferroni step-down adjustment, order preserved."""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        value = (m - rank) * pvalues[i]
        running = max(running, min(1.0, value))
        adjusted[i] = running
    return adjusted


# ---------------------------------------------------------------------------
# 1. Per-cell success probability, Wilson interval, readout-corrected value
# ---------------------------------------------------------------------------

def compute_cells(rows):
    """One record per row, with its case's readout ceiling attached.

    ``readout_ceiling`` / ``corrected_p`` are ``None`` when the case has no
    readout row (a missing readout row is a degenerate but real possibility
    -- a batch that failed to submit that one circuit -- and must not crash
    the rest of the analysis).
    """
    by_case = collections.defaultdict(list)
    for row in rows:
        by_case[(row["device"], row["case_id"])].append(row)

    cells = []
    for (device, case_id), group in by_case.items():
        readout = next((r for r in group if r["kind"] == "readout"), None)
        readout_p = readout["p"] if readout is not None else None
        for row in group:
            shots = row["shots"]
            lo, hi = wilson(row["successes"], shots) if shots else (None, None)
            corrected = (row["p"] / readout_p
                        if row["p"] is not None and readout_p not in (None, 0)
                        else None)
            cells.append(dict(row, wilson_lo=lo, wilson_hi=hi,
                              readout_ceiling=readout_p, corrected_p=corrected))
    return cells


# ---------------------------------------------------------------------------
# 2. Within-batch variance: null vs. its matching control
# ---------------------------------------------------------------------------

def compute_within_batch(cells):
    """The null replicate's distance from the Qiskit control, per case.

    PREREG §2: the null is "one exact copy of the Qiskit control per case,
    same batch" -- so the reference builder for the control side is always
    qiskit. With one replicate this is the only variance estimate available;
    it is explicitly not a stability claim (PREREG §7).
    """
    by_case = collections.defaultdict(list)
    for cell in cells:
        by_case[(cell["device"], cell["case_id"])].append(cell)

    out = []
    for (device, case_id), group in by_case.items():
        control = next((c for c in group
                        if c["kind"] == "control" and c["builder"] == "qiskit"),
                       None)
        null = next((c for c in group if c["kind"] == "null"), None)
        if control is None or null is None:
            continue
        if control["p"] is None or null["p"] is None:
            continue
        out.append({
            "device": device, "case_id": case_id,
            "control_p": control["p"], "null_p": null["p"],
            "distance": abs(control["p"] - null["p"]),
            "note": ("within-batch only (n=1 replicate); not a stability "
                     "claim -- PREREG §7"),
        })
    return out


# ---------------------------------------------------------------------------
# 3. Builder effect (primary hypothesis) + 4. 2-qubit floor calibration
# ---------------------------------------------------------------------------

def _case_qubits(cells):
    qubits = {}
    for cell in cells:
        if cell.get("num_qubits") is not None:
            qubits.setdefault(cell["case_id"], cell["num_qubits"])
    return qubits


def compute_builder_effect(cells, builder_order=DEFAULT_BUILDER_ORDER):
    """Per device, per 4-qubit case: adjacent-pair one-sided tests in the
    routed-count ranking, Holm-corrected across the 4-qubit cases, separately
    per device and per adjacent-pair family (PREREG §4).

    Ranking source: ``isa_two_qubit_gates`` when every builder's control row
    in that (device, case) carries it; otherwise the CLI-supplied
    ``builder_order`` (PREREG task note: "tolerate isa_two_qubit_gates being
    absent"). The chosen source is recorded per case.
    """
    case_qubits = _case_qubits(cells)
    controls = collections.defaultdict(dict)  # (device, case) -> builder -> cell
    devices = set()
    for cell in cells:
        if cell["kind"] != "control":
            continue
        controls[(cell["device"], cell["case_id"])][cell["builder"]] = cell
        devices.add(cell["device"])

    n_pairs = len(builder_order) - 1
    result = {}
    for device in sorted(devices):
        four_q_cases = sorted({
            case_id for (dev, case_id) in controls
            if dev == device and case_qubits.get(case_id) == 4
        })

        case_ranking, case_source = {}, {}
        for case_id in four_q_cases:
            here = controls[(device, case_id)]
            have_all = all(b in here for b in builder_order)
            have_isa = have_all and all(
                here[b].get("isa_two_qubit_gates") is not None
                for b in builder_order)
            if have_isa:
                case_ranking[case_id] = sorted(
                    builder_order, key=lambda b: here[b]["isa_two_qubit_gates"])
                case_source[case_id] = "isa_two_qubit_gates"
            else:
                case_ranking[case_id] = list(builder_order)
                case_source[case_id] = "cli-order (isa_two_qubit_gates unavailable)"

        # Collect p-values per adjacent-pair family (pair index), across cases.
        family_pvalues = {i: [] for i in range(n_pairs)}
        family_cases = {i: [] for i in range(n_pairs)}
        pending = {}  # (case_id, pair_index) -> pre-Holm record
        for case_id in four_q_cases:
            here = controls[(device, case_id)]
            ranking = case_ranking[case_id]
            for i in range(n_pairs):
                lower_name, higher_name = ranking[i], ranking[i + 1]
                lower, higher = here.get(lower_name), here.get(higher_name)
                if (lower is None or higher is None
                        or not lower["shots"] or not higher["shots"]):
                    continue
                diff = lower["p"] - higher["p"]
                _, p_value = one_sided_higher(lower["successes"], lower["shots"],
                                              higher["successes"], higher["shots"])
                if p_value is None:
                    continue
                family_pvalues[i].append(p_value)
                family_cases[i].append(case_id)
                pending[(case_id, i)] = {
                    "device": device, "case_id": case_id,
                    "lower_gate_builder": lower_name,
                    "higher_gate_builder": higher_name,
                    "p_lower": lower["p"], "p_higher": higher["p"],
                    "diff": diff,
                    "isa_two_qubit_gates_lower": lower.get("isa_two_qubit_gates"),
                    "isa_two_qubit_gates_higher": higher.get("isa_two_qubit_gates"),
                    "p_value": p_value,
                    "ranking_source": case_source[case_id],
                }

        for i in range(n_pairs):
            adjusted = holm(family_pvalues[i])
            for case_id, p_holm in zip(family_cases[i], adjusted):
                record = pending[(case_id, i)]
                record["p_holm"] = p_holm
                record["reject"] = bool(p_holm < ALPHA
                                        and record["diff"] >= MIN_DIFFERENCE)

        cases_out = {}
        for case_id in four_q_cases:
            pairs = [pending[(case_id, i)] for i in range(n_pairs)
                     if (case_id, i) in pending]
            supported = bool(pairs) and len(pairs) == n_pairs and all(
                p["reject"] for p in pairs)
            cases_out[case_id] = {
                "ranking": case_ranking[case_id],
                "ranking_source": case_source[case_id],
                "pairs": pairs,
                "h1_supported": supported,
            }

        supported_count = sum(1 for c in cases_out.values() if c["h1_supported"])
        result[device] = {
            "cases": cases_out,
            "h1_supported_cases": supported_count,
            "h1_total_4q_cases": len(four_q_cases),
        }
    return result


def compute_floor_calibration(cells):
    """2-qubit cases, descriptive only (PREREG §4): the gap between the
    readout baseline and the control, per builder. NOT a statistical test --
    the design has no power at 2 qubits (PREREG §7/§9)."""
    case_qubits = _case_qubits(cells)
    by_case = collections.defaultdict(list)
    for cell in cells:
        by_case[(cell["device"], cell["case_id"])].append(cell)

    out = []
    for (device, case_id), group in by_case.items():
        if case_qubits.get(case_id) != 2:
            continue
        readout = next((c for c in group if c["kind"] == "readout"), None)
        readout_p = readout["p"] if readout is not None else None
        for cell in group:
            if cell["kind"] != "control":
                continue
            gap = (readout_p - cell["p"]
                  if readout_p is not None and cell["p"] is not None else None)
            out.append({
                "device": device, "case_id": case_id, "builder": cell["builder"],
                "control_p": cell["p"], "readout_p": readout_p, "gap": gap,
                "note": ("descriptive only -- 2-qubit cases are not tested, "
                         "PREREG §4"),
            })
    return out


# ---------------------------------------------------------------------------
# 5. Mutant detectability -- the stage-3 answer
#
# Amendment A2 (docs/planning/PREREG-grover-hw-matrix.md) supersedes §5's
# single seed-searched mutant per cell with a 3-rung ladder: ``small``
# (target Delta_sim 0.10), ``medium`` (target 0.25) and ``large``
# (``gate.remove``, Delta_sim recorded, not targeted). 2-qubit cases carry
# ``large`` only. This script's own convention -- since no real archive
# exists yet -- is that a mutant manifest entry's ``kind`` IS the rung name
# ("small" | "medium" | "large"), which is what lets a (device, builder,
# case) cell carry more than one mutant row.
# ---------------------------------------------------------------------------

#: Rung order for the stage-3 "lowest detectable rung" comparison (A2). Must
#: NOT rely on dict/file order -- an entry whose rung is missing or unknown
#: (a pre-A2 ladder file, or any value outside this ladder) sorts after
#: every named rung rather than crashing or silently comparing as equal.
RUNG_ORDER = ("small", "medium", "large")


def _rung_rank(rung):
    return RUNG_ORDER.index(rung) if rung in RUNG_ORDER else len(RUNG_ORDER)


def compute_mutants(cells, mutants):
    """Per (device, builder, case, rung): the paired control-vs-mutant
    verdict, ``delta_sim``/``epsilon``/``off_target`` from the ladder file,
    the observed hardware delta, and whether they agree.

    Every combination handles absence gracefully: no ladder file at all
    (parallel work package not landed yet), a ladder entry marked "no
    qualifying mutant" (no hardware circuit exists for that cell/rung
    either), a hardware mutant row with no ladder counterpart, or a ladder
    entry missing ``rung``/``epsilon``/``off_target`` (an older ladder file
    -- tolerated the same way the file's total absence already is).
    """
    controls = {(c["device"], c["builder"], c["case_id"]): c
                for c in cells if c["kind"] == "control"}
    # Mutant hardware rows are tagged by rung in `kind` (see module note above).
    mutant_rows = collections.defaultdict(dict)
    for c in cells:
        if c["kind"] not in FIXED_KINDS:
            rung = c["kind"] or "unspecified"
            mutant_rows[(c["device"], c["builder"], c["case_id"])][rung] = c

    ladder = collections.defaultdict(dict)
    for m in mutants:
        rung = m.get("rung") or "unspecified"
        ladder[(m.get("builder"), m.get("case"))][rung] = m

    keys = set(controls) | set(mutant_rows)
    out = []
    for device, builder, case_id in sorted(keys):
        control = controls.get((device, builder, case_id))
        expected = control.get("expected") if control else None
        hw_here = mutant_rows.get((device, builder, case_id), {})
        if not expected:
            for r in hw_here.values():
                if r.get("expected"):
                    expected = r["expected"]
                    break
        ladder_here = (ladder.get((builder, case_id))
                       or (ladder.get((builder, expected)) if expected else {})
                       or {})
        rungs = sorted(set(ladder_here) | set(hw_here), key=_rung_rank)

        for rung in rungs:
            sim = ladder_here.get(rung)
            delta_sim = sim.get("delta_sim") if sim else None
            epsilon = sim.get("epsilon") if sim else None
            off_target = sim.get("off_target") if sim else None
            no_qualifying = bool(sim) and (
                sim.get("status") == "no qualifying mutant" or delta_sim is None)

            base = {"device": device, "builder": builder, "case_id": case_id,
                    "rung": rung, "delta_sim": delta_sim, "epsilon": epsilon,
                    "off_target": off_target, "hw_delta": None, "p_value": None,
                    "detected": None, "agrees_with_sim": None}

            if no_qualifying:
                out.append(dict(base, note=(
                    "no qualifying mutant for this rung: this cell/rung "
                    "carries no mutant test")))
                continue

            mutant = hw_here.get(rung)
            if control is None or mutant is None:
                out.append(dict(base, note="no hardware mutant circuit for this cell/rung"))
                continue
            if not control["shots"] or not mutant["shots"]:
                out.append(dict(base, note="control or mutant cell has zero shots"))
                continue

            hw_delta = control["p"] - mutant["p"]
            _, p_value = one_sided_higher(control["successes"], control["shots"],
                                          mutant["successes"], mutant["shots"])
            detected = bool(p_value is not None and p_value < ALPHA
                            and hw_delta >= MIN_DIFFERENCE)
            agrees = (None if delta_sim is None
                     else bool(detected == (delta_sim >= MIN_DIFFERENCE)))
            out.append({
                "device": device, "builder": builder, "case_id": case_id,
                "rung": rung, "delta_sim": delta_sim, "epsilon": epsilon,
                "off_target": off_target, "hw_delta": hw_delta,
                "p_value": p_value, "detected": detected,
                "agrees_with_sim": agrees, "note": "",
            })
    return out


def compute_smallest_detectable_rung(mutant_table):
    """Per device: the lowest rung (small < medium < large) whose paired
    test rejects, Amendment A2's stage-3 headline -- "the smallest
    simulator-measured fault that remains detectable under each device's
    noise", reported as a quantity rather than a verdict.

    AMBIGUITY, flagged rather than silently resolved: A2 says this is
    "reported per device as the lowest rung whose paired test still
    rejects" without saying whether that must hold for every (builder, case)
    cell run at that rung on the device, or for at least one. This
    implementation requires EVERY cell that ran the rung on that device to
    reject -- the more conservative reading ("detectable under the device's
    noise" as a device-wide property, not "detectable for one favourable
    circuit"). The per-cell verdicts remain visible in
    ``mutant_detectability`` for a reader who wants the per-cell answer.
    """
    by_device_rung = collections.defaultdict(list)
    devices = set()
    for row in mutant_table:
        devices.add(row["device"])
        if row["detected"] is not None:
            by_device_rung[(row["device"], row["rung"])].append(row["detected"])

    result = {}
    for device in sorted(devices):
        rungs_tested = sorted(
            {rung for (dev, rung) in by_device_rung if dev == device},
            key=_rung_rank)
        chosen = next((rung for rung in rungs_tested
                      if by_device_rung[(device, rung)]
                      and all(by_device_rung[(device, rung)])), None)
        if chosen is None:
            result[device] = {
                "rung": None, "delta_sim": None,
                "note": "no rung rejected on every tested cell for this device",
            }
            continue
        deltas = [row["delta_sim"] for row in mutant_table
                 if row["device"] == device and row["rung"] == chosen
                 and row["delta_sim"] is not None]
        result[device] = {
            "rung": chosen,
            "delta_sim": (sum(deltas) / len(deltas)) if deltas else None,
            "note": "",
        }
    return result


# ---------------------------------------------------------------------------
# What this run cannot support -- PREREG §9, carried into every output
# ---------------------------------------------------------------------------

CAVEATS = [
    "Fault localisation: a hardware disagreement is device error by "
    "default; only the simulator matrix localises software faults.",
    "Cross-device ranking: devices differ in qubits, calibration and native "
    "gates; the M axis is compared descriptively only.",
    "Stability over time: one replicate only.",
    "Full independence of the N axis on hardware: all three paths share "
    "Qiskit's transpiler; routing overhead is compiler-determined.",
    "Separation of PennyLane vs Cirq at the first run's power (PREREG §7).",
]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_analysis(manifest_patterns, polled_patterns, mutants_path=None,
                 builder_order=DEFAULT_BUILDER_ORDER):
    manifests = load_manifests(manifest_patterns)
    polled = load_polled(polled_patterns)
    mutants = load_mutants(mutants_path)

    rows = build_rows(manifests, polled)
    cells = compute_cells(rows)
    within_batch = compute_within_batch(cells)
    builder_effect = compute_builder_effect(cells, builder_order)
    floor_calibration = compute_floor_calibration(cells)
    mutant_table = compute_mutants(cells, mutants)
    smallest_detectable_rung = compute_smallest_detectable_rung(mutant_table)

    summary = {
        "config": {
            "confidence": CONFIDENCE, "alpha": ALPHA,
            "min_difference": MIN_DIFFERENCE, "alternative": ALTERNATIVE,
            "multiplicity": MULTIPLICITY,
            "builder_order": list(builder_order),
        },
        "n_manifests": len(manifests), "n_polled": len(polled),
        "n_rows": len(rows), "n_mutant_ladder_entries": len(mutants),
        "devices": sorted({c["device"] for c in cells}),
        "within_batch_variance": within_batch,
        "builder_effect": builder_effect,
        "floor_calibration_2q": floor_calibration,
        "mutant_detectability": mutant_table,
        "smallest_detectable_rung": smallest_detectable_rung,
        "what_this_run_cannot_support": CAVEATS,
    }
    return summary, cells


CELL_FIELDS = [
    "device", "job_id", "label", "kind", "builder", "case_id", "partition",
    "expected", "num_qubits", "isa_two_qubit_gates", "isa_depth",
    "successes", "shots", "p", "wilson_lo", "wilson_hi", "readout_ceiling",
    "corrected_p",
]


def write_cells_csv(path, cells):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CELL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for cell in sorted(cells, key=lambda c: (c["device"] or "", c["case_id"] or "",
                                                  c["kind"] or "", c["builder"] or "")):
            writer.writerow(cell)


# ---------------------------------------------------------------------------
# HTML report -- house style from tools/build_report_index.py
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#eef1f3;--card:#fff;--ink:#12181d;--muted:#5b6771;--rule:#d3dbe0;
 --copper:#a85d22;--teal:#0e6e78;--good:#2e6b3e;--bad:#8c2f2a;--sel:#dbe7ea;
 --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
 --sans:system-ui,-apple-system,"Segoe UI",sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
 font-size:15px;line-height:1.45}
.wrap{max-width:1180px;margin:0 auto;padding:2rem 1.25rem 4rem}
h1{font-size:1.5rem;margin:0 0 .25rem}
h2{font-size:1rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
 margin:2rem 0 .6rem}
p.sub{color:var(--muted);margin:0 0 1.25rem;max-width:72ch}
.card{background:var(--card);border:1px solid var(--rule);border-radius:8px;
 overflow:hidden;margin-bottom:1rem}
table{border-collapse:collapse;width:100%}
th,td{padding:.45rem .6rem;text-align:left;border-bottom:1px solid var(--rule);
 font-size:.84rem;white-space:nowrap}
th{color:var(--muted);font-weight:600;text-transform:uppercase;
 letter-spacing:.04em;font-size:.7rem}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--sel)}
.mono{font-family:var(--mono);font-size:.8rem}
.dim{color:var(--muted)}
.scroll{overflow-x:auto}
.hit{color:var(--good);font-weight:600}
.miss{color:var(--bad);font-weight:600}
.none{color:var(--copper);font-weight:600}
.counts{color:var(--muted);font-size:.8rem;margin:.5rem 0 0}
footer{color:var(--muted);font-size:.78rem;margin-top:2.5rem;
 border-top:1px solid var(--rule);padding-top:.8rem}
ul.caveats{color:var(--muted);font-size:.85rem}
"""


def _fmt(value, digits=3):
    if value is None:
        return "&mdash;"
    if isinstance(value, bool):
        return '<span class="hit">yes</span>' if value else '<span class="miss">no</span>'
    if isinstance(value, float):
        return "%.*f" % (digits, value)
    return html.escape(str(value))


def _table(headers, rows):
    head = "<tr>" + "".join("<th>%s</th>" % html.escape(h) for h in headers) + "</tr>"
    body = "".join(
        "<tr>" + "".join("<td class=\"mono\">%s</td>" % cell for cell in row) + "</tr>"
        for row in rows)
    return ('<div class="card scroll"><table><thead>%s</thead><tbody>%s'
            '</tbody></table></div>' % (head, body))


def render_html(summary, cells):
    sections = []

    sections.append("<h2>Configuration (PREREG §4)</h2>" + _table(
        ["parameter", "value"],
        [[html.escape(k), _fmt(v)] for k, v in summary["config"].items()]))

    sections.append("<h2>Builder effect (primary hypothesis)</h2>")
    for device, payload in sorted(summary["builder_effect"].items()):
        rows = []
        for case_id, case in sorted(payload["cases"].items()):
            for pair in case["pairs"]:
                rows.append([
                    html.escape(device), html.escape(case_id),
                    html.escape(pair["lower_gate_builder"]),
                    html.escape(pair["higher_gate_builder"]),
                    _fmt(pair["isa_two_qubit_gates_lower"], 0),
                    _fmt(pair["isa_two_qubit_gates_higher"], 0),
                    _fmt(pair["p_lower"]), _fmt(pair["p_higher"]),
                    _fmt(pair["diff"]), _fmt(pair["p_value"], 5),
                    _fmt(pair["p_holm"], 5), _fmt(pair["reject"]),
                    html.escape(pair["ranking_source"]),
                ])
        sections.append("<p class=\"sub\">%s &mdash; H1 supported in %d/%d "
                        "4-qubit case(s)</p>"
                        % (html.escape(device), payload["h1_supported_cases"],
                           payload["h1_total_4q_cases"]))
        sections.append(_table(
            ["device", "case", "lower-gate builder", "higher-gate builder",
             "gates (lower)", "gates (higher)", "p (lower)", "p (higher)",
             "diff", "p", "p (Holm)", "reject", "ranking source"], rows))

    sections.append("<h2>2-qubit floor calibration (descriptive only)</h2>")
    sections.append(_table(
        ["device", "case", "builder", "control p", "readout p", "gap"],
        [[html.escape(r["device"]), html.escape(r["case_id"]),
          html.escape(r["builder"]), _fmt(r["control_p"]), _fmt(r["readout_p"]),
          _fmt(r["gap"])] for r in summary["floor_calibration_2q"]]))

    sections.append("<h2>Within-batch variance (null vs. control)</h2>")
    sections.append(_table(
        ["device", "case", "control p", "null p", "distance"],
        [[html.escape(r["device"]), html.escape(r["case_id"]),
          _fmt(r["control_p"]), _fmt(r["null_p"]), _fmt(r["distance"])]
         for r in summary["within_batch_variance"]]))

    sections.append("<h2>Mutant detectability (stage-3 answer, Amendment A2)</h2>")
    sections.append(_table(
        ["device", "builder", "case", "rung", "delta_sim", "epsilon",
         "off-target", "hw delta", "p", "detected", "agrees with sim", "note"],
        [[html.escape(r["device"]), html.escape(r["builder"]),
          html.escape(r["case_id"]), html.escape(r["rung"] or ""),
          _fmt(r["delta_sim"]), _fmt(r["epsilon"]), _fmt(r["off_target"]),
          _fmt(r["hw_delta"]), _fmt(r["p_value"], 5), _fmt(r["detected"]),
          _fmt(r["agrees_with_sim"]), html.escape(r.get("note") or "")]
         for r in summary["mutant_detectability"]]))

    sections.append("<h2>Stage-3 headline: smallest detectable rung per device</h2>"
                    "<p class=\"sub\">Amendment A2: the lowest rung (small &lt; "
                    "medium &lt; large) whose paired test still rejects on "
                    "every cell that ran it on that device. If no rung "
                    "rejects everywhere, that is stated explicitly rather "
                    "than a blank cell.</p>")
    sections.append(_table(
        ["device", "smallest detectable rung", "delta_sim", "note"],
        [[html.escape(device), html.escape(row["rung"] or "none detected"),
          _fmt(row["delta_sim"]), html.escape(row["note"] or "")]
         for device, row in sorted(summary["smallest_detectable_rung"].items())]))

    sections.append("<h2>What this run cannot support (PREREG §9)</h2>"
                    "<ul class=\"caveats\">" +
                    "".join("<li>%s</li>" % html.escape(c) for c in CAVEATS) +
                    "</ul>")

    page = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Grover HW matrix analysis</title><style>" + CSS + "</style></head>"
        "<body><div class=\"wrap\"><h1>Grover N-M hardware matrix</h1>"
        "<p class=\"sub\">Preregistered analysis "
        "(docs/planning/PREREG-grover-hw-matrix.md §4). %d cell(s) across "
        "%d device(s), from %d manifest(s) and %d polled document(s).</p>"
        % (len(cells), len(summary["devices"]), summary["n_manifests"],
           summary["n_polled"])
        + "".join(sections) +
        "<footer>Generated " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M") +
        " by <code>experiments/grover_hw_matrix_analysis.py</code>.</footer>"
        "</div></body></html>")
    return page


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifests", nargs="+", required=True,
                        help="glob(s) matching archived manifest JSON files")
    parser.add_argument("--polled", nargs="+", required=True,
                        help="glob(s) matching archived polled-document JSON files")
    parser.add_argument("--mutants", default="experiments/grover_hw_mutants.json",
                        help="the Delta_sim ladder (PREREG §5); tolerated absent")
    parser.add_argument("--builder-order", default=",".join(DEFAULT_BUILDER_ORDER),
                        help="fallback builder ranking (ascending expected "
                             "two-qubit gate count) used ONLY when "
                             "isa_two_qubit_gates is absent from the manifest")
    parser.add_argument("--out", default=None,
                        help="output directory for summary.json, cells.csv, "
                             "report.html (default: a timestamped directory "
                             "under experiments/out/grover_hw_matrix_analysis/)")
    args = parser.parse_args(argv)

    builder_order = tuple(b.strip() for b in args.builder_order.split(",") if b.strip())

    summary, cells = run_analysis(args.manifests, args.polled, args.mutants,
                                  builder_order)

    if args.out:
        out_dir = Path(args.out)
    else:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = OUT_ROOT / "grover_hw_matrix_analysis" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    write_cells_csv(out_dir / "cells.csv", cells)
    (out_dir / "report.html").write_text(render_html(summary, cells))

    print("wrote %s/summary.json, cells.csv, report.html" % out_dir)
    print("%d cell(s) across %d device(s)" % (len(cells), len(summary["devices"])))
    for device, payload in sorted(summary["builder_effect"].items()):
        print("  %-16s H1 supported in %d/%d 4-qubit case(s)"
              % (device, payload["h1_supported_cases"], payload["h1_total_4q_cases"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
