"""Framework-neutral Pauli-sum parser, shared by every ``<Framework>Hamiltonian``
processor (Qiskit today; Cirq, Qrisp, PennyLane planned).

The whole point of this module is a *single* Hamiltonian input syntax across all
frameworks. It parses to a neutral representation that each framework's builder
maps onto its own operator type:

    terms       : list of (pauli: str, indices: list[int], coeff: float)
                  e.g. ("XX", [0, 1], 0.5)  — identity term is ("", [], coeff)
    num_qubits  : int

That tuple is exactly what each framework wants:
    Qiskit     SparsePauliOp.from_sparse_list(terms, num_qubits)
    Cirq       sum(coeff * prod(P(qubits[i]) for ...) ...)
    Qrisp      sum(coeff * prod(P(i) for ...) ...)        # qrisp.operators
    PennyLane  sum(coeff * prod(qml.P(i) for ...) ...)

Two input forms, auto-detected:

  * Compact-indexed text (canonical) — OpenFermion / Qrisp / PennyLane style::

        "Z0 + Z1 + 0.5 X0 X1"
        "0.5 X0 X1 + -1.5 Z2 + 0.25"     # bare number = identity term

    Spaced factors are also accepted for back-compat ("Z 0 + 0.5 X 0 X 1").
    Terms are separated by ``+`` or ``-``; coefficients are plain decimals
    (scientific notation is not supported — write 0.001, not 1e-3).

  * JSON — explicit/machine form::

        [{"pauli": "ZZ", "qubits": [0, 1], "coeff": 0.5},
         {"pauli": "Z",  "qubits": [0],    "coeff": 1.0}]

    or an object ``{"num_qubits": n, "terms": [ ...same dicts... ]}``.

Only the four single-qubit Paulis are valid: I, X, Y, Z (case-insensitive). I is
the identity and may be omitted; any qubit not named in a term is identity there.
"""
from __future__ import annotations

import json
import re

_PAULIS = ("I", "X", "Y", "Z")
_FACTOR_RE = re.compile(r"([IXYZ])\s*(\d+)", re.IGNORECASE)
_COEFF_RE = re.compile(r"^(\d*\.?\d+)")


class PauliDSLError(ValueError):
    """Raised when a Hamiltonian expression cannot be parsed."""


def parse_pauli_sum(spec: str, num_qubits_hint: int = 0):
    """Parse a Hamiltonian spec (text or JSON) into ``(terms, num_qubits)``.

    Args:
        spec: the compact-indexed text or JSON string.
        num_qubits_hint: register size; 0 means derive from the highest index.

    Returns:
        (terms, num_qubits) — see module docstring.

    Raises:
        PauliDSLError: on malformed input.
    """
    if spec is None or not spec.strip():
        raise PauliDSLError("Empty Hamiltonian expression.")
    stripped = spec.strip()
    if stripped[0] in "[{":
        return _parse_json(stripped, num_qubits_hint)
    return _parse_text(stripped, num_qubits_hint)


def _finalize(raw_terms, max_index, num_qubits_hint):
    n = num_qubits_hint if num_qubits_hint and num_qubits_hint > 0 else max_index + 1
    if n <= 0:
        n = 1
    bad = [t for t in raw_terms for idx in t[1] if idx >= n]
    if bad:
        raise PauliDSLError(
            "qubit index out of range for num_qubits={}: term {!r}".format(n, bad[0]))
    return raw_terms, n


def _parse_text(expr, num_qubits_hint):
    # Strip whitespace so 'X 0' and 'X0' parse identically, then split into
    # signed terms on top-level + / - .
    compact = re.sub(r"\s+", "", expr)
    term_strs = re.findall(r"[+-]?[^+-]+", compact)
    if not term_strs:
        raise PauliDSLError("No terms found in {!r}.".format(expr))

    terms = []
    max_index = -1
    for ts in term_strs:
        sign = 1.0
        if ts and ts[0] in "+-":
            sign = -1.0 if ts[0] == "-" else 1.0
            ts = ts[1:]
        if not ts:
            raise PauliDSLError("Dangling sign in {!r}.".format(expr))

        m = _COEFF_RE.match(ts)
        if m:
            coeff = float(m.group(1)) * sign
            rest = ts[m.end():]
        else:
            coeff = sign
            rest = ts

        paulis = ""
        indices = []
        consumed = 0
        for fm in _FACTOR_RE.finditer(rest):
            consumed += len(fm.group(0))
            p = fm.group(1).upper()
            idx = int(fm.group(2))
            max_index = max(max_index, idx)
            if p != "I":
                paulis += p
                indices.append(idx)
        if rest and consumed != len(rest):
            raise PauliDSLError(
                "Could not parse term {!r} (valid Paulis are I/X/Y/Z followed by "
                "a qubit index; use plain decimals for coefficients).".format(ts))
        terms.append((paulis, indices, coeff))

    return _finalize(terms, max_index, num_qubits_hint)


def _parse_json(text, num_qubits_hint):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PauliDSLError("Invalid JSON: {}".format(exc)) from exc

    if isinstance(data, dict):
        num_qubits_hint = num_qubits_hint or int(data.get("num_qubits", 0))
        entries = data.get("terms", [])
    elif isinstance(data, list):
        entries = data
    else:
        raise PauliDSLError("JSON must be a list of terms or an object with 'terms'.")

    terms = []
    max_index = -1
    for e in entries:
        if not isinstance(e, dict):
            raise PauliDSLError("Each JSON term must be an object, got {!r}.".format(e))
        pauli = str(e.get("pauli", "")).upper()
        qubits = list(e.get("qubits", []))
        coeff = float(e.get("coeff", 1.0))
        if len(pauli) != len(qubits):
            raise PauliDSLError(
                "term {!r}: 'pauli' length must match 'qubits' length.".format(e))
        keep_p, keep_i = "", []
        for p, idx in zip(pauli, qubits):
            if p not in _PAULIS:
                raise PauliDSLError("term {!r}: invalid Pauli '{}'.".format(e, p))
            idx = int(idx)
            max_index = max(max_index, idx)
            if p != "I":
                keep_p += p
                keep_i.append(idx)
        terms.append((keep_p, keep_i, coeff))

    if not terms:
        raise PauliDSLError("No terms in JSON Hamiltonian.")
    return _finalize(terms, max_index, num_qubits_hint)


# ---------------------------------------------------------------------------
# Wire format — the serialized Hamiltonian carried on the FlowFile content
# ---------------------------------------------------------------------------
# This is the framework-neutral on-the-wire form every ``<Framework>Hamiltonian``
# processor emits and every ``<Framework>VQE`` reads, so a Qiskit Hamiltonian can
# feed a Cirq solver and vice versa. It is dense Qiskit-style labels (MSB-first:
# the leftmost char is the highest-index qubit) plus a (real, imag) coefficient,
# identical to ``SparsePauliOp.to_list()`` output:
#
#     {"num_qubits": n, "terms": [["IZ", 1.0, 0.0], ["XX", 0.5, 0.0], ...]}

def _dense_label(pauli, indices, n):
    chars = ["I"] * n
    for p, idx in zip(pauli, indices):
        chars[n - 1 - idx] = p  # MSB-first, matching Qiskit's label convention
    return "".join(chars)


def terms_to_wire(terms, num_qubits):
    """Neutral ``(pauli, indices, coeff)`` terms -> the wire dict (see above)."""
    return {
        "num_qubits": int(num_qubits),
        "terms": [[_dense_label(p, idx, num_qubits), float(c), 0.0] for p, idx, c in terms],
    }


def wire_to_terms(data):
    """Wire dict -> ``(terms, num_qubits)`` neutral form.

    ``data`` may be the parsed dict or a JSON string. Coefficients are taken as
    their real part (Hamiltonians are Hermitian)."""
    if isinstance(data, str):
        data = json.loads(data)
    n = int(data["num_qubits"])
    out = []
    for label, re_, _im in data["terms"]:
        pauli, indices = "", []
        for pos, ch in enumerate(label):
            if ch != "I":
                pauli += ch
                indices.append(n - 1 - pos)
        out.append((pauli, indices, float(re_)))
    return out, n


# ---------------------------------------------------------------------------
# Diagonal (Ising) helpers — shared by the <Framework>QAOA solvers
# ---------------------------------------------------------------------------

def is_diagonal(terms):
    """True when every term is built from I/Z only (a classical cost function)."""
    return all(set(pauli) <= {"Z"} for pauli, _idx, _c in terms)


def diagonal_values(terms, num_qubits):
    """Evaluate a diagonal (I/Z-only) Pauli sum on every computational basis
    state.

    Returns a numpy array ``v`` of length ``2**num_qubits`` where ``v[k]`` is
    the energy of basis state ``k`` (bit ``i`` of ``k`` = qubit ``i``; bit set
    means the qubit is |1> and contributes a -1 eigenvalue to each Z on it).
    Used by the QAOA solvers to compute the exact optimum for the
    approximation ratio. Raises ``PauliDSLError`` for non-diagonal terms or
    registers too large to enumerate.
    """
    import numpy as np

    if not is_diagonal(terms):
        raise PauliDSLError("Hamiltonian is not diagonal (contains X/Y terms).")
    if num_qubits > 22:
        raise PauliDSLError(
            "register too large to enumerate exactly ({} qubits).".format(num_qubits))
    states = np.arange(2 ** num_qubits, dtype=np.int64)
    values = np.zeros(2 ** num_qubits, dtype=np.float64)
    for pauli, indices, coeff in terms:
        contrib = np.full(values.shape, float(coeff))
        for idx in indices:
            contrib *= 1.0 - 2.0 * ((states >> idx) & 1)
        values += contrib
    return values
