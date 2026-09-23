"""
Shared preparation for hardware batch submission.

Both batch submitters buffer K circuits, search a physical layout using the
first one, and pin that layout for the whole job. That works only if every
circuit in the batch has the same width. The arithmetic adders do not: at 2-bit
operands they span 5 to 8 qubits, and ``transpile`` refuses the mismatch with

    InvalidLayoutError: The length of the layout is different than the size of
    the circuit: 5 <> 6

Padding every circuit up to the batch maximum fixes it. The alternative,
grouping by width and submitting one job per group, would put each group on a
different set of physical qubits, and then any measured difference between two
implementations would mix algorithm quality with qubit quality. Since the whole
point of a differential experiment is that the only difference between branches
is the code, that is the wrong trade. One job, one layout, same physical qubits,
same calibration drift for everyone.

Padding qubits are widened into the register but deliberately **not measured**.
Measuring idle lines would charge the narrow implementations readout error on
qubits their specification never mentions, and would change the width of the
counts keys, invalidating the ``arithmetic.result_qubits`` indices that the
oracle marginalises on. Measuring only the original qubits keeps both intact.

No framework import at module scope: NiFi installs a processor's dependencies
per processor, and this module is imported by processors on two different
provider lanes.
"""

# stdlib only -- the deferred-import rule above is about framework packages.
import os


LAYOUT_ATTEMPT_LIMIT = 3
RETRY_SEQUENCE_STRATEGY = "ranked-distinct-physical-sets-v1"
LAYOUT_SCREEN_CANDIDATE_LIMIT = 5
LAYOUT_SCREEN_STRATEGY = "result-aware-diverse-five-v1"


def widen(circuit, target_qubits):
    """Return ``circuit`` on a ``target_qubits``-wide register, unmeasured lines idle.

    Returns the circuit unchanged (a copy) when it is already wide enough.
    """
    from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister  # noqa: PLC0415

    if circuit.num_qubits > target_qubits:
        raise ValueError(
            "circuit has %d qubits, cannot widen to %d"
            % (circuit.num_qubits, target_qubits))
    if circuit.num_qubits == target_qubits:
        return circuit.copy()

    wider = QuantumCircuit(QuantumRegister(target_qubits, "q"))
    for register in circuit.cregs:
        wider.add_register(ClassicalRegister(len(register), register.name))
    wider.compose(circuit,
                  qubits=list(range(circuit.num_qubits)),
                  clbits=list(range(len(circuit.clbits))),
                  inplace=True)
    return wider


def pad_batch(circuits):
    """Widen a batch to a common width and measure each circuit's own qubits.

    Returns ``(padded, widths, target)`` where ``widths[i]`` is circuit *i*'s
    original qubit count and ``target`` is the common width. A batch that is
    already uniform and already measured comes back untouched, which is what
    keeps the existing GHZ lanes byte-identical.
    """
    from qiskit import ClassicalRegister  # noqa: PLC0415

    widths = [c.num_qubits for c in circuits]
    target = max(widths) if widths else 0

    padded = []
    for circuit, width in zip(circuits, widths):
        wider = widen(circuit, target)
        if not wider.clbits:
            # Measure the ORIGINAL qubits only. measure_all() would add the
            # padding lines, which is the whole thing this module avoids.
            creg = ClassicalRegister(width, "meas")
            wider.add_register(creg)
            wider.barrier(range(width))
            for index in range(width):
                wider.measure(index, creg[index])
        padded.append(wider)
    return padded, widths, target


def circuits_from_qasm(qasms):
    """Parse a batch of OpenQASM strings, leaving measurement to :func:`pad_batch`.

    ``QuantumCircuit.from_qasm_str`` was removed in Qiskit 2.x; ``qasm2.loads``
    is the supported API (and exists in 1.x as well). The legacy custom
    instructions resolve the standard ``qelib1.inc`` gate library.
    """
    from qiskit import qasm2  # noqa: PLC0415

    return [
        qasm2.loads(qasm, custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
        for qasm in qasms
    ]


def captured_attributes(attributes, prefix):
    """The subset of a FlowFile's attributes a batch entry should carry.

    A batch manifest that does not carry ground truth produces a result nobody
    can score. Rather than hardcoding the arithmetic attribute names here, the
    submitter names a prefix, so the same mechanism serves any experiment whose
    circuits travel with their expected answer.
    """
    if not prefix:
        return {}
    return {k: v for k, v in attributes.items() if k.startswith(prefix)}


def layout_probe(circuits):
    """The circuit a batch's layout should be searched on.

    After :func:`pad_batch` every circuit is the same *width*, but padding adds
    no gates -- a 6-qubit adder widened to 9 still entangles only 6, and the
    transpiler is free to put the three idle lines anywhere because they cost it
    nothing. Search the layout on such a circuit and the qubits it ignores get
    placed arbitrarily; a version that actually uses all nine then inherits that
    arbitrary placement for exactly the qubits it depends on.

    Measured on IQM garnet (20 qubits, so no slack to absorb a bad placement),
    with the layout searched on `cdkm` -- which is simply whichever circuit the
    kind-interleave happened to put first:

        cdkm      2q=49   depth=82     (6 of 9 qubits active)
        qft       2q=35   depth=63     (5 of 9)
        outadder  2q=88   depth=162    (9 of 9)

    and with it searched on `outadder` instead:

        cdkm      2q=49   depth=87
        qft       2q=32   depth=62
        outadder  2q=58   depth=93

    outadder pays 88 two-qubit gates instead of 58 and 162 depth instead of 93,
    while the other two are unchanged or better -- 172 two-qubit gates across the
    batch against 139. That is why `outadder` failed IQM calibration on
    2026-08-29 (24/28 repeated modes, aggregate modes wrong) while being perfect
    on IBM, where 156 qubits leave enough good silicon that even an arbitrary
    placement lands somewhere usable.

    So: probe with the circuit that has the most two-qubit gates. It is the one
    with the least freedom, and the others have slack to absorb its choice.
    """
    return max(circuits,
               key=lambda c: sum(i.operation.num_qubits == 2 for i in c.data))


def layout_for(circuit, backend, seeds):
    """Search ``seeds`` transpiler seeds and return (two_qubit_gates, layout).

    Shared so both provider lanes pick a layout the same way. Pass the circuit
    :func:`layout_probe` chooses, not an arbitrary member of the batch.
    """
    from qiskit import transpile  # noqa: PLC0415

    best = None
    for seed in range(max(1, seeds)):
        candidate = transpile(circuit, backend=backend,
                              optimization_level=3, seed_transpiler=seed)
        two_q = sum(i.operation.num_qubits == 2 for i in candidate.data)
        if best is None or two_q < best[0]:
            best = (two_q, candidate)
    layout = [int(best[1].layout.initial_layout[q]) for q in circuit.qubits]
    return best[0], layout


def layout_representatives(circuits, widths=None):
    """One demanding circuit for each active width in a mixed-width batch.

    The three arithmetic implementations use different numbers of wires.  After
    padding they have one physical width, but their active widths remain visible
    as the number of qubits touched by operations.  Keeping the most two-qubit-
    intensive circuit at each active width gives the layout search one CDKM,
    one QFT and one SemiAdder-shaped representative without depending on labels
    or on mutation outcomes.
    """
    chosen = {}
    widths = list(widths) if widths is not None else [None] * len(circuits)
    if len(widths) != len(circuits):
        raise ValueError("width metadata must align with circuits")
    for circuit, original_width in zip(circuits, widths):
        active = {q for instruction in circuit.data for q in instruction.qubits}
        width = int(original_width) if original_width is not None else len(active)
        cost = sum(instruction.operation.num_qubits == 2
                   for instruction in circuit.data)
        depth = circuit.depth()
        if width not in chosen or (cost, depth) > chosen[width][0]:
            chosen[width] = ((cost, depth), circuit)
    return [chosen[width][1] for width in sorted(chosen)]


def _transpiled_cost(circuit, backend, layout, seed=11):
    from qiskit import transpile  # noqa: PLC0415

    candidate = transpile(circuit, backend=backend, optimization_level=3,
                          initial_layout=layout, seed_transpiler=seed)
    return {
        "two_qubit_gates": sum(i.operation.num_qubits == 2
                               for i in candidate.data),
        "depth": int(candidate.depth()),
    }


def final_measurement_map(circuit):
    """Return classical-bit index -> final physical measurement qubit.

    A pinned ``initial_layout`` is only where a logical wire starts. Routing can
    move its state before measurement, independently for every implementation.
    The transpiled measurement instructions are therefore the only safe source
    for deciding which physical qubits carry ``arithmetic.result_qubits``.
    """
    mapping = {}
    for instruction in circuit.data:
        if instruction.operation.name != "measure":
            continue
        if len(instruction.qubits) != 1 or len(instruction.clbits) != 1:
            continue
        classical = int(circuit.find_bit(instruction.clbits[0]).index)
        physical = int(circuit.find_bit(instruction.qubits[0]).index)
        mapping[classical] = physical
    return mapping


def _metadata_result_qubits(metadata):
    raw = (metadata or {}).get("arithmetic.result_qubits", "")
    return [int(value) for value in str(raw).split(",") if value.strip()]


def _measure_error(backend, physical):
    """Provider-reported assignment error for one qubit, if available."""
    target = getattr(backend, "target", None)
    if target is None:
        return None
    for operation in ("measure", "measure_2"):
        try:
            properties = target[operation].get((int(physical),))
        except (KeyError, AttributeError, TypeError):
            properties = None
        if properties is not None:
            return _safe_number(getattr(properties, "error", None))
    return None


def _profile_transpiled(circuit, backend, metadata=None):
    """Compact execution profile used by the result-aware layout screen."""
    two_q = sum(instruction.operation.num_qubits == 2
                for instruction in circuit.data)
    active = sorted({int(circuit.find_bit(qubit).index)
                     for instruction in circuit.data
                     if instruction.operation.name not in ("barrier", "delay")
                     for qubit in instruction.qubits})
    measured = final_measurement_map(circuit)
    result_logical = _metadata_result_qubits(metadata)
    result_physical = [measured.get(index) for index in result_logical]
    errors = [_measure_error(backend, physical)
              for physical in result_physical if physical is not None]
    return {
        "two_qubit_gates": int(two_q),
        "depth": int(circuit.depth()),
        "active_physical": active,
        "measurement_map": {str(k): int(v) for k, v in sorted(measured.items())},
        "result_logical": result_logical,
        "result_physical": result_physical,
        "result_mapping_complete": bool(result_logical) and all(
            physical is not None for physical in result_physical),
        "result_measure_errors": errors,
    }


def _transpiled_profiles(circuits, backend, layout, metadata=None, seed=11):
    from qiskit import transpile  # noqa: PLC0415

    metadata = list(metadata) if metadata is not None else [{} for _ in circuits]
    if len(metadata) != len(circuits):
        raise ValueError("circuit metadata must align with circuits")
    transpiled = transpile(circuits, backend=backend, optimization_level=3,
                           initial_layout=layout, seed_transpiler=seed)
    return [_profile_transpiled(circuit, backend, attrs)
            for circuit, attrs in zip(transpiled, metadata)]


def _result_profile(profiles, metadata, excluded_physical_qubits=()):
    """Summarise final output placement over every clean campaign circuit."""
    by_version, all_result, active = {}, set(), set()
    errors, missing = [], 0
    for profile, attrs in zip(profiles, metadata):
        version = str((attrs or {}).get("arithmetic.implementation", "unknown"))
        placement = tuple(profile["result_physical"])
        by_version.setdefault(version, set()).add(placement)
        active.update(profile["active_physical"])
        for physical in placement:
            if physical is not None:
                all_result.add(int(physical))
        for error in profile["result_measure_errors"]:
            if isinstance(error, (int, float)):
                errors.append(float(error))
            else:
                missing += 1
        if not profile["result_mapping_complete"]:
            missing += 1
    excluded = {int(q) for q in excluded_physical_qubits}
    return {
        "result_physical_by_implementation": {
            version: [list(placement) for placement in sorted(placements)]
            for version, placements in sorted(by_version.items())},
        "result_physical": sorted(all_result),
        "active_physical": sorted(active),
        "result_measure_error_missing": int(missing),
        "result_measure_error_worst": max(errors) if errors else None,
        "result_measure_error_sum": sum(errors) if errors else None,
        "excluded_result_hits": sorted(all_result & excluded),
        "excluded_active_hits": sorted(active & excluded),
    }


def _screen_candidate_sequence(evaluated, prior_layouts=(),
                               limit=LAYOUT_SCREEN_CANDIDATE_LIMIT):
    """Choose a bounded, physically diverse clean-screen sequence.

    Known-bad physical qubits are a hard exclusion before ranking. Structural
    cost remains bounded to 80% above the best normalized overhead and 50%
    above the best worst-case absolute two-qubit cost. Those deliberately loose
    ceilings admit different chip regions without accepting unbounded routing;
    final result quality and physical distance then decide the order. Hardware outcomes
    do not enter this function; prior failed *regions* enter only as sets whose
    overlap should be minimized.
    """
    eligible = [row for row in evaluated
                if not row.get("excluded_active_hits") and
                not row.get("excluded_result_hits") and
                row.get("result_mapping_complete", True)]
    if not eligible:
        raise ValueError("layout search found no candidate outside excluded qubits")
    best_ratio = min(float(row["objective"][0]) for row in eligible)
    best_worst_two_q = min(float(row["objective"][1]) for row in eligible)
    bounded = [row for row in eligible
               if float(row["objective"][0]) <= 1.80 * best_ratio and
               float(row["objective"][1]) <= 1.50 * best_worst_two_q]
    prior_sets = [set(int(q) for q in layout) for layout in prior_layouts]

    def quality(row):
        missing = int(row.get("result_measure_error_missing", 0))
        worst = row.get("result_measure_error_worst")
        total = row.get("result_measure_error_sum")
        return (missing,
                float(worst) if isinstance(worst, (int, float)) else 1.0,
                float(total) if isinstance(total, (int, float)) else 1.0,
                tuple(row["objective"]), tuple(row["layout"]))

    selected, remaining = [], list(bounded)
    while remaining and len(selected) < max(1, int(limit)):
        def selection_key(row):
            physical = set(int(q) for q in row["physical_qubits"])
            comparisons = prior_sets + [set(item["physical_qubits"])
                                        for item in selected]
            maximum_overlap = max((len(physical & other)
                                   for other in comparisons), default=0)
            return (maximum_overlap,) + quality(row)

        winner = min(remaining, key=selection_key)
        physical = set(int(q) for q in winner["physical_qubits"])
        comparisons = prior_sets + [set(item["physical_qubits"])
                                    for item in selected]
        row = dict(winner)
        row.update({
            "screen_order": len(selected) + 1,
            "max_physical_overlap_with_prior": max(
                (len(physical & other) for other in prior_sets), default=0),
            "max_physical_overlap_with_selected": max(
                (len(physical & set(item["physical_qubits"]))
                 for item in selected), default=0),
        })
        selected.append(row)
        remaining.remove(winner)
    if len(selected) < max(1, int(limit)):
        raise ValueError("only %d structurally eligible diverse candidates; need %d"
                         % (len(selected), int(limit)))
    return selected


def _candidate_sequence(evaluated, limit=LAYOUT_ATTEMPT_LIMIT):
    """Freeze the best candidates on distinct physical-qubit sets.

    Several transpiler seeds commonly return permutations of the same physical
    region.  Treating those as independent retry candidates would send a failed
    qualification straight back to the same hardware.  Preserve the existing
    objective order, but retain only the first layout on each distinct physical
    set.  The original rank is recorded so the choice remains auditable.
    """
    ranked = sorted(evaluated, key=lambda row: (tuple(row["objective"]),
                                                tuple(row["layout"])))
    selected, seen = [], set()
    for rank, row in enumerate(ranked, 1):
        physical_set = tuple(sorted(int(q) for q in row["layout"]))
        if physical_set in seen:
            continue
        seen.add(physical_set)
        prior_sets = [set(candidate["physical_qubits"]) for candidate in selected]
        overlap = max((len(set(physical_set) & prior) for prior in prior_sets),
                      default=0)
        selected.append({
            "attempt": len(selected) + 1,
            "search_rank": rank,
            "layout": [int(q) for q in row["layout"]],
            "physical_qubits": list(physical_set),
            "max_physical_overlap_with_prior": overlap,
            "objective": list(row["objective"]),
            "representatives": row["representatives"],
            "source": row["source"],
        })
        if len(selected) >= max(1, int(limit)):
            break
    return selected


def _safe_number(value):
    """Turn provider numeric types into stable JSON scalars."""
    import math

    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return number if math.isfinite(number) else str(value)


def backend_snapshot(backend, layouts):
    """Compact calibration/topology evidence for the frozen layout sequence.

    Only properties touching a candidate qubit are retained; the full coupling
    map is included because it explains routing outside the initial placement.
    The digest excludes ``captured_at`` so identical provider data has an
    identical identity even when inspected twice.
    """
    import datetime
    import hashlib
    import importlib.metadata
    import json
    import platform

    def package_version(name):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    name = getattr(backend, "name", "")
    if callable(name):
        name = name()
    relevant = sorted({int(q) for layout in layouts for q in layout})
    coupling = getattr(backend, "coupling_map", None)
    edges = []
    if coupling is not None:
        try:
            edges = sorted([int(a), int(b)] for a, b in coupling.get_edges())
        except (AttributeError, TypeError, ValueError):
            edges = []

    calibrations = []
    target = getattr(backend, "target", None)
    if target is not None:
        for operation in sorted(getattr(target, "operation_names", ())):
            try:
                mapping = target[operation]
                items = mapping.items()
            except (KeyError, AttributeError, TypeError):
                continue
            for qargs, properties in items:
                if not qargs:
                    continue
                qargs = tuple(int(q) for q in qargs)
                if len(qargs) > 2 or not all(q in relevant for q in qargs):
                    continue
                calibrations.append({
                    "operation": str(operation),
                    "qubits": list(qargs),
                    "error": _safe_number(getattr(properties, "error", None)),
                    "duration_seconds": _safe_number(
                        getattr(properties, "duration", None)),
                })

    payload = {
        "device": str(name),
        "num_qubits": int(getattr(backend, "num_qubits", 0) or 0),
        "dt_seconds": _safe_number(getattr(backend, "dt", None)),
        "candidate_qubits": relevant,
        "coupling_edges": edges,
        "candidate_calibrations": calibrations,
        "software": {
            "python": platform.python_version(),
            "qiskit": package_version("qiskit"),
            "qiskit_ibm_runtime": package_version("qiskit-ibm-runtime"),
            "iqm_client": package_version("iqm-client"),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    return {
        "schema": "quanifi-backend-layout-snapshot/v1",
        "captured_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        "snapshot_sha256": hashlib.sha256(encoded).hexdigest(),
        "snapshot": payload,
    }


def balanced_layout_for(circuits, backend, seeds, widths=None, metadata=None,
                        excluded_physical_qubits=(), prior_layouts=(),
                        screen_candidate_limit=LAYOUT_SCREEN_CANDIDATE_LIMIT):
    """Choose one layout fairly across every implementation shape.

    Candidate layouts are proposed by every representative and transpiler seed.
    Each candidate is then evaluated on *all* representatives.  Ranking minimizes
    the worst two-qubit overhead relative to that representative's best observed
    candidate, followed by worst absolute two-qubit cost, total two-qubit cost,
    worst depth and total depth.  No hardware results or mutant outcomes enter
    the objective, so the selection can be preregistered before data collection.

    Returns ``(worst_two_q, layout, evidence)``.  Evidence is JSON-serializable
    and records the full winning objective and per-representative costs.
    """
    from qiskit import transpile  # noqa: PLC0415

    reps = layout_representatives(circuits, widths)
    representative_widths = (sorted(set(int(width) for width in widths))
                             if widths is not None else [
                                 len({q for instruction in rep.data
                                      for q in instruction.qubits}) for rep in reps])
    if not reps:
        raise ValueError("cannot search a layout for an empty circuit batch")
    candidates = {}
    for rep_index, circuit in enumerate(reps):
        for seed in range(max(1, int(seeds))):
            proposed = transpile(circuit, backend=backend, optimization_level=3,
                                 seed_transpiler=seed)
            layout = tuple(int(proposed.layout.initial_layout[q])
                           for q in circuit.qubits)
            candidates.setdefault(layout, {"source_rep": rep_index,
                                            "source_seed": seed})

    # Locate the representatives in the original list so their cost can be
    # taken from the same all-circuit transpilation that supplies final output
    # mappings. Object identity is preserved by ``layout_representatives``.
    representative_indices = [next(i for i, item in enumerate(circuits)
                                   if item is rep) for rep in reps]
    evaluated = []
    for layout, source in candidates.items():
        if metadata is None:
            costs = [_transpiled_cost(rep, backend, list(layout)) for rep in reps]
            evaluated.append({"layout": list(layout), "source": source,
                              "representatives": costs,
                              "physical_qubits": sorted(int(q) for q in layout)})
        else:
            profiles = _transpiled_profiles(circuits, backend, list(layout), metadata)
            costs = [{"two_qubit_gates": profiles[index]["two_qubit_gates"],
                      "depth": profiles[index]["depth"]}
                     for index in representative_indices]
            result = _result_profile(profiles, metadata,
                                     excluded_physical_qubits)
            evaluated.append({"layout": list(layout), "source": source,
                              "representatives": costs,
                              "physical_qubits": sorted(int(q) for q in layout),
                              "result_mapping_complete": all(
                                  profile["result_mapping_complete"]
                                  for profile in profiles),
                              **result})
    best_two_q = [min(row["representatives"][i]["two_qubit_gates"]
                      for row in evaluated) for i in range(len(reps))]
    for row in evaluated:
        costs = row["representatives"]
        ratios = [cost["two_qubit_gates"] / max(1, floor)
                  for cost, floor in zip(costs, best_two_q)]
        row["objective"] = [max(ratios),
                            max(c["two_qubit_gates"] for c in costs),
                            sum(c["two_qubit_gates"] for c in costs),
                            max(c["depth"] for c in costs),
                            sum(c["depth"] for c in costs)]
    if metadata is None:
        sequence = _candidate_sequence(evaluated)
        strategy = "balanced-worst-normalized-two-qubit-v1"
        retry_strategy = RETRY_SEQUENCE_STRATEGY
    else:
        sequence = _screen_candidate_sequence(
            evaluated, prior_layouts=prior_layouts,
            limit=screen_candidate_limit)
        strategy = LAYOUT_SCREEN_STRATEGY
        retry_strategy = "bounded-clean-screen-five-v1"
    if not sequence:
        raise ValueError("layout search produced no retry candidates")
    winner = sequence[0]
    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415
    sequence_sha256 = hashlib.sha256(json.dumps(
        sequence, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    evidence = {
        "strategy": strategy,
        "retry_sequence_strategy": retry_strategy,
        "attempt_limit": (LAYOUT_ATTEMPT_LIMIT if metadata is None
                          else int(screen_candidate_limit)),
        "candidate_count": len(evaluated),
        "distinct_physical_set_count": len({
            tuple(sorted(row["layout"])) for row in evaluated}),
        "representative_count": len(reps),
        "representative_active_widths": representative_widths,
        "best_observed_two_qubit_gates": best_two_q,
        "winning_objective": winner["objective"],
        "winning_representatives": winner["representatives"],
        "winning_source": winner["source"],
        "candidate_sequence_sha256": sequence_sha256,
        "candidate_sequence": sequence,
        "backend_provenance": backend_snapshot(
            backend, [candidate["layout"] for candidate in sequence]),
    }
    if metadata is not None:
        evidence.update({
            "excluded_physical_qubits": sorted(
                int(q) for q in excluded_physical_qubits),
            "prior_failed_layouts": [[int(q) for q in layout]
                                     for layout in prior_layouts],
            "candidate_pool": evaluated,
            "selection_is_clean_only": True,
            "selection_data_enter_confirmatory_analysis": False,
        })
    return int(winner["objective"][1]), list(winner["layout"]), evidence


def pinned_layout_evidence(circuits, backend, layout, widths=None, metadata=None):
    """Report the same balanced costs for an already selected layout."""
    reps = layout_representatives(circuits, widths)
    representative_widths = (sorted(set(int(width) for width in widths))
                             if widths is not None else [
                                 len({q for instruction in rep.data
                                      for q in instruction.qubits}) for rep in reps])
    if metadata is None:
        costs = [_transpiled_cost(rep, backend, layout) for rep in reps]
        result_profile = {}
    else:
        profiles = _transpiled_profiles(circuits, backend, layout, metadata)
        representative_indices = [next(i for i, item in enumerate(circuits)
                                       if item is rep) for rep in reps]
        costs = [{"two_qubit_gates": profiles[index]["two_qubit_gates"],
                  "depth": profiles[index]["depth"]}
                 for index in representative_indices]
        result_profile = _result_profile(profiles, metadata)
        result_profile["result_mapping_complete"] = all(
            profile["result_mapping_complete"] for profile in profiles)
    return {
        "strategy": "pinned-balanced-evaluation-v1",
        "candidate_count": 1,
        "representative_count": len(reps),
        "representative_active_widths": representative_widths,
        "winning_representatives": costs,
        "winning_objective": [max(c["two_qubit_gates"] for c in costs),
                              sum(c["two_qubit_gates"] for c in costs),
                              max(c["depth"] for c in costs),
                              sum(c["depth"] for c in costs)],
        "backend_provenance": backend_snapshot(backend, [layout]),
        **result_profile,
    }


def attach_layout_attempt(evidence, ordered):
    """Attach the campaign's frozen retry identity to layout evidence.

    These values originate on the GenerateFlowFile trigger and are captured on
    every circuit.  Requiring one value across the batch prevents a mixed retry
    from being archived under a plausible-looking single attempt number.
    """
    keys = {
        "qualification_attempt": "arithmetic.layout_attempt",
        "candidate_sequence_sha256":
            "arithmetic.layout_candidate_sequence_sha256",
        "selection_snapshot_sha256":
            "arithmetic.layout_selection_snapshot_sha256",
    }
    for output, attribute in keys.items():
        values = {str((entry.get("attributes") or {}).get(attribute, "")).strip()
                  for entry in ordered}
        values.discard("")
        if len(values) > 1:
            raise ValueError("batch has inconsistent %s values" % attribute)
        if values:
            value = next(iter(values))
            evidence[output] = int(value) if output == "qualification_attempt" else value
    evidence.setdefault("attempt_limit", LAYOUT_ATTEMPT_LIMIT)
    return evidence


def resolve_secret(configured, *env_names):
    """A provider credential, from the processor property or the environment.

    NiFi does not evaluate Expression Language on a *sensitive* property -- it
    returns an empty string rather than the referenced value -- so a property
    left as `${IBM_QUANTUM_TOKEN}` arrives here blank. The canvas is built that
    way on purpose (a credential in a flow file is a credential in a git repo),
    and `just nifi-start` puts the real values in NiFi's process environment,
    which the Python processes inherit. So an unset property is not an error:
    it means "look in the environment".

    Diagnosed on 2026-08-28: IQM already had this fallback and worked; IBM did
    not, and every preflight failed with "Unable to find account" -- which is
    what qiskit-ibm-runtime says when the token is empty.
    """
    value = (configured or "").strip()
    if value:
        return value
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


#: An armed job's manifest is the only thing that says which circuit produced
#: which result. It travelled as FlowFile content and was never written to disk,
#: so rebuilding the 2026-08-29 reports meant solving the ordering back out of
#: the results themselves (see experiments/rebuild_hardware_reports.py).
#: Persisting it costs one small file per job and removes that problem for good.
#: It lives here rather than in either submitter because both vendors must write
#: the same shape -- a manifest written differently per vendor is one no reader
#: can trust.
def qasm_digest(qasm):
    """SHA-256 of one circuit's OpenQASM, as the join key between the two copies.

    The FlowFile manifest carries the digest and the archived manifest carries
    the source, so "is this the circuit that ran?" is answerable without either
    copy having to trust the other.
    """
    import hashlib

    return hashlib.sha256((qasm or "").encode("utf-8")).hexdigest()


def manifest_entries(ordered, widths):
    """One self-describing entry per circuit, in submission order.

    Shared by both vendors for the same reason ``persist_manifest`` is: a
    manifest whose shape depends on who wrote it is one no reader can trust.
    The QASM itself is deliberately NOT here -- it goes on disk only, through
    ``persist_manifest`` -- so the manifest that rides on every downstream
    FlowFile stays small, while the archived copy stays complete.
    """
    rows = []
    for entry, width in zip(ordered, widths):
        row = {"label": entry["label"], "kind": entry["kind"],
               "num_qubits": width,
               "qasm_sha256": qasm_digest(entry.get("qasm"))}
        if entry.get("attributes"):
            row["attributes"] = entry["attributes"]
        rows.append(row)
    return rows


def persist_manifest(directory, manifest, circuits=None):
    """Write a submitted job's manifest as ``<job_id>.json``. Never fatal.

    The QPU time is already spent by the time this is called, so a disk problem
    must not turn a successful submission into a failed one. Returns
    ``(path, None)`` on success and ``(None, error)`` on failure, for the caller
    to record on the FlowFile either way.

    ``circuits`` is the OpenQASM that was submitted, aligned index-for-index
    with ``manifest["entries"]``. It is written into the archived copy only.
    Both submitters used to drop it on the grounds that the circuits are
    reproducible from the flow -- true only while the flow is unchanged, which
    is exactly what a mutation campaign does not guarantee. On disk it costs
    ~130 KB a job and makes the archive answer "what did you actually run?"
    without a rebuild. A mismatched length is ignored rather than zipped short:
    a manifest pairing circuits with the wrong entries is worse than one with
    no circuits at all.
    """
    import datetime
    import json
    import os

    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "%s.json" % manifest["job_id"])
        entries = manifest.get("entries") or []
        if circuits is not None and len(circuits) == len(entries):
            entries = [dict(entry, qasm=qasm)
                       for entry, qasm in zip(entries, circuits)]
        body = dict(manifest, entries=entries, submit_mode="armed",
                    submitted_at=datetime.datetime.now(
                        datetime.timezone.utc).isoformat())
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(body, handle, indent=2)
        return path, None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
