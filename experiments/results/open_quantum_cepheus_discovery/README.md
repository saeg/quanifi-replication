# Open Quantum defect: discovery on Rigetti Cepheus

The runs in which the Open Quantum circuit-modification defect was first seen
and then isolated, before the two-route comparison in
`../open_quantum_route_comparison/`. All jobs went through Open Quantum. These
runs were exploratory: none of them enters a confirmatory analysis.

| Folder | Device | What ran | Circuits |
|---|---|---|---|
| `qualification_run/` | `rigetti:cepheus-1-108q` | three two-bit adders (Qiskit CDKM, QFT, semi-adder) on seven inputs | 21 |
| `repeat_probe/` | `rigetti:cepheus-1-108q` | CDKM on `3+0`, `1+3` and `0+3`, four repeats each | 12 |
| `layout_probe/` | `rigetti:cepheus-1-108q` | CDKM on the same three inputs, pinned to different qubit regions | 9 of 12 collected |
| `toffoli_probe/` | Cepheus and IQM `garnet` | a single Toffoli as `ccx` and decomposed, plus sanity, bit-order and routing checks | 14 |

`qualification_run/plan.json` also lists two later stages (`carry.break` and
`gate.remove` mutants). They were never submitted; `report.json` records zero
submitted circuits for both.

## Files

Each campaign folder has the plan (circuits as submitted, expected answers,
result-qubit positions), the free preflight, the submission record and the
collected counts. Counts are keyed q0-left. `toffoli_probe/` has the two
Toffoli circuits, the job list with expected and observed outcomes
(`results/jobs_summary.csv`), raw counts (`results/raw_counts.json`), and a
local `quilc` compilation of the decomposed Toffoli that is correct
(`offline_quilc/`).

## What the runs show

`../defect_evidence_tables.md` decodes every run. For the CDKM adder, the sum is
stored little-endian in q2, q3 and q4:

- The qualification run gave the right sum for five of seven inputs.
- `3+0` returned `111010` (sum 5) in 44–48% of the shots across four repeats.
  The correct `111100` (sum 3) appeared in at most 3 of 512 shots.
- `1+3` returned `100000` (sum 0) in 75–79% of the shots, where `100010`
  (sum 4) was expected.
- The decomposed Toffoli returned `110` on both Cepheus (0.87) and `garnet`
  (0.96), while `ccx` was correct on both. Because the fault appeared on two
  vendors, it pointed to the Open Quantum layer, the part of the path they
  share.

Simulating the submitted adder circuits with every Rz gate removed reproduces
the two wrong sums (`3+0` gives 5, `1+3` gives 0); see
`../open_quantum_route_comparison/adders/checks/offline_checks.md`.

## Rerun on 25 September 2026 (`rerun_2026-09-25/`)

The two Toffoli circuits of `toffoli_probe/` and the CDKM `3+0` and `1+3`
circuits of `repeat_probe/` (byte-identical files in `circuits/`) were sent again
to Rigetti Cepheus through Open Quantum, 512 shots. The results repeat the
September pattern: `3+0` returned `111010` (sum 5) at 0.654 and the correct
`111100` never appeared; `1+3` returned `100000` (sum 0) at 0.711; the decomposed
Toffoli returned `110` at 0.867 while `ccx` gave `111` at 0.594.
