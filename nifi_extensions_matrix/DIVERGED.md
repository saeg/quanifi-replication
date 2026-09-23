# Declared divergences from `nifi_extensions/`

Every file in this directory must be byte-identical to its counterpart in
`nifi_extensions/` at the commit recorded in `PROVENANCE.md`, unless it is
listed here. `tests/test_extension_fork_drift.py` enforces that.

Keep this table honest: an undeclared difference fails the test suite, which is
the whole point of the fork having a manifest.

## New files (no counterpart upstream)

| File | Why |
|---|---|
| `QrispGroverCircuit.py` | Build-only Qrisp Grover builder. Upstream has only the all-in-one `QrispGroverSearch`, whose builder and engine factors cannot be separated, so Qrisp could not appear on the builder axis. |
| `PROVENANCE.md`, `DIVERGED.md` | Fork bookkeeping. |
| `dashboard_generator.py` | Master-detail HTML dashboard for the matrix distribution oracle; no upstream counterpart. |

## Modified files

| File | Change | Why |
|---|---|---|
| `QiskitGroverCircuit.py` | Adds `builder.framework = qiskit` and `circuit.bit_order = canonical` to the emitted attributes. | The matrix study groups rows by builder identity, and the four builders must declare it the same way. Purely additive: no existing attribute or behaviour changes. |
| `CirqGroverCircuit.py` | Adds `builder.framework = cirq` and `circuit.bit_order = canonical`. | Same reason. |
| `Generation2CalibrationEvaluator.py` | Upstream propagates sealed campaign identity, device, and provider-job metadata. | The Marrakesh v6 hardware campaign is fail-closed and auditable; the frozen simulator-study fork does not run that campaign. |
| `Generation2EnsembleBuilder.py` | Upstream propagates campaign, provider, layout, usage, and completion metadata. | Required for complete v6 hardware reports, but outside the matrix fork's frozen study scope. |
| `Generation2JobAdapter.py` | Upstream validates and adapts sealed campaign and provider execution metadata. | Required for v6 hardware evidence and not needed by the matrix study. |
| `Generation2PseudoOracle.py` | Upstream carries sealed campaign and provider execution metadata into oracle reports. | Required for v6 hardware evidence and not needed by the matrix study. |
| `Generation2TruthEvaluator.py` | Upstream verifies the sealed truth file, its digest, schema, records, and campaign identity. | The v6 campaign must reject altered or mismatched truth artifacts before unblinding. |
| `QuantumBatchResultExpander.py` | Upstream propagates fixed layout, usage, and submission/completion timestamps. | These fields are required by the v6 hardware audit trail. |
| `QuantumIBMBatchPoller.py` | Upstream records actual usage and authoritative completion timestamps. | The v6 quota ledger and completion-relative windows depend on provider evidence. |
| `QuantumIQMBatchPoller.py` | Upstream keeps IQM's whole job document and derives usage and completion timestamps from it. | The cross-vendor v6 campaign needs the same provider evidence from IQM that the IBM arm records; the frozen simulator study polls no provider. |
| `QuantumIBMBatchSubmitter.py` | Upstream adds the immediate fail-closed IBM quota guard and immutable source/ISA circuit evidence. | Paid v6 hardware submissions require these controls; the matrix fork is simulator-only. |
| `batch_prep.py` | Upstream binds and verifies one serialized ISA artifact per ordered circuit. | The v6 campaign must prove exactly what was submitted to hardware. |
| `BraketSimulator.py` | Upstream adds an optional `Noise Model` / `Error Probability` pair: any value but `none` switches from `braket_sv` to the `braket_dm` density-matrix simulator and injects `#pragma braket noise <channel>(<p>) q[i]` after each translated 1-qubit gate. The fork keeps the ideal `braket_sv`-only version. | The fork is frozen at the engine set that produced the published version-matrix results. Braket is one of its six engines, so giving it a noise mode here would change the distributions behind those comparisons. Upstream's old "Braket is noiseless" claim was simply wrong — `braket_dm` ships in the installed SDK and OpenQASM noise pragmas work — so the correction belongs upstream, not in the frozen artifact. |
| `QuantumDistributionOracle.py` | Report written as `{flow}-data.json` + `dashboard_generator.render_matrix_dashboard` instead of `reporting.write_card`, plus extra CSS for the master-detail layout. Statistics, properties (including Multiple Comparison Correction) and attributes are kept in lockstep with upstream by hand. | The matrix study's 20-branch dashboard needs a master-detail view that the shared single-card reporting helper cannot render; everything upstream of the report write (the consensus math, the property set) stays hand-synced rather than diverging. |
| `QSharpSimulator.py` | Upstream removes the Qiskit hop (`_measured_qasm3` → `_analyze_qasm`): OpenQASM 2/3 is parsed natively via `qdk.openqasm.circuit()` instead of `qiskit.qasm2`/`qasm3`, the Clifford SIGSEGV guard is a pure-Python allowlist walk over the QDK's own circuit-synthesis JSON instead of `qiskit.quantum_info.Clifford`, and `qiskit`/`qiskit-qasm3-import` are dropped from `dependencies`. Full-register measurement normalisation is preserved. The fork keeps the Qiskit-based version. | The QDK accepts OpenQASM 2 natively (verified on qdk 1.30.0, the pinned version), so the hop is no longer needed — and it mattered for independence: it made the Q# engine share Qiskit's QASM front end with the Braket path, a correlated component between two of the six "versions." The fix belongs upstream, but the fork stays frozen because Q# is one of the six engines behind the published version-matrix results. |

## Backport candidates

The PennyLane Grover circuit builder was promoted to `nifi_extensions/`
byte-identical (Grover hardware N-M matrix work) and is no longer listed here
as a new file — it now has an upstream counterpart and the two copies are
identical, which is not a divergence.

Otherwise nothing pending. Note the direction of travel for `BraketSimulator.py` above:
the fix landed in `nifi_extensions/` and the fork stays behind on purpose, which
is the opposite of a backport. If a genuine defect is found in a shared
processor here, fix it upstream in `nifi_extensions/` as an additive patch and
re-sync the file, rather than letting the two copies diverge on a bug fix.
