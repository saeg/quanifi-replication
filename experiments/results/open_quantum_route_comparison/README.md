# Open Quantum route comparison

Full paired results behind Table II of the paper, which prints only the Open
Quantum column. Both routes reached the same device, IQM's `garnet`, and
received identical circuit files at the same time, with 512 shots per circuit.

## Method

Four inputs, `0+0` through `1+1`, with three adders: Qiskit's CDKM ripple-carry,
Cirq's QFT, and PennyLane's out-of-place. Each adder was compiled once to the
gates {H, X, CX, Rz}, then the X gates preparing each input were appended, so
all four inputs share one compiled adder body and no input-specific compiler
optimisation can change the comparison.

The CDKM adder was additionally submitted with its two Toffoli gates left
**intact** (undecomposed), to test whether the failure depends on the circuit's
representation. A single Toffoli gate was submitted both as `ccx` and as its
decomposition.

Two routes:

- **Direct** — IQM Resonance
- **Open Quantum** — the multi-vendor cloud service in front of the same device

## Results

Probabilities are of the correct answer unless a wrong bit string is named, in
which case the probability is that wrong answer's.

| Circuit | Case | Direct | Open Quantum |
|---|---|---|---|
| Toffoli | decomposed | 0.74 | wrong, `110` (0.97) |
| Toffoli | intact | 0.78 | 0.83 |
| CDKM adder | decomposed, `0+0` | 0.86 | 0.93 |
| CDKM adder | decomposed, `0+1` and `1+1` | 0.74–0.83 | 0.88–0.93 |
| CDKM adder | decomposed, `1+0` | 0.80 | wrong, `11` (0.86) |
| CDKM adder | intact, all four | 0.73–0.86 | 0.70–0.92 |
| PennyLane adder | decomposed, `0+0` | 0.88 | 1.00 |
| PennyLane adder | decomposed, other three | 0.82–0.86 | wrong, `00` (0.99–1.00) |
| QFT adder | decomposed, all four | 0.78–0.80 | 0.22–0.31 (chance is 0.25) |

Every circuit returned the correct answer as its most frequent outcome through
the direct route. The two CDKM versions differed by at most 0.02 there. No job
on either route reported an error or a warning.

Several wrong answers through Open Quantum carried higher probabilities than the
correct answers returned through the direct route, so the failures cannot be
read as added noise.

## The Rz-removal rule

Removing every Rz gate from the compiled body, keeping the X gates that prepare
the inputs, and simulating the remainder reproduces **every row** of the Open
Quantum column above, including the inputs for which the decomposed CDKM adder
still succeeds.

Matching these outcomes does not establish what the service changed. Open
Quantum does not return the executed circuit, and the Grover route comparison
includes a case this rule predicts incorrectly: it predicts failures for
PennyLane's two-qubit circuits that were not observed.

## Disclosure

Reported to the provider with the circuits and job identifiers. The provider
confirmed the defect, traced it to a QASM parsing function, and fixed it on
17 September 2026. Every result here was collected before that fix.

## Data availability

The per-job records for this experiment are **not archived in this repository**,
unlike the Grover hardware matrix under `../grover_hw_matrix/`. The figures above
are the reported summary. Raw counts per circuit and per input, and the job
identifiers on both routes, are held with the submission records.
