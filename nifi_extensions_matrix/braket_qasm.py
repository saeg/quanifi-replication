"""qasm2/qasm3 -> Braket-native OpenQASM 3 translation, shared by
BraketSimulator and BraketDevice.

Braket ships no stdgates.inc, and inlining Qiskit's copy (the previous
approach) is unsound: those gate definitions bottom out in the spec's
U / gphase / ctrl-modifier primitives, whose phase conventions Braket's
interpreter implements differently. Uncontrolled gates only differ by a
global phase, so simple circuits still sample correctly — but any
multi-controlled-gate decomposition (Grover oracles, QPE, QAOA mixers)
relies on phase kickback across cx/rz sequences, where the convention
mismatch becomes a *relative* phase error that the next Hadamard converts
into visibly wrong counts (found via the differential study: the 4-qubit
Grover peak split 50/50 between the marked state and its neighbour).

The sound path: parse with Qiskit, transpile to the {h, cx, rz, x} basis,
and emit only gates Braket implements natively (cx under its Braket name
cnot). No gate definitions are shipped at all, so Braket's own gate
semantics apply end to end.
"""
import re

# Universal basis whose every member is a native Braket gate (cx as cnot).
BRAKET_SAFE_BASIS = ["h", "cx", "rz", "x"]


def to_braket_qasm3(fmt, source):
    """Translate qasm2/qasm3 text into Braket-native OpenQASM 3.

    Returns (qasm3_src, translation_note). Raises ValueError on an
    unsupported format so callers can route to their failure relationship.
    """
    from qiskit import qasm2, qasm3, transpile

    if fmt == "qasm3":
        circuit = qasm3.loads(source)
        note = "qasm3 -> braket-native basis via qiskit"
    elif fmt == "qasm2":
        circuit = qasm2.loads(
            source, custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
        note = "qasm2 -> braket-native basis via qiskit"
    else:
        raise ValueError(
            "Unsupported circuit.format '{}'. Braket processors accept qasm3 "
            "or qasm2; set Output Format on the upstream circuit "
            "processor.".format(fmt))

    circuit.remove_final_measurements()
    circuit = transpile(
        circuit, basis_gates=BRAKET_SAFE_BASIS, optimization_level=0)
    src = qasm3.dumps(circuit)
    src = src.replace('include "stdgates.inc";', "")
    # Global phase is unobservable in counts; Braket rejects gphase outside
    # gate bodies, so drop any top-level statement the exporter emitted.
    src = re.sub(r"(?m)^\s*gphase\(.*?\);\s*$", "", src)
    src = re.sub(r"\bcx\b", "cnot", src)
    return src, note


def ensure_full_register_measure(qasm3_src):
    """Braket simulates only the qubits a program touches, so idle qubits
    silently vanish from the counts ('010' becomes '1'). Differential
    comparison needs every simulator to report the declared register width,
    so when the program has no measure statements append a register-level
    measure for each declared qubit register."""
    if re.search(r"\bmeasure\b", qasm3_src):
        return qasm3_src
    lines = [qasm3_src]
    for size, name in re.findall(r"qubit\[(\d+)\]\s+(\w+)\s*;", qasm3_src):
        lines.append(
            "bit[{0}] __meas_{1};\n__meas_{1} = measure {1};".format(size, name))
    return "\n".join(lines)
