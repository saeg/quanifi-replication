import math
import random
import time
import zlib

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


# ---------------------------------------------------------------------------
# Layer A mutation operators (gate-level circuit-content mutation)
# ---------------------------------------------------------------------------
# The Muskit/QMutBench operator set, applied to the parsed circuit *between* a
# builder and a simulator.  Unlike the Layer-B config operators in
# QuantumTestCaseSource (which corrupt the test-case row that all K branches
# read), these mutate the program artifact itself — the circuit — which is
# what makes the survival rate a mutation-testing metric in the classical
# sense (see MUTATION_TESTING.md §2 Layer A).
#
# Each operator is ``fn(circuit, rng, eps) -> outcome dict | None``.  ``None``
# means the operator is inapplicable to this circuit (e.g. rotation.perturb on
# a circuit with no parameterised gates) and must be returned *before* the
# circuit is touched, so the caller can fall through to the next operator.
# The outcome dict carries the QMutBench characteristic-vector fields:
# ``gate`` (gate involved), ``position`` (0-based index among the circuit's
# gate statements, measure/barrier excluded), ``total`` (gate-statement count
# the position is relative to), ``original`` (pre-mutation value, for the
# report).

#: Attribute naming the wires that carry operand-preparation gates.
#:
#: Spelled out rather than imported from ``arithmetic_spec``: NiFi loads a
#: processor module by file path, so a sibling module is NOT importable at
#: module scope (it raises ModuleNotFoundError and the processor silently
#: fails to load). Duplicating one string also keeps this mutator generic --
#: it works on any circuit, and must not depend on the arithmetic package.
#: tests/test_fixed_locus_mutation.py asserts the two constants stay equal.
PREP_QUBITS_ATTR = "arithmetic.prep_qubits"

_ONE_QUBIT_POOL = ("x", "y", "z", "h", "s", "t")
_TWO_QUBIT_POOL = ("cx", "cz")


def _std_gate(name):
    from qiskit.circuit.library import (
        XGate, YGate, ZGate, HGate, SGate, TGate, CXGate, CZGate,
    )
    return {
        "x": XGate, "y": YGate, "z": ZGate, "h": HGate,
        "s": SGate, "t": TGate, "cx": CXGate, "cz": CZGate,
    }[name]()


def _gate_positions(circuit):
    """Data indices of mutable gate statements (measure/barrier excluded).

    Every operator enumerates its candidates through this one function, which
    is why the operand-preparation filter lives here rather than being threaded
    through five signatures: a new operator is then body-correct by default,
    and cannot silently reintroduce the confound the fixed-locus design exists
    to remove. ``_mark_body`` attaches the excluded indices to the circuit; the
    attribute is absent for every caller that has not asked for it, so the
    behaviour is unchanged unless a locus is in play.
    """
    skip = getattr(circuit, "_quanifi_prep_indices", frozenset())
    return [
        i for i, instr in enumerate(circuit.data)
        if instr.operation.name not in ("measure", "barrier") and i not in skip
    ]


def _prep_indices(circuit, prep_wires):
    """Data indices of the operand-preparation gates, one per prepared wire.

    The prep gate is the FIRST instruction touching its wire. That is what
    makes this reorder-proof: Qiskit's ``decompose()`` freely reorders
    operations on disjoint wires -- for CDKM it moves the prep gate on the high
    B bit *after* the first body gate -- but it never reorders two operations
    that share a qubit, so "first on this wire" survives where "first N
    instructions" does not.
    """
    if not prep_wires:
        return frozenset()
    index_of = {q: i for i, q in enumerate(circuit.qubits)}
    wanted, first = set(prep_wires), {}
    for position, instr in enumerate(circuit.data):
        if instr.operation.name in ("measure", "barrier"):
            continue
        for qubit in instr.qubits:
            wire = index_of[qubit]
            if wire in wanted and wire not in first:
                first[wire] = position
    return frozenset(first.values())


def _mark_body(circuit, prep_wires):
    """Restrict every subsequent ``_gate_positions`` call to the body."""
    circuit._quanifi_prep_indices = _prep_indices(circuit, prep_wires)
    return circuit


def _body_signature(circuit):
    """Stable digest of the body's gate sequence, ignoring the operands.

    Recorded on every mutant so an analysis can *check* that a locus really did
    address the same program on every operand pair instead of assuming it.
    Measured on the boundary:2 suite, cdkm and qft are identical across all
    seven pairs; Qrisp's qcla is not -- it permutes two ancilla assignments for
    ``1+1`` and ``3+1`` -- so for that lane the digest is the evidence that
    those two cells must be reported separately rather than pooled.
    """
    import hashlib
    index_of = {q: i for i, q in enumerate(circuit.qubits)}
    parts = []
    for i in _gate_positions(circuit):
        instr = circuit.data[i]
        wires = ",".join(str(index_of[q]) for q in instr.qubits)
        parts.append("%s(%s)" % (instr.operation.name, wires))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


class _LocusChooser:
    """A deterministic stand-in for ``random.Random`` that picks by quantile.

    The operators all select their target with ``rng.randrange(n)`` or
    ``rng.choice(seq)``. Swapping the RNG for this object therefore pins every
    operator to the same position in its own candidate list -- ``first``,
    ``middle`` or ``last`` -- without touching a single operator body, and
    keeps whatever secondary draws an operator makes (a replacement gate, a
    parameter index) equally deterministic.
    """

    QUANTILES = {"first": 0.0, "middle": 0.5, "last": 1.0}

    def __init__(self, locus):
        if locus not in self.QUANTILES:
            raise ValueError("unknown locus %r" % (locus,))
        self.locus = locus
        self.quantile = self.QUANTILES[locus]

    def _pick(self, n):
        if n <= 0:
            raise ValueError("empty candidate list")
        return min(int(self.quantile * n), n - 1)

    def randrange(self, n):
        return self._pick(n)

    def choice(self, seq):
        seq = list(seq)
        return seq[self._pick(len(seq))]

    def random(self):
        return self.quantile

    def sample(self, population, k):
        """Deterministic k distinct items, quantile-anchored (gate.add uses it)."""
        population = list(population)
        if k > len(population):
            raise ValueError("sample larger than population")
        start = self._pick(len(population))
        return [population[(start + i) % len(population)] for i in range(k)]


def _position_interval(position, total):
    """QMutBench decile bin for a gate position, e.g. '30-40%'."""
    lo = min(int(position / max(total, 1) * 10), 9) * 10
    return f"{lo}-{lo + 10}%"


def _mut_add_gate(circuit, rng, eps):
    """Insert one pool gate (1q, or 2q when the circuit has >= 2 qubits)."""
    from qiskit.circuit import CircuitInstruction
    n = circuit.num_qubits
    if n == 0:
        return None
    positions = _gate_positions(circuit)
    total = len(positions)
    slot = rng.randrange(total + 1)  # before any gate statement, or at the end
    data_idx = positions[slot] if slot < total else len(circuit.data)
    if n >= 2 and rng.random() < 0.5:
        name = rng.choice(_TWO_QUBIT_POOL)
        wires = rng.sample(range(n), 2)
    else:
        name = rng.choice(_ONE_QUBIT_POOL)
        wires = [rng.randrange(n)]
    gate = _std_gate(name)
    circuit.data.insert(
        data_idx, CircuitInstruction(gate, [circuit.qubits[q] for q in wires])
    )
    return {"gate": name, "position": slot, "total": total + 1, "original": ""}


def _mut_remove_gate(circuit, rng, eps):
    """Delete one gate statement (measure/barrier are kept — separate concern)."""
    positions = _gate_positions(circuit)
    if not positions:
        return None
    k = rng.randrange(len(positions))
    name = circuit.data[positions[k]].operation.name
    del circuit.data[positions[k]]
    return {"gate": name, "position": k, "total": len(positions), "original": name}


def _mut_replace_gate(circuit, rng, eps):
    """Swap one gate for a different pool gate of the same qubit count."""
    from qiskit.circuit import CircuitInstruction
    positions = _gate_positions(circuit)
    candidates = []
    for k, di in enumerate(positions):
        op = circuit.data[di].operation
        if op.num_qubits == 1:
            pool = [g for g in _ONE_QUBIT_POOL if g != op.name]
        elif op.num_qubits == 2:
            pool = [g for g in _TWO_QUBIT_POOL if g != op.name]
        else:
            continue  # Muskit constrains replacement to equal qubit count
        if pool:
            candidates.append((k, di, pool))
    if not candidates:
        return None
    k, di, pool = candidates[rng.randrange(len(candidates))]
    old = circuit.data[di]
    new_gate = _std_gate(rng.choice(pool))
    circuit.data[di] = CircuitInstruction(
        new_gate, old.qubits[: new_gate.num_qubits]
    )
    return {
        "gate": new_gate.name, "position": k, "total": len(positions),
        "original": old.operation.name,
    }


def _mut_perturb_rotation(circuit, rng, eps):
    """Add ``eps`` to one rotation angle.

    QASM readers preserve some common phase rotations as named gates with no
    numeric ``params``. Treating those gates as their canonical angles keeps
    this fault model representation-independent: for example ``t`` becomes
    ``p(pi/4 + eps)``. This is required by PennyLane's SemiAdder, whose phase
    rotations are exported as T/S gates rather than parameterised P gates.
    """
    import numbers
    from qiskit.circuit import CircuitInstruction
    from qiskit.circuit.library import PhaseGate

    fixed_angles = {
        "t": math.pi / 4,
        "tdg": -math.pi / 4,
        "s": math.pi / 2,
        "sdg": -math.pi / 2,
        "z": math.pi,
    }
    positions = _gate_positions(circuit)
    candidates = []
    for k, di in enumerate(positions):
        op = circuit.data[di].operation
        if op.params and all(isinstance(p, numbers.Real) for p in op.params):
            candidates.append((k, di, None))
        elif op.name in fixed_angles:
            candidates.append((k, di, fixed_angles[op.name]))
    if not candidates:
        return None
    k, di, fixed_angle = candidates[rng.randrange(len(candidates))]
    old = circuit.data[di]
    if fixed_angle is None:
        op = old.operation.copy()
        j = rng.randrange(len(op.params))
        original = float(op.params[j])
        params = list(op.params)
        params[j] = original + eps
        op.params = params
        original_repr = repr(original)
    else:
        original = fixed_angle
        op = PhaseGate(original + eps)
        original_repr = "%s=%r" % (old.operation.name, original)
    circuit.data[di] = CircuitInstruction(op, old.qubits, old.clbits)
    return {
        "gate": op.name, "position": k, "total": len(positions),
        "original": original_repr,
    }


def _mut_break_carry(circuit, rng, eps, meta=None):
    """Remove a gate acting on the carry-out qubit.

    A domain-aware operator for reversible adders. ``gate.remove`` deletes a
    uniformly chosen gate, which usually breaks the answer on every input; this
    one deletes a gate on the qubit that carries the overflow bit, so the fault
    only manifests on operand pairs whose sum actually overflows. That
    input-dependence is the property the arithmetic test suite exists to
    demonstrate, and a generic operator cannot produce it reliably.

    Needs ``meta["result_qubits"]`` (published by every arithmetic builder as
    ``arithmetic.result_qubits``). Returns None when that is absent or when no
    gate touches the carry-out qubit, so the caller falls through to another
    operator rather than silently mutating something else.
    """
    result_qubits = (meta or {}).get("result_qubits")
    if not result_qubits:
        return None

    carry = max(result_qubits)
    index_of = {q: i for i, q in enumerate(circuit.qubits)}
    if carry >= len(circuit.qubits):
        return None

    positions = _gate_positions(circuit)
    candidates = [
        k for k, i in enumerate(positions)
        if carry in (index_of[q] for q in circuit.data[i].qubits)
    ]
    if not candidates:
        return None

    k = rng.choice(candidates)
    data_index = positions[k]
    instr = circuit.data[data_index]
    original = instr.operation.name
    del circuit.data[data_index]
    return {
        "gate":     original,
        # Ordinal among the body's gate statements, as every other operator
        # reports. This used to be the raw ``circuit.data`` index, which shifts
        # with the number of operand-preparation gates -- so the same logical
        # gate carried a different mut.position on different operand pairs, and
        # mut.position_interval was a decile of the wrong denominator.
        "position": k,
        "total":    len(positions),
        "original": "%s on carry qubit q%d" % (original, carry),
    }


# name -> transform fn
_GATE_MUTATION_OPERATORS = {
    "gate.add":          _mut_add_gate,
    "gate.remove":       _mut_remove_gate,
    "gate.replace":      _mut_replace_gate,
    "rotation.perturb":  _mut_perturb_rotation,
    "carry.break":       _mut_break_carry,
}

#: Operators that need FlowFile context beyond the circuit itself. Keeping this
#: explicit avoids introspecting signatures at dispatch time.
_META_OPERATORS = {"carry.break"}


def _derive_seed(base_seed, salt):
    """Reproducible per-case seed; same mix as QuantumTestCaseSource."""
    return (base_seed * 1_000_003 + salt) & 0x7FFFFFFF


class QuantumMutator(FlowFileTransform):
    """
    Layer-A gate-level circuit mutator (the Muskit/QMutBench port — see
    MUTATION_TESTING.md §2).  Sits *between a circuit builder and a simulator*
    on **one** framework branch, reads the circuit from the FlowFile content
    (qasm2 or qasm3, per the ``circuit.format`` attribute), applies one
    syntactic mutation, and emits the mutant circuit in the same format::

        QiskitGroverCircuit -> QuantumMutator -> QiskitAerSimulator -+
        CirqGroverCircuit   -> CirqSimulator  ----------------------+-> QuantumConsensusOracle
        QrispGroverSearch   -----------------------------------------+

    Because only one branch runs the mutant, the K-1 unmutated branches form a
    reference-free jury: a detected mutant surfaces as a single-branch
    **DISAGREE** at the oracle (killed), no ``test.expected`` needed.  This is
    the single-branch injection mode of MUTATION_TESTING.md §3 — it evaluates
    the differential harness itself.

    **Pass-through gating** (so paired control/mutant runs work from one
    matrix): a FlowFile with ``mut.applied = "false"`` (a control row from
    ``QuantumTestCaseSource``) passes through byte-for-byte unmutated; a
    FlowFile already carrying a Layer-B mutation (``mut.applied = "true"``
    *and* ``mut.operator`` set) also passes through, so the two layers never
    stack and kill attribution stays per-operator.  Everything else is
    mutated.  To drive paired runs, use an explicit-table Test Matrix with
    each case duplicated as ``{"mut.applied": "false"}`` / ``{"mut.applied":
    "true"}`` rows and hoist ``mut.applied`` in EvaluateJsonPath.

    Mutants carry the full ``mut.*`` bookkeeping contract (MUTATION_TESTING.md
    §4) including the Layer-A characteristic-vector fields ``mut.gate`` /
    ``mut.position`` / ``mut.position_interval`` (QMutBench decile bins) /
    ``mut.original_format``, and the stale ``circuit.*`` metrics from the
    builder (depth, gate_count, ...) are recomputed for the mutant.

    Reproducibility: the per-case seed is ``Mutation Seed`` mixed with a hash
    of ``test.case_id``, recorded as ``mut.seed`` — the same seed and matrix
    reproduce identical mutants.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Gate-level (Layer-A) mutation-testing operator: reads a qasm2/qasm3 "
            "circuit, applies one syntactic mutation (gate.add, gate.remove, "
            "gate.replace same-arity, rotation.perturb) and emits the mutant in the "
            "same format with mut.* bookkeeping attributes. Connect between a circuit "
            "builder and a simulator on ONE branch of a K-branch consensus flow so a "
            "killed mutant surfaces as a single-branch DISAGREE at "
            "QuantumConsensusOracle. Control rows (mut.applied=false) and Layer-B "
            "mutants pass through unmutated."
        )
        tags = ["quantum", "testing", "mutation", "qmutbench", "muskit",
                "differential", "qasm"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-qasm3-import"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.mutation_operators = PropertyDescriptor(
            name="Mutation Operators",
            description=(
                "Comma-separated Layer-A operators. One is chosen per FlowFile "
                "(seed-driven); if it is inapplicable to the circuit the others are "
                "tried in turn, and a circuit no listed operator can mutate routes to "
                "failure. Available: " + ", ".join(sorted(_GATE_MUTATION_OPERATORS))
                + ". Use EL (e.g. ${mut.operator}) to let the test-case row pick the "
                "operator."
            ),
            required=True,
            default_value="gate.add, gate.remove, gate.replace",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.mutation_seed = PropertyDescriptor(
            name="Mutation Seed",
            description=(
                "Integer base seed for reproducible mutants. The per-case seed is "
                "derived from it plus test.case_id and recorded as mut.seed. Blank = "
                "a time-based seed per FlowFile (not reproducible)."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.rotation_epsilon = PropertyDescriptor(
            name="Rotation Epsilon",
            description=(
                "Angle offset in radians added by rotation.perturb. Small values "
                "make near-equivalent, hard-to-kill mutants (high survival rate); "
                "large values are easy kills."
            ),
            required=True,
            default_value="0.1",
            validators=[StandardValidators.NUMBER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.mutation_locus = PropertyDescriptor(
            name="Mutation Locus",
            description=(
                "Where in the arithmetic body to mutate: first, middle or last "
                "candidate for the chosen operator. Set this to hold the mutation "
                "FIXED across operand pairs, which is what an input-dependence "
                "claim requires -- with it, the seed is unused and the same "
                "logical gate is mutated for every input. Blank (the default) "
                "keeps the historical seeded-random behaviour. Operand-preparation "
                "gates are excluded from the candidate list either way when "
                "arithmetic.prep_qubits is present, so the locus indexes the "
                "arithmetic body, not the input encoding."
            ),
            required=False,
            default_value="",
            allowable_values=["", "first", "middle", "last"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.mutation_operators,
            self.mutation_seed,
            self.mutation_locus,
            self.rotation_epsilon,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        def get(prop):
            return (
                context.getProperty(prop)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )

        raw = bytes(flowFile.getContentsAsBytes())
        fmt = flowFile.getAttribute("circuit.format")
        applied = flowFile.getAttribute("mut.applied")
        layer_b_op = flowFile.getAttribute("mut.operator")

        # ----- Pass-through gating: controls and Layer-B mutants -----
        if applied == "false":
            self.logger.info("QuantumMutator: control row, passing through unmutated")
            return FlowFileTransformResult(relationship="success", contents=raw)
        if applied == "true" and layer_b_op:
            self.logger.warn(
                "QuantumMutator: FlowFile already carries Layer-B mutation '{}'; "
                "passing through so mutation layers do not stack".format(layer_b_op)
            )
            return FlowFileTransformResult(relationship="success", contents=raw)

        # ----- Resolve and validate config -----
        ops_raw  = (get(self.mutation_operators) or "").strip()
        seed_raw = (get(self.mutation_seed) or "").strip()
        locus_raw = (get(self.mutation_locus) or "").strip().lower()
        eps_raw  = (get(self.rotation_epsilon) or "").strip()

        op_names = [o.strip() for o in ops_raw.split(",") if o.strip()]
        unknown = [o for o in op_names if o not in _GATE_MUTATION_OPERATORS]
        if not op_names or unknown:
            error_msg = (
                "unknown mutation operator(s): " + ", ".join(unknown)
                if unknown else "Mutation Operators is empty"
            ) + ". Available: " + ", ".join(sorted(_GATE_MUTATION_OPERATORS)) + "."
            self.logger.error("QuantumMutator: " + error_msg)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"mut.error": error_msg},
            )
        try:
            eps = float(eps_raw or "0.1")
        except ValueError:
            error_msg = f"Rotation Epsilon must be a number, got {eps_raw!r}"
            self.logger.error("QuantumMutator: " + error_msg)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"mut.error": error_msg},
            )

        # ----- Guard: input must be a circuit in a QASM format -----
        if not fmt:
            error_msg = (
                "Input FlowFile has no `circuit.format` attribute. QuantumMutator "
                "must be connected between a circuit-producing processor "
                "(QiskitGroverCircuit, CirqGroverCircuit, QiskitQFTCircuit, etc.) "
                "and a simulator. It cannot be connected to a simulator output "
                "(measurement counts) or a generic data source."
            )
            self.logger.error("QuantumMutator: " + error_msg)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"mut.error": error_msg,
                            "mut.error_type": "missing_circuit_format_attribute"},
            )

        # ----- Parse the circuit (QASM is the cross-framework bridge) -----
        try:
            if fmt == "qasm2":
                from qiskit import qasm2
                circuit = qasm2.loads(
                    raw.decode("utf-8"),
                    custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS,
                )
            elif fmt == "qasm3":
                from qiskit import qasm3
                circuit = qasm3.loads(raw.decode("utf-8"))
            else:
                error_msg = (
                    f"Unsupported circuit.format '{fmt}'. QuantumMutator mutates the "
                    "QASM bridge formats only (qasm2, qasm3) so one mutator serves "
                    "every framework. Set the upstream builder's Output Format to "
                    "qasm2 or qasm3, or place the mutator on a QASM-emitting branch."
                )
                self.logger.error("QuantumMutator: " + error_msg)
                return FlowFileTransformResult(
                    relationship="failure", contents=raw,
                    attributes={"mut.error": error_msg,
                                "mut.error_type": "unsupported_format"},
                )
        except Exception as exc:
            error_msg = f"Failed to parse {fmt} circuit: {exc}"
            self.logger.error("QuantumMutator: " + error_msg)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"mut.error": error_msg,
                            "mut.error_type": "parse_failure"},
            )

        # ----- Restrict the candidate list to the arithmetic body -------------
        # Operand preparation is not part of the program under test: mutating
        # it changes the *input*, not the implementation. Excluding it is also
        # what makes a locus mean the same thing on every operand pair, since
        # the number of prep gates varies with the operands.
        prep_raw = flowFile.getAttribute(PREP_QUBITS_ATTR)
        prep_wires = [int(i) for i in (prep_raw or "").split(",")
                      if i.strip().isdigit()]
        _mark_body(circuit, prep_wires)
        body_signature = _body_signature(circuit)

        # ----- Operator choice: fixed locus, or the historical seeded random --
        base_seed = (
            int(seed_raw) if seed_raw.lstrip("-").isdigit()
            else int(time.time() * 1000)
        )
        case_id = flowFile.getAttribute("test.case_id") or ""
        seed = _derive_seed(base_seed, zlib.crc32(case_id.encode("utf-8")))
        if locus_raw:
            # Deterministic: the seed plays no part, so the same logical gate is
            # mutated for every operand pair. This is the mode a confirmatory
            # input-dependence claim needs.
            rng = _LocusChooser(locus_raw)
        else:
            rng = random.Random(seed)

        # Context some operators need beyond the circuit: the arithmetic
        # builders publish where the result (and therefore the carry) lives.
        result_raw = flowFile.getAttribute("arithmetic.result_qubits") or ""
        meta = {
            "result_qubits": [int(i) for i in result_raw.split(",") if i.strip().isdigit()],
            "bit_width": flowFile.getAttribute("arithmetic.bit_width"),
        }

        chosen, outcome = None, None
        start = rng.randrange(len(op_names))
        for off in range(len(op_names)):
            name = op_names[(start + off) % len(op_names)]
            fn = _GATE_MUTATION_OPERATORS[name]
            outcome = (fn(circuit, rng, eps, meta) if name in _META_OPERATORS
                       else fn(circuit, rng, eps))
            if outcome is not None:
                chosen = name
                break
        if outcome is None:
            error_msg = (
                "no listed operator is applicable to this circuit "
                f"({', '.join(op_names)}; {circuit.num_qubits} qubits, "
                f"{len(_gate_positions(circuit))} gate statements)"
            )
            self.logger.error("QuantumMutator: " + error_msg)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"mut.error": error_msg,
                            "mut.error_type": "no_applicable_operator"},
            )

        # ----- Serialise the mutant + refresh the circuit.* metrics -----
        try:
            if fmt == "qasm2":
                from qiskit import qasm2
                text = qasm2.dumps(circuit)
            else:
                from qiskit import qasm3
                text = qasm3.dumps(circuit)
        except Exception as exc:
            error_msg = f"Failed to serialise mutant back to {fmt}: {exc}"
            self.logger.error("QuantumMutator: " + error_msg)
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"mut.error": error_msg,
                            "mut.error_type": "serialise_failure"},
            )

        ops_counts = circuit.count_ops()
        attrs = {
            "circuit.format":         fmt,
            "circuit.num_qubits":     str(circuit.num_qubits),
            "circuit.depth":          str(circuit.depth()),
            "circuit.gate_count":     str(sum(
                v for k, v in ops_counts.items() if k not in ("barrier", "measure")
            )),
            "circuit.nonlocal_gates": str(circuit.num_nonlocal_gates()),
            "circuit.t_count":        str(ops_counts.get("t", 0) + ops_counts.get("tdg", 0)),
            "circuit.diagram":        str(circuit.draw(output="text")),
            "mut.applied":            "true",
            "mut.operator":           chosen,
            "mut.target_attr":        f"{outcome['gate']}@{outcome['position']}",
            "mut.original_value":     outcome["original"],
            "mut.seed":               "" if locus_raw else str(seed),
            "mut.gate":               outcome["gate"],
            "mut.position":           str(outcome["position"]),
            "mut.position_interval":  _position_interval(outcome["position"], outcome["total"]),
            "mut.original_format":    fmt,
            # Provenance for the fixed-locus design: which position was chosen,
            # how many candidates it was chosen from, and a digest of the
            # unmutated body so an analysis can verify that a locus addressed
            # the same program on every operand pair instead of assuming it.
            "mut.locus":              locus_raw or "random",
            "mut.candidate_count":    str(outcome["total"]),
            "mut.body_signature":     body_signature,
            "mut.prep_qubits":        prep_raw or "",
            # The mutant is a fresh circuit: blank stale presentation attrs so the
            # report renders the MUTANT, not an upstream Cirq SVG / other-dialect QASM.
            "circuit.svg":            "",
        }
        attrs["circuit.qasm3" if fmt == "qasm3" else "circuit.qasm2"] = text
        # Blank the dialect we did NOT emit so a stale copy can't survive the merge.
        attrs["circuit.qasm2" if fmt == "qasm3" else "circuit.qasm3"] = ""
        if case_id:
            attrs["mut.base_case_id"] = case_id

        self.logger.warn(
            "QuantumMutator: applied {} at {} (gate={}, seed={}, case={})".format(
                chosen, attrs["mut.position_interval"], outcome["gate"], seed,
                case_id or "<none>",
            )
        )
        return FlowFileTransformResult(
            relationship="success",
            contents=text.encode("utf-8"),
            attributes=attrs,
        )
