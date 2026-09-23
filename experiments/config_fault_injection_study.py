#!/usr/bin/env python3
"""Configuration fault-injection study for the low-code layer.

This is Layer B from the mutation-testing design: the program artifact is held
fixed, while test-case attributes such as the marked state, iteration count, or
shot budget are perturbed before they reach the framework branches.  The
expected answer remains the original row's ground truth, so a K-way consensus
oracle can distinguish:

  PASS     the configuration fault escaped;
  FAIL     all branches agreed, but on the wrong answer;
  DISAGREE one or more branches dissented from the majority.

Outputs (under experiments/out/config_faults/<timestamp>/):
  results.csv   one row per control/mutant case
  summary.json  survival/mutation score overall and per operator
  report.html   human-readable summary

Usage:
  .venv/bin/python experiments/config_fault_injection_study.py
"""
import csv
import datetime
import html
import json
from collections import defaultdict

from _harness import MockContext, MockFlowFile, OUT_ROOT, result_to_flowfile

from QuantumTestCaseSource import QuantumTestCaseSource
from QuantumConsensusOracle import _consensus, _normalize
from QiskitGroverCircuit import QiskitGroverCircuit
from QiskitAerSimulator import QiskitAerSimulator
from CirqGroverCircuit import CirqGroverCircuit
from CirqSimulator import CirqSimulator
from QrispGroverSearch import QrispGroverSearch


BASE_ROWS = [
    {
        "grover.marked_state": "11",
        "grover.num_iterations": "1",
        "grover.shots": "1024",
        "test.expected": "11",
        "test.partition": "two-qubit-symmetric",
    },
    {
        "grover.marked_state": "110",
        "grover.num_iterations": "2",
        "grover.shots": "1024",
        "test.expected": "110",
        "test.partition": "non-palindromic-bit-order",
    },
    {
        "grover.marked_state": "0110",
        "grover.num_iterations": "2",
        "grover.shots": "1024",
        "test.expected": "0110",
        "test.partition": "four-qubit-phase-kickback",
    },
]

DEFAULT_OPERATORS = [
    "marked_state.bitflip",
    "iterations.offbyone",
    "iterations.zero",
    "shots.shrink",
]


def counts_from_result(result):
    return json.loads(result.contents.decode("utf-8"))


def run_qiskit(case, seed):
    builder = QiskitGroverCircuit().transform(
        MockContext(**{
            "Marked State": case["grover.marked_state"],
            "Num Iterations": case["grover.num_iterations"],
            "Output Format": "qasm2",
        }),
        MockFlowFile(attributes=case),
    )
    if builder.relationship != "success":
        raise RuntimeError("QiskitGroverCircuit failed: {}".format(builder.attributes))
    sim = QiskitAerSimulator().transform(
        MockContext(**{
            "Shots": case.get("grover.shots", "1024"),
            "Random Seed": str(seed),
            "Noise Model": case.get("sim.noise_model", "none"),
        }),
        result_to_flowfile(builder),
    )
    if sim.relationship != "success":
        raise RuntimeError("QiskitAerSimulator failed: {}".format(sim.attributes))
    return counts_from_result(sim)


def run_cirq(case, seed):
    builder = CirqGroverCircuit().transform(
        MockContext(**{
            "Marked State": case["grover.marked_state"],
            "Num Iterations": case["grover.num_iterations"],
            "Output Format": "qasm2",
        }),
        MockFlowFile(attributes=case),
    )
    if builder.relationship != "success":
        raise RuntimeError("CirqGroverCircuit failed: {}".format(builder.attributes))
    sim = CirqSimulator().transform(
        MockContext(**{
            "Shots": case.get("grover.shots", "1024"),
            "Random Seed": str(seed),
            "Noise Model": case.get("sim.noise_model", "none"),
        }),
        result_to_flowfile(builder),
    )
    if sim.relationship != "success":
        raise RuntimeError("CirqSimulator failed: {}".format(sim.attributes))
    return counts_from_result(sim)


def run_qrisp(case, seed):
    result = QrispGroverSearch().transform(
        MockContext(**{
            "Marked State": case["grover.marked_state"],
            "Num Iterations": case["grover.num_iterations"],
            "Shots": case.get("grover.shots", "1024"),
            "Random Seed": str(seed),
        }),
        MockFlowFile(attributes=case),
    )
    if result.relationship != "success":
        raise RuntimeError("QrispGroverSearch failed: {}".format(result.attributes))
    return counts_from_result(result)


BRANCHES = {
    "qiskit": run_qiskit,
    "cirq": run_cirq,
    "qrisp": run_qrisp,
}


def generate_cases(run_id, operators, mutation_seed):
    source = QuantumTestCaseSource().transform(
        MockContext(**{
            "Test Matrix": json.dumps(BASE_ROWS),
            "Case ID Prefix": "cfg",
            "Run ID": run_id,
            "Mutation Operators": ",".join(operators),
            "Mutation Seed": str(mutation_seed),
        }),
        MockFlowFile(),
    )
    if source.relationship != "success":
        raise RuntimeError("QuantumTestCaseSource failed: {}".format(source.attributes))
    return json.loads(source.contents.decode("utf-8"))


def classify(verdict):
    return "survived" if verdict == "PASS" else "killed"


def summarize(rows):
    mutants = [r for r in rows if r["mut_applied"]]
    controls = [r for r in rows if not r["mut_applied"]]
    killed = sum(r["outcome"] == "killed" for r in mutants)
    by_operator = {}
    groups = defaultdict(list)
    for row in mutants:
        groups[row["operator"]].append(row)
    for op, op_rows in sorted(groups.items()):
        op_killed = sum(r["outcome"] == "killed" for r in op_rows)
        by_operator[op] = {
            "mutants": len(op_rows),
            "killed": op_killed,
            "survived": len(op_rows) - op_killed,
            "mutation_score": round(op_killed / len(op_rows), 4),
            "survival_rate": round((len(op_rows) - op_killed) / len(op_rows), 4),
        }
    return {
        "controls": len(controls),
        "control_dissent": sum(r["verdict"] != "PASS" for r in controls),
        "mutants": len(mutants),
        "killed": killed,
        "survived": len(mutants) - killed,
        "mutation_score": round(killed / len(mutants), 4) if mutants else 0.0,
        "survival_rate": round((len(mutants) - killed) / len(mutants), 4)
        if mutants else 0.0,
        "by_operator": by_operator,
    }


def pct(value):
    return round(value * 100.0, 1)


def render_html(out_dir, rows, summary, operators):
    lines = [
        "<!doctype html>",
        "<html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<title>Quanifi configuration fault injection</title>",
        "<style>",
        "body{font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:32px;max-width:1120px;color:#172033}",
        "h1,h2{line-height:1.2} code{background:#f3f4f6;padding:2px 5px;border-radius:4px}",
        "table{border-collapse:collapse;width:100%;margin:16px 0} th,td{border:1px solid #d7dce2;padding:7px 9px;text-align:left}",
        "th{background:#eef1f5}.note{border-left:4px solid #6f2da8;background:#f7f0ff;padding:10px 14px}",
        "</style></head><body>",
        "<h1>Configuration fault-injection study</h1>",
        "<p class=\"note\">Operators: <code>{}</code>. Controls keep the original attributes; mutants perturb one FlowFile attribute while preserving <code>test.expected</code>.</p>".format(
            html.escape(", ".join(operators))
        ),
        "<h2>Summary</h2>",
        "<table><tbody>",
        "<tr><th>Controls</th><td>{}</td></tr>".format(summary["controls"]),
        "<tr><th>Control dissents</th><td>{}</td></tr>".format(summary["control_dissent"]),
        "<tr><th>Mutants</th><td>{}</td></tr>".format(summary["mutants"]),
        "<tr><th>Killed</th><td>{}</td></tr>".format(summary["killed"]),
        "<tr><th>Mutation score</th><td>{:.1f}%</td></tr>".format(pct(summary["mutation_score"])),
        "</tbody></table>",
        "<h2>Per operator</h2>",
        "<table><thead><tr><th>Operator</th><th>Mutants</th><th>Killed</th><th>Survived</th><th>Mutation score</th></tr></thead><tbody>",
    ]
    for op, data in summary["by_operator"].items():
        lines.append(
            "<tr><td><code>{}</code></td><td>{}</td><td>{}</td><td>{}</td><td>{:.1f}%</td></tr>".format(
                html.escape(op), data["mutants"], data["killed"],
                data["survived"], pct(data["mutation_score"])
            )
        )
    lines += [
        "</tbody></table>",
        "<h2>Case detail</h2>",
        "<table><thead><tr><th>Case</th><th>Mutation</th><th>Target</th><th>Iterations</th><th>Shots</th><th>Majority</th><th>Verdict</th><th>Outcome</th></tr></thead><tbody>",
    ]
    for row in rows:
        lines.append(
            "<tr><td><code>{}</code></td><td><code>{}</code></td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                html.escape(row["case_id"]), html.escape(row["operator"] or "control"),
                html.escape(row["marked_state"]), html.escape(row["iterations"]),
                html.escape(row["shots"]), html.escape(row["majority_top"]),
                html.escape(row["verdict"]), html.escape(row["outcome"]),
            )
        )
    lines += [
        "</tbody></table>",
        "<p>Artifacts in this directory: <code>results.csv</code>, <code>summary.json</code>, and this <code>report.html</code>.</p>",
        "</body></html>",
    ]
    (out_dir / "report.html").write_text("\n".join(lines))


def main():
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--operators", default=",".join(DEFAULT_OPERATORS))
    ap.add_argument("--seed", type=int, default=11, help="sampling seed")
    ap.add_argument("--mutation-seed", type=int, default=4242)
    args = ap.parse_args()

    operators = [op.strip() for op in args.operators.split(",") if op.strip()]
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = "cfg-{}".format(stamp)
    out_dir = OUT_ROOT / "config_faults" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    cases = generate_cases(run_id, operators, args.mutation_seed)
    rows = []
    for case in cases:
        entries = []
        errors = []
        for label, fn in BRANCHES.items():
            try:
                counts = fn(case, args.seed)
                entries.append({"label": label, "dist": _normalize(counts)})
            except Exception as exc:  # noqa: BLE001 - recorded as an experimental outcome
                errors.append("{}: {}".format(label, exc))
        if errors:
            verdict = "DISAGREE"
            result = {
                "verdict": verdict,
                "reason": "; ".join(errors),
                "majority_top": "",
                "majority_count": 0,
                "branches": len(entries),
                "dissenters": [e.split(":", 1)[0] for e in errors],
                "max_hellinger": 0.0,
            }
        else:
            result = _consensus(
                entries,
                expected=case.get("test.expected", ""),
                check_gt=True,
            )
            verdict = result["verdict"]
        mut_applied = case.get("mut.applied") == "true"
        outcome = classify(verdict) if mut_applied else (
            "control_dissent" if verdict != "PASS" else "control_pass"
        )
        rows.append({
            "case_id": case.get("test.case_id", ""),
            "base_case_id": case.get("mut.base_case_id", ""),
            "mut_applied": mut_applied,
            "operator": case.get("mut.operator", ""),
            "target_attr": case.get("mut.target_attr", ""),
            "original_value": case.get("mut.original_value", ""),
            "marked_state": case.get("grover.marked_state", ""),
            "iterations": case.get("grover.num_iterations", ""),
            "shots": case.get("grover.shots", ""),
            "expected": case.get("test.expected", ""),
            "majority_top": result["majority_top"],
            "majority_count": result["majority_count"],
            "branches": result["branches"],
            "dissenters": ",".join(result["dissenters"]),
            "max_hellinger": round(result["max_hellinger"], 6),
            "verdict": verdict,
            "reason": result["reason"],
            "outcome": outcome,
        })

    summary = summarize(rows)
    summary.update({
        "run_id": run_id,
        "seed": args.seed,
        "mutation_seed": args.mutation_seed,
        "operators": operators,
        "branches": sorted(BRANCHES),
        "base_rows": BASE_ROWS,
    })

    with open(out_dir / "results.csv", "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    render_html(out_dir, rows, summary, operators)

    print("configuration fault injection: {} mutants, {:.1f}% killed".format(
        summary["mutants"], pct(summary["mutation_score"])
    ))
    for op, data in summary["by_operator"].items():
        print("  {:24s} {:>5.1f}% ({}/{})".format(
            op, pct(data["mutation_score"]), data["killed"], data["mutants"]
        ))
    print("wrote {}".format(out_dir))


if __name__ == "__main__":
    main()
