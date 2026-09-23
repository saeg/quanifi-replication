#!/usr/bin/env python3
"""Study 1 — the builder x engine matrix.

The paper's two N-version studies never meet: one holds the builder fixed and
varies the engine, the other varies the builder but runs each on its own engine.
Builder and engine are therefore confounded in every branch of the second, and a
disagreement cannot be attributed to either factor. This study crosses them.

Four independently implemented builders (Qiskit, Cirq, Qrisp, PennyLane) each
emit the same Grover search as OpenQASM 2.0; six independently implemented
engines (Aer, Cirq, Qrisp, PennyLane, Braket, Q#) each sample every one of them.
Both prior studies reappear as margins of the result: the Qiskit row is the
engine-axis study, and the builder-with-own-engine diagonal is the builder-axis
study.

Processors are loaded from ``nifi_extensions_matrix/`` (the forked extension
directory the NiFi 2.10.0 instance serves), by file path rather than by putting
that directory on ``sys.path``, so the two extension trees cannot shadow each
other. Engines are unmodified in the fork and come from the normal path.

Outputs (under experiments/out/version_matrix/<timestamp>/):
  results.csv     one row per (builder, engine, case, seed) cell
  comparisons.csv one row per within-case engine pair and builder pair
  summary.json    attribution: builder effect, engine effect, outlier cells
  report.html     the matrix as a table, for the paper artifacts

Usage:
  .venv/bin/python experiments/version_matrix_study.py
  .venv/bin/python experiments/version_matrix_study.py \
      --builders qiskit,cirq --engines aer,cirq --shots 2048
"""
import argparse
import csv
import datetime
import hashlib
import html
import importlib.util
import itertools
import json
import platform
import sys
import time
from pathlib import Path

from _harness import MockContext, MockFlowFile, OUT_ROOT, ROOT

from QuantumDistributionComparison import (
    _chi2_two_sample, _hellinger, _normalize, _total_variation)
from multiple_comparisons import holm as holm_bonferroni  # shared with QuantumDistributionOracle

FORK = ROOT / "nifi_extensions_matrix"

# Test cases cross the two factors that matter for this circuit family: the
# width of the outcome space, and whether the marked state is changed by bit
# reversal. A palindromic marked state cannot expose an ordering fault, because
# the wrong answer and the right one are the same string -- so a width covered
# only by palindromes is a width at which ordering faults are undetectable by
# construction. `11` and `0110`, two of the three cases used previously, are
# both their own reverse, which left the four-qubit width with no ordering
# coverage at all. Each case is (marked state, iterations, partition label).
CASES = [
    ("10", 1, "asymmetric-2q"),
    ("11", 1, "palindrome-2q"),
    ("110", 2, "asymmetric-3q"),
    ("010", 2, "palindrome-3q"),
    ("0111", 2, "asymmetric-4q"),
    ("0110", 2, "palindrome-4q"),
]

BUILDERS = {
    "qiskit": "QiskitGroverCircuit",
    "cirq": "CirqGroverCircuit",
    "qrisp": "QrispGroverCircuit",
    "pennylane": "PennylaneGroverCircuit",
    "pyquil": "PyquilGroverCircuit",
}

ENGINES = {
    "aer": "QiskitAerSimulator",
    "cirq": "CirqSimulator",
    "qrisp": "QrispSimulator",
    "pennylane": "PennylaneSimulator",
    "braket": "BraketSimulator",   # no seed API: sampling varies run to run
    "qsharp": "QSharpSimulator",
    "pyquil": "PyquilSimulator",
}

# Which engine a builder's own framework provides. These cells are the diagonal
# that reproduces the paper's builder-axis study.
NATIVE_ENGINE = {
    "qiskit": "aer",
    "cirq": "cirq",
    "qrisp": "qrisp",
    "pennylane": "pennylane",
    "pyquil": "pyquil",
}


def load_builder(name):
    """Load a builder from the fork by path, under a distinct module name."""
    module = BUILDERS[name]
    key = "forkext_" + module
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, FORK / f"{module}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return getattr(sys.modules[key], module)


def load_engine(name):
    module = ENGINES[name]
    return getattr(__import__(module), module)


def build(builder, marked, iterations):
    """Build one circuit. Returns (qasm2 bytes, attributes) or (None, reason)."""
    cls = load_builder(builder)
    res = cls().transform(
        MockContext(**{"Marked State": marked,
                       "Num Iterations": str(iterations),
                       "Output Format": "qasm2"}),
        MockFlowFile())
    if res.relationship != "success":
        return None, res.attributes.get("grover.error", "build failed")
    return res.contents, res.attributes


def sample(engine, qasm2, shots, seed):
    """Sample one circuit on one engine. Returns (counts, None) or (None, reason)."""
    cls = load_engine(engine)
    props = {"Shots": str(shots)}
    if "Random Seed" in [d.name for d in cls().descriptors]:
        props["Random Seed"] = str(seed)
    ff = MockFlowFile(content=qasm2, attributes={"circuit.format": "qasm2"})
    try:
        res = cls().transform(MockContext(**props), ff)
    except Exception as exc:                      # an engine crash is data, not a stop
        return None, "{}: {}".format(type(exc).__name__, exc)
    if res.relationship != "success":
        return None, res.attributes.get("sim.error", "engine failed")
    return json.loads(res.contents.decode("utf-8")), None


def versions():
    out = {"python": platform.python_version()}
    for mod in ("qiskit", "cirq", "qrisp", "pennylane", "braket._sdk", "qsharp", "pyquil"):
        try:
            m = __import__(mod, fromlist=["__version__"])
            out[mod] = getattr(m, "__version__", "?")
        except Exception:
            out[mod] = "unavailable"
    return out


def compare(counts_a, counts_b):
    pa, pb = _normalize(counts_a), _normalize(counts_b)
    chi = _chi2_two_sample(counts_a, counts_b)
    return {
        "hellinger": round(_hellinger(pa, pb), 6),
        "total_variation": round(_total_variation(pa, pb), 6),
        "chi2": round(chi[0], 4) if chi else None,
        "dof": chi[1] if chi else None,
        "p_value": chi[2] if chi else None,
    }


def top_of(counts):
    return max(counts.items(), key=lambda kv: kv[1])[0] if counts else ""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shots", type=int, default=1024)
    ap.add_argument("--seeds", default="11,22")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--hellinger-threshold", type=float, default=0.1)
    ap.add_argument("--all-pairs", action="store_true",
                    help="also compare pairs that differ in BOTH builder "
                         "and engine, so every one of the C(N*M,2) pairs "
                         "in a case is compared, not only the margins")
    ap.add_argument("--builders", default=",".join(BUILDERS))
    ap.add_argument("--engines", default=",".join(ENGINES))
    ap.add_argument("--cases", default="",
                    help="comma-separated marked states; default is all four")
    args = ap.parse_args()

    seeds = [int(s) for s in args.seeds.split(",")]
    builders = [b.strip() for b in args.builders.split(",") if b.strip()]
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    unknown = set(builders) - set(BUILDERS) | set(engines) - set(ENGINES)
    if unknown:
        raise SystemExit("unknown builder/engine: " + ", ".join(sorted(unknown)))
    cases = CASES
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",")}
        cases = [c for c in CASES if c[0] in wanted]
        if not cases:
            raise SystemExit("no case matched --cases")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = OUT_ROOT / "version_matrix" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- Phase 1: build every circuit once per (builder, case) ----------
    circuits, build_failures = {}, []
    for builder, (marked, iters, _partition) in itertools.product(builders, cases):
        qasm, attrs = build(builder, marked, iters)
        if qasm is None:
            build_failures.append({"builder": builder, "marked_state": marked,
                                   "reason": attrs})
            print(f"  ! {builder}/{marked}: build unsupported — {attrs}")
            continue
        circuits[(builder, marked)] = (qasm, attrs)

    # A builder axis is only informative if the builders are distinct programs.
    digests = {k: hashlib.sha256(v[0]).hexdigest() for k, v in circuits.items()}
    collisions = []
    for (marked, _iters, _p) in cases:
        per_case = {b: digests[(b, marked)] for b in builders
                    if (b, marked) in digests}
        seen = {}
        for b, d in per_case.items():
            if d in seen:
                collisions.append({"marked_state": marked,
                                   "builders": [seen[d], b], "digest": d})
            seen[d] = b

    # ---- Phase 2: every cell -------------------------------------------
    rows = []
    counts_by_cell = {}
    for builder, (marked, iters, _partition), seed in itertools.product(
            builders, cases, seeds):
        key = (builder, marked)
        if key not in circuits:
            for engine in engines:
                rows.append({"builder": builder, "engine": engine,
                             "marked_state": marked, "iterations": iters,
                             "seed": seed, "shots": args.shots,
                             "status": "unsupported", "reason": "builder failed",
                             "top_outcome": "", "top_prob": None,
                             "correct": False, "circuit_digest": "",
                             "circuit_depth": None, "circuit_gates": None,
                             "wall_seconds": None})
            continue
        qasm, attrs = circuits[key]
        for engine in engines:
            t0 = time.time()
            counts, reason = sample(engine, qasm, args.shots, seed)
            elapsed = round(time.time() - t0, 3)
            if counts is None:
                rows.append({"builder": builder, "engine": engine,
                             "marked_state": marked, "iterations": iters,
                             "seed": seed, "shots": args.shots,
                             "status": "unsupported", "reason": reason,
                             "top_outcome": "", "top_prob": None,
                             "correct": False,
                             "circuit_digest": digests[key][:12],
                             "circuit_depth": attrs.get("circuit.depth"),
                             "circuit_gates": attrs.get("circuit.gate_count"),
                             "wall_seconds": elapsed})
                print(f"  ! {builder}/{engine}/{marked}: {reason}")
                continue
            counts_by_cell[(builder, engine, marked, seed)] = counts
            total = sum(counts.values()) or 1
            top = top_of(counts)
            rows.append({"builder": builder, "engine": engine,
                         "marked_state": marked, "iterations": iters,
                         "seed": seed, "shots": args.shots,
                         "status": "ok", "reason": "",
                         "top_outcome": top,
                         "top_prob": round(counts[top] / total, 4),
                         "correct": top == marked,
                         "circuit_digest": digests[key][:12],
                         "circuit_depth": attrs.get("circuit.depth"),
                         "circuit_gates": attrs.get("circuit.gate_count"),
                         "wall_seconds": elapsed})

    # ---- Phase 3: the two margins --------------------------------------
    # Engine margin: builder held fixed, engines compared pairwise.
    # Builder margin: engine held fixed, builders compared pairwise.
    comparisons = []
    for (marked, _iters, _p), seed in itertools.product(cases, seeds):
        for builder in builders:
            for a, b in itertools.combinations(sorted(engines), 2):
                ca = counts_by_cell.get((builder, a, marked, seed))
                cb = counts_by_cell.get((builder, b, marked, seed))
                if ca is None or cb is None:
                    continue
                comparisons.append({"axis": "engine", "held_fixed": builder,
                                    "a": a, "b": b, "marked_state": marked,
                                    "seed": seed, **compare(ca, cb)})
        for engine in engines:
            for a, b in itertools.combinations(sorted(builders), 2):
                ca = counts_by_cell.get((a, engine, marked, seed))
                cb = counts_by_cell.get((b, engine, marked, seed))
                if ca is None or cb is None:
                    continue
                comparisons.append({"axis": "builder", "held_fixed": engine,
                                    "a": a, "b": b, "marked_state": marked,
                                    "seed": seed, **compare(ca, cb)})

        if args.all_pairs:
            # The two margins above cover pairs sharing a builder or an engine.
            # The oracle on the canvas sees all 24 branches at once, so it also
            # compares the pairs that differ in both. Adding them makes the
            # comparison count C(N*M, 2) per case.
            cells = [(b, e) for b in builders for e in engines]
            for (ba, ea), (bb, eb) in itertools.combinations(cells, 2):
                if ba == bb or ea == eb:
                    continue
                ca = counts_by_cell.get((ba, ea, marked, seed))
                cb = counts_by_cell.get((bb, eb, marked, seed))
                if ca is None or cb is None:
                    continue
                comparisons.append({"axis": "cross", "held_fixed": "",
                                    "a": f"{ba}/{ea}", "b": f"{bb}/{eb}",
                                    "marked_state": marked,
                                    "seed": seed, **compare(ca, cb)})

    reject = holm_bonferroni([c["p_value"] for c in comparisons], args.alpha)
    for c, rej in zip(comparisons, reject):
        c["significant_holm"] = rej
        # Uncorrected significance: what an oracle that runs one test per pair
        # and does not correct for how many pairs it runs would report.
        c["significant_raw"] = (c["p_value"] is not None
                                and c["p_value"] < args.alpha)
        c["agree"] = (not rej) and c["hellinger"] < args.hellinger_threshold

    # ---- Phase 4: attribution ------------------------------------------
    def axis_stats(axis):
        sub = [c for c in comparisons if c["axis"] == axis]
        if not sub:
            return {"comparisons": 0}
        return {
            "comparisons": len(sub),
            "agreement_rate": round(sum(c["agree"] for c in sub) / len(sub), 4),
            "max_hellinger": max(c["hellinger"] for c in sub),
            "mean_hellinger": round(
                sum(c["hellinger"] for c in sub) / len(sub), 6),
            "holm_significant": int(sum(c["significant_holm"] for c in sub)),
        }

    # An interaction candidate is a cell that is wrong while its whole row and
    # its whole column are otherwise right: neither factor alone explains it, so
    # the defect is in that builder-engine pairing — one adapter.
    ok_rows = [r for r in rows if r["status"] == "ok"]
    interaction_candidates = []
    for r in ok_rows:
        if r["correct"]:
            continue
        row_peers = [x for x in ok_rows
                     if x["builder"] == r["builder"] and x["marked_state"] == r["marked_state"]
                     and x["seed"] == r["seed"] and x["engine"] != r["engine"]]
        col_peers = [x for x in ok_rows
                     if x["engine"] == r["engine"] and x["marked_state"] == r["marked_state"]
                     and x["seed"] == r["seed"] and x["builder"] != r["builder"]]
        if row_peers and col_peers and all(x["correct"] for x in row_peers) \
                and all(x["correct"] for x in col_peers):
            interaction_candidates.append({
                "builder": r["builder"], "engine": r["engine"],
                "marked_state": r["marked_state"], "seed": r["seed"],
                "top_outcome": r["top_outcome"],
                "note": "only this pairing is wrong; row and column are correct",
            })

    # The two prior studies, recovered as margins.
    engine_margin_qiskit = [c for c in comparisons
                            if c["axis"] == "engine" and c["held_fixed"] == "qiskit"]
    diagonal = [r for r in ok_rows if NATIVE_ENGINE.get(r["builder"]) == r["engine"]]

    # The agreement threshold is a fixed constant, but the Hellinger distance
    # between two CORRECT runs of the same circuit grows with the width of the
    # outcome space: more outcomes, fewer shots each, more sampling noise. Every
    # comparison here is between implementations that agree, so these numbers
    # are a measured noise floor, and a threshold below the floor would report
    # disagreements that are only sampling.
    noise_floor = {}
    for marked, _iters, _p in cases:
        vals = [c["hellinger"] for c in comparisons if c["marked_state"] == marked]
        if not vals:
            continue
        noise_floor[marked] = {
            "qubits": len(marked),
            "comparisons": len(vals),
            "max_hellinger": max(vals),
            "mean_hellinger": round(sum(vals) / len(vals), 6),
            "headroom_to_threshold": round(args.hellinger_threshold - max(vals), 6),
        }

    # Per-case verdicts, in the shape the paper's matrix table reports: the
    # top-1 oracle votes on the most frequent outcome, the distributional
    # oracle flags a case when any compared pair exceeds the threshold.
    per_case = {}
    for marked, _iters, part in cases:
        vals = sorted(c["hellinger"] for c in comparisons
                      if c["marked_state"] == marked)
        cells_c = [r for r in ok_rows if r["marked_state"] == marked]
        if not vals or not cells_c:
            continue
        mid = len(vals) // 2
        median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
        per_case[marked] = {
            "qubits": len(marked),
            "partition": part,
            "pairs_compared": len(vals),
            "median_hellinger": round(median, 6),
            "max_hellinger": max(vals),
            "top1_verdict": "pass" if all(r["correct"] for r in cells_c) else "fail",
            "distributional_verdict": (
                "disagree" if max(vals) >= args.hellinger_threshold else "pass"),
            "significant_pairs_raw": sum(
                c["significant_raw"] for c in comparisons
                if c["marked_state"] == marked),
            "significant_pairs_holm": sum(
                c["significant_holm"] for c in comparisons
                if c["marked_state"] == marked),
        }

    unsupported = [r for r in rows if r["status"] == "unsupported"]
    summary = {
        "design": {
            "builders": builders, "engines": engines,
            "cases": [{"marked_state": m, "iterations": i, "partition": p}
                      for m, i, p in cases],
            "case_selection": ("circuit width x bit-reversal symmetry: one "
                               "palindromic and one non-palindromic marked "
                               "state at each of 2, 3 and 4 qubits"),
            "seeds": seeds, "shots": args.shots,
            "cells_attempted": len(rows),
            "cells_ok": len(ok_rows),
            "cells_unsupported": len(unsupported),
        },
        "correctness": {
            "cells_correct": sum(r["correct"] for r in ok_rows),
            "correct_rate": round(
                sum(r["correct"] for r in ok_rows) / len(ok_rows), 4) if ok_rows else None,
            "incorrect_cells": [
                {k: r[k] for k in ("builder", "engine", "marked_state", "seed",
                                   "top_outcome", "top_prob")}
                for r in ok_rows if not r["correct"]],
        },
        "noise_floor_by_case": noise_floor,
        "per_case": per_case,
        "all_pairs": args.all_pairs,
        "attribution": {
            "engine_axis_builder_fixed": axis_stats("engine"),
            "builder_axis_engine_fixed": axis_stats("builder"),
            "interaction_candidates": interaction_candidates,
        },
        "reproduces_prior_studies": {
            "engine_axis_qiskit_row": {
                "comparisons": len(engine_margin_qiskit),
                "agreement_rate": round(
                    sum(c["agree"] for c in engine_margin_qiskit) / len(engine_margin_qiskit), 4)
                if engine_margin_qiskit else None,
                "max_hellinger": max((c["hellinger"] for c in engine_margin_qiskit),
                                     default=None),
                "note": "this row is the paper's Scenario A",
            },
            "builder_own_engine_diagonal": {
                "cells": len(diagonal),
                "all_correct": all(r["correct"] for r in diagonal) if diagonal else None,
                "note": "these cells are the paper's Scenario B",
            },
        },
        "circuit_independence": {
            "distinct_digests_per_case": {
                m: len({digests[(b, m)] for b in builders if (b, m) in digests})
                for m, _i, _p in cases},
            "collisions": collisions,
            "note": ("two builders emitting one circuit would make their "
                     "agreement vacuous"),
        },
        "build_failures": build_failures,
        "unsupported_cells": [
            {k: r[k] for k in ("builder", "engine", "marked_state", "reason")}
            for r in unsupported],
        "unseedable_engines": ["braket"],
        "alpha": args.alpha,
        "hellinger_threshold": args.hellinger_threshold,
        "versions": versions(),
    }

    # ---- Write ----------------------------------------------------------
    with open(out_dir / "results.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    if comparisons:
        with open(out_dir / "comparisons.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(comparisons[0].keys()))
            w.writeheader()
            w.writerows(comparisons)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (out_dir / "report.html").write_text(render_html(summary, rows, builders,
                                                     engines, cases))

    # ---- Print ----------------------------------------------------------
    print(f"\nbuilder x engine matrix: {len(builders)} builders x {len(engines)} "
          f"engines x {len(cases)} cases x {len(seeds)} seeds = {len(rows)} cells")
    print(f"  ok {len(ok_rows)}   unsupported {len(unsupported)}   "
          f"correct {summary['correctness']['cells_correct']}/{len(ok_rows)}")
    print("\ncorrect-cell matrix (per builder row, over all cases and seeds):")
    header = "  {:<11}".format("") + "".join(f"{e:>11}" for e in engines)
    print(header)
    for b in builders:
        cells = []
        for e in engines:
            sub = [r for r in rows if r["builder"] == b and r["engine"] == e]
            ok = sum(1 for r in sub if r["status"] == "ok" and r["correct"])
            cells.append(f"{ok}/{len(sub)}")
        print("  {:<11}".format(b) + "".join(f"{c:>11}" for c in cells))
    print("\nsampling-noise floor (all these comparisons AGREE, so this is "
          "noise, not divergence):")
    print("  case      qubits   comparisons   max H    mean H   headroom to "
          f"{args.hellinger_threshold}")
    for marked, d in sorted(noise_floor.items(), key=lambda kv: (kv[1]["qubits"], kv[0])):
        print(f"  |{marked:<6s}| {d['qubits']:>5d}   {d['comparisons']:>11d}   "
              f"{d['max_hellinger']:.4f}   {d['mean_hellinger']:.4f}   "
              f"{d['headroom_to_threshold']:+.4f}")
    tight = [m for m, d in noise_floor.items()
             if d["headroom_to_threshold"] < args.hellinger_threshold * 0.25]
    if tight:
        print(f"  NOTE: {', '.join('|' + m + '>' for m in tight)} sit within 25% "
              f"of the threshold on noise alone; the fixed threshold is not "
              f"width-independent.")

    ea = summary["attribution"]["engine_axis_builder_fixed"]
    ba = summary["attribution"]["builder_axis_engine_fixed"]
    print(f"\nengine axis  (builder fixed): agree {ea.get('agreement_rate')}  "
          f"max Hellinger {ea.get('max_hellinger')}")
    print(f"builder axis (engine fixed):  agree {ba.get('agreement_rate')}  "
          f"max Hellinger {ba.get('max_hellinger')}")
    if interaction_candidates:
        print("\ninteraction candidates (one pairing wrong, row and column right):")
        for c in interaction_candidates:
            print(f"  {c['builder']} x {c['engine']} on {c['marked_state']} "
                  f"-> {c['top_outcome']}")
    else:
        print("\nno interaction candidates")
    if collisions:
        print("\nWARNING: builders emitted identical circuits: " + repr(collisions))
    print(f"\nwrote {out_dir}/results.csv, comparisons.csv, summary.json, report.html")


def render_html(summary, rows, builders, engines, cases):
    def cell(b, e):
        sub = [r for r in rows if r["builder"] == b and r["engine"] == e]
        ok = [r for r in sub if r["status"] == "ok"]
        good = sum(1 for r in ok if r["correct"])
        if not sub:
            return '<td class="na">-</td>'
        if len(ok) < len(sub):
            return f'<td class="bad">{good}/{len(sub)} (unsupported)</td>'
        klass = "good" if good == len(sub) else "bad"
        return f'<td class="{klass}">{good}/{len(sub)}</td>'

    body = ["<tr><th></th>" + "".join(f"<th>{html.escape(e)}</th>"
                                      for e in engines) + "</tr>"]
    for b in builders:
        body.append(f"<tr><th>{html.escape(b)}</th>"
                    + "".join(cell(b, e) for e in engines) + "</tr>")

    att = summary["attribution"]
    ic = att["interaction_candidates"]
    ic_html = ("<p class='ok'>No interaction candidates: no cell is wrong while "
               "its row and column are right.</p>" if not ic else
               "<ul>" + "".join(
                   "<li><b>{} x {}</b> on |{}⟩ returned |{}⟩ while every other "
                   "engine for that builder and every other builder on that "
                   "engine were correct.</li>".format(
                       html.escape(c["builder"]), html.escape(c["engine"]),
                       html.escape(c["marked_state"]), html.escape(c["top_outcome"]))
                   for c in ic) + "</ul>")

    return f"""<!doctype html>
<meta charset="utf-8">
<title>Builder x engine matrix</title>
<style>
 body {{ font: 14px/1.5 system-ui, sans-serif; margin: 2rem; max-width: 60rem; }}
 table {{ border-collapse: collapse; margin: 1rem 0; }}
 th, td {{ border: 1px solid #ccc; padding: .4rem .7rem; text-align: center; }}
 th {{ background: #f4f4f6; }}
 td.good {{ background: #e8f5e9; }}
 td.bad  {{ background: #ffebee; }}
 td.na   {{ background: #fafafa; color: #999; }}
 .ok {{ color: #2e7d32; }}
 code {{ background: #f4f4f6; padding: .1rem .3rem; }}
</style>
<h1>Builder &times; engine matrix</h1>
<p>Each cell is one circuit builder's OpenQASM 2.0 executed by one engine,
counted as <i>correct cells / attempted cells</i> over
{len(cases)} cases and {len(summary['design']['seeds'])} seeds at
{summary['design']['shots']} shots.</p>
<table>{''.join(body)}</table>

<h2>Attribution</h2>
<p><b>Engine axis</b> (builder held fixed):
agreement {att['engine_axis_builder_fixed'].get('agreement_rate')},
max Hellinger {att['engine_axis_builder_fixed'].get('max_hellinger')},
over {att['engine_axis_builder_fixed'].get('comparisons')} comparisons.</p>
<p><b>Builder axis</b> (engine held fixed):
agreement {att['builder_axis_engine_fixed'].get('agreement_rate')},
max Hellinger {att['builder_axis_engine_fixed'].get('max_hellinger')},
over {att['builder_axis_engine_fixed'].get('comparisons')} comparisons.</p>

<h2>Interaction candidates</h2>
{ic_html}

<h2>Sampling-noise floor</h2>
<p>Every comparison in this study is between implementations that agree, so the
distances below are sampling noise rather than divergence. They grow with the
width of the outcome space, which means a fixed agreement threshold is not
width-independent: at some width the noise floor crosses it and correct
circuits start being reported as disagreeing.</p>
<table><tr><th>case</th><th>qubits</th><th>comparisons</th><th>max Hellinger</th>
<th>mean</th><th>headroom</th></tr>
{"".join(
  "<tr><td>|{}&rang;</td><td>{}</td><td>{}</td><td>{:.4f}</td><td>{:.4f}</td>"
  "<td>{:+.4f}</td></tr>".format(
      html.escape(m), d["qubits"], d["comparisons"], d["max_hellinger"],
      d["mean_hellinger"], d["headroom_to_threshold"])
  for m, d in sorted(summary["noise_floor_by_case"].items(),
                     key=lambda kv: (kv[1]["qubits"], kv[0])))}
</table>

<h2>Circuit independence</h2>
<p>Distinct circuit digests per case:
<code>{html.escape(json.dumps(summary['circuit_independence']['distinct_digests_per_case']))}</code>.
Two builders emitting one circuit would make their agreement vacuous.</p>

<h2>Environment</h2>
<p><code>{html.escape(json.dumps(summary['versions']))}</code><br>
Unseedable engines: {', '.join(summary['unseedable_engines'])} &mdash; their rows
can vary between executions.</p>
"""


if __name__ == "__main__":
    main()
