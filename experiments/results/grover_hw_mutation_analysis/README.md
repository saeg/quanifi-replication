# Grover hardware mutation results

Analysis added on 2026-09-15 using archived September 12–13 hardware jobs.
No new hardware jobs were submitted for this analysis.

## Method

- **alpha:** 0.05
- **minimum_probability_difference:** 0.1
- **detection:** One-sided control vs mutant; original unadjusted rule.
- **detection_sensitivity:** Holm across all 24 mutant comparisons per job.
- **row_check:** One mutant and two other builders' controls, same job; three two-sided tests, Holm per case/scenario/device/job; exactly two flagged pairs sharing one builder.
- **baseline:** No row identification accepted if any original-control pair flags.
- **scope:** Row checks use archived counts; no additional hardware execution. Cross-device batches are not paired or treated as simultaneous. Two-qubit mutants were calibration in Amendment A2. Repeated executions are not distinct mutants or independent calibrations.

The original plan includes hardware mutant detectability. The row check is specified here; it is not an endpoint specified in PREREG §9. The original plan and amendments have not been rewritten.

Counts below are executions of fixed mutants. The same three builders and two cases occur in each width/rung group (six mutants), repeated in seven IBM batches and five IQM batches. Several batches share calibrations.

## Detection against the same builder's control

| Device | Rung | Qubits | Detected / executions | Holm sensitivity | Drop range |
|---|---|---:|---:|---:|---:|
| garnet | large | 2 | 30/30 | 30 | 0.441–0.946 |
| garnet | large | 4 | 0/30 | 0 | 0.011–0.084 |
| garnet | medium | 4 | 0/30 | 0 | -0.011–0.049 |
| garnet | small | 4 | 0/30 | 0 | -0.020–0.039 |
| ibm_kingston | large | 2 | 42/42 | 42 | 0.406–0.945 |
| ibm_kingston | large | 4 | 42/42 | 42 | 0.197–0.589 |
| ibm_kingston | medium | 4 | 34/42 | 34 | 0.077–0.214 |
| ibm_kingston | small | 4 | 4/42 | 4 | -0.002–0.146 |

## Identification from the other builders

Each execution is checked separately on its own device. These totals do not count complete, time-matched 3 × 2 matrices.

| Device | Rung | Qubits | Identified | Baseline disagreement | Ambiguous | No difference flagged | Wrong builder | Total |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| garnet | large | 2 | 30 | 0 | 0 | 0 | 0 | 30 |
| garnet | large | 4 | 0 | 0 | 0 | 30 | 0 | 30 |
| garnet | medium | 4 | 0 | 0 | 0 | 30 | 0 | 30 |
| garnet | small | 4 | 0 | 0 | 0 | 30 | 0 | 30 |
| ibm_kingston | large | 2 | 42 | 0 | 0 | 0 | 0 | 42 |
| ibm_kingston | large | 4 | 24 | 18 | 0 | 0 | 0 | 42 |
| ibm_kingston | medium | 4 | 19 | 18 | 3 | 2 | 0 | 42 |
| ibm_kingston | small | 4 | 1 | 18 | 3 | 20 | 0 | 42 |

Null-control comparisons: 48; maximum absolute difference: 0.046875.

Detection verdicts changed by the Holm sensitivity check: 0.
Input circuit hashes match across batches and devices: True.

## Reproduce

From the replication repository, using its Python environment:

```sh
.venv/bin/python experiments/grover_hw_mutation_analysis.py
```

`summary.json` records the input paths and SHA-256 hashes. `detections.csv` retains all per-job controls, mutants, effects and p-values. `row_checks.json` retains every baseline and substituted comparison, including unadjusted and adjusted p-values. `null_controls.csv` contains the Qiskit null comparisons. Missing or duplicate circuit results fail the analysis instead of being silently omitted.
