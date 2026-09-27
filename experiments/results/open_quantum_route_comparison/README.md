# Open Quantum route comparison (Experiment 2)

Per-circuit counts and job identifiers behind Table III of the paper and the
Grover route comparison in its Section VI. Both routes reached the same device,
IQM's `garnet`, on 17 September 2026, with 512 shots per circuit:

- **Direct**: IQM Resonance. The client transpiled each file for `garnet`
  (Qiskit, `optimization_level=1`, `seed_transpiler=11`) and sent all circuits
  of an experiment as one job. Both direct jobs report calibration set
  `149b0089-909e-46d0-badf-842d1d57fce8`.
- **Open Quantum**: the multi-vendor cloud service in front of the same device.
  The same files were sent byte-for-byte, one job per circuit.

## Circuits

**Adders** (`adders/`): four inputs, `0+0` to `1+1`, with three adders:
Qiskit's CDKM ripple-carry, Cirq's QFT adder and PennyLane's out-of-place adder.
Each adder was compiled once to {H, X, CX, Rz}, and the X gates that prepare
each input were then added, so all four inputs share one compiled adder body.
The CDKM adder was also submitted with its two Toffoli gates left intact. A
single Toffoli gate was submitted both as `ccx` and decomposed.

**Grover** (`grover/`): the 12 builder circuits and 4 readout baselines of the
Grover hardware matrix, copied from the `garnet` manifest in
`../grover_hw_matrix/`, compiled to the same {H, X, CX, Rz} set. Null replicates
and mutants were not included.

## Files

| Path | Content |
|---|---|
| `adders/direct_iqm/adder-defect-direct-01a0ae3a-….json` | direct job: circuit names, raw counts (Qiskit order, q0 rightmost), IQM job document |
| `adders/open_quantum/job_ids.json` | per circuit: Open Quantum job id and counts (q0 leftmost) |
| `adders/checks/circuits/` | the submitted files (`.qasm`) |
| `adders/checks/offline_checks.{json,md}`, `controlled_reproduction.json`, `cdkm_whole_adder_verification.json` | noiseless checks: every circuit simulates to the correct answer; predictions of the Rz-removal rule |
| `adders/checks/preflight_garnet.json` | free Open Quantum preflight (validation and price quote) |
| `grover/direct_iqm/adder-defect-direct-01a0ae53-….json` | direct Grover job (same format as the adder file) |
| `grover/open_quantum/job_ids.json` | Open Quantum Grover jobs and counts |
| `grover/checks/` | Grover circuit files and noiseless checks |

Absolute local paths inside provenance fields were shortened to repository
names. Counts, job identifiers and circuits are unchanged.

## Results

`../defect_evidence_tables.md` (written by `experiments/defect_evidence_tables.py`)
lists the probability of the correct answer and the most frequent outcome for
every circuit on both routes.

In short, every circuit returned the correct answer as its most frequent outcome
through the direct route (0.73–0.88). Through Open Quantum, the decomposed
Toffoli returned `110` (0.97); the decomposed CDKM adder gave a wrong sum for
`1+0`; PennyLane's adder returned `00` for every input except `0+0` (0.99–1.00);
the QFT adder's probability of a correct answer fell to 0.22–0.30, close to the
chance level of 0.25. Circuits with intact Toffoli gates were correct on both
routes. For Grover, only Cirq's two-qubit circuits failed through Open Quantum
(`00` at 0.98 and 0.99). No job on either route reported an error or warning.

## The Rz-removal rule

Removing every Rz gate from the compiled body, keeping the X gates that prepare
the inputs, and simulating the remainder matches the pattern of the adder and
Toffoli results through Open Quantum, including the inputs for which the
decomposed CDKM adder still succeeds. It does not establish what the service
changed: Open Quantum does not return the executed circuit, and the rule wrongly
predicts failures for PennyLane's two-qubit Grover circuits.

## Timeline and disclosure

The defect was first seen on Rigetti `Cepheus-1-108Q` and isolated with a
minimal Toffoli probe on Rigetti and IQM through Open Quantum before this
experiment (see `../open_quantum_cepheus_discovery/`). This two-route comparison
was run afterwards. The provider confirmed the defect, traced it to a QASM
parsing function, and on 18 September reported it fixed as of 17 September.
Cirq's Grover circuits still failed through Open Quantum in this run, so the
defect was active when these jobs executed. The 25 September rerun below shows
that the reported fix did not cover these submissions.

## Rerun on 25 September 2026 (`rerun_2026-09-25/`)

The same 18 adder/Toffoli files and 16 Grover files were sent again through both
routes with the same submission code, eight days after the provider reported the
defect fixed. Direct IQM jobs: `01a0da40-2323-7075-a91c-21587d178506` (adders)
and `01a0da40-744c-7570-9ec6-a1b5c154848f` (Grover), calibration set
`8e4abb91-9b6d-4cbd-b2ef-fbb8af6e3ae3`. Open Quantum job ids are in the
`open_quantum/job_ids.json` files.

The failures reproduced: through Open Quantum the decomposed Toffoli returned
`110` (0.92), PennyLane's adder returned `00` on its three non-zero inputs, the
decomposed CDKM adder gave `11` for `1+0`, the QFT adder stayed near chance, and
Cirq's two-qubit Grover circuits returned `00` (0.98, 0.99). The direct route was
correct for every adder, the Toffoli and every two-qubit Grover circuit.
`../defect_evidence_tables.md` compares both dates circuit by circuit.
