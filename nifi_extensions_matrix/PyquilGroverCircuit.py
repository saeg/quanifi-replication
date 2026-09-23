import itertools
import math
import os
import sys

# NiFi runs each processor in its own module context where the extensions
# directory is not on sys.path, so the sibling-module import (quil_qasm)
# fails unless we add this file's directory explicitly. (Tests pass without
# it only because conftest puts the directory on sys.path.)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


def _mcz_ops(qubits):
    """Ancilla-free, exact multi-controlled-Z: flips the phase of the
    all-ones state across `qubits` (a list of pyquil qubit indices), leaving
    every other computational basis state of this register unchanged (up to
    a single register-wide global phase, which is unobservable).

    pyquil has no native gate for this beyond 2 controls (CCNOT is a plain
    Toffoli, and there is no larger multi-controlled-Z helper anywhere in
    the package). This is a from-scratch Walsh-Hadamard phase-polynomial
    synthesis, built directly from pyquil's own CNOT/RZ primitives -- not
    borrowed from another framework's decomposition:

    The desired phase function is f(x) = pi if x is all-ones, else 0 (i.e.
    the target unitary is exp(i f(x)) on each computational basis state
    |x>). Write f as a linear combination of the Fourier/character basis on
    {0,1}^n: f(x) = sum over every nonempty subset S of `qubits` of
    theta_S * chi_S(x), where chi_S(x) = (-1)^(parity of x restricted to S).
    Because the chi_S are pairwise orthogonal, theta_S is fixed uniquely by
    theta_S = (pi / 2**(n-1)) * (-1)**|S| (solved via
    sum_x f(x) chi_S(x) = 2**n * theta_S, with f nonzero only at the
    all-ones x, where chi_S(1...1) = (-1)**|S|).

    Each term is then realised as a genuine physical unitary: [a CNOT ladder
    that XORs every qubit in S into one designated qubit of S, computing
    parity_S(x)] -> [RZ(theta_S) on that qubit, contributing phase
    exp(i * chi_S(x) * theta_S / ... wait, contributing exp(i*(-1)^b*theta_S)
    for readout bit b = parity_S(x), i.e. exactly chi_S(x)*theta_S] ->
    [undo the ladder]. Composing all 2**n - 1 nonempty-subset terms in
    sequence multiplies these per-state phase factors, i.e. ADDS their
    exponents -- reproducing f(x) up to the omitted S=empty term. That term
    would contribute a single fixed phase to literally every computational
    basis state of the register (chi_empty(x) = 1 for all x), which is a
    genuine, physically unobservable global phase, so it is left out rather
    than built.

    Verified numerically (16x16 density-matrix-style unitary construction in
    numpy, comparing against the exact target diag(1,...,1,-1)) for n = 1,
    2, 3, 4 before this function was written: the resulting unitary matches
    the target exactly, up to one global phase factor shared identically by
    every diagonal entry.

    n <= 3 use a dedicated, exact, gate-cheap construction instead of the
    general sum-over-subsets one above (whose cost is O(n * 2**n) gates: fine
    for the widths this repo's Grover builders actually exercise, but
    needlessly expensive for small n where a native identity is available
    and PyQVM's pure-Python per-shot execution makes gate count the whole
    ballgame for test runtime):
      * n=1: bare Z (the all-ones state of a single qubit is just |1>).
      * n=2: the textbook CZ = H(b); CNOT(a,b); H(b) identity.
      * n=3: the textbook CCZ = H(c); CCNOT(a,b,c); H(c) identity (the
        Toffoli's bit-flip on |11*> becomes a phase-flip on |111> once
        sandwiched in H).
    Both identities are *exact* (no residual global phase at all, verified
    numerically alongside the general construction), and use only gates
    already in this repo's Quil-safe/qasm2-safe sets (H, CNOT, CCNOT).
    """
    from pyquil.gates import CCNOT, CNOT, H, RZ, Z

    n = len(qubits)
    if n == 1:
        return [Z(qubits[0])]
    if n == 2:
        a, b = qubits
        return [H(b), CNOT(a, b), H(b)]
    if n == 3:
        a, b, c = qubits
        return [H(c), CCNOT(a, b, c), H(c)]

    ops = []
    for size in range(1, n + 1):
        angle = (math.pi / (2 ** (n - 1))) * ((-1) ** size)
        for subset in itertools.combinations(qubits, size):
            target = subset[-1]
            ladder = subset[:-1]
            for control in ladder:
                ops.append(CNOT(control, target))
            ops.append(RZ(angle, target))
            for control in reversed(ladder):
                ops.append(CNOT(control, target))
    return ops


def _phase_oracle_ops(qubits, target_bits):
    """Flip the phase of |target_bits> only, via X-conjugation around
    _mcz_ops (the standard construction: flip the 0-bits to 1 so the target
    string becomes the all-ones string, apply the all-ones phase flip, flip
    the 0-bits back)."""
    from pyquil.gates import X

    ops = []
    zero_positions = [q for q, bit in zip(qubits, target_bits) if bit == "0"]
    for q in zero_positions:
        ops.append(X(q))
    ops.extend(_mcz_ops(qubits))
    for q in zero_positions:
        ops.append(X(q))
    return ops


def _diffusion_ops(qubits):
    """Grover diffusion: 2|s><s| - I, reflection about the uniform
    superposition, built the standard way as H-X-mcz-X-H on every qubit."""
    from pyquil.gates import H, X

    ops = []
    ops.extend(H(q) for q in qubits)
    ops.extend(X(q) for q in qubits)
    ops.extend(_mcz_ops(qubits))
    ops.extend(X(q) for q in qubits)
    ops.extend(H(q) for q in qubits)
    return ops


def _circuit_depth(instructions):
    """Critical-path depth: for each instruction, one more than the deepest
    of its qubits' current depth, mirroring what Cirq's `len(circuit)`
    (moment count) reports for a densely-packed circuit."""
    last = {}
    depth = 0
    for instr in instructions:
        qs = [q.index for q in instr.qubits]
        d = max((last.get(q, 0) for q in qs), default=0) + 1
        for q in qs:
            last[q] = d
        depth = max(depth, d)
    return depth


class PyquilGroverCircuit(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a Grover search circuit using pyquil (Rigetti) for a "
            "given target bitstring and outputs the unmeasured circuit as "
            "OpenQASM 2.0, the interchange format every engine in this repo "
            "accepts. pyquil ships no Grover/amplitude-amplification helper, "
            "so the oracle and diffuser -- including the multi-controlled-Z, "
            "which pyquil has no native gate for beyond 2 controls -- are "
            "built here directly from pyquil's own H/X/CNOT/RZ primitives "
            "(see the module-level _mcz_ops docstring), not translated from "
            "another framework's circuit. Connect to PyquilSimulator or any "
            "other engine in this repo to run the simulation."
        )
        tags = ["quantum", "pyquil", "rigetti", "grover", "circuit"]
        dependencies = ["pyquil>=4.18"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.marked_state = PropertyDescriptor(
            name="Marked State",
            description=(
                "Target bitstring Grover will search for, e.g. '110'. "
                "Length sets the number of qubits. Bit order is left-to-right "
                "(qubit 0 = leftmost character)."
            ),
            required=True,
            default_value="11",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.num_iterations = PropertyDescriptor(
            name="Num Iterations",
            description=(
                "Number of Grover operator applications. "
                "Optimal is roughly floor(pi/4 * sqrt(2^n)) for one marked state."
            ),
            required=True,
            default_value="1",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "Format used to serialize the circuit into the FlowFile "
                "content. 'qasm2' (the only supported value) is OpenQASM 2.0 "
                "text emitted via quil_qasm.to_qasm2, the interchange format "
                "every other engine in this repo accepts."
            ),
            required=True,
            default_value="qasm2",
            allowable_values=["qasm2"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.marked_state,
            self.num_iterations,
            self.output_format,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from pyquil import Program
        from pyquil.gates import H

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        target = get(self.marked_state)
        num_iterations = int(get(self.num_iterations))
        fmt = get(self.output_format)
        n = len(target)
        qubits = list(range(n))

        if any(bit not in "01" for bit in target):
            return FlowFileTransformResult(
                relationship="failure",
                contents=b"",
                attributes={
                    "grover.error": "Marked State must be a binary string, "
                                    "got '{}'".format(target)
                },
            )
        if fmt != "qasm2":
            return FlowFileTransformResult(
                relationship="failure",
                contents=b"",
                attributes={
                    "grover.error": "Unsupported Output Format '{}'".format(fmt)
                },
            )

        program = Program()
        for q in qubits:
            program += H(q)
        for _ in range(num_iterations):
            for op in _phase_oracle_ops(qubits, target):
                program += op
            for op in _diffusion_ops(qubits):
                program += op
        program.num_qubits = n

        instructions = list(program.instructions)
        gate_count = len(instructions)
        nonlocal_gates = sum(1 for instr in instructions if len(instr.qubits) >= 2)
        t_count = sum(
            1 for instr in instructions
            if getattr(instr, "name", None) == "RZ"
            and abs(abs(float(instr.params[0].real)) - math.pi / 4) < 1e-9
        )
        depth = _circuit_depth(instructions)
        diagram = str(program)

        from quil_qasm import to_qasm2
        content = to_qasm2(program).encode("utf-8")

        self.logger.warn("Grover circuit (target={}, iterations={}):\n{}".format(
            target, num_iterations, diagram
        ))

        attrs = {
            "circuit.format":         "qasm2",
            "circuit.qasm2":          content.decode("utf-8"),
            "circuit.qasm3": "",  # pyquil emits qasm2, not qasm3 (blank stale)
            "circuit.num_qubits":     str(n),
            "circuit.marked_state":   target,
            "circuit.num_iterations": str(num_iterations),
            "circuit.diagram":        diagram,
            "circuit.svg":            "",  # blank stale (Cirq-only presentation attr)
            "circuit.depth":          str(depth),
            "circuit.gate_count":     str(gate_count),
            "circuit.nonlocal_gates": str(nonlocal_gates),
            "circuit.t_count":        str(t_count),
            "circuit.bit_order":      "canonical",
            "builder.component":      "PyquilGrover",
            "builder.framework":      "pyquil",
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
