# Superseded Grover hardware-matrix data

Kept for provenance, deliberately OUTSIDE `experiments/results/grover_hw_matrix/`
so the analysis glob `grover_hw_matrix/**/*.json` cannot pick it up.

## quantum-inspire-prefix-rx-defect/ (batch jobs 835019-835022, 2026-09-12)

First Tuna-17 run, submitted before commit ead2c4d. Transpiled against the
adapter's advertised Target, so Cirq's circuits contained `rx`, which Tuna-17
executes with a negated angle. The Cirq arm returned the bitwise complement of
every marked state (P 0.012 and 0.006) and measures a device defect, not Cirq.
The basis also differs from the corrected run (cp/cx/swap were allowed), so the
routed gate counts are not comparable. Superseded by the post-fix runs.
Qiskit and PennyLane in this run were rx-free and correct, but are excluded so
one configuration is analysed, not two.

## iqm-no-results/ (job 01a09665-2828-7243-8c23-7627ba28db84)

Manifest only. The job is not found under the funded IQM account and never
produced counts; the completed IQM run is 01a096a0-7e89-7f32-ac36-6dae3a09a75a.

## ibm-rep1-adhoc-shape/ (job daimhhr9k43c73ag8js0)

The first archived polled document for the confirmatory IBM run was written by
an ad-hoc merge, not by `QuantumIBMBatchPoller`: its counts were pre-reversed to
q0_left and it carried extra per-entry fields the poller never emits. It was
replaced on 2026-09-13 by `experiments/collect_grover_hw_matrix.py --force`,
which drives the real poller (raw counts, `bit_order = q0_right`). Both resolve
to identical canonical counts; the replacement matches the shape every IBM
replicate is collected in.

## quantum-inspire-restart-refire/ (batch jobs 835700-835703, 2026-09-13 05:07 UTC)

Not a requested run. The `GroverMatrix-HW-quantum-inspire-rep2` group was left
RUNNING and armed, and a NiFi restart re-fired its one-shot trigger, so the
batch carries the rep2 label (`rep2-run-1789276034799`) while being a fourth
Tuna-17 execution. Excluded per PREREG Amendment A4.1; kept for provenance only.

## quantum-inspire-rep3-duplicate-partial/ (batch job 835820, 2026-09-13 09:00 UTC)

A duplicate of Tuna-17 rep3. Two test-case FlowFiles left queued by the failed rep3 attempt
were released together with the real trigger, so three identical batches were assembled.
`rep3-run-1789290012776` (835816-835819) completed and is the rep3 analysed.
`rep3-run-1789290012774` got one chunk (835820, readout and controls for case `10`) accepted
before Quantum Inspire returned 429; kept here for provenance only. The third batch
(`...778`) submitted nothing. PREREG Amendment A4.7.
