# Quantum Inspire Tuna-17: Rx sign probe

Evidence that Tuna-17 executes `Rx` as though its angle were negated, reported
as [QuTech-Delft/qiskit-quantuminspire#395](https://github.com/QuTech-Delft/qiskit-quantuminspire/issues/395).

## Probe (batch job 843300, 20 September 2026)

Five one-qubit circuits in one job, 1024 shots each, written in the device's
own gates and transpiled at `optimization_level=0`, so each `rx` reaches the
device as `rx`. Per-circuit job ids are in `probe-843300.json`.

| Circuit | Gates | Standard Rx gives | Measured |
|---|---|---|---|
| `rx_sign_probe` | rx(π/2), rz(π/2), h | 0 | 1 in 957/1024 (0.935) |
| `rx_sign_probe_neg` | rx(−π/2), rz(π/2), h | 1 | 0 in 922/1024 (0.900) |
| `ry_sign_control` | ry(π/2), h | 0 | 0 (0.902) |
| `rx_pi_control` | rx(π) | 1 | 1 (0.952) |
| `idle_control` | measure only | 0 | 0 (0.928) |

The two probes are mirror images, and both returned the answer that a negated
angle gives. `Ry` in the same job is correct.

## Related data

- The first Grover batch on Tuna-17 (jobs 835019–835022, 12 September), in which
  only Cirq's circuits contained `rx` and only they failed, is in
  `../grover_hw_matrix_superseded/quantum-inspire-prefix-rx-defect/`.
- `advertised_basis_gates.json`: the gates the Qiskit adapter advertised for
  Tuna-17 next to the device's own gate set (issue
  [#394](https://github.com/QuTech-Delft/qiskit-quantuminspire/issues/394)).
- `circuits/`: the five probe circuits as submitted.

## Follow-up on 25 September 2026

- `probe-848824.json`: the five circuits of job 843300 again. `rx_sign_probe` → 1
  at 0.981 and `rx_sign_probe_neg` → 0 at 0.990 (the negated-angle answers);
  the three controls were correct (0.985–0.994).
- `followup_2026-09-25/compile-check-20260925/`: Quantum Inspire's compiler
  (`qi files compile`, decomposition and routing stages) returns the adapter's
  cQASM unchanged, with `Rx(±1.5707963)` intact. No job was run.
- `followup_2026-09-25/native-1474757.json`: four one-qubit cQASM programs sent
  with the Quantum Inspire API, so no Qiskit translation is involved. `Rx(π/2)`,
  `Rx(−π/2)`, and the native `X90` and `mX90`, each followed by `Rz(π/2)`, `H`.
  All four returned the mirrored answer (0.980–0.991). The native X90 is mirrored
  exactly like Rx, so the reversal is not in how Rx is mapped to native gates.
- `followup_2026-09-25/cirq-diagnostic-848830.json`, `-848831.json`: Cirq's
  two-qubit Grover circuits as in the first run (containing `rx`) returned the
  complement of the marked state (`01` at 0.939, `00` at 0.943); the same circuits
  with every `rx` angle negated returned the marked state (0.962, 0.923); Qiskit's
  circuits were correct (0.932, 0.933). The circuits are the builders' QASM from
  the first-run manifests, re-transpiled with the recorded basis, optimisation
  level and seed, because the exact circuits of 12 September were not saved.

Interpretation limit: negating every X rotation and negating every Z rotation
give identical measurement probabilities for any circuit (the two are complex
conjugates, and Ry, H and CZ are real), so no computational-basis measurement
can tell them apart. What the data establish is that Tuna-17 applies X and Z
rotations with the opposite relative sign to the standard convention; describing
it as negated `Rx` angles is equivalent.

## Recovered diagnostic batches (12–13 September 2026)

The diagnostic runs made between the first Grover batch and the corrected rerun
were not archived at the time. On 26 September their programs (cQASM as sent)
and counts were read back from the Quantum Inspire API into
`recovered_2026-09-12-13/batch-<id>.json` (batches 835027, 835548, 835555,
835559, 835568). Counts are stored exactly as the API returns them.

Batch 835568 (13 September, 02:03 UTC) is the measurement behind the paper's
"negating the Rx angles brought the marked state back with probability 0.93":

| Job | Circuit | Most frequent outcome |
|---|---|---|
| 1442927 | Cirq, marked `11`, as sent (contains `Rx(1.5707963)` on both qubits) | `00` at 0.944 |
| 1442931 | the same program with both `Rx` angles negated (only difference) | `11` at 0.930 |
| 1442928 | Qiskit, marked `11` (no `Rx`) | `11` at 0.885 |
| 1442930 | `Rx(π/2)`, `Rz(π/2)`, `Ry(π/2)`: standard answer 1 | `0` at 0.986 |
| 1442929 | `Ry(π/2)`, `Rz(π/2)`, `Rx(π/2)`: standard answer 0 | `1` at 0.932 |

`11` and `00` are palindromes, so the bit order of the returned keys does not
affect this reading.

Each recovered program is also stored as a readable cQASM file in
`recovered_2026-09-12-13/circuits/batch-<id>/job-<id>.cq` (written by
`experiments/defect_evidence_tables.py` from the JSON, which stays the source).

Table 12 of `../defect_evidence_tables.md` (`../defect_evidence_qi_recovered_batches.csv`)
simulates every recovered program twice, as written and with every `Rx` angle
negated. All 22 jobs returned the outcome predicted with `Rx` negated: the 11
jobs whose outcome depends on the sign of `Rx` returned the mirrored answer,
and the 11 whose outcome does not depend on it (no `Rx`, or `Rx` angles adding
up to a multiple of π) matched the ideal. The API returns keys with `b[0]` as
the rightmost character, as the `X q[1]` readout baseline (job 1441798) shows.
