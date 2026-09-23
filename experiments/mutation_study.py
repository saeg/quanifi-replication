#!/usr/bin/env python3
"""Mutation campaign: single-framework vs cross-framework consensus oracles.

For each test case (a Grover circuit as qasm2) every engine first samples the
ORIGINAL circuit (seeded) as its reference distribution. Then, per mutation
operator and per mutant seed, QuantumMutator produces a gate-level (Layer-A)
mutant that runs on one branch (Qiskit Aer — the single-branch injection mode
of MUTATION_TESTING.md §3). Each oracle then compares the mutant's counts
against a reference:

  single-framework oracle   mutant-on-aer vs original-on-aer
  cross-framework oracles   mutant-on-aer vs original-on-{cirq,qrisp,
                            pennylane}; consensus kill = majority
                            (braket optional via --engines; unseeded)

A mutant is killed by an oracle when the chi-squared test rejects (after
Benjamini-Hochberg correction across the whole campaign, per oracle) AND the
Hellinger distance clears the effect-size threshold — the same two-gate rule
QuantumDistributionComparison applies on the canvas. An engine crash on a
mutant counts as a kill (the defect was detected, just not statistically).

Outputs (under experiments/out/mutation/<timestamp>/):
  results.csv   one row per (case, operator, mutant seed)
  summary.json  per-operator mutation scores, single vs consensus

Usage:
  .venv/bin/python experiments/mutation_study.py [--shots 1024] [--seed 11]
      [--mutants 10] [--alpha 0.05] [--hellinger-threshold 0.1]
      [--rotation-epsilon 0.1] [--engines aer,cirq,qrisp,pennylane]
"""
import argparse
import csv
import datetime
import itertools
import json
import platform
from pathlib import Path

from _harness import MockContext, MockFlowFile, OUT_ROOT

from QiskitGroverCircuit import QiskitGroverCircuit
from QuantumMutator import QuantumMutator
from QiskitAerSimulator import QiskitAerSimulator
from CirqSimulator import CirqSimulator
from QrispSimulator import QrispSimulator
from PennylaneSimulator import PennylaneSimulator
from BraketSimulator import BraketSimulator
from QuantumDistributionComparison import _chi2_two_sample, _hellinger, _normalize
from multiple_comparisons import benjamini_hochberg  # shared with QuantumDistributionOracle

CASES = [
    ("11", 1),
    ("110", 2),
    ("0110", 2),
]

OPERATORS = ["gate.add", "gate.remove", "gate.replace", "rotation.perturb"]

ALL_ENGINES = {
    "aer": QiskitAerSimulator,
    "cirq": CirqSimulator,
    "qrisp": QrispSimulator,
    "pennylane": PennylaneSimulator,
    "braket": BraketSimulator,
}

# Seeded engines only by default: Braket's local simulator cannot be seeded.
# Module-level so other studies (shot_budget_detectability.py) can import them.
DEFAULT_ENGINES = ["aer", "cirq", "qrisp", "pennylane"]
ENGINES = {e: ALL_ENGINES[e] for e in DEFAULT_ENGINES}
CROSS_ENGINES = [e for e in ENGINES if e != "aer"]


def build_circuit(marked, iterations):
    res = QiskitGroverCircuit().transform(
        MockContext(**{"Marked State": marked, "Num Iterations": str(iterations),
                       "Output Format": "qasm2"}),
        MockFlowFile())
    assert res.relationship == "success", res.attributes
    return res.contents


def run_engine(engine_cls, qasm2, shots, seed):
    """Counts dict, or None when the engine rejects/crashes on the circuit."""
    props = {"Shots": str(shots)}
    if "Random Seed" in [d.name for d in engine_cls().descriptors]:
        props["Random Seed"] = str(seed)
    ff = MockFlowFile(content=qasm2, attributes={"circuit.format": "qasm2"})
    try:
        res = engine_cls().transform(MockContext(**props), ff)
    except Exception:
        return None
    if res.relationship != "success":
        return None
    return json.loads(res.contents.decode("utf-8"))


def mutate(qasm2, operator, mut_seed, eps):
    ff = MockFlowFile(content=qasm2, attributes={"circuit.format": "qasm2"})
    res = QuantumMutator().transform(
        MockContext(**{"Mutation Operators": operator,
                       "Mutation Seed": str(mut_seed),
                       "Rotation Epsilon": str(eps)}),
        ff)
    if res.relationship != "success":
        return None, res.attributes
    return res.contents, res.attributes


def versions():
    out = {"python": platform.python_version()}
    for mod in ("qiskit", "cirq", "qrisp", "pennylane", "braket._sdk"):
        try:
            m = __import__(mod, fromlist=["__version__"])
            out[mod] = getattr(m, "__version__", "?")
        except Exception:
            out[mod] = "unavailable"
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shots", type=int, default=1024)
    ap.add_argument("--seed", type=int, default=11,
                    help="engine sampling seed (one per campaign; mutants vary)")
    ap.add_argument("--mutants", type=int, default=10,
                    help="mutant seeds per (case, operator) cell")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--hellinger-threshold", type=float, default=0.1)
    ap.add_argument("--rotation-epsilon", type=float, default=0.1)
    ap.add_argument("--engines", type=str, default=",".join(DEFAULT_ENGINES),
                    help="comma-separated engine names (aer must be included)")
    args = ap.parse_args()

    # Validate and build ENGINES and CROSS_ENGINES from --engines option
    selected_names = [e.strip() for e in args.engines.split(",")]
    unknown = [e for e in selected_names if e not in ALL_ENGINES]
    if unknown:
        ap.error(f"unknown engines: {', '.join(unknown)}")
    if "aer" not in selected_names:
        ap.error("'aer' must be included in --engines")

    ENGINES = {e: ALL_ENGINES[e] for e in selected_names}
    CROSS_ENGINES = [e for e in selected_names if e != "aer"]

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = OUT_ROOT / "mutation" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    # References: every engine samples each original circuit once.
    originals, references = {}, {}
    for marked, iters in CASES:
        qasm2 = build_circuit(marked, iters)
        originals[marked] = qasm2
        for eng, cls in ENGINES.items():
            counts = run_engine(cls, qasm2, args.shots, args.seed)
            assert counts is not None, ("reference run failed", marked, eng)
            references[(marked, eng)] = counts

    rows, tests = [], {eng: [] for eng in ENGINES}  # per-oracle p-value lists
    for (marked, iters), op, k in itertools.product(
            CASES, OPERATORS, range(args.mutants)):
        mut_seed = 1000 + k
        mutant, mattrs = mutate(originals[marked], op, mut_seed,
                                args.rotation_epsilon)
        row = {"marked_state": marked, "iterations": iters,
               "operator": op, "mut_seed": mut_seed,
               "applied": mutant is not None,
               "op_applied": mattrs.get("mut.operator", ""),
               "mut_detail": mattrs.get("mut.gate", "") or mattrs.get(
                   "mut.original_value", "")}
        if mutant is None:
            row["mut_error"] = mattrs.get("mut.error", "")
            rows.append(row)
            for eng in ENGINES:
                tests[eng].append(None)
            continue

        mutant_counts = run_engine(QiskitAerSimulator, mutant,
                                   args.shots, args.seed)
        row["engine_crash"] = mutant_counts is None
        for eng in ENGINES:
            if mutant_counts is None:
                row["hellinger_{}".format(eng)] = None
                tests[eng].append(None)
                continue
            ref = references[(marked, eng)]
            chi = _chi2_two_sample(mutant_counts, ref)
            row["hellinger_{}".format(eng)] = round(
                _hellinger(_normalize(mutant_counts), _normalize(ref)), 6)
            tests[eng].append(chi[2] if chi else None)
        rows.append(row)

    # BH correction per oracle across the campaign, then the two-gate kill rule.
    reject = {eng: benjamini_hochberg(tests[eng], args.alpha) for eng in ENGINES}
    for i, row in enumerate(rows):
        if not row["applied"]:
            continue
        if row.get("engine_crash"):
            for eng in ENGINES:
                row["killed_{}".format(eng)] = True
        else:
            for eng in ENGINES:
                h = row["hellinger_{}".format(eng)]
                row["killed_{}".format(eng)] = bool(
                    reject[eng][i]
                    and h is not None and h >= args.hellinger_threshold)
        row["killed_single"] = row["killed_aer"]
        cross = [row["killed_{}".format(e)] for e in CROSS_ENGINES]
        row["killed_consensus"] = sum(cross) > len(cross) / 2

    fieldnames = sorted({k for r in rows for k in r},
                        key=lambda k: (k not in rows[0], k))
    with open(out_dir / "results.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    per_op = {}
    for r in rows:
        d = per_op.setdefault(r["operator"], {
            "requested": 0, "applied": 0,
            "killed_single": 0, "killed_consensus": 0})
        d["requested"] += 1
        if r["applied"]:
            d["applied"] += 1
            d["killed_single"] += bool(r.get("killed_single"))
            d["killed_consensus"] += bool(r.get("killed_consensus"))
    for d in per_op.values():
        n = d["applied"] or 1
        d["score_single"] = round(d["killed_single"] / n, 4)
        d["score_consensus"] = round(d["killed_consensus"] / n, 4)

    unseedable = ["braket"] if "braket" in ENGINES else []
    summary = {
        "cases": [c[0] for c in CASES], "operators": OPERATORS,
        "mutants_per_cell": args.mutants, "shots": args.shots,
        "engine_seed": args.seed, "alpha": args.alpha,
        "hellinger_threshold": args.hellinger_threshold,
        "rotation_epsilon": args.rotation_epsilon,
        "correction": "benjamini-hochberg per oracle",
        "engines": selected_names,
        "cross_engines": CROSS_ENGINES,
        "unseedable_engines": unseedable,
        "per_operator": per_op,
        "versions": versions(),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    print("\n{} mutants requested ({} cases x {} operators x {})".format(
        len(rows), len(CASES), len(OPERATORS), args.mutants))
    print("{:18s} {:>8s} {:>8s} {:>14s} {:>17s}".format(
        "operator", "applied", "requested", "score(single)", "score(consensus)"))
    for op, d in per_op.items():
        print("{:18s} {:>8d} {:>8d} {:>14.2%} {:>17.2%}".format(
            op, d["applied"], d["requested"],
            d["score_single"], d["score_consensus"]))
    print("\nwrote {}/results.csv and summary.json".format(out_dir))


if __name__ == "__main__":
    main()
