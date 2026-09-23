"""Known faults, ambiguous baselines, and archive completeness for row checks."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))
import grover_hw_mutation_analysis as analysis


def cells(probabilities, shots=10000):
    return {
        builder: {"p": p, "shots": shots, "successes": round(p * shots)}
        for builder, p in zip(analysis.BUILDERS, probabilities)
    }


@pytest.mark.parametrize("mutated", analysis.BUILDERS)
def test_identifies_each_planted_builder_without_mutation_labels(mutated):
    observed = cells([0.93, 0.93, 0.93])
    observed[mutated] = {"p": 0.2, "shots": 10000, "successes": 2000}
    assert analysis.identify_builder(analysis.disagreements(observed)) == mutated


@pytest.mark.parametrize("probabilities", [[0.93, 0.93, 0.93], [0.2, 0.5, 0.8]])
def test_no_unique_builder_for_zero_or_three_disagreements(probabilities):
    assert (
        analysis.identify_builder(analysis.disagreements(cells(probabilities))) is None
    )


def test_statistical_significance_alone_does_not_clear_effect_margin():
    pairs = analysis.disagreements(cells([0.50, 0.59, 0.59], shots=100000))
    assert any(p["p_holm"] < 0.05 for p in pairs)
    assert not any(p["flagged"] for p in pairs)


def archive(job_id="job-1", device="ibm_kingston", control_p=(0.93, 0.93, 0.93)):
    manifest = {"job_id": job_id, "device": device, "entries": []}
    polled = {"job_id": job_id, "bit_order": "q0_right", "entries": []}
    ladder = []
    for builder, p in zip(analysis.BUILDERS, control_p):
        for kind, probability in (("control", p), ("large", 0.20)):
            label = f"{builder}-{kind}"
            manifest["entries"].append(
                {
                    "label": label,
                    "kind": kind,
                    "num_qubits": 2,
                    "qasm_sha256": f"same-{label}",
                    "attributes": {
                        "grover.builder": builder,
                        "grover.expected": "10",
                        "grover.case_id": "case-0",
                    },
                }
            )
            successes = round(probability * 10000)
            polled["entries"].append(
                {
                    "label": label,
                    "shots_done": 10000,
                    "counts": {"01": successes, "00": 10000 - successes},
                }
            )
        ladder.append(
            {"builder": builder, "case": "10", "rung": "large", "delta_sim": 0.73}
        )
    null = copy.deepcopy(manifest["entries"][-2])
    null.update(label="null", kind="null")
    manifest["entries"].append(null)
    result = copy.deepcopy(polled["entries"][-2])
    result["label"] = "null"
    polled["entries"].append(result)
    return manifest, polled, ladder


def test_nonpalindromic_bit_order_and_same_job_reference():
    detections, checks, nulls = analysis.analyze_job(*archive())
    assert all(d["control_p"] == 0.93 and d["mutant_p"] == 0.20 for d in detections)
    assert all(c["status"] == "identified" for c in checks)
    assert nulls[0]["distance"] == 0


def test_preexisting_builder_disagreement_is_not_a_localization_success():
    _, checks, _ = analysis.analyze_job(*archive(control_p=(0.93, 0.93, 0.60)))
    assert all(c["status"] == "baseline_disagreement" for c in checks)


@pytest.mark.parametrize("defect", ["missing", "duplicate", "wrong_job", "zero_shots"])
def test_incomplete_or_mismatched_results_are_rejected(defect):
    manifest, polled, ladder = archive()
    if defect == "missing":
        polled["entries"].pop()
    elif defect == "duplicate":
        polled["entries"].append(polled["entries"][0])
    elif defect == "wrong_job":
        polled["job_id"] = "different-job"
    else:
        polled["entries"][0]["shots_done"] = 0
    with pytest.raises(ValueError):
        analysis.analyze_job(manifest, polled, ladder)


def test_repeated_jobs_are_retained_without_counting_new_mutants(tmp_path):
    for provider, job, device in (
        ("ibm", "first", "ibm_kingston"),
        ("ibm", "repeat", "ibm_kingston"),
        ("iqm", "first", "garnet"),
    ):
        manifest, polled, ladder = archive(job, device)
        for folder, suffix, data in (
            ("manifests", "manifest", manifest),
            ("polled", "polled", polled),
        ):
            directory = tmp_path / folder / provider
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{job}.{suffix}.json").write_text(json.dumps(data))
    ladder_path = tmp_path / "ladder.json"
    ladder_path.write_text(json.dumps(ladder))
    summary, detections, _, _ = analysis.run(tmp_path, ladder_path)
    ibm = next(t for t in summary["totals"] if t["device"] == "ibm_kingston")
    assert len(detections) == 9
    assert ibm["executions"] == 6
    assert ibm["distinct_mutants"] == 3
    assert ibm["batches"] == 2
    assert summary["input_qasm_identical_across_jobs_and_devices"]
