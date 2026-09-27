# OpenQuantum adder rz-drop defect: offline visibility checks

All numbers below come from local, noiseless, exact-probability simulation. No hardware job was ever submitted or armed to produce this section.

**Whole-form note:** only `qiskit/cdkm` has a genuine "whole" (Toffoli-preserving) control, rebuilt directly from `CDKMRippleCarryAdder` (the same construction the builder itself uses) and transpiled only down to `{ccx, cx, x, h}` -- see `cdkm_whole_adder_verification.json` / the verification table below. `cirq/qft` and `pennylane/outadder` have **no** whole form: not applicable -- no whole-circuit control exists for this builder: its raw circuit output contains no multi-controlled gate to preserve, so there is nothing distinct from the decomposed, defect-exposed form

## Controlled reproduction (fourth-pass correction) -- AUTHORITATIVE

Physically separates operand preparation from the compiled adder body (see the module docstring's "Fourth-pass correction" section) so no transpile or rz-stripping call can ever touch the operand encoding. This section supersedes the `qiskit/cdkm` `visible`/`detectable`/`stripped` fields in the historical per-builder tables further down -- those were computed on a circuit where the leading operand `x` gates can be folded into the adder body's own Euler synthesis by the basis transpile. `cirq/qft` and `pennylane/outadder` are verified below to be UNAFFECTED by that confound (their numbers are unchanged).

### Step 1: operand-invariance check

For each builder/bit_width, the raw builder-output circuit body -- with the operand-preparation gate(s) removed on a PER-WIRE basis (the first single-qubit instruction touching each expected prep qubit, wherever it falls in instruction order) -- is compared across all four operand pairs. A per-wire (not positional) removal is required because `qiskit/cdkm`'s `circuit.decompose(reps=4)` serialisation can reorder independent-wire instructions.

| builder | bit_width | invariant? | detail |
|---|---|---|---|
| `cirq/qft` | 1 | yes | body has 41 instruction(s) once preparation gates are removed, identical across all 4 operand pairs |
| `pennylane/outadder` | 1 | yes | body has 36 instruction(s) once preparation gates are removed, identical across all 4 operand pairs |
| `qiskit/cdkm` | 1 | yes | body has 35 instruction(s) once preparation gates are removed, identical across all 4 operand pairs |
| `qiskit/cdkm` | 2 | yes | body has 69 instruction(s) once preparation gates are removed, identical across all 16 operand pairs |

### Step 4: verification (ideal, unstripped simulation)

Every (builder, bit_width, form, operand pair) combination below simulates to the correct answer with P >= 0.999 BEFORE any rz-stripping is applied -- the correctness gate for trusting the hypothesis-model predictions that follow.

| builder | bit_width | a | b | form | ideal P_correct |
|---|---|---|---|---|---|
| `cirq/qft` | 1 | 0 | 0 | decomposed | 1.0000 |
| `cirq/qft` | 1 | 0 | 1 | decomposed | 1.0000 |
| `cirq/qft` | 1 | 1 | 0 | decomposed | 1.0000 |
| `cirq/qft` | 1 | 1 | 1 | decomposed | 1.0000 |
| `pennylane/outadder` | 1 | 0 | 0 | decomposed | 1.0000 |
| `pennylane/outadder` | 1 | 0 | 1 | decomposed | 1.0000 |
| `pennylane/outadder` | 1 | 1 | 0 | decomposed | 1.0000 |
| `pennylane/outadder` | 1 | 1 | 1 | decomposed | 1.0000 |
| `qiskit/cdkm` | 1 | 0 | 0 | decomposed | 1.0000 |
| `qiskit/cdkm` | 1 | 0 | 0 | whole | 1.0000 |
| `qiskit/cdkm` | 1 | 0 | 1 | decomposed | 1.0000 |
| `qiskit/cdkm` | 1 | 0 | 1 | whole | 1.0000 |
| `qiskit/cdkm` | 1 | 1 | 0 | decomposed | 1.0000 |
| `qiskit/cdkm` | 1 | 1 | 0 | whole | 1.0000 |
| `qiskit/cdkm` | 1 | 1 | 1 | decomposed | 1.0000 |
| `qiskit/cdkm` | 1 | 1 | 1 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 0 | 0 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 0 | 0 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 0 | 1 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 0 | 1 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 0 | 2 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 0 | 2 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 0 | 3 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 0 | 3 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 1 | 0 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 1 | 0 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 1 | 1 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 1 | 1 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 1 | 2 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 1 | 2 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 1 | 3 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 1 | 3 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 2 | 0 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 2 | 0 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 2 | 1 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 2 | 1 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 2 | 2 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 2 | 2 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 2 | 3 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 2 | 3 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 3 | 0 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 3 | 0 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 3 | 1 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 3 | 1 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 3 | 2 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 3 | 2 | whole | 1.0000 |
| `qiskit/cdkm` | 2 | 3 | 3 | decomposed | 1.0000 |
| `qiskit/cdkm` | 2 | 3 | 3 | whole | 1.0000 |

### Step 5: rz-strip HYPOTHESIS MODEL predictions

Explicitly a hypothesis consistent with the Toffoli hardware reproducer, **not** a confirmed universal mechanism. For each row, the prepended operand `x` gate count is asserted unchanged before/after stripping (`assert_prep_x_count_unchanged`).

| builder | bit_width | a | b | form | correct | predicted top | p(top) | P_correct | visible? | detectable? |
|---|---|---|---|---|---|---|---|---|---|---|
| `cirq/qft` | 1 | 0 | 0 | decomposed | 00 | 11 | 0.2500 | 0.2500 | no | yes |
| `cirq/qft` | 1 | 0 | 1 | decomposed | 10 | 10 | 0.2500 | 0.2500 | no | yes |
| `cirq/qft` | 1 | 1 | 0 | decomposed | 10 | 00 | 0.2500 | 0.2500 | no | yes |
| `cirq/qft` | 1 | 1 | 1 | decomposed | 01 | 10 | 0.2500 | 0.2500 | no | yes |
| `pennylane/outadder` | 1 | 0 | 0 | decomposed | 00 | 00 | 1.0000 | 1.0000 | no | no |
| `pennylane/outadder` | 1 | 0 | 1 | decomposed | 10 | 00 | 1.0000 | 0.0000 | yes | yes |
| `pennylane/outadder` | 1 | 1 | 0 | decomposed | 10 | 00 | 1.0000 | 0.0000 | yes | yes |
| `pennylane/outadder` | 1 | 1 | 1 | decomposed | 01 | 00 | 1.0000 | 0.0000 | yes | yes |
| `qiskit/cdkm` | 1 | 0 | 0 | decomposed | 00 | 00 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 1 | 0 | 0 | whole | 00 | 00 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 1 | 0 | 1 | decomposed | 10 | 10 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 1 | 0 | 1 | whole | 10 | 10 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 1 | 1 | 0 | decomposed | 10 | 11 | 1.0000 | 0.0000 | yes | yes |
| `qiskit/cdkm` | 1 | 1 | 0 | whole | 10 | 10 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 1 | 1 | 1 | decomposed | 01 | 01 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 1 | 1 | 1 | whole | 01 | 01 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 0 | 0 | decomposed | 000 | 000 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 0 | 0 | whole | 000 | 000 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 0 | 1 | decomposed | 100 | 100 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 0 | 1 | whole | 100 | 100 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 0 | 2 | decomposed | 010 | 010 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 0 | 2 | whole | 010 | 010 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 0 | 3 | decomposed | 110 | 110 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 0 | 3 | whole | 110 | 110 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 1 | 0 | decomposed | 100 | 110 | 1.0000 | 0.0000 | yes | yes |
| `qiskit/cdkm` | 2 | 1 | 0 | whole | 100 | 100 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 1 | 1 | decomposed | 010 | 010 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 1 | 1 | whole | 010 | 010 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 1 | 2 | decomposed | 110 | 100 | 1.0000 | 0.0000 | yes | yes |
| `qiskit/cdkm` | 2 | 1 | 2 | whole | 110 | 110 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 1 | 3 | decomposed | 001 | 000 | 1.0000 | 0.0000 | yes | yes |
| `qiskit/cdkm` | 2 | 1 | 3 | whole | 001 | 001 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 2 | 0 | decomposed | 010 | 011 | 1.0000 | 0.0000 | yes | yes |
| `qiskit/cdkm` | 2 | 2 | 0 | whole | 010 | 010 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 2 | 1 | decomposed | 110 | 111 | 1.0000 | 0.0000 | yes | yes |
| `qiskit/cdkm` | 2 | 2 | 1 | whole | 110 | 110 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 2 | 2 | decomposed | 001 | 001 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 2 | 2 | whole | 001 | 001 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 2 | 3 | decomposed | 101 | 101 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 2 | 3 | whole | 101 | 101 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 3 | 0 | decomposed | 110 | 101 | 1.0000 | 0.0000 | yes | yes |
| `qiskit/cdkm` | 2 | 3 | 0 | whole | 110 | 110 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 3 | 1 | decomposed | 001 | 001 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 3 | 1 | whole | 001 | 001 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 3 | 2 | decomposed | 101 | 111 | 1.0000 | 0.0000 | yes | yes |
| `qiskit/cdkm` | 2 | 3 | 2 | whole | 101 | 101 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 3 | 3 | decomposed | 011 | 011 | 1.0000 | 1.0000 | no | no |
| `qiskit/cdkm` | 2 | 3 | 3 | whole | 011 | 011 | 1.0000 | 1.0000 | no | no |

### Confound impact: what changed once operand preparation was protected

- `qiskit/cdkm` bit_width=1 a=0 b=1: OLD (confounded) circuit predicted **00** (wrong); the CONTROLLED circuit now predicts **10** (correct, P_correct=1.0000) -- the defect is NOT VISIBLE here once the confound is removed.
- `qiskit/cdkm` bit_width=1 a=1 b=0: still visible under the controlled circuit, but the predicted WRONG value changed from **00** (old, confounded) to **11** (new, controlled).
- `qiskit/cdkm` bit_width=1 a=1 b=1: OLD (confounded) circuit predicted **00** (wrong); the CONTROLLED circuit now predicts **01** (correct, P_correct=1.0000) -- the defect is NOT VISIBLE here once the confound is removed.
- `qiskit/cdkm` bit_width=2 a=0 b=1: OLD (confounded) circuit predicted **000** (wrong); the CONTROLLED circuit now predicts **100** (correct, P_correct=1.0000) -- the defect is NOT VISIBLE here once the confound is removed.
- `qiskit/cdkm` bit_width=2 a=0 b=2: OLD (confounded) circuit predicted **000** (wrong); the CONTROLLED circuit now predicts **010** (correct, P_correct=1.0000) -- the defect is NOT VISIBLE here once the confound is removed.
- `qiskit/cdkm` bit_width=2 a=0 b=3: OLD (confounded) circuit predicted **000** (wrong); the CONTROLLED circuit now predicts **110** (correct, P_correct=1.0000) -- the defect is NOT VISIBLE here once the confound is removed.
- `qiskit/cdkm` bit_width=2 a=1 b=0: still visible under the controlled circuit, but the predicted WRONG value changed from **000** (old, confounded) to **110** (new, controlled).
- `qiskit/cdkm` bit_width=2 a=1 b=1: OLD (confounded) circuit predicted **000** (wrong); the CONTROLLED circuit now predicts **010** (correct, P_correct=1.0000) -- the defect is NOT VISIBLE here once the confound is removed.
- `qiskit/cdkm` bit_width=2 a=1 b=2: still visible under the controlled circuit, but the predicted WRONG value changed from **000** (old, confounded) to **100** (new, controlled).
- `qiskit/cdkm` bit_width=2 a=2 b=0: still visible under the controlled circuit, but the predicted WRONG value changed from **000** (old, confounded) to **011** (new, controlled).
- `qiskit/cdkm` bit_width=2 a=2 b=1: still visible under the controlled circuit, but the predicted WRONG value changed from **000** (old, confounded) to **111** (new, controlled).
- `qiskit/cdkm` bit_width=2 a=2 b=2: OLD (confounded) circuit predicted **000** (wrong); the CONTROLLED circuit now predicts **001** (correct, P_correct=1.0000) -- the defect is NOT VISIBLE here once the confound is removed.
- `qiskit/cdkm` bit_width=2 a=2 b=3: OLD (confounded) circuit predicted **000** (wrong); the CONTROLLED circuit now predicts **101** (correct, P_correct=1.0000) -- the defect is NOT VISIBLE here once the confound is removed.
- `qiskit/cdkm` bit_width=2 a=3 b=0: still visible under the controlled circuit, but the predicted WRONG value changed from **000** (old, confounded) to **101** (new, controlled).
- `qiskit/cdkm` bit_width=2 a=3 b=1: OLD (confounded) circuit predicted **000** (wrong); the CONTROLLED circuit now predicts **001** (correct, P_correct=1.0000) -- the defect is NOT VISIBLE here once the confound is removed.
- `qiskit/cdkm` bit_width=2 a=3 b=2: still visible under the controlled circuit, but the predicted WRONG value changed from **000** (old, confounded) to **111** (new, controlled).
- `qiskit/cdkm` bit_width=2 a=3 b=3: OLD (confounded) circuit predicted **000** (wrong); the CONTROLLED circuit now predicts **011** (correct, P_correct=1.0000) -- the defect is NOT VISIBLE here once the confound is removed.

## Historical: original per-builder offline sweep

Kept as originally computed (several existing tests pin these exact values). **For `qiskit/cdkm`, treat `visible`/`detectable`/`stripped` below as SUPERSEDED** by the controlled reproduction above -- see the module docstring. Verified UNCHANGED (not confounded) for `cirq/qft` and `pennylane/outadder`.

## `qiskit/cdkm` genuine whole-adder verification (bit_width=1)

Built from `CDKMRippleCarryAdder(1, kind="half")` -- the same construction `QiskitQuantumArithmetic._build_adder("cdkm", 1)` gives the builder -- transpiled only down to `{ccx, cx, x, h}`. Register layout (`result_qubits`) is read back from the reconstruction and asserted equal to the builder's own, then the resulting circuit is ideally simulated and marginalized onto that layout for every operand pair.

| a | b | result_qubits | correct | P_correct | ccx | cx |
|---|---|---|---|---|---|---|
| 0 | 0 | [1, 2] | 00 | 1.0000 | 2 | 5 |
| 0 | 1 | [1, 2] | 10 | 1.0000 | 2 | 5 |
| 1 | 0 | [1, 2] | 10 | 1.0000 | 2 | 5 |
| 1 | 1 | [1, 2] | 01 | 1.0000 | 2 | 5 |

## `qiskit/cdkm`

### bit_width = 1 (4 qubits)

| a | b | correct | ideal P_correct | stripped top | stripped P(top) | stripped P_correct | visible? | detectable? |
|---|---|---|---|---|---|---|---|---|
| 0 | 0 | 00 | 1.0000 | 00 | 1.0000 | 1.0000 | no | no |
| 0 | 1 | 10 | 1.0000 | 00 | 1.0000 | 0.0000 | yes | yes |
| 1 | 0 | 10 | 1.0000 | 00 | 1.0000 | 0.0000 | yes | yes |
| 1 | 1 | 01 | 1.0000 | 00 | 1.0000 | 0.0000 | yes | yes |

### bit_width = 2 (6 qubits)

| a | b | correct | ideal P_correct | stripped top | stripped P(top) | stripped P_correct | visible? | detectable? |
|---|---|---|---|---|---|---|---|---|
| 0 | 0 | 000 | 1.0000 | 000 | 1.0000 | 1.0000 | no | no |
| 0 | 1 | 100 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 0 | 2 | 010 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 0 | 3 | 110 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 1 | 0 | 100 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 1 | 1 | 010 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 1 | 2 | 110 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 1 | 3 | 001 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 2 | 0 | 010 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 2 | 1 | 110 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 2 | 2 | 001 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 2 | 3 | 101 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 3 | 0 | 110 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 3 | 1 | 001 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 3 | 2 | 101 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 3 | 3 | 011 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |

**Visible pairs for `qiskit/cdkm`:**

- bit_width=1, a=0, b=1: correct=10, stripped model predicts **00** with p=1.0000
- bit_width=1, a=1, b=0: correct=10, stripped model predicts **00** with p=1.0000
- bit_width=1, a=1, b=1: correct=01, stripped model predicts **00** with p=1.0000
- bit_width=2, a=0, b=1: correct=100, stripped model predicts **000** with p=1.0000
- bit_width=2, a=0, b=2: correct=010, stripped model predicts **000** with p=1.0000
- bit_width=2, a=0, b=3: correct=110, stripped model predicts **000** with p=1.0000
- bit_width=2, a=1, b=0: correct=100, stripped model predicts **000** with p=1.0000
- bit_width=2, a=1, b=1: correct=010, stripped model predicts **000** with p=1.0000
- bit_width=2, a=1, b=2: correct=110, stripped model predicts **000** with p=1.0000
- bit_width=2, a=1, b=3: correct=001, stripped model predicts **000** with p=1.0000
- bit_width=2, a=2, b=0: correct=010, stripped model predicts **000** with p=1.0000
- bit_width=2, a=2, b=1: correct=110, stripped model predicts **000** with p=1.0000
- bit_width=2, a=2, b=2: correct=001, stripped model predicts **000** with p=1.0000
- bit_width=2, a=2, b=3: correct=101, stripped model predicts **000** with p=1.0000
- bit_width=2, a=3, b=0: correct=110, stripped model predicts **000** with p=1.0000
- bit_width=2, a=3, b=1: correct=001, stripped model predicts **000** with p=1.0000
- bit_width=2, a=3, b=2: correct=101, stripped model predicts **000** with p=1.0000
- bit_width=2, a=3, b=3: correct=011, stripped model predicts **000** with p=1.0000

## `cirq/qft`

### bit_width = 1 (3 qubits)

| a | b | correct | ideal P_correct | stripped top | stripped P(top) | stripped P_correct | visible? | detectable? |
|---|---|---|---|---|---|---|---|---|
| 0 | 0 | 00 | 1.0000 | 11 | 0.2500 | 0.2500 | no | yes |
| 0 | 1 | 10 | 1.0000 | 10 | 0.2500 | 0.2500 | no | yes |
| 1 | 0 | 10 | 1.0000 | 00 | 0.2500 | 0.2500 | no | yes |
| 1 | 1 | 01 | 1.0000 | 10 | 0.2500 | 0.2500 | no | yes |

### bit_width = 2 (5 qubits)

| a | b | correct | ideal P_correct | stripped top | stripped P(top) | stripped P_correct | visible? | detectable? |
|---|---|---|---|---|---|---|---|---|
| 0 | 0 | 000 | 1.0000 | 100 | 0.1250 | 0.1250 | no | yes |
| 0 | 1 | 100 | 1.0000 | 010 | 0.1250 | 0.1250 | no | yes |
| 0 | 2 | 010 | 1.0000 | 010 | 0.1250 | 0.1250 | no | yes |
| 0 | 3 | 110 | 1.0000 | 000 | 0.1250 | 0.1250 | no | yes |
| 1 | 0 | 100 | 1.0000 | 000 | 0.1250 | 0.1250 | no | yes |
| 1 | 1 | 010 | 1.0000 | 000 | 0.1250 | 0.1250 | no | yes |
| 1 | 2 | 110 | 1.0000 | 111 | 0.1250 | 0.1250 | no | yes |
| 1 | 3 | 001 | 1.0000 | 101 | 0.1250 | 0.1250 | no | yes |
| 2 | 0 | 010 | 1.0000 | 011 | 0.1250 | 0.1250 | no | yes |
| 2 | 1 | 110 | 1.0000 | 001 | 0.1250 | 0.1250 | no | yes |
| 2 | 2 | 001 | 1.0000 | 000 | 0.1250 | 0.1250 | no | yes |
| 2 | 3 | 101 | 1.0000 | 111 | 0.1250 | 0.1250 | no | yes |
| 3 | 0 | 110 | 1.0000 | 111 | 0.1250 | 0.1250 | no | yes |
| 3 | 1 | 001 | 1.0000 | 011 | 0.1250 | 0.1250 | no | yes |
| 3 | 2 | 101 | 1.0000 | 100 | 0.1250 | 0.1250 | no | yes |
| 3 | 3 | 011 | 1.0000 | 001 | 0.1250 | 0.1250 | no | yes |

**Visible pairs for `cirq/qft`:**

- none

## `pennylane/outadder`

### bit_width = 1 (6 qubits)

| a | b | correct | ideal P_correct | stripped top | stripped P(top) | stripped P_correct | visible? | detectable? |
|---|---|---|---|---|---|---|---|---|
| 0 | 0 | 00 | 1.0000 | 00 | 1.0000 | 1.0000 | no | no |
| 0 | 1 | 10 | 1.0000 | 00 | 1.0000 | 0.0000 | yes | yes |
| 1 | 0 | 10 | 1.0000 | 00 | 1.0000 | 0.0000 | yes | yes |
| 1 | 1 | 01 | 1.0000 | 00 | 1.0000 | 0.0000 | yes | yes |

### bit_width = 2 (9 qubits)

| a | b | correct | ideal P_correct | stripped top | stripped P(top) | stripped P_correct | visible? | detectable? |
|---|---|---|---|---|---|---|---|---|
| 0 | 0 | 000 | 1.0000 | 000 | 1.0000 | 1.0000 | no | no |
| 0 | 1 | 100 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 0 | 2 | 010 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 0 | 3 | 110 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 1 | 0 | 100 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 1 | 1 | 010 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 1 | 2 | 110 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 1 | 3 | 001 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 2 | 0 | 010 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 2 | 1 | 110 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 2 | 2 | 001 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 2 | 3 | 101 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 3 | 0 | 110 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 3 | 1 | 001 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 3 | 2 | 101 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |
| 3 | 3 | 011 | 1.0000 | 000 | 1.0000 | 0.0000 | yes | yes |

**Visible pairs for `pennylane/outadder`:**

- bit_width=1, a=0, b=1: correct=10, stripped model predicts **00** with p=1.0000
- bit_width=1, a=1, b=0: correct=10, stripped model predicts **00** with p=1.0000
- bit_width=1, a=1, b=1: correct=01, stripped model predicts **00** with p=1.0000
- bit_width=2, a=0, b=1: correct=100, stripped model predicts **000** with p=1.0000
- bit_width=2, a=0, b=2: correct=010, stripped model predicts **000** with p=1.0000
- bit_width=2, a=0, b=3: correct=110, stripped model predicts **000** with p=1.0000
- bit_width=2, a=1, b=0: correct=100, stripped model predicts **000** with p=1.0000
- bit_width=2, a=1, b=1: correct=010, stripped model predicts **000** with p=1.0000
- bit_width=2, a=1, b=2: correct=110, stripped model predicts **000** with p=1.0000
- bit_width=2, a=1, b=3: correct=001, stripped model predicts **000** with p=1.0000
- bit_width=2, a=2, b=0: correct=010, stripped model predicts **000** with p=1.0000
- bit_width=2, a=2, b=1: correct=110, stripped model predicts **000** with p=1.0000
- bit_width=2, a=2, b=2: correct=001, stripped model predicts **000** with p=1.0000
- bit_width=2, a=2, b=3: correct=101, stripped model predicts **000** with p=1.0000
- bit_width=2, a=3, b=0: correct=110, stripped model predicts **000** with p=1.0000
- bit_width=2, a=3, b=1: correct=001, stripped model predicts **000** with p=1.0000
- bit_width=2, a=3, b=2: correct=101, stripped model predicts **000** with p=1.0000
- bit_width=2, a=3, b=3: correct=011, stripped model predicts **000** with p=1.0000

## Preflight (`iqm:garnet`, free validation + price quote only)

**Fourth-pass correction:** this selection is now built from the CONTROLLED (`circuits/controlled/`), not the old fixed 14-tuple: for each builder, operand pair (0,0) as a sanity anchor plus every other pair the corrected rz-strip hypothesis model finds `detectable` (see the "Controlled reproduction" section above), restricted to bit_width=1. `qiskit/cdkm` includes both `decomposed` and `whole` for every pair it selects; `pennylane/outadder` and `cirq/qft` decomposed-only; plus the `toffoli-raw`/`toffoli-decomposed` reproducer control pair, copied byte-for-byte from `~/Desktop/openquantum-bug-report/circuits`, unchanged. The prior (possibly-confounded) 14-circuit selection is preserved under `circuits/exported-path/` and is no longer part of the default/recommended submission set (see the recommendation at the end of this section).

| name | builder | bit_width | a | b | form | status | price |
|---|---|---|---|---|---|---|---|
| qiskit_cdkm-bw1-a0-b0-decomposed | qiskit/cdkm | 1 | 0 | 0 | decomposed | pass | 1 |
| qiskit_cdkm-bw1-a0-b0-whole | qiskit/cdkm | 1 | 0 | 0 | whole | pass | 1 |
| qiskit_cdkm-bw1-a0-b1-decomposed | qiskit/cdkm | 1 | 0 | 1 | decomposed | pass | 1 |
| qiskit_cdkm-bw1-a0-b1-whole | qiskit/cdkm | 1 | 0 | 1 | whole | pass | 1 |
| qiskit_cdkm-bw1-a1-b0-decomposed | qiskit/cdkm | 1 | 1 | 0 | decomposed | pass | 1 |
| qiskit_cdkm-bw1-a1-b0-whole | qiskit/cdkm | 1 | 1 | 0 | whole | pass | 1 |
| qiskit_cdkm-bw1-a1-b1-decomposed | qiskit/cdkm | 1 | 1 | 1 | decomposed | pass | 1 |
| qiskit_cdkm-bw1-a1-b1-whole | qiskit/cdkm | 1 | 1 | 1 | whole | pass | 1 |
| pennylane_outadder-bw1-a0-b0-decomposed | pennylane/outadder | 1 | 0 | 0 | decomposed | pass | 1 |
| pennylane_outadder-bw1-a0-b1-decomposed | pennylane/outadder | 1 | 0 | 1 | decomposed | pass | 1 |
| pennylane_outadder-bw1-a1-b0-decomposed | pennylane/outadder | 1 | 1 | 0 | decomposed | pass | 1 |
| pennylane_outadder-bw1-a1-b1-decomposed | pennylane/outadder | 1 | 1 | 1 | decomposed | pass | 1 |
| cirq_qft-bw1-a0-b0-decomposed | cirq/qft | 1 | 0 | 0 | decomposed | pass | 1 |
| cirq_qft-bw1-a0-b1-decomposed | cirq/qft | 1 | 0 | 1 | decomposed | pass | 1 |
| cirq_qft-bw1-a1-b0-decomposed | cirq/qft | 1 | 1 | 0 | decomposed | pass | 1 |
| cirq_qft-bw1-a1-b1-decomposed | cirq/qft | 1 | 1 | 1 | decomposed | pass | 1 |
| toffoli-raw | toffoli-reproducer | - | - | - | raw | pass | 1 |
| toffoli-decomposed | toffoli-reproducer | - | - | - | decomposed | pass | 1 |

**Total price for the selected set (18 circuits): 18.0**

Shot-count probe: First live preflight (qiskit_cdkm-bw1-a0-b0-decomposed, shots=512): status=pass. No indication the platform's accepted/priced shot count differs from 512 for this backend; used unchanged for the whole selection.

### Notes

- **qiskit_cdkm-bw1-a0-b0-decomposed**: sanity anchor (all-zero operands) [decomposed form]
- **qiskit_cdkm-bw1-a0-b0-whole**: sanity anchor (all-zero operands) [whole form]
- **qiskit_cdkm-bw1-a0-b1-decomposed**: selected: rz-strip hypothesis model detectable=False visible=False (predicts '10' at p=1.0000, P_correct=1.0000)
- **qiskit_cdkm-bw1-a0-b1-whole**: selected: rz-strip hypothesis model detectable=False visible=False (predicts '10' at p=1.0000, P_correct=1.0000)
- **qiskit_cdkm-bw1-a1-b0-decomposed**: selected: rz-strip hypothesis model detectable=True visible=True (predicts '11' at p=1.0000, P_correct=0.0000)
- **qiskit_cdkm-bw1-a1-b0-whole**: selected: rz-strip hypothesis model detectable=True visible=True (predicts '11' at p=1.0000, P_correct=0.0000)
- **qiskit_cdkm-bw1-a1-b1-decomposed**: selected: rz-strip hypothesis model detectable=False visible=False (predicts '01' at p=1.0000, P_correct=1.0000)
- **qiskit_cdkm-bw1-a1-b1-whole**: selected: rz-strip hypothesis model detectable=False visible=False (predicts '01' at p=1.0000, P_correct=1.0000)
- **pennylane_outadder-bw1-a0-b0-decomposed**: sanity anchor (all-zero operands)
- **pennylane_outadder-bw1-a0-b1-decomposed**: selected: rz-strip hypothesis model detectable=True visible=True (predicts '00' at p=1.0000, P_correct=0.0000)
- **pennylane_outadder-bw1-a1-b0-decomposed**: selected: rz-strip hypothesis model detectable=True visible=True (predicts '00' at p=1.0000, P_correct=0.0000)
- **pennylane_outadder-bw1-a1-b1-decomposed**: selected: rz-strip hypothesis model detectable=True visible=True (predicts '00' at p=1.0000, P_correct=0.0000)
- **cirq_qft-bw1-a0-b0-decomposed**: sanity anchor (all-zero operands)
- **cirq_qft-bw1-a0-b1-decomposed**: selected: rz-strip hypothesis model detectable=True visible=False (predicts '10' at p=0.2500, P_correct=0.2500)
- **cirq_qft-bw1-a1-b0-decomposed**: selected: rz-strip hypothesis model detectable=True visible=False (predicts '00' at p=0.2500, P_correct=0.2500)
- **cirq_qft-bw1-a1-b1-decomposed**: selected: rz-strip hypothesis model detectable=True visible=False (predicts '10' at p=0.2500, P_correct=0.2500)
- **toffoli-raw**: shared control pair, copied byte-for-byte from experiments/results/open_quantum_cepheus_discovery/toffoli_probe/circuits/A_toffoli_raw_ccx.qasm (never regenerated/re-transpiled)
- **toffoli-decomposed**: shared control pair, copied byte-for-byte from experiments/results/open_quantum_cepheus_discovery/toffoli_probe/circuits/B_toffoli_decomposed.qasm (never regenerated/re-transpiled)

### `rigetti:cepheus-1-108q` backend-param confirmation

- qiskit/cdkm bit_width=1 a=0 b=0 (decomposed) on `rigetti:cepheus-1-108q`: **pass** (price=1)

### Recommendation: exported-path (confounded) circuits

The `circuits/exported-path/` set (the previous fixed 14-circuit selection, kept for the record -- see `circuits/exported-path/README.md`) should **not** be part of any real submission set. It is confounded for `qiskit/cdkm` (operand preparation can be folded into the adder body by the basis transpile, so a wrong answer there may reflect lost operand encoding rather than the arithmetic defect) and, even where it happens to be unconfounded (`cirq/qft`, `pennylane/outadder`), it is now redundant with the corrected `circuits/controlled/` set. `circuits/controlled/` is the default/recommended submission set.

