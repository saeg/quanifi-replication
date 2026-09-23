#!/usr/bin/env python3
"""Compare archived Grover hardware controls and mutants, one job at a time.

No provider access. Reuses the original analysis and oracle statistics.
The row check selects one mutant result and two other builders' control
results from the same job. It does not represent another hardware run.
"""

import argparse
import collections
import csv
import hashlib
import itertools
import json
from pathlib import Path

import grover_hw_matrix_analysis as base

ROOT = Path(__file__).resolve().parent.parent
BUILDERS = ("cirq", "pennylane", "qiskit")


def disagreements(cells):
    """Three two-sided comparisons, Holm-adjusted together, with |delta| >= .10.

    Receives no indication of which builder, if any, was mutated. Failure to
    reject is called no flagged difference, not statistical equivalence.
    """
    if set(cells) != set(BUILDERS):
        raise ValueError("A comparison needs exactly three builders")
    comparisons = []
    for left, right in itertools.combinations(BUILDERS, 2):
        a, b = cells[left], cells[right]
        if not a["shots"] or not b["shots"]:
            raise ValueError("Cannot compare a circuit with no shots")
        _, p_value = base.two_proportion_test(
            a["successes"],
            a["shots"],
            b["successes"],
            b["shots"],
            alternative="two-sided",
        )
        comparisons.append(
            {
                "left": left,
                "right": right,
                "delta": a["p"] - b["p"],
                "p_value": p_value,
            }
        )
    adjusted = base.holm([c["p_value"] for c in comparisons])
    for comparison, p_holm in zip(comparisons, adjusted):
        comparison["p_holm"] = p_holm
        comparison["flagged"] = (
            p_holm < base.ALPHA and abs(comparison["delta"]) >= base.MIN_DIFFERENCE
        )
    return comparisons


def identify_builder(comparisons):
    """Identify the common builder of exactly two flagged pairs, else none."""
    edges = [set((c["left"], c["right"])) for c in comparisons if c["flagged"]]
    if len(edges) != 2:
        return None
    common = edges[0] & edges[1]
    return next(iter(common)) if len(common) == 1 else None


def analyze_job(manifest, polled, ladder):
    """Keep job identity in every output; reject incomplete/duplicate inputs."""
    job_id = manifest["job_id"]
    if polled["job_id"] != job_id:
        raise ValueError("Manifest and results belong to different jobs")
    for document in (manifest, polled):
        labels = [e["label"] for e in document["entries"]]
        if len(labels) != len(set(labels)):
            raise ValueError("Duplicate circuit labels within a job")
    rows = base.build_rows({job_id: manifest}, {job_id: polled})
    if len(rows) != len(manifest["entries"]) or any(not r["shots"] for r in rows):
        raise ValueError("Incomplete circuit results")
    keys = [(r["case_id"], r["builder"], r["kind"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate case/builder/kind within a job")
    by_case = collections.defaultdict(dict)
    for row in rows:
        if row["kind"] == "control":
            by_case[row["case_id"]][row["builder"]] = row
    baselines = {case: disagreements(controls) for case, controls in by_case.items()}
    metadata = {
        "job_id": job_id,
        "device": manifest["device"],
        "submitted_at": manifest.get("submitted_at"),
    }
    detections = base.compute_mutants(rows, ladder)
    if any(d["detected"] is None for d in detections):
        raise ValueError("Missing control, mutant, or ladder entry")
    # Sensitivity check: all 24 tests in a batch, including the calibration
    # mutants. This does not change the archived script's unadjusted rule.
    adjusted = base.holm([d["p_value"] for d in detections])
    row_checks = []
    for detection, p_holm in zip(detections, adjusted):
        detection.update(metadata)
        detection["p_holm_batch"] = p_holm
        detection["detected_holm_batch"] = (
            p_holm < base.ALPHA and detection["hw_delta"] >= base.MIN_DIFFERENCE
        )
        controls = by_case[detection["case_id"]]
        builder = detection["builder"]
        control = controls[builder]
        mutant = next(
            r
            for r in rows
            if r["case_id"] == detection["case_id"]
            and r["builder"] == builder
            and r["kind"] == detection["rung"]
        )
        detection.update(
            {
                "num_qubits": control["num_qubits"],
                "expected": control["expected"],
                "control_successes": control["successes"],
                "control_shots": control["shots"],
                "mutant_successes": mutant["successes"],
                "mutant_shots": mutant["shots"],
                "control_p": control["p"],
                "mutant_p": mutant["p"],
            }
        )
        selected = dict(controls)
        selected[builder] = mutant
        comparisons = disagreements(selected)
        identified = identify_builder(comparisons)
        baseline_flagged = any(c["flagged"] for c in baselines[detection["case_id"]])
        if baseline_flagged:
            status = "baseline_disagreement"
        elif identified == builder:
            status = "identified"
        elif identified is not None:
            status = "wrong_builder"
        elif any(c["flagged"] for c in comparisons):
            status = "ambiguous"
        else:
            status = "no_disagreement"
        row_checks.append(
            {
                **metadata,
                "case_id": detection["case_id"],
                "expected": control["expected"],
                "num_qubits": control["num_qubits"],
                "mutated_builder": builder,
                "rung": detection["rung"],
                "status": status,
                "identified_builder": identified,
                "baseline_comparisons": baselines[detection["case_id"]],
                "comparisons": comparisons,
            }
        )
    nulls = [dict(r, **metadata) for r in base.compute_within_batch(rows)]
    return detections, row_checks, nulls


def aggregate(detections, row_checks):
    groups = collections.defaultdict(list)
    for d, check in zip(detections, row_checks):
        groups[(d["device"], d["rung"], d["num_qubits"])].append((d, check))
    output = []
    for (device, rung, width), group in sorted(groups.items()):
        counts = collections.Counter(c["status"] for _, c in group)
        output.append(
            {
                "device": device,
                "rung": rung,
                "num_qubits": width,
                "executions": len(group),
                "distinct_mutants": len(
                    {(d["builder"], d["case_id"]) for d, _ in group}
                ),
                "batches": len({d["job_id"] for d, _ in group}),
                "detected": sum(d["detected"] for d, _ in group),
                "detected_holm_batch": sum(d["detected_holm_batch"] for d, _ in group),
                "hw_delta_min": min(d["hw_delta"] for d, _ in group),
                "hw_delta_max": max(d["hw_delta"] for d, _ in group),
                **{
                    status: counts[status]
                    for status in (
                        "identified",
                        "baseline_disagreement",
                        "ambiguous",
                        "no_disagreement",
                        "wrong_builder",
                    )
                },
            }
        )
    return output


def run(archive, ladder_path):
    ladder = base.load_mutants(ladder_path)
    detections, row_checks, nulls, inputs = [], [], [], []
    circuit_hashes = collections.defaultdict(set)
    for provider in ("ibm", "iqm"):
        paths = sorted((archive / "manifests" / provider).glob("*.manifest.json"))
        if not paths:
            raise ValueError(f"No manifests for {provider}")
        for path in paths:
            poll_path = (
                archive
                / "polled"
                / provider
                / path.name.replace(".manifest.json", ".polled.json")
            )
            manifest = json.loads(path.read_text())
            polled = json.loads(poll_path.read_text())
            ds, cs, ns = analyze_job(manifest, polled, ladder)
            detections.extend(ds)
            row_checks.extend(cs)
            nulls.extend(ns)
            for entry in manifest["entries"]:
                a = entry["attributes"]
                key = (a["grover.case_id"], a["grover.builder"].lower(), entry["kind"])
                circuit_hashes[key].add(entry["qasm_sha256"])
            inputs.append(
                {
                    "manifest": str(path.relative_to(archive)),
                    "polled": str(poll_path.relative_to(archive)),
                    "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "polled_sha256": hashlib.sha256(poll_path.read_bytes()).hexdigest(),
                    "job_id": manifest["job_id"],
                    "device": manifest["device"],
                    "submitted_at": manifest.get("submitted_at"),
                }
            )
    summary = {
        "method": {
            "alpha": base.ALPHA,
            "minimum_probability_difference": base.MIN_DIFFERENCE,
            "detection": "One-sided control vs mutant; original unadjusted rule.",
            "detection_sensitivity": "Holm across all 24 mutant comparisons per job.",
            "row_check": "One mutant and two other builders' controls, same job; "
            "three two-sided tests, Holm per case/scenario/device/job; "
            "exactly two flagged pairs sharing one builder.",
            "baseline": "No row identification accepted if any original-control pair flags.",
            "scope": "Row checks use archived counts; no additional hardware execution. "
            "Cross-device batches are not paired or treated as simultaneous. "
            "Two-qubit mutants were calibration in Amendment A2. "
            "Repeated executions are not distinct mutants or independent calibrations.",
        },
        "inputs": inputs,
        "ladder_sha256": hashlib.sha256(ladder_path.read_bytes()).hexdigest(),
        "input_qasm_identical_across_jobs_and_devices": all(
            len(hashes) == 1 for hashes in circuit_hashes.values()
        ),
        "totals": aggregate(detections, row_checks),
        "null_comparisons": len(nulls),
        "max_null_control_distance": max(n["distance"] for n in nulls),
        "holm_changed_verdicts": sum(
            d["detected"] != d["detected_holm_batch"] for d in detections
        ),
    }
    return summary, detections, row_checks, nulls


def write_csv(path, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_report(out, summary, detections, row_checks, nulls):
    out.mkdir(parents=True, exist_ok=True)
    for name, data in (("summary", summary), ("row_checks", row_checks)):
        (out / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")
    write_csv(out / "detections.csv", detections)
    write_csv(out / "null_controls.csv", nulls)
    write_csv(out / "totals.csv", summary["totals"])
    lines = [
        "# Grover hardware mutation results",
        "",
        "Analysis added on 2026-09-15 using archived September 12–13 hardware jobs.",
        "No new hardware jobs were submitted for this analysis.",
        "",
        "## Method",
        "",
        *[f"- **{k}:** {v}" for k, v in summary["method"].items()],
        "",
        "The original plan includes hardware mutant detectability. The row check "
        "is specified here; it is not an endpoint specified in PREREG §9. "
        "The original plan and amendments have not been rewritten.",
        "",
        "Counts below are executions of fixed mutants. The same three builders and "
        "two cases occur in each width/rung group (six mutants), repeated in seven "
        "IBM batches and five IQM batches. Several batches share calibrations.",
        "",
        "## Detection against the same builder's control",
        "",
        "| Device | Rung | Qubits | Detected / executions | Holm sensitivity | Drop range |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for r in summary["totals"]:
        lines.append(
            f"| {r['device']} | {r['rung']} | {r['num_qubits']} | "
            f"{r['detected']}/{r['executions']} | {r['detected_holm_batch']} | "
            f"{r['hw_delta_min']:.3f}–{r['hw_delta_max']:.3f} |"
        )
    lines += [
        "",
        "## Identification from the other builders",
        "",
        "Each execution is checked separately on its own device. These totals "
        "do not count complete, time-matched 3 × 2 matrices.",
        "",
        "| Device | Rung | Qubits | Identified | Baseline disagreement | Ambiguous | No difference flagged | Wrong builder | Total |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in summary["totals"]:
        lines.append(
            f"| {r['device']} | {r['rung']} | {r['num_qubits']} | "
            f"{r['identified']} | {r['baseline_disagreement']} | {r['ambiguous']} | "
            f"{r['no_disagreement']} | {r['wrong_builder']} | {r['executions']} |"
        )
    lines += [
        "",
        f"Null-control comparisons: {summary['null_comparisons']}; "
        f"maximum absolute difference: {summary['max_null_control_distance']:.6f}.",
        "",
        f"Detection verdicts changed by the Holm sensitivity check: {summary['holm_changed_verdicts']}.",
        f"Input circuit hashes match across batches and devices: "
        f"{summary['input_qasm_identical_across_jobs_and_devices']}.",
        "",
        "## Reproduce",
        "",
        "From the replication repository, using its Python environment:",
        "",
        "```sh",
        ".venv/bin/python experiments/grover_hw_mutation_analysis.py",
        "```",
        "",
        "`summary.json` records the input paths and SHA-256 hashes. "
        "`detections.csv` retains all per-job controls, mutants, effects and p-values. "
        "`row_checks.json` retains every baseline and substituted comparison, "
        "including unadjusted and adjusted p-values. `null_controls.csv` contains "
        "the Qiskit null comparisons. Missing or duplicate circuit results fail "
        "the analysis instead of being silently omitted.",
        "",
    ]
    (out / "README.md").write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive", type=Path, default=ROOT / "experiments/results/grover_hw_matrix"
    )
    parser.add_argument(
        "--ladder", type=Path, default=ROOT / "experiments/grover_hw_mutants.json"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "experiments/results/grover_hw_mutation_analysis",
    )
    args = parser.parse_args()
    results = run(args.archive, args.ladder)
    write_report(args.out, *results)
    print(json.dumps(results[0]["totals"], indent=2))


if __name__ == "__main__":
    main()
