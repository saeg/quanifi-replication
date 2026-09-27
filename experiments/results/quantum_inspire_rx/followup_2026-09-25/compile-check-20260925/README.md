# Quantum Inspire compile-only check, 2026-09-25 (no job, no hardware)

Inputs: the cQASM that `qiskit_quantuminspire.cqasm.dumps` (qiskit-quantuminspire 0.18.2)
emits for `rx_sign_probe` and `rx_sign_probe_neg` of job 843300.
Command: `qi files compile --file <f>.cq --backend-type-id 7 --compile-stage {decomposition,routing}`
(Tuna-17, quantuminspire CLI 4.0.0).

Result: the compiled output is byte-identical to the input at both stages; `Rx(1.5707963)` and
`Rx(-1.5707963)` pass through unchanged. The sign reversal therefore happens after the compilation
stages Quantum Inspire exposes (in its execution stack or on the device), not in the Qiskit adapter
or the cQASM compiler.
