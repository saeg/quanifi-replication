#!/usr/bin/env python3
"""Pin the Grover hardware-matrix mutant ladder (PREREG-grover-hw-matrix.md,
Amendment A2 -- supersedes the original §5 seed search below).

Stage 3 asks whether a mutation signal survives device error on real
hardware. That question needs mutants of *known* effect size, measured on a
noiseless simulator first, pinned to ``experiments/grover_hw_mutants.json``
before any armed hardware run consumes it.

--------------------------------------------------------------------------
History: why this is a ladder, not a seed search (PREREG §5 -> A2)
--------------------------------------------------------------------------
The original design (§5) searched seeds 11, 12, 13, ... for a ``gate.remove``
mutant at ``Mutation Locus = "middle"`` whose simulator delta reached 0.10.
Two things were measured while implementing it:

1. **With a pinned Mutation Locus, QuantumMutator ignores Mutation Seed.**
   ``_LocusChooser`` (nifi_extensions/QuantumMutator.py:123-161) replaces the
   RNG with a quantile pick by design, so ``build_mutant(control, 11)`` and
   ``build_mutant(control, 20)`` are byte-identical for a single-operator
   list. The seed-advance loop searched only the simulator's shot-sampling
   noise around one fixed mutant, never a different mutant.
2. **``gate.remove`` at ``middle`` is catastrophic on Grover, not marginal.**
   All twelve (builder, case) cells qualified at the very first seed
   searched, with delta_sim between 0.51 and 1.00. A catastrophic-only
   mutant set cannot answer "how small a fault survives device noise?".

Amendment A2 replaces the seed search with a **ladder of target effect
sizes** per 4-qubit cell, built with ``rotation.perturb`` (whose
``Rotation Epsilon`` *is* a genuine, seed-independent tuning knob) plus the
original ``gate.remove`` mutant kept as the ladder's top rung:

    Rung     Target delta_sim   Operator
    small    0.10                rotation.perturb, epsilon searched
    medium   0.25                rotation.perturb, epsilon searched
    large    (not targeted)      gate.remove, locus middle

2-qubit cells carry the ``large`` rung only (PREREG §4: they are calibration,
not a builder/detectability comparison).

Locus is ``middle`` throughout -- including for ``rotation.perturb``, where
it plays the same role: the candidate rotation gate is picked once,
deterministically, by quantile, and only ``Rotation Epsilon`` is searched.

Epsilon search is a bisection on [0.05, 3.0], 24 iterations, against
QiskitAerSimulator at 4096 shots (not 1024 -- this is measuring an effect
size, not simulating the hardware batch) with Random Seed = 11 held fixed.
The epsilon whose achieved delta_sim is closest to the rung's target is kept;
the ACHIEVED delta_sim is what is recorded, never the target. A rung whose
achieved delta_sim misses its target by more than 0.03 is marked
``off_target: true`` and is still kept, with its achieved value.

The ``large`` rung keeps the original §5 measurement exactly: gate.remove at
seed 11, 1024 shots (recomputed here rather than hard-coded, but the code
path and inputs are unchanged, so the numbers are identical).

Processors are loaded from ``nifi_extensions/`` by file path via importlib
(the pattern in experiments/version_matrix_study.py), driven through the
MockContext/MockFlowFile harness in experiments/_harness.py -- identical code
paths to the canvas, no NiFi/JVM required, nothing reaches a provider.

Determinism: every measurement is seeded (Random Seed = 11, fixed) and every
iteration order is a fixed literal list, so two runs produce byte-identical
JSON apart from the ``generated_utc`` provenance field. Bisection over a
seeded simulator is itself deterministic -- the same sequence of epsilons is
evaluated every run.

Usage:
  .venv/bin/python experiments/grover_mutant_seeds.py
"""
import datetime
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import MockContext, MockFlowFile, ROOT  # noqa: E402

EXT_DIR = ROOT / "nifi_extensions"

#: builders = qiskit, pennylane, cirq, in the order given in the work-package
#: spec. (display name, processor module/class name)
BUILDERS = [
    ("qiskit", "QiskitGroverCircuit"),
    ("pennylane", "PennylaneGroverCircuit"),
    ("cirq", "CirqGroverCircuit"),
]

#: Mirrors tools/add_grover_hw_matrix_group.py CASES: (marked state,
#: iterations, partition label, num_qubits).
CASES = [
    ("10", 1, "asymmetric-2q", 2),
    ("11", 1, "palindrome-2q", 2),
    ("0111", 2, "asymmetric-4q", 4),
    ("0110", 2, "palindrome-4q", 4),
]

LOCUS = "middle"
SEED = 11  # fixed measurement seed -- no longer a search variable (A2 finding 1)

#: gate.remove/large rung: unchanged from the original §5 measurement.
LARGE_RUNG_SHOTS = 1024

#: rotation.perturb epsilon search (A2 §"Epsilon search is a bisection...").
EPSILON_SEARCH_SHOTS = 4096
EPSILON_BOUNDS = (0.05, 3.0)
EPSILON_MAX_ITERATIONS = 24
OFF_TARGET_TOLERANCE = 0.03

#: The ladder. (rung name, operator, target delta_sim or None if untargeted).
RUNGS_4Q = [
    ("small", "rotation.perturb", 0.10),
    ("medium", "rotation.perturb", 0.25),
    ("large", "gate.remove", None),
]
RUNGS_2Q = [
    ("large", "gate.remove", None),
]

OUT_PATH = ROOT / "experiments" / "grover_hw_mutants.json"


def _load(name):
    """Load a processor class from nifi_extensions/ by file path.

    Mirrors experiments/version_matrix_study.py's load_builder/load_engine:
    NiFi loads a processor module by file path (not by package import), so a
    processor cannot import a sibling module at module scope, and this
    script must load them the same way to exercise identical code paths.
    """
    key = "grover_mutant_seeds_ext_" + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, EXT_DIR / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return getattr(sys.modules[key], name)


def build_control(builder_module, marked_state, iterations):
    """Build one control circuit. Returns qasm2 bytes."""
    cls = _load(builder_module)
    res = cls().transform(
        MockContext(**{
            "Marked State": marked_state,
            "Num Iterations": str(iterations),
            "Output Format": "qasm2",
        }),
        MockFlowFile(),
    )
    if res.relationship != "success":
        raise RuntimeError(
            f"{builder_module}: control build failed for marked_state="
            f"{marked_state!r}: {res.attributes.get('grover.error')}"
        )
    return res.contents


def build_mutant(control_qasm2, operator, seed=SEED, epsilon=None):
    """Pass the control's qasm2 through QuantumMutator. Returns (qasm2, attrs).

    Locus is always "middle" (module constant LOCUS) -- see the module
    docstring for why the seed plays no part in which gate is chosen once
    the locus is pinned.
    """
    cls = _load("QuantumMutator")
    props = {
        "Mutation Operators": operator,
        "Mutation Locus": LOCUS,
        "Mutation Seed": str(seed),
    }
    if epsilon is not None:
        props["Rotation Epsilon"] = str(epsilon)
    ff = MockFlowFile(content=control_qasm2, attributes={"circuit.format": "qasm2"})
    res = cls().transform(MockContext(**props), ff)
    if res.relationship != "success":
        raise RuntimeError(
            f"QuantumMutator failed (operator={operator}, epsilon={epsilon}): "
            f"{res.attributes.get('mut.error')}"
        )
    return res.contents, res.attributes


def sample(qasm2, seed, shots):
    """Run one qasm2 circuit on the repo's QiskitAerSimulator. Returns counts dict."""
    cls = _load("QiskitAerSimulator")
    ff = MockFlowFile(content=qasm2, attributes={"circuit.format": "qasm2"})
    res = cls().transform(
        MockContext(**{"Shots": str(shots), "Random Seed": str(seed)}),
        ff,
    )
    if res.relationship != "success":
        raise RuntimeError(
            f"QiskitAerSimulator failed at seed {seed}: {res.attributes.get('sim.error')}"
        )
    return json.loads(res.contents.decode("utf-8"))


def success_probability(counts, marked_state, shots):
    """Probability of the marked state, canonical q0-left order.

    QiskitAerSimulator already normalises its counts keys to
    sim.bit_order = "q0_left" (see nifi_extensions/QiskitAerSimulator.py), the
    same convention the builders' "Marked State" property uses, so the
    marked-state string indexes the counts dict directly.
    """
    return counts.get(marked_state, 0) / shots


def bisect_epsilon(control_qasm, marked_state, p_control, target):
    """Search Rotation Epsilon in EPSILON_BOUNDS for delta_sim closest to target.

    Standard bisection, always run to EPSILON_MAX_ITERATIONS (no early exit,
    so the sequence of evaluated points -- and therefore the result -- is
    identical on every run regardless of how quickly it appears to converge).
    Both endpoints are evaluated once up front, then each iteration evaluates
    the current midpoint and narrows toward the target assuming delta_sim is
    increasing in epsilon (measured true here and in A2's own table); the
    accepted point is the one **closest to target among every point
    evaluated** (not necessarily the final midpoint), which is what makes
    this robust to any shot-noise-driven non-monotonicity.

    Returns a dict: epsilon, delta_sim (achieved), off_target, bounds,
    mutant_attrs (of the accepted point).
    """
    lo, hi = EPSILON_BOUNDS
    evaluated = []  # (epsilon, delta_sim, mutant_attrs), in evaluation order

    def evaluate(eps):
        mutant_qasm, mutant_attrs = build_mutant(
            control_qasm, "rotation.perturb", seed=SEED, epsilon=eps
        )
        p_mutant = success_probability(
            sample(mutant_qasm, SEED, EPSILON_SEARCH_SHOTS),
            marked_state, EPSILON_SEARCH_SHOTS,
        )
        achieved = p_control - p_mutant
        evaluated.append((eps, achieved, mutant_attrs))
        return achieved

    delta_lo = evaluate(lo)
    delta_hi = evaluate(hi)
    for _ in range(EPSILON_MAX_ITERATIONS):
        mid = (lo + hi) / 2
        delta_mid = evaluate(mid)
        if delta_mid < target:
            lo = mid
        else:
            hi = mid

    best_eps, best_delta, best_attrs = min(
        evaluated, key=lambda item: abs(item[1] - target)
    )
    off_target = abs(best_delta - target) > OFF_TARGET_TOLERANCE
    return {
        "epsilon": best_eps,
        "delta_sim": best_delta,
        "off_target": off_target,
        "bounds": list(EPSILON_BOUNDS),
        "mutant_attrs": best_attrs,
    }


def build_rung_entry(builder_name, builder_module, marked_state, iterations,
                      partition, num_qubits, control_qasm, rung, operator, target):
    """One ladder entry: builds the mutant, measures it, and shapes the row."""
    if operator == "gate.remove":
        shots = LARGE_RUNG_SHOTS
        p_control = success_probability(
            sample(control_qasm, SEED, shots), marked_state, shots
        )
        mutant_qasm, mutant_attrs = build_mutant(control_qasm, operator, seed=SEED)
        p_mutant = success_probability(
            sample(mutant_qasm, SEED, shots), marked_state, shots
        )
        delta_sim = p_control - p_mutant
        entry = {
            "epsilon": None,
            "target_delta": None,
            "delta_sim": delta_sim,
            "off_target": None,
            "bisection_bounds": None,
            "shots": shots,
        }
    elif operator == "rotation.perturb":
        shots = EPSILON_SEARCH_SHOTS
        p_control = success_probability(
            sample(control_qasm, SEED, shots), marked_state, shots
        )
        result = bisect_epsilon(control_qasm, marked_state, p_control, target)
        mutant_attrs = result["mutant_attrs"]
        entry = {
            "epsilon": result["epsilon"],
            "target_delta": target,
            "delta_sim": result["delta_sim"],
            "off_target": result["off_target"],
            "bisection_bounds": result["bounds"],
            "shots": shots,
        }
    else:
        raise ValueError(f"unknown operator {operator!r}")

    entry.update({
        "builder": builder_name,
        "case": marked_state,
        "partition": partition,
        "num_qubits": num_qubits,
        "iterations": iterations,
        "rung": rung,
        "operator": operator,
        "locus": LOCUS,
        "seed": SEED,
        "mutant_gate": mutant_attrs.get("mut.gate"),
        "mutant_position": int(mutant_attrs.get("mut.position")),
        "mutant_candidate_count": int(mutant_attrs.get("mut.candidate_count")),
    })
    return entry


def build_selection():
    """The deterministic ladder: one entry per (builder, case, rung).

    Contains no wall-clock or dict-ordering dependence -- iteration is over
    the fixed BUILDERS x CASES x rung literal lists in order, so two calls to
    this function always return an identical structure.
    """
    mutants = []
    for builder_name, builder_module in BUILDERS:
        for marked_state, iterations, partition, num_qubits in CASES:
            control_qasm = build_control(builder_module, marked_state, iterations)
            rungs = RUNGS_4Q if num_qubits == 4 else RUNGS_2Q
            for rung, operator, target in rungs:
                mutants.append(build_rung_entry(
                    builder_name, builder_module, marked_state, iterations,
                    partition, num_qubits, control_qasm, rung, operator, target,
                ))
    return {"mutants": mutants}


def build_output():
    """The full JSON payload: provenance block + the deterministic ladder."""
    selection = build_selection()
    import qiskit
    provenance = {
        "locus": LOCUS,
        "seed": SEED,
        "large_rung_shots": LARGE_RUNG_SHOTS,
        "epsilon_search_shots": EPSILON_SEARCH_SHOTS,
        "epsilon_bounds": list(EPSILON_BOUNDS),
        "epsilon_max_iterations": EPSILON_MAX_ITERATIONS,
        "off_target_tolerance": OFF_TARGET_TOLERANCE,
        "rung_targets": {"small": 0.10, "medium": 0.25, "large": None},
        "qiskit_version": qiskit.__version__,
        # The only non-deterministic field. Selection above does not depend
        # on it.
        "generated_utc": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return {"provenance": provenance, **selection}


def main():
    output = build_output()
    OUT_PATH.write_text(json.dumps(output, indent=2) + "\n")
    print(f"wrote {OUT_PATH}")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
