"""
Framework-neutral specification for the small reversible arithmetic circuits
used by the arithmetic differential/mutation study.

This module is deliberately free of any quantum framework import.  It answers
three questions that every arithmetic builder needs to agree on, and that the
oracles need in order to score a run:

  1. What is the classical answer?          -> ``classical_result``
  2. Where does it live in the register?    -> ``RegisterLayout``
  3. What bitstring should a correct run
     produce, in the project's canonical
     counts order?                          -> ``expected_bitstring``

Bit order
---------
The project pins one counts convention across every engine (see
``tests/test_bit_order.py``): **qubit 0 is the LEFTMOST character** of a counts
key, and engines advertise ``sim.bit_order = "q0_left"``.  Everything here uses
that order.

Within a value register, qubit *i* carries bit *i* of the value, least
significant bit at the lowest qubit index.  Combined with q0-left, a register
holding the value 1 in 3 bits reads ``"100"``, not ``"001"``.  That looks
backwards if you are used to writing numbers MSB-first; it is the direct
consequence of the two conventions above and is pinned by a test.
"""

import hashlib
import itertools
import json
import re
from dataclasses import dataclass, field


# --- FlowFile attribute contract -------------------------------------------
# Every arithmetic builder emits exactly these.  Downstream oracles and the
# experiment driver read them by name, so they live here rather than being
# spelled out in each processor.

ATTR_OPERATION        = "arithmetic.operation"
ATTR_OPERAND_A        = "arithmetic.operand_a"
ATTR_OPERAND_B        = "arithmetic.operand_b"
ATTR_BIT_WIDTH        = "arithmetic.bit_width"
ATTR_EXPECTED_RESULT  = "arithmetic.expected_result"
ATTR_EXPECTED_BITS    = "arithmetic.expected_bitstring"
ATTR_RESULT_QUBITS    = "arithmetic.result_qubits"
ATTR_RESULT_BITS      = "arithmetic.expected_result_bits"
ATTR_IMPLEMENTATION   = "arithmetic.implementation"
ATTR_FRAMEWORK        = "arithmetic.framework"
ATTR_PREP_QUBITS      = "arithmetic.prep_qubits"

# Case-set provenance.  These live under the ``arithmetic.`` prefix on purpose:
# the batch submitter captures every attribute with that prefix onto the
# manifest entry (see batch_prep.captured_attributes), so an attribute named
# this way is the only kind that survives batching as genuinely *per circuit*.
# Anything outside the prefix is stamped from the parent FlowFile onto all N
# children by the expander and reads the same on every row.
ATTR_CASE_ORDINAL     = "arithmetic.test_case_ordinal"
ATTR_CASE_SET_SHA     = "arithmetic.test_case_set_sha256"
ATTR_MANIFEST_SHA     = "arithmetic.test_manifest_sha256"
ATTR_SUITE_ID         = "arithmetic.test_suite_id"
ATTR_SUITE_SCHEMA     = "arithmetic.test_suite_schema"

#: Emitted by every builder so a reader never has to guess the counts order.
BIT_ORDER = "q0_left"

OPERATIONS = ("add", "subtract", "multiply")

MIN_BIT_WIDTH = 1
MAX_BIT_WIDTH = 8


class ArithmeticSpecError(ValueError):
    """Raised when a specification cannot be realised as a circuit."""


# --- register layout --------------------------------------------------------

@dataclass(frozen=True)
class RegisterLayout:
    """Which qubit indices carry what, for one arithmetic circuit.

    Indices are absolute positions in the emitted ``qreg``, and therefore also
    positions in a ``q0_left`` counts key.
    """

    num_qubits: int
    a_qubits: tuple = ()
    b_qubits: tuple = ()
    result_qubits: tuple = ()
    ancilla_qubits: tuple = field(default=())

    def __post_init__(self):
        seen = list(self.a_qubits) + list(self.b_qubits) + list(self.ancilla_qubits)
        for idx in list(seen) + list(self.result_qubits):
            if not 0 <= idx < self.num_qubits:
                raise ArithmeticSpecError(
                    "qubit index %d outside register of %d qubits"
                    % (idx, self.num_qubits))
        # a/b/ancilla partition the register; result_qubits overlaps one of
        # them (an in-place adder writes its answer over an operand), so it is
        # checked for range but deliberately not for disjointness.
        if len(set(seen)) != len(seen):
            raise ArithmeticSpecError("operand and ancilla qubits overlap")


def result_width(operation, bit_width):
    """Number of bits needed to hold the answer without truncation."""
    _check_width(bit_width)
    if operation == "multiply":
        return 2 * bit_width
    # add carries out one bit; subtract is taken modulo 2**bit_width
    return bit_width + 1 if operation == "add" else bit_width


def classical_result(operation, a, b, bit_width):
    """The value a correct circuit must produce, as a non-negative integer.

    ``subtract`` is modular over ``2**bit_width`` (two's complement wrap),
    which is what a reversible subtractor computes.
    """
    validate_operands(operation, a, b, bit_width)
    if operation == "add":
        return a + b
    if operation == "subtract":
        return (a - b) % (2 ** bit_width)
    if operation == "multiply":
        return a * b
    raise ArithmeticSpecError("unknown operation %r" % (operation,))


def validate_operands(operation, a, b, bit_width):
    """Raise ``ArithmeticSpecError`` if the request cannot be represented."""
    if operation not in OPERATIONS:
        raise ArithmeticSpecError(
            "unknown operation %r; expected one of %s"
            % (operation, ", ".join(OPERATIONS)))
    _check_width(bit_width)
    limit = 2 ** bit_width
    for label, value in (("operand A", a), ("operand B", b)):
        if not isinstance(value, int):
            raise ArithmeticSpecError("%s must be an integer, got %r" % (label, value))
        if value < 0:
            raise ArithmeticSpecError(
                "%s is %d; negative operands are not supported" % (label, value))
        if value >= limit:
            raise ArithmeticSpecError(
                "%s is %d, which does not fit in %d bits (maximum %d)"
                % (label, value, bit_width, limit - 1))


def _check_width(bit_width):
    if not isinstance(bit_width, int):
        raise ArithmeticSpecError("bit width must be an integer, got %r" % (bit_width,))
    if not MIN_BIT_WIDTH <= bit_width <= MAX_BIT_WIDTH:
        raise ArithmeticSpecError(
            "bit width %d outside supported range %d..%d"
            % (bit_width, MIN_BIT_WIDTH, MAX_BIT_WIDTH))


# --- bitstring encoding -----------------------------------------------------

def encode_value(value, width):
    """Render ``value`` across ``width`` qubits in q0-left, LSB-at-q0 order."""
    if value < 0 or value >= 2 ** width:
        raise ArithmeticSpecError(
            "value %d does not fit in %d bits" % (value, width))
    return "".join("1" if (value >> i) & 1 else "0" for i in range(width))


def decode_value(bits):
    """Inverse of :func:`encode_value`."""
    return sum(1 << i for i, ch in enumerate(bits) if ch == "1")


def expected_bitstring(layout, operation, a, b, bit_width):
    """Full-register bitstring a correct run must produce, in q0-left order.

    Every qubit position is filled: operand registers keep whatever the circuit
    leaves in them, the result register holds the answer, and ancillas are
    returned to zero.  Positions the layout does not mention are zero.
    """
    result = classical_result(operation, a, b, bit_width)
    chars = ["0"] * layout.num_qubits

    # Operand registers first, so an in-place result overwrites them below.
    for reg, value in ((layout.a_qubits, a), (layout.b_qubits, b)):
        if not reg:
            continue
        bits = encode_value(value, len(reg))
        for pos, idx in enumerate(reg):
            chars[idx] = bits[pos]

    if layout.result_qubits:
        width = len(layout.result_qubits)
        if result >= 2 ** width:
            raise ArithmeticSpecError(
                "result %d does not fit in the %d-qubit result register"
                % (result, width))
        bits = encode_value(result, width)
        for pos, idx in enumerate(layout.result_qubits):
            chars[idx] = bits[pos]

    return "".join(chars)


def expected_result_bits(layout, operation, a, b, bit_width):
    """Just the result register's bits, in q0-left order."""
    full = expected_bitstring(layout, operation, a, b, bit_width)
    return "".join(full[i] for i in layout.result_qubits)


def marginalize(counts, result_qubits):
    """Collapse full-register counts onto the result register only.

    Lets an oracle score the answer without caring what an implementation left
    in its operand or ancilla qubits.
    """
    out = {}
    for key, n in counts.items():
        try:
            sub = "".join(key[i] for i in result_qubits)
        except IndexError:
            raise ArithmeticSpecError(
                "counts key %r is shorter than the result register" % (key,))
        out[sub] = out.get(sub, 0) + n
    return out


# --- test-suite generation --------------------------------------------------
# Lives here rather than in the experiments layer because two callers need it:
# ``experiments/arithmetic_analysis.py`` (which re-exports it) and
# ``QuantumTestCaseSource``, a processor that cannot import from experiments/.
# One definition, so a canvas run and a headless run cover the same inputs.

#: Boundary conditions a case may cover. Recorded per case so a report can say
#: which part of the input space detected a fault.
BOUNDARY_ZERO_A      = "zero_a"
BOUNDARY_ZERO_B      = "zero_b"
BOUNDARY_MAX_A       = "max_a"
BOUNDARY_MAX_B       = "max_b"
BOUNDARY_CARRY_OUT   = "carry_out"
BOUNDARY_NO_CARRY    = "no_carry"
BOUNDARY_ALL_CARRY   = "all_carry"

SUITE_STRATEGIES = ("exhaustive", "boundary")


def carry_positions(a, b, bit_width):
    """Bit positions at which the addition actually produces a carry.

    This is what separates inputs that exercise the carry chain from inputs
    that do not, and therefore what makes a carry-chain fault input-dependent.
    """
    positions = []
    carry = 0
    for i in range(bit_width):
        bit_a = (a >> i) & 1
        bit_b = (b >> i) & 1
        if bit_a + bit_b + carry >= 2:
            positions.append(i)
            carry = 1
        else:
            carry = 0
    return positions


def classify(a, b, bit_width):
    """Which boundary conditions the operand pair (a, b) covers."""
    limit = 2 ** bit_width - 1
    tags = []
    if a == 0:
        tags.append(BOUNDARY_ZERO_A)
    if b == 0:
        tags.append(BOUNDARY_ZERO_B)
    if a == limit:
        tags.append(BOUNDARY_MAX_A)
    if b == limit:
        tags.append(BOUNDARY_MAX_B)
    if a + b > limit:
        tags.append(BOUNDARY_CARRY_OUT)
    if carry_positions(a, b, bit_width):
        tags.append(BOUNDARY_ALL_CARRY if a + b > limit else BOUNDARY_NO_CARRY)
    else:
        tags.append(BOUNDARY_NO_CARRY)
    return tags


def make_case(a, b, bit_width, operation="add"):
    return {
        "operation":  operation,
        "a":          a,
        "b":          b,
        "bit_width":  bit_width,
        "expected":   classical_result(operation, a, b, bit_width),
        "boundaries": classify(a, b, bit_width),
        "carries":    carry_positions(a, b, bit_width),
        "exercises_carry": bool(carry_positions(a, b, bit_width)),
    }


def exhaustive_suite(bit_width, operation="add"):
    """Every ordered operand pair representable in ``bit_width`` bits."""
    return [make_case(a, b, bit_width, operation)
            for a, b in itertools.product(range(2 ** bit_width), repeat=2)]


def boundary_suite(bit_width, operation="add", seed=0):
    """A small suite covering zero, maximum, and carrying operands.

    Deterministic: the same bit width and seed always give the same cases in
    the same order. The seed is accepted so that a future sampling strategy can
    slot in without changing callers, and is recorded by ``describe_suite``.
    """
    limit = 2 ** bit_width - 1
    picks = [
        (0, 0),               # both zero
        (0, limit),           # zero plus maximum
        (limit, 0),           # maximum plus zero
        (limit, limit),       # maximum plus maximum: carries at every position
        (1, limit),           # minimal operand that forces a full carry ripple
    ]
    if bit_width >= 2:
        picks.append((1, 1))                       # no carry at all
        picks.append((limit, 1))                   # carry out of the top bit
    ordered, seen = [], set()
    for a, b in picks:
        if (a, b) not in seen and a <= limit and b <= limit:
            seen.add((a, b))
            ordered.append(make_case(a, b, bit_width, operation))
    return ordered


def suite(strategy, bit_width, operation="add", seed=0):
    if strategy == "exhaustive":
        return exhaustive_suite(bit_width, operation)
    if strategy == "boundary":
        return boundary_suite(bit_width, operation, seed)
    raise ValueError("unknown suite strategy %r" % (strategy,))


def describe_suite(cases, strategy, bit_width, seed=0):
    """Metadata recorded alongside a suite so a run is reproducible."""
    covered = set()
    for case in cases:
        covered.update(case["boundaries"])
    return {
        "strategy":   strategy,
        "bit_width":  bit_width,
        "seed":       seed,
        "case_count": len(cases),
        "boundaries_covered": sorted(covered),
        "carrying_cases":     sum(1 for c in cases if c["exercises_carry"]),
    }


# --- case manifests ---------------------------------------------------------
# A suite generated from ``strategy:bit_width`` is compact but invisible: the
# canvas shows ``boundary:2`` and not the seven operand pairs that string
# selects.  A *manifest* states the pairs explicitly, so the experimental inputs
# can be read off a processor property or a version-controlled file.
#
# The manifest carries only the independent inputs -- operation, bit width and
# the ordered operand pairs.  Expected answers, carry classification and
# partitions are always DERIVED here, by the same ``make_case`` a generated
# suite goes through, so a typo in a hand-written file cannot become the oracle.
# Everything below is standard library only: it is imported by a NiFi processor.

MANIFEST_SCHEMA_V1 = "quanifi.arithmetic-cases/v1"
SUPPORTED_MANIFEST_SCHEMAS = (MANIFEST_SCHEMA_V1,)

#: Version 1 is a closed schema: an unknown key is a rejection, not a warning.
#: A key the parser silently ignored would be a second, invisible source of
#: truth the moment someone added ``"expected_result"`` to a case.
MANIFEST_FIELDS = ("schema", "suite_id", "operation", "bit_width", "cases")
MANIFEST_CASE_FIELDS = ("a", "b")

#: Conservative caps.  A property or FlowFile larger than this is a mistake or
#: an attack, never a real seven-case definition.
MAX_MANIFEST_BYTES = 1048576
MAX_MANIFEST_CASES = 256

#: ``suite_id`` ends up in an HTML report card that archive_canvas_run.py reads
#: back with a ``\S+`` regex, so whitespace in it would silently truncate the
#: archived provenance.  Pin the charset rather than discover that later.
_SUITE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class ArithmeticManifestError(ArithmeticSpecError):
    """A manifest is not a valid, complete case definition.

    Carries a stable ``code`` so a canvas operator can route on the *kind* of
    failure without parsing the message text.
    """

    def __init__(self, message, code="manifest.invalid"):
        super().__init__(message)
        self.code = code


def _require_int(value, label, code):
    """Reject bools explicitly: ``isinstance(True, int)`` is True in Python."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArithmeticManifestError(
            "%s must be an integer, got %r" % (label, value), code)
    return value


def parse_manifest(raw, max_bytes=MAX_MANIFEST_BYTES, max_cases=MAX_MANIFEST_CASES):
    """Validate a version-1 case manifest and derive its cases.

    ``raw`` is the supplied bytes or text.  Returns a normalized dict::

        {"schema", "suite_id", "operation", "bit_width",
         "pairs":  [(a, b), ...],          # declared order, significant
         "cases":  [make_case(...), ...],  # derived ground truth
         "case_set_sha256", "manifest_sha256", "raw_sha256"}

    Rejects the whole input on any problem -- there is no partial parse, so a
    malformed definition cannot emit a prefix of the cases it meant to.
    """
    if isinstance(raw, str):
        data = raw.encode("utf-8")
    elif isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
    else:
        raise ArithmeticManifestError(
            "manifest must be bytes or text, got %s" % type(raw).__name__,
            "manifest.type")

    if len(data) > max_bytes:
        raise ArithmeticManifestError(
            "manifest is %d bytes, above the %d byte limit"
            % (len(data), max_bytes), "manifest.too_large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArithmeticManifestError(
            "manifest is not valid UTF-8: %s" % (exc,), "manifest.not_utf8")
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ArithmeticManifestError(
            "manifest is not valid JSON: %s" % (exc,), "manifest.not_json")
    if not isinstance(doc, dict):
        raise ArithmeticManifestError(
            "manifest must be a JSON object, got %s" % type(doc).__name__,
            "manifest.not_object")

    unknown = sorted(set(doc) - set(MANIFEST_FIELDS))
    if unknown:
        raise ArithmeticManifestError(
            "unknown manifest field(s): %s; version 1 accepts %s"
            % (", ".join(unknown), ", ".join(MANIFEST_FIELDS)),
            "manifest.unknown_field")

    schema = doc.get("schema")
    if schema not in SUPPORTED_MANIFEST_SCHEMAS:
        raise ArithmeticManifestError(
            "unsupported schema %r; expected one of %s"
            % (schema, ", ".join(SUPPORTED_MANIFEST_SCHEMAS)), "manifest.schema")

    suite_id = doc.get("suite_id")
    if not isinstance(suite_id, str) or not _SUITE_ID_RE.match(suite_id):
        raise ArithmeticManifestError(
            "suite_id must match %s, got %r" % (_SUITE_ID_RE.pattern, suite_id),
            "manifest.suite_id")

    operation = doc.get("operation")
    if operation not in OPERATIONS:
        raise ArithmeticManifestError(
            "unknown operation %r; expected one of %s"
            % (operation, ", ".join(OPERATIONS)), "manifest.operation")

    bit_width = _require_int(doc.get("bit_width"), "bit_width", "manifest.bit_width")
    if not MIN_BIT_WIDTH <= bit_width <= MAX_BIT_WIDTH:
        raise ArithmeticManifestError(
            "bit_width %d outside supported range %d..%d"
            % (bit_width, MIN_BIT_WIDTH, MAX_BIT_WIDTH), "manifest.bit_width")

    raw_cases = doc.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ArithmeticManifestError(
            "cases must be a non-empty JSON array", "manifest.cases")
    if len(raw_cases) > max_cases:
        raise ArithmeticManifestError(
            "manifest declares %d cases, above the %d case limit"
            % (len(raw_cases), max_cases), "manifest.cases")

    pairs, seen_pairs, seen_ids = [], set(), set()
    for i, entry in enumerate(raw_cases):
        if not isinstance(entry, dict):
            raise ArithmeticManifestError(
                "case %d is not an object: %r" % (i, entry), "manifest.case_field")
        extra = sorted(set(entry) - set(MANIFEST_CASE_FIELDS))
        if extra:
            raise ArithmeticManifestError(
                "case %d has unknown field(s) %s; version 1 declares inputs only "
                "(%s) and derives everything else"
                % (i, ", ".join(extra), ", ".join(MANIFEST_CASE_FIELDS)),
                "manifest.case_field")
        missing = [f for f in MANIFEST_CASE_FIELDS if f not in entry]
        if missing:
            raise ArithmeticManifestError(
                "case %d is missing %s" % (i, ", ".join(missing)),
                "manifest.case_field")
        a = _require_int(entry["a"], "case %d operand a" % i, "manifest.operand")
        b = _require_int(entry["b"], "case %d operand b" % i, "manifest.operand")
        try:
            validate_operands(operation, a, b, bit_width)
        except ArithmeticSpecError as exc:
            raise ArithmeticManifestError(
                "case %d: %s" % (i, exc), "manifest.operand")
        if (a, b) in seen_pairs:
            raise ArithmeticManifestError(
                "case %d duplicates operand pair %d+%d" % (i, a, b),
                "manifest.duplicate_case")
        cid = case_id(operation, a, b)
        if cid in seen_ids:
            raise ArithmeticManifestError(
                "case %d duplicates case id %s" % (i, cid),
                "manifest.duplicate_case_id")
        seen_pairs.add((a, b))
        seen_ids.add(cid)
        pairs.append((a, b))

    manifest = {
        "schema": schema,
        "suite_id": suite_id,
        "operation": operation,
        "bit_width": bit_width,
        "pairs": pairs,
        "cases": [make_case(a, b, bit_width, operation) for a, b in pairs],
    }
    manifest["case_set_sha256"] = case_set_digest(operation, bit_width, pairs)
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["raw_sha256"] = raw_digest(data)
    return manifest


def case_id(operation, a, b):
    """The stable per-case identifier every arithmetic row carries."""
    return "%s-%d+%d" % (operation, a, b)


def case_pairs(cases):
    """The ordered ``(a, b)`` pairs of a list of ``make_case`` dicts."""
    return [(c["a"], c["b"]) for c in cases]


def _sha256_json(payload):
    """Digest a canonical JSON rendering: sorted keys, no insignificant space."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def case_set_digest(operation, bit_width, pairs):
    """The scientific input identity: what the experiment actually measures.

    Covers the operation, the bit width and the ordered operand pairs, and
    nothing else.  Reformatting a file or renaming its ``suite_id`` leaves this
    unchanged; changing or reordering a case does not.  A different case-set
    digest is a different test-case condition, whose results must not be pooled
    with a campaign pinned to the old one.
    """
    return _sha256_json({
        "operation": operation,
        "bit_width": int(bit_width),
        "cases": [[int(a), int(b)] for a, b in pairs],
    })


def manifest_digest(manifest):
    """The normalized configuration identity: case set plus its labelling.

    Adds ``schema`` and ``suite_id`` to the case-set digest, so relabelling a
    file is visible without being mistaken for a new scientific treatment.
    """
    return _sha256_json({
        "schema": manifest["schema"],
        "suite_id": manifest["suite_id"],
        "operation": manifest["operation"],
        "bit_width": int(manifest["bit_width"]),
        "cases": [{"a": int(a), "b": int(b)} for a, b in manifest["pairs"]],
    })


def raw_digest(raw):
    """The exact supplied bytes, for audit only.  Whitespace changes this."""
    data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    return hashlib.sha256(data).hexdigest()


def case_rows(cases):
    """Turn derived cases into the FlowFile row contract the builders read.

    One definition, so a generated suite, an inline manifest and a manifest
    arriving as FlowFile content emit byte-identical rows for the same operand
    pairs.  Key insertion order is part of the contract -- it is the order the
    rows are serialised in -- so ``arithmetic.test_case_ordinal`` is appended
    last rather than woven into the legacy fields.
    """
    rows = []
    for ordinal, case in enumerate(cases):
        rows.append({
            ATTR_OPERATION:       str(case["operation"]),
            ATTR_OPERAND_A:       str(case["a"]),
            ATTR_OPERAND_B:       str(case["b"]),
            ATTR_BIT_WIDTH:       str(case["bit_width"]),
            ATTR_EXPECTED_RESULT: str(case["expected"]),
            "arithmetic.exercises_carry": "true" if case["exercises_carry"] else "false",
            "test.case_id":  case_id(case["operation"], case["a"], case["b"]),
            "test.partition": "|".join(case["boundaries"]),
            ATTR_CASE_ORDINAL: str(ordinal),
        })
    return rows


def missing_anchors(pairs, anchors):
    """Anchor cases a manifest fails to supply exactly once, in ``a+b`` form.

    The hardware group routes null replicates by operand value, so a manifest
    that drops ``0+0`` silently produces fewer null circuits than the batch
    expects and the submitter waits for a circuit nobody will build.
    """
    counted = {}
    for a, b in pairs:
        key = "%d+%d" % (a, b)
        counted[key] = counted.get(key, 0) + 1
    return [anchor for anchor in anchors if counted.get(anchor, 0) != 1]


def prep_qubits(a, b, bit_width, layout):
    """Wires that carry an operand-preparation gate, in emitted-qreg indices.

    Every builder encodes its operands the same way: one X per set bit, on
    ``layout.a_qubits[i]`` / ``layout.b_qubits[i]`` for bit *i*. So the set of
    prepared wires is a pure function of the operands and the layout, and no
    builder has to report it separately.

    This exists so a consumer can separate operand preparation from the
    arithmetic body. Counting prep gates is not enough: Qiskit's
    ``decompose()`` reorders operations on disjoint wires, so for CDKM the prep
    gate on the high B bit lands *after* the first body gate. What survives
    that reorder is per-wire order -- the prep gate is still the first
    instruction touching its wire, because reordering never swaps two
    operations that share a qubit. Splitting on wires is therefore stable where
    splitting on position is not.
    """
    prepared = []
    for i in range(bit_width):
        if (a >> i) & 1 and i < len(layout.a_qubits):
            prepared.append(layout.a_qubits[i])
        if (b >> i) & 1 and i < len(layout.b_qubits):
            prepared.append(layout.b_qubits[i])
    return sorted(prepared)


def describe(operation, a, b, bit_width, layout, implementation, framework):
    """Build the full FlowFile attribute dict for one arithmetic circuit."""
    result = classical_result(operation, a, b, bit_width)
    return {
        ATTR_OPERATION:       str(operation),
        ATTR_OPERAND_A:       str(a),
        ATTR_OPERAND_B:       str(b),
        ATTR_BIT_WIDTH:       str(bit_width),
        ATTR_EXPECTED_RESULT: str(result),
        ATTR_EXPECTED_BITS:   expected_bitstring(layout, operation, a, b, bit_width),
        ATTR_RESULT_BITS:     expected_result_bits(layout, operation, a, b, bit_width),
        ATTR_RESULT_QUBITS:   ",".join(str(i) for i in layout.result_qubits),
        ATTR_IMPLEMENTATION:  str(implementation),
        ATTR_FRAMEWORK:       str(framework),
        ATTR_PREP_QUBITS:     ",".join(
            str(i) for i in prep_qubits(a, b, bit_width, layout)),
        "sim.bit_order":      BIT_ORDER,
    }
