#!/usr/bin/env python3
"""Add the GroverMatrix-HW process group to a stopped NiFi.

The hardware twin of tools/add_version_matrix_group.py's VersionMatrix study.
Three independently written Grover builders (Qiskit, Cirq, PennyLane) run the
SAME four test cases and are batched onto one real device with one shared,
searched qubit layout, so a builder-vs-builder disagreement can be attributed
to who wrote the circuit rather than to per-job device drift.

    Trigger -> Test Cases -> SplitJson -> Case to Attributes
                     |
                     +-> Qiskit Grover   -> Mark qiskit Cell     -+
                     +-> Cirq Grover     -> Mark cirq Cell       -+-> Batch Submitter (PREFLIGHT)
                     +-> PennyLane Grover -> Mark pennylane Cell -+          |
      submitted -- Poller --(pending loops)-- success -- Expand Batch
                                                                |
                                              SplitJson -> Hoist Row Fields
                                                                |
                                    Counts To Content -> Success Probability Oracle -> Report
      preflight ------------------------------------------------------------------------> Report

Four qubit/marked-state cases: 2q and 4q, one palindromic and one asymmetric
marked state at each width, each carrying its own analytic
``grover.ideal_success`` (sin^2((2k+1)*asin(2^(-n/2)))) for comparison against
the measured hardware success rate.

Why ``grover.`` is the ONE prefix every branch writes under: the submitter's
``Attribute Prefix`` (``batch_prep.captured_attributes``) takes exactly one
prefix string, so whatever ground truth is to ride through the batch -- the
case's own marked state, expected outcome and partition, PLUS the per-branch
circuit metrics the builders emit under ``circuit.*`` -- has to be re-homed
under ``grover.`` by an ``UpdateAttribute`` ("Mark <fw> Cell") between the
builder and the submitter. Only ``circuit.label`` sits outside that prefix
(read directly by the submitter's own ``Circuit Label`` property, never
captured onto the manifest), and the literal branch identity
(``grover.builder``) is written as a plain string per Mark instance rather
than as an Expression Language self-reference -- NiFi's UpdateAttribute
evaluates every configured property against the FlowFile's INCOMING
attributes, so a property in this same processor cannot read another
property this same processor is setting.

Version resolution: bundle versions for every ``python-extensions`` processor
are read from the processor's own source file (see ``pybundle`` below, copied
from tools/add_version_matrix_group.py and pointed at ``nifi_extensions/``)
rather than hardcoded. ``tools/add_arithmetic_hw_group.py:567`` hardcodes
``QuanifiReport`` at bundle ``0.2.0`` while the source declares ``0.3.1`` -- a
latent ghost-component bug (NiFi cannot match a flow entry's bundle version to
an installed one, and the processor becomes a Ghost component on the canvas).
That bug is not copied here and is not fixed in that other tool.

Case-major ordering: the new ``QuantumInspireBatchSubmitter`` sorts its batch
with ``case_major_order`` -- by test case first, builder second -- so that the
three builders' circuits for one test case land adjacent in the submitted
batch, which is what a builder-vs-builder comparison needs (see that
submitter's own docstring for why kind-based ``interleave()`` would
degenerate to non-deterministic arrival order here). The existing
``QuantumIBMBatchSubmitter``/``QuantumIQMBatchSubmitter`` are reused
UNCHANGED and still order by ``interleave()`` (null/control/mutant buckets);
with every circuit here in the same "control" (or one mutant) bucket that
becomes arrival order for those two providers, which is a known, accepted
limitation of reusing them as-is.

Nothing here can spend QPU time on its own: every submitter is added with
``Submit Mode = preflight``, which transpiles, searches a layout and estimates
usage but never contacts the provider. Arming is a property change a person
makes by hand, deliberately, later.

NiFi must be stopped. A timestamped backup of the flow is written first.

Usage:
  python tools/add_grover_hw_matrix_group.py --flow ~/projects/nifi-2.9.0/conf/flow.json.gz
  python tools/add_grover_hw_matrix_group.py --flow ... --provider ibm --device ibm_brisbane
  python tools/add_grover_hw_matrix_group.py --flow ... --mutant gate.remove --locus middle
  python tools/add_grover_hw_matrix_group.py --flow ... --provider iqm --remove
"""
import argparse
import gzip
import json
import math
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

from add_flow import NIFI_BUNDLE, make_connection, make_label, make_processor

GROUP_BASE = "GroverMatrix-HW"

GROUP_COMMENT = (
    "Grover N-M version matrix on real hardware. Three independently written "
    "Grover builders (Qiskit, Cirq, PennyLane) batched onto one device on one "
    "shared, searched layout, polled, expanded per circuit, and scored "
    "against the analytic ideal by QuantumSuccessProbabilityOracle. The "
    "submitter is in PREFLIGHT mode: it costs the batch and submits nothing "
    "until Submit Mode is set to armed."
)

VALID_SCHEDULED_STATES = {"ENABLED", "DISABLED", "RUNNING"}
NOT_RUNNING = "ENABLED"

# Three independently written Grover builders. Chosen because for the 4-qubit
# case they emit 56 / 88 / 112 two-qubit gates for the identical logical
# algorithm (see docs/planning/PLAN-grover-hw-matrix.md), so they should
# separate on hardware while agreeing on every simulator. ``mark`` is the
# lowercase tag used in the "Mark <fw> Cell" processor name and nowhere else
# -- ``grover.builder`` itself is always the display label below, written as
# a literal (see the module docstring for why it cannot be an EL
# self-reference).
BRANCHES = [
    ("Qiskit", "QiskitGroverCircuit", "qiskit"),
    ("Cirq", "CirqGroverCircuit", "cirq"),
    ("PennyLane", "PennylaneGroverCircuit", "pennylane"),
]

# Per-provider submitter/poller pair, default device, the bit order the
# expander must normalise from, and the hard circuit cap that submitter
# enforces.
PROVIDERS = {
    "ibm": {
        "submitter": "QuantumIBMBatchSubmitter",
        "poller": "QuantumIBMBatchPoller",
        # Every archived IBM run in this repo used kingston; the sealed
        # campaign pins it at generation2/core.py:97.
        "device": "ibm_kingston",
        # Qiskit/IBM return counts MSB-first; the expander normalises to q0_left.
        "bit_order": "q0_right",
        "max_circuits": "256",
        "chunked": False,
    },
    "iqm": {
        "submitter": "QuantumIQMBatchSubmitter",
        # The BATCH poller, not IQMJobPoller -- see add_arithmetic_hw_group.py
        # for why pairing a batch submitter with the single-circuit poller
        # silently loses a job.
        "poller": "QuantumIQMBatchPoller",
        "device": "garnet",
        # The IQM serializer already emits q0_left.
        "bit_order": "q0_left",
        "max_circuits": "100",
        "chunked": False,
    },
    "quantum-inspire": {
        "submitter": "QuantumInspireBatchSubmitter",
        "poller": "QuantumInspireBatchPoller",
        "device": "Tuna-17",
        # Verified against a real completed Tuna-17 job: marked state '10'
        # (q0-left) came back under key '01'.
        "bit_order": "q0_right",
        "max_circuits": "50",
        # The single switch the wiring below reads to decide whether a
        # SplitJson chunk-fan-out sits between submitter and poller. Tuna-17's
        # max_jobs_per_batch_job (5) is well under this study's 44-circuit
        # batch, so QuantumInspireBatchSubmitter partitions it into several
        # provider batch jobs; IBM and iqm are provably untouched by leaving
        # this False.
        "chunked": True,
    },
}


#: Mutant rungs, in ascending order of simulated effect size. The ORDER
#: matters: the stage-3 result is the LOWEST rung a device can still detect,
#: so anything comparing rungs must sort by this, never by dict order.
RUNGS = ("small", "medium", "large")

#: Where the preregistered mutant ladder lives (PREREG Amendment A2). The
#: file pins, per (builder, case, rung), the operator and the rotation
#: epsilon whose SIMULATED effect size hits the rung's target -- so a mutant
#: that hardware fails to detect is known not to have been a no-op.
DEFAULT_MUTANTS_FILE = (
    Path(__file__).resolve().parent.parent
    / "experiments" / "grover_hw_mutants.json")


def load_ladder(path=None):
    """Read the mutant ladder into {(builder, case, rung): entry}.

    Returns an empty dict when the file is absent, which is what makes the
    control-only group (``--no-mutants``) work without it.
    """
    path = Path(path or DEFAULT_MUTANTS_FILE)
    if not path.exists():
        return {}
    doc = json.loads(path.read_text())
    # Top-level key is "mutants" (not "accepted"); `large` entries carry
    # epsilon=None, so never assume a float.
    return {(e["builder"], e["case"], e["rung"]): e
            for e in doc.get("mutants", [])}


def _ideal_success(n_qubits, iterations):
    """Analytic success probability of an n-qubit, k-iteration Grover search.

    sin^2((2k+1) * asin(2^(-n/2))) for one marked state out of 2^n. 2 qubits /
    1 iteration is exact resonance (1.000000); 4 qubits / 2 iterations is
    slightly past it (0.908447) -- 3 iterations would raise that to 0.961319
    but costs a third Grover operator's worth of two-qubit gates, which on
    hardware costs more than it gains. Kept at 2 iterations to stay in step
    with experiments/version_matrix_study.py.
    """
    theta = math.asin(2.0 ** (-n_qubits / 2.0))
    return math.sin((2 * iterations + 1) * theta) ** 2


#: The four cases this group runs: 2q and 4q, one palindromic and one
#: asymmetric marked state at each width. A palindromic marked state cannot
#: expose a qubit-ordering fault (the wrong answer reads the same as the
#: right one reversed), so both are needed at every width for ordering
#: coverage. Rows verbatim from tools/add_version_matrix_group.py's
#: DEFAULT_CASES (the asymmetric-2q / palindrome-2q / asymmetric-4q /
#: palindrome-4q rows only -- the 3-qubit rows are dropped, this study only
#: needs the two widths that calibrate the device floor and separate the
#: builders). ``grover.ideal_success`` is additional: it never becomes a
#: FlowFile attribute (see ``_case_wire_fields`` below) and exists purely so
#: the measured hardware success rate has an analytic number to compare
#: against, and so this table can be checked against the formula directly.
CASES = [
    {"grover.marked_state": "10", "grover.iterations": "1",
     "test.partition": "asymmetric-2q", "test.expected": "10",
     "grover.ideal_success": _ideal_success(2, 1)},
    {"grover.marked_state": "11", "grover.iterations": "1",
     "test.partition": "palindrome-2q", "test.expected": "11",
     "grover.ideal_success": _ideal_success(2, 1)},
    {"grover.marked_state": "0111", "grover.iterations": "2",
     "test.partition": "asymmetric-4q", "test.expected": "0111",
     "grover.ideal_success": _ideal_success(4, 2)},
    {"grover.marked_state": "0110", "grover.iterations": "2",
     "test.partition": "palindrome-4q", "test.expected": "0110",
     "grover.ideal_success": _ideal_success(4, 2)},
]

#: What "Case to Attributes" (EvaluateJsonPath) hoists from each split test
#: case onto the FlowFile. test.case_id / test.run_id are stamped by
#: QuantumTestCaseSource itself (auto-assigned, not present in CASES);
#: everything else is a CASES key. ``grover.ideal_success`` is deliberately
#: NOT here -- see CASES' docstring -- so it never becomes a FlowFile
#: attribute and never needs a ROW_FIELDS entry.
CASE_ATTRIBUTES = [
    "grover.marked_state", "grover.iterations", "test.partition",
    "test.case_id", "test.run_id", "test.expected",
]


def _case_wire_fields(case):
    """The subset of one CASES row that actually reaches the canvas.

    ``grover.ideal_success`` is analytic bookkeeping for this tool and the
    test suite, not a runtime input -- QuantumTestCaseSource's Test Matrix
    only needs to carry what the builders and Mark processors actually read.
    """
    return {k: v for k, v in case.items() if k != "grover.ideal_success"}


def case_ladder_fields(case, ladder):
    """Per-case mutant parameters, flattened onto the test-case row.

    One mutator processor serves a whole branch, but the operator and epsilon
    differ per (builder, case, rung) -- so the values have to travel WITH the
    case and be read back through Expression Language. Keys are namespaced by
    builder and rung so each branch's mutator reads only its own:

        grover.mut_qiskit_small_operator / _epsilon / _delta

    A 2-qubit case carries only its `large` rung (PREREG Amendment A2), so
    the small/medium keys are simply absent and those lanes are never routed
    a 2-qubit FlowFile (see the 4-qubit RouteOnAttribute in build_group).
    """
    marked = case["grover.marked_state"]
    out = {}
    for _, _, tag in BRANCHES:
        for rung in RUNGS:
            entry = ladder.get((tag, marked, rung))
            if entry is None:
                continue
            prefix = "grover.mut_%s_%s" % (tag, rung)
            out[prefix + "_operator"] = entry["operator"]
            # `large` is gate.remove and carries epsilon=None; QuantumMutator
            # ignores Rotation Epsilon for a non-rotation operator, but the
            # property must still be a string.
            out[prefix + "_epsilon"] = (
                "" if entry.get("epsilon") is None else "%.6f" % entry["epsilon"])
            out[prefix + "_delta"] = "%.6f" % entry["delta_sim"]
    return out


def rungs_for(case, ladder):
    """Which rungs this case actually carries, in ascending effect size."""
    marked = case["grover.marked_state"]
    return [r for r in RUNGS
            if any((tag, marked, r) in ladder for _, _, tag in BRANCHES)]


def expected_circuits(cases, ladder, with_mutants=True):
    """The one place this number is computed (PREREG Amendment A2).

    Per case: one circuit per builder, plus one readout baseline, plus one
    null replicate of the reference builder. Plus, when the ladder is in
    play, one mutant per builder per rung THAT CASE CARRIES -- 4-qubit cases
    carry three rungs, 2-qubit cases only `large`.

    At the preregistered design this is 20 + 18 + 6 = 44.
    """
    total = 0
    for case in cases:
        total += len(BRANCHES) + 2          # builders + readout + null
        if with_mutants:
            total += len(BRANCHES) * len(rungs_for(case, ladder))
    return total


def filter_cases(raw, cases=CASES):
    """Filter CASES by comma-separated partition labels or marked states.

    Accepts e.g. 'asymmetric-2q', '10', or 'asymmetric-2q,palindrome-2q'.
    Blank/None returns all cases unchanged.
    """
    if not raw or not str(raw).strip():
        return list(cases)
    tokens = [t.strip() for t in str(raw).split(",") if t.strip()]
    selected = []
    for token in tokens:
        matches = [c for c in cases
                   if c["test.partition"] == token
                   or c["grover.marked_state"] == token
                   or c.get("test.case_id") == token]
        if not matches:
            raise SystemExit("unknown case or partition in --cases: %r" % token)
        for m in matches:
            if m not in selected:
                selected.append(m)
    return selected


#: Submission order ranking inside case_major_order: readout first (rank 0),
#: then control (rank 1), then null replicate (rank 2), then mutants (rank 3).
_KIND_RANK = {"readout": 0, "control": 1, "null": 2}


#: Fields the expander writes onto each row, hoisted back into attributes.
#:
#: Every ``grover.``-prefixed attribute present on a circuit's FlowFile when
#: it reaches the submitter is captured onto that circuit's manifest entry
#: (``batch_prep.captured_attributes`` matches by prefix, not by a fixed key
#: list) and rides through the poller into the expanded row's ``attributes``.
#: That set is: the two case-sourced attributes hoisted straight from the
#: test case (``grover.marked_state``, ``grover.iterations``) PLUS everything
#: "Mark <fw> Cell"/"Mark <fw> Mutant" write under the same prefix
#: (``grover.builder``, ``.case_id``, ``.partition``, ``.expected``,
#: ``.kind``, ``.two_qubit_gates``, ``.depth``, ``.gate_count``).
#:
#: A field present here but omitted from ROW_FIELDS is NOT dropped -- it is
#: silently replaced by the PARENT FlowFile's value (one arbitrary circuit's,
#: for the whole batch), which is worse than missing because it still looks
#: per-circuit. See tools/add_arithmetic_hw_group.py's own ROW_FIELDS comment
#: for the archived incident this exact bug caused there.
ROW_FIELDS = [
    "grover.marked_state", "grover.iterations",
    "grover.builder", "grover.case_id", "grover.partition", "grover.expected",
    "grover.kind", "grover.two_qubit_gates", "grover.depth", "grover.gate_count",
    # Ladder provenance, written by the Mark <fw> <rung> processors. The
    # analysis joins hardware rows to the simulated effect size on these.
    "grover.rung", "grover.delta_sim", "grover.epsilon",
    # Per-circuit ROUTED two-qubit count, from batch_prep.isa_profile via the
    # submitter's manifest entry. This is the study's independent variable:
    # the EMITTED count (grover.two_qubit_gates) is not what the device runs.
    "grover.isa_two_qubit_gates", "grover.isa_depth",
    "batch.label", "batch.kind", "batch.entry_index", "sim.bit_order",
    # Chunk provenance: a Quantum Inspire batch that was partitioned polls as
    # several documents, one per chunk, so batch.entry_index alone (which
    # restarts at 0 in each chunk) cannot tell rows from different chunks
    # apart. batch.batch_index is the position in the WHOLE submitted batch;
    # batch.chunk_index/batch.chunk_count/batch.job_id are per-chunk. All
    # four are present-but-constant (job_id) or absent (the rest) on IBM/IQM
    # rows, which are never chunked.
    "batch.batch_index", "batch.chunk_index", "batch.chunk_count", "batch.job_id",
    # Restricted transpile basis actually used for this batch (comma-joined),
    # empty when the fallback backend=backend path was used. See
    # QuantumInspireBatchSubmitter.native_basis_gates.
    "batch.native_basis_gates",
]

# ---------------------------------------------------------------------------
# Bundle version resolution -- read from source, never hardcoded. Copied from
# tools/add_version_matrix_group.py and pointed at nifi_extensions/ (the
# 2.9.0 tree this tool targets), not nifi_extensions_matrix/.
# ---------------------------------------------------------------------------

EXTENSIONS_DIR = Path(__file__).resolve().parent.parent / "nifi_extensions"

_VERSION_RE = re.compile(r"^\s+version\s*=\s*[\"']([^\"']+)[\"']", re.M)
_version_cache = {}


def pybundle(proc_type):
    """The python-extensions bundle for one processor, at ITS declared version.

    NiFi matches a flow entry to a Python processor by type AND version, so a
    wrong version yields "Unknown Python Processor type [X] or version [Y]"
    and a Ghost component on the canvas -- with the processor still listed as
    discovered in the log, which makes it look like a NiFi problem, not a
    stale hardcoded version in the tool that wrote the flow.
    """
    if proc_type not in _version_cache:
        source = EXTENSIONS_DIR / ("%s.py" % proc_type)
        if not source.exists():
            raise SystemExit("no such processor source: %s" % source)
        match = _VERSION_RE.search(source.read_text())
        if not match:
            raise SystemExit(
                "%s declares no ProcessorDetails.version; the flow entry "
                "would guess and produce a Ghost component" % source.name)
        _version_cache[proc_type] = match.group(1)
    return {"group": "org.apache.nifi", "artifact": "python-extensions",
            "version": _version_cache[proc_type]}


#: UpdateAttribute is NOT in nifi-standard-nar, and its class is under
#: `processors.attributes`, not `processors.standard`. Getting either wrong
#: makes NiFi reject the processor with "not a valid Processor type" only
#: after a restart. Copied verbatim from tools/add_arithmetic_hw_group.py.
UPDATE_ATTRIBUTE = "org.apache.nifi.processors.attributes.UpdateAttribute"
UPDATE_ATTRIBUTE_BUNDLE = {"group": "org.apache.nifi",
                           "artifact": "nifi-update-attribute-nar",
                           "version": "2.9.0"}

#: Loci a mutant can be pinned to. "middle" is what the plan uses; all three
#: are accepted because QuantumMutator itself validates the value.
LOCI = ("first", "middle", "last")

#: Column positions. A NiFi processor node is about 352 wide, so a step below
#: that overlaps its neighbour; 380 leaves a visible gap without stretching
#: the lane. Rows step by 150 for the same reason (node height is about 104).
COL, ROW = 380, 150

#: Dead-letter funnels, in canvas order. Always built (unlike
#: add_arithmetic_hw_group.py's conditional funnel): every failure downstream
#: of a successful submission carries results that are already paid for, and
#: a costing group is exactly as capable of losing a real batch as an armed
#: one is.
BANDS = ("source", "build", "run", "decide")

#: The spine -- trigger through expander -- runs along this row; everything
#: else is placed a whole number of rows above or below it.
SPINE_Y = 200

X = {"trigger": 0, "source": 380, "split": 760, "eval": 1140, "build": 1520,
     "mark": 1900, "mutate": 2280, "markmutant": 2660, "submit": 3040,
     "chunksplit": 3230,
     "poll": 3420, "expand": 3800, "rsplit": 4180, "rhoist": 4560,
     "rcontent": 4940, "oracle": 5320, "report": 5700}


def row(k):
    return SPINE_Y + k * ROW


def uid():
    return str(uuid.uuid4())


def make_funnel(x, y, group_id):
    """A dead-letter sink. Shape copied from an existing funnel in flow.json.gz."""
    return {
        "identifier": uid(), "instanceIdentifier": uid(),
        "position": {"x": float(x), "y": float(y)},
        "componentType": "FUNNEL", "groupIdentifier": group_id,
    }


def make_funnel_connection(src, src_rel, funnel, group_id, z_index):
    """A processor relationship routed into a funnel rather than a processor."""
    return {
        "identifier": uid(), "instanceIdentifier": uid(),
        "name": "dead letter",
        "source": {"id": src["identifier"], "type": "PROCESSOR",
                   "groupId": group_id, "name": src["name"], "comments": "",
                   "instanceIdentifier": src["instanceIdentifier"]},
        "destination": {"id": funnel["identifier"], "type": "FUNNEL",
                        "groupId": group_id, "name": "Funnel", "comments": "",
                        "instanceIdentifier": funnel["instanceIdentifier"]},
        "labelIndex": 0, "zIndex": z_index,
        "selectedRelationships": [src_rel],
        "backPressureObjectThreshold": 10000,
        "backPressureDataSizeThreshold": "1 GB",
        "flowFileExpiration": "0 sec",
        "prioritizers": [], "bends": [],
        "loadBalanceStrategy": "DO_NOT_LOAD_BALANCE",
        "partitioningAttribute": "",
        "loadBalanceCompression": "DO_NOT_COMPRESS",
        "componentType": "CONNECTION", "groupIdentifier": group_id,
    }


def _group_position(provider):
    """Distinct canvas coordinates per provider so the three groups this tool
    can build (one per invocation) do not overlap if all three end up on the
    same canvas at once, per the plan's "the three coexist" requirement."""
    providers_sorted = sorted(PROVIDERS)
    return -25000.0, -2200.0 + providers_sorted.index(provider) * 1800.0


def build_group(group_id, provider, device, shots, seed, reports_dir,
                fixed_layout="", mutants=DEFAULT_MUTANTS_FILE, locus="middle",
                state_dir="", batch_namespace="", group_name=None,
                cases_filter=None, replicate=1, mutant=None):
    """Build the group. Returns ``(processors, connections, funnels, labels)``.

    Carries 4 branches: ReadoutBaselineCircuit, 3 Grover builders (Qiskit,
    Cirq, PennyLane), and on Qiskit a null replicate. When mutants are
    enabled (the default), each builder branch also carries the mutant
    ladder rungs: large for all cases, plus small and medium for 4-qubit
    cases, yielding 44 circuits per Amendment A2.
    """
    spec = PROVIDERS[provider]
    processors, connections, funnels, labels = [], [], [], []
    z = 0

    if mutant is not None:
        ladder = load_ladder(DEFAULT_MUTANTS_FILE)
        with_mutants = True
    elif mutants is None or mutants is False:
        ladder = {}
        with_mutants = False
    elif isinstance(mutants, str) and mutants.lower() in ("none", "false", "no", ""):
        ladder = {}
        with_mutants = False
    elif isinstance(mutants, dict):
        ladder = mutants
        with_mutants = bool(ladder)
    else:
        ladder = load_ladder(mutants)
        with_mutants = bool(ladder)

    if group_name is None:
        group_name = "%s-%s-rep%d" % (GROUP_BASE, provider, replicate) if replicate > 1 else ("%s-%s" % (GROUP_BASE, provider))

    cases = filter_cases(cases_filter, CASES)

    def connect(src, rel, dst):
        nonlocal z
        connections.append(make_connection(src, rel, dst, group_id, z))
        z += 1

    funnel = {name: make_funnel(x, y, group_id) for name, x, y in
              (("source", X["eval"], row(-2)),
               ("build", X["mark"] + COL, row(2)),
               ("run", X["submit"] + COL, row(3)),
               ("decide", X["oracle"], row(4)))}
    funnels.extend(funnel[name] for name in BANDS)

    def dead_letter(src, rel, band="build"):
        """Route a relationship into that band's dead-letter funnel."""
        nonlocal z
        connections.append(
            make_funnel_connection(src, rel, funnel[band], group_id, z))
        z += 1

    trigger = make_processor(
        name="Grover HW — Trigger",
        proc_type="org.apache.nifi.processors.standard.GenerateFlowFile",
        bundle=NIFI_BUNDLE,
        properties={"Batch Size": "1", "Data Format": "Text",
                    "Unique FlowFiles": "false", "File Size": "0B",
                    "Character Set": "UTF-8", "generate-ff-custom-text": "go"},
        scheduling_period="8760 h",
        x=X["trigger"], y=200, group_id=group_id, scheduled_state=NOT_RUNNING)

    case_attrs = list(CASE_ATTRIBUTES)
    if with_mutants:
        for _, _, tag in BRANCHES:
            for rung in RUNGS:
                case_attrs.extend([
                    "grover.mut_%s_%s_operator" % (tag, rung),
                    "grover.mut_%s_%s_epsilon" % (tag, rung),
                    "grover.mut_%s_%s_delta" % (tag, rung),
                ])

    matrix = []
    for c in cases:
        wire = _case_wire_fields(c)
        if with_mutants:
            wire.update(case_ladder_fields(c, ladder))
        matrix.append(wire)

    source = make_processor(
        name="Grover HW — Test Cases",
        proc_type="QuantumTestCaseSource", bundle=pybundle("QuantumTestCaseSource"),
        properties={
            "Case ID Prefix": "grover-hw",
            "Partition Label": "grover",
            "Mutation Seed": str(seed),
            "Test Matrix": json.dumps(matrix),
        },
        x=X["source"], y=200, group_id=group_id, scheduled_state=NOT_RUNNING,
        auto_terminate=[])
    dead_letter(source, "failure", "source")

    split = make_processor(
        name="Grover HW — SplitJson",
        proc_type="org.apache.nifi.processors.standard.SplitJson",
        bundle=NIFI_BUNDLE,
        properties={"JsonPath Expression": "$",
                    "Null Value Representation": "empty string"},
        x=X["split"], y=200, group_id=group_id, scheduled_state=NOT_RUNNING,
        auto_terminate=["original", "failure"])

    evaluate = make_processor(
        name="Grover HW — Case to Attributes",
        proc_type="org.apache.nifi.processors.standard.EvaluateJsonPath",
        bundle=NIFI_BUNDLE,
        properties=dict(
            {"Destination": "flowfile-attribute", "Return Type": "json",
             "Path Not Found Behavior": "ignore",
             "Null Value Representation": "empty string"},
            **{f: "$['%s']" % f for f in case_attrs}),
        x=X["eval"], y=200, group_id=group_id, scheduled_state=NOT_RUNNING,
        auto_terminate=["unmatched", "failure"])

    expected = expected_circuits(cases, ladder, with_mutants=with_mutants)
    if expected > int(spec["max_circuits"]):
        raise SystemExit(
            "this matrix needs %d circuits but %s caps a job at %s; the "
            "submitter would reject the slot after accumulating it"
            % (expected, provider, spec["max_circuits"]))

    if replicate and int(replicate) > 1:
        rep_tag = "rep%d" % int(replicate)
        batch_label = ("%s-%s-${test.run_id}" % (batch_namespace, rep_tag) if batch_namespace
                       else "%s-${test.run_id}" % rep_tag)
    else:
        batch_label = ("%s-${test.run_id}" % batch_namespace if batch_namespace
                       else "${test.run_id}")

    manifest_dir = "experiments/results/grover_hw_matrix/manifests/%s" % provider
    default_state_dir = "reports/tmp/quanifi_grover_hw_batch_state/%s" % provider
    submitter_props = {
        "Batch Label": batch_label,
        "Expected Circuits": str(expected),
        "Device": device,
        "Shots": str(shots),
        # Mark <fw> Cell/Mutant write the kind and label per lane, so one
        # submitter serves every branch, control and mutant alike.
        "Circuit Kind": "${grover.kind}",
        "Circuit Label": "${circuit.label}",
        "Fixed Layout": fixed_layout,
        "Layout Search Seeds": "24",
        # PREFLIGHT. Costs the batch, submits nothing. Change to armed only
        # after reading the preflight numbers.
        "Submit Mode": "preflight",
        "Attribute Prefix": "grover.",
        "Maximum Estimated Usage Seconds": "120",
        "Maximum Circuits": spec["max_circuits"],
        "Maximum Shots Per Circuit": "4096",
        "Control Success Probability": "0.30",
        "Minimum Detectable Difference": "0.10",
        "State Directory": state_dir or default_state_dir,
        "Manifest Directory": manifest_dir,
        "Slot TTL Seconds": "3600",
        "Batch Group Key": "${test.case_id}",
        "Batch Member Key": "${grover.builder}",
    }
    if provider == "ibm":
        submitter_props.update({
            "API Token": "", "Instance": "",
            "Maximum Pending Jobs": "10",
            "Minimum Remaining IBM QPU Seconds": "10",
            "Excluded Physical Qubits": "",
        })
    elif provider == "iqm":
        submitter_props.update({
            "API Token": "", "Server URL": "https://resonance.iqm.tech",
            # IQM publishes no documented balance endpoint, so the balance
            # is always unreadable; the repo owner has explicitly accepted
            # the risk of submitting without a credit check for this study.
            "Allow Unreadable Credit Balance": "true",
        })
    else:
        submitter_props.update({
            "Credentials File": "~/.quantuminspire/config.json",
            # Refuse the batch, before spending, if it would need more than
            # 12 provider batch jobs; wait up to 15 minutes for an earlier
            # chunk's queue slot to free before submitting the next. The
            # other three chunking properties (Maximum Circuits Per Batch
            # Job, Maximum Batch Jobs In Flight, Chunk Queue Poll Interval
            # Seconds) are left at their device-derived defaults.
            "Maximum Batch Jobs": "12",
            "Chunk Queue Wait Seconds": "900",
        })

    submitter = make_processor(
        name="Grover HW — Batch Submitter (PREFLIGHT)",
        proc_type=spec["submitter"], bundle=pybundle(spec["submitter"]),
        properties=submitter_props,
        x=X["submit"], y=200, group_id=group_id, scheduled_state=NOT_RUNNING,
        # `waiting` is auto-terminated by design (see the poller `pending`
        # comment below for the matching submitter rule); `failure` never is.
        auto_terminate=["original", "waiting"])
    dead_letter(submitter, "failure", "run")

    chunk_split = None
    if spec["chunked"]:
        # Quantum Inspire's max_jobs_per_batch_job is well under this
        # study's circuit count, so QuantumInspireBatchSubmitter's
        # `submitted` relationship carries a JSON ARRAY of one manifest per
        # provider batch job -- a stock SplitJson fans that back out to one
        # FlowFile per chunk before the poller, which (like every batch
        # poller here) handles exactly one manifest per invocation.
        chunk_split = make_processor(
            name="Grover HW — Split Chunk Manifests",
            proc_type="org.apache.nifi.processors.standard.SplitJson",
            bundle=NIFI_BUNDLE,
            properties={"JsonPath Expression": "$",
                       "Null Value Representation": "empty string"},
            x=X["chunksplit"], y=200, group_id=group_id,
            scheduled_state=NOT_RUNNING, auto_terminate=["original"])
        # `original` is auto-terminated for the same reason the case
        # splitter does it above: the durable record of what was submitted
        # is the chunk index document on disk, not this FlowFile.  `failure`
        # is dead-lettered, never auto-terminated -- a chunk manifest that
        # will not split represents provider batch jobs already paid for.
        dead_letter(chunk_split, "failure", "run")

    poller_props = {"Job ID": "${batch.job_id}",
                    "Poll Timeout Seconds": "60", "Poll Interval Seconds": "10"}
    if provider == "ibm":
        poller_props.update({"API Token": "", "Instance": ""})
    elif provider == "iqm":
        poller_props.update({"API Token": "",
                             "Server URL": "https://resonance.iqm.tech"})
    else:
        poller_props.update({"Job Handle Path": ""})

    poller = make_processor(
        name="Grover HW — Poller",
        proc_type=spec["poller"], bundle=pybundle(spec["poller"]),
        properties=poller_props,
        x=X["poll"], y=200, group_id=group_id, scheduled_state=NOT_RUNNING,
        auto_terminate=[])
    dead_letter(poller, "failure", "run")

    expander = make_processor(
        name="Grover HW — Expand Batch",
        proc_type="QuantumBatchResultExpander",
        bundle=pybundle("QuantumBatchResultExpander"),
        properties={"Source Bit Order": spec["bit_order"]},
        x=X["expand"], y=200, group_id=group_id, scheduled_state=NOT_RUNNING,
        auto_terminate=[])
    dead_letter(expander, "failure", "run")

    result_split = make_processor(
        name="Grover HW — Split Results",
        proc_type="org.apache.nifi.processors.standard.SplitJson",
        bundle=NIFI_BUNDLE,
        properties={"JsonPath Expression": "$",
                    "Null Value Representation": "empty string"},
        x=X["rsplit"], y=200, group_id=group_id, scheduled_state=NOT_RUNNING,
        auto_terminate=["original"])
    dead_letter(result_split, "failure", "run")

    result_attrs = make_processor(
        name="Grover HW — Hoist Row Fields",
        proc_type="org.apache.nifi.processors.standard.EvaluateJsonPath",
        bundle=NIFI_BUNDLE,
        properties=dict(
            {"Destination": "flowfile-attribute", "Return Type": "json",
             "Path Not Found Behavior": "ignore",
             "Null Value Representation": "empty string"},
            **{f: "$['attributes']['%s']" % f for f in ROW_FIELDS}),
        x=X["rhoist"], y=200, group_id=group_id, scheduled_state=NOT_RUNNING,
        auto_terminate=["unmatched"])
    dead_letter(result_attrs, "failure", "run")

    # Second EvaluateJsonPath: attributes and content are separate
    # destinations, and the oracle reads counts from the CONTENT. Destination
    # flowfile-content only supports one dynamic JsonPath property.
    result_content = make_processor(
        name="Grover HW — Counts To Content",
        proc_type="org.apache.nifi.processors.standard.EvaluateJsonPath",
        bundle=NIFI_BUNDLE,
        properties={"Destination": "flowfile-content", "Return Type": "json",
                    "Path Not Found Behavior": "warn",
                    "counts": "$['counts']"},
        x=X["rcontent"], y=200, group_id=group_id, scheduled_state=NOT_RUNNING,
        auto_terminate=["unmatched"])
    dead_letter(result_content, "failure", "run")

    oracle = make_processor(
        name="Grover HW — Success Probability Oracle",
        proc_type="QuantumSuccessProbabilityOracle",
        bundle=pybundle("QuantumSuccessProbabilityOracle"),
        properties={
            "Expected Outcome": "${grover.expected}", "Mode": "single",
            "Confidence Level": "0.95", "Alpha": "0.05",
            "Alternative": "two-sided",
            "Minimum Difference": "0.10",
            "Comparison Label": "${test.run_id}-${batch.label}",
            "State Directory": "reports/tmp/quanifi_grover_hw_oracle_state"},
        x=X["oracle"], y=200, group_id=group_id, scheduled_state=NOT_RUNNING)

    report = make_processor(
        name="Grover HW — Report",
        proc_type="QuanifiReport", bundle=pybundle("QuanifiReport"),
        properties={"Reports Directory": reports_dir, "Flow Name": GROUP_BASE},
        x=X["report"], y=200, group_id=group_id, scheduled_state=NOT_RUNNING,
        auto_terminate=["success", "failure"])

    processors += [trigger, source, split, evaluate]
    for src, rel, dst in ((trigger, "success", source),
                          (source, "success", split),
                          (split, "split", evaluate)):
        connect(src, rel, dst)

    # 1. Readout baseline branch
    readout_builder = make_processor(
        name="Grover HW — Readout Baseline",
        proc_type="ReadoutBaselineCircuit", bundle=pybundle("ReadoutBaselineCircuit"),
        properties={"Marked State": "${grover.marked_state}",
                    "Output Format": "qasm2"},
        x=X["build"], y=row(-2), group_id=group_id,
        scheduled_state=NOT_RUNNING,
        auto_terminate=["original"])
    processors.append(readout_builder)
    connect(evaluate, "matched", readout_builder)
    dead_letter(readout_builder, "failure", "build")

    mark_readout = make_processor(
        name="Grover HW — Mark readout Cell",
        proc_type=UPDATE_ATTRIBUTE, bundle=UPDATE_ATTRIBUTE_BUNDLE,
        properties={
            "grover.builder": "readout",
            "grover.case_id": "${test.case_id}",
            "grover.partition": "${test.partition}",
            "grover.expected": "${test.expected}",
            "grover.kind": "readout",
            "grover.two_qubit_gates": "${circuit.nonlocal_gates}",
            "grover.depth": "${circuit.depth}",
            "grover.gate_count": "${circuit.gate_count}",
            "circuit.label": "readout@${test.case_id}",
        },
        x=X["mark"], y=row(-2), group_id=group_id,
        scheduled_state=NOT_RUNNING)
    processors.append(mark_readout)
    connect(readout_builder, "success", mark_readout)
    connect(mark_readout, "success", submitter)

    # 2. Builder branches: control, null (qiskit), and ladder mutants
    for index, (fw_label, builder_type, mark_tag) in enumerate(BRANCHES):
        builder = make_processor(
            name="Grover HW — %s Builder" % fw_label,
            proc_type=builder_type, bundle=pybundle(builder_type),
            properties={"Marked State": "${grover.marked_state}",
                        "Num Iterations": "${grover.iterations}",
                        "Output Format": "qasm2"},
            x=X["build"], y=row(index - 1), group_id=group_id,
            scheduled_state=NOT_RUNNING,
            auto_terminate=["original"])
        processors.append(builder)
        connect(evaluate, "matched", builder)
        dead_letter(builder, "failure", "build")

        mark_control = make_processor(
            name="Grover HW — Mark %s Cell" % mark_tag,
            proc_type=UPDATE_ATTRIBUTE, bundle=UPDATE_ATTRIBUTE_BUNDLE,
            properties={
                "grover.builder": fw_label,
                "grover.case_id": "${test.case_id}",
                "grover.partition": "${test.partition}",
                "grover.expected": "${test.expected}",
                "grover.kind": "control",
                "grover.two_qubit_gates": "${circuit.nonlocal_gates}",
                "grover.depth": "${circuit.depth}",
                "grover.gate_count": "${circuit.gate_count}",
                "circuit.label": "%s@${test.case_id}" % fw_label,
            },
            x=X["mark"], y=row(index - 1), group_id=group_id,
            scheduled_state=NOT_RUNNING)
        processors.append(mark_control)
        connect(builder, "success", mark_control)
        connect(mark_control, "success", submitter)

        # Null replicate on the reference builder (Qiskit):
        if mark_tag == "qiskit":
            mark_null = make_processor(
                name="Grover HW — Mark %s Null" % mark_tag,
                proc_type=UPDATE_ATTRIBUTE, bundle=UPDATE_ATTRIBUTE_BUNDLE,
                properties={
                    "grover.builder": fw_label,
                    "grover.case_id": "${test.case_id}",
                    "grover.partition": "${test.partition}",
                    "grover.expected": "${test.expected}",
                    "grover.kind": "null",
                    "grover.two_qubit_gates": "${circuit.nonlocal_gates}",
                    "grover.depth": "${circuit.depth}",
                    "grover.gate_count": "${circuit.gate_count}",
                    "circuit.label": "%s@${test.case_id}@null" % fw_label,
                },
                x=X["mark"], y=row(index - 1) - 70, group_id=group_id,
                scheduled_state=NOT_RUNNING)
            processors.append(mark_null)
            connect(builder, "success", mark_null)
            connect(mark_null, "success", submitter)

        if with_mutants:
            base_row = index * 3 + 2
            # 1) large rung runs on all cases (both 2q and 4q):
            mutator_large = make_processor(
                name="Grover HW — Mutate %s (large)" % fw_label,
                proc_type="QuantumMutator", bundle=pybundle("QuantumMutator"),
                properties={"Mutation Operators": "${grover.mut_%s_large_operator}" % mark_tag,
                            "Mutation Seed": str(seed),
                            "Mutation Locus": locus,
                            "Rotation Epsilon": "${grover.mut_%s_large_epsilon}" % mark_tag},
                x=X["mutate"], y=row(base_row), group_id=group_id,
                scheduled_state=NOT_RUNNING, auto_terminate=["original"])
            processors.append(mutator_large)
            connect(builder, "success", mutator_large)
            dead_letter(mutator_large, "failure", "build")

            mark_large = make_processor(
                name="Grover HW — Mark %s large Mutant" % mark_tag,
                proc_type=UPDATE_ATTRIBUTE, bundle=UPDATE_ATTRIBUTE_BUNDLE,
                properties={
                    "grover.builder": fw_label,
                    "grover.case_id": "${test.case_id}",
                    "grover.partition": "${test.partition}",
                    "grover.expected": "${test.expected}",
                    "grover.kind": "large",
                    "grover.rung": "large",
                    "grover.delta_sim": "${grover.mut_%s_large_delta}" % mark_tag,
                    "grover.epsilon": "${grover.mut_%s_large_epsilon}" % mark_tag,
                    "grover.two_qubit_gates": "${circuit.nonlocal_gates}",
                    "grover.depth": "${circuit.depth}",
                    "grover.gate_count": "${circuit.gate_count}",
                    "circuit.label": "%s@${test.case_id}@large" % fw_label,
                },
                x=X["markmutant"], y=row(base_row), group_id=group_id,
                scheduled_state=NOT_RUNNING)
            processors.append(mark_large)
            connect(mutator_large, "success", mark_large)
            connect(mark_large, "success", submitter)

            # 2) small and medium rungs only run on 4-qubit cases:
            filter_4q = make_processor(
                name="Grover HW — %s 4Q Filter" % fw_label,
                proc_type="org.apache.nifi.processors.standard.RouteOnAttribute",
                bundle=NIFI_BUNDLE,
                properties={"Routing Strategy": "Route to Property name",
                            "four_qubit": "${grover.marked_state:length():equals(4)}"},
                x=X["mutate"] - COL // 2, y=row(base_row + 1), group_id=group_id,
                scheduled_state=NOT_RUNNING, auto_terminate=["unmatched"])
            processors.append(filter_4q)
            connect(builder, "success", filter_4q)

            for ri, rung in enumerate(["small", "medium"]):
                mutator_rung = make_processor(
                    name="Grover HW — Mutate %s (%s)" % (fw_label, rung),
                    proc_type="QuantumMutator", bundle=pybundle("QuantumMutator"),
                    properties={"Mutation Operators": "${grover.mut_%s_%s_operator}" % (mark_tag, rung),
                                "Mutation Seed": str(seed),
                                "Mutation Locus": locus,
                                "Rotation Epsilon": "${grover.mut_%s_%s_epsilon}" % (mark_tag, rung)},
                    x=X["mutate"], y=row(base_row + 1 + ri), group_id=group_id,
                    scheduled_state=NOT_RUNNING, auto_terminate=["original"])
                processors.append(mutator_rung)
                connect(filter_4q, "four_qubit", mutator_rung)
                dead_letter(mutator_rung, "failure", "build")

                mark_rung = make_processor(
                    name="Grover HW — Mark %s %s Mutant" % (mark_tag, rung),
                    proc_type=UPDATE_ATTRIBUTE, bundle=UPDATE_ATTRIBUTE_BUNDLE,
                    properties={
                        "grover.builder": fw_label,
                        "grover.case_id": "${test.case_id}",
                        "grover.partition": "${test.partition}",
                        "grover.expected": "${test.expected}",
                        "grover.kind": rung,
                        "grover.rung": rung,
                        "grover.delta_sim": "${grover.mut_%s_%s_delta}" % (mark_tag, rung),
                        "grover.epsilon": "${grover.mut_%s_%s_epsilon}" % (mark_tag, rung),
                        "grover.two_qubit_gates": "${circuit.nonlocal_gates}",
                        "grover.depth": "${circuit.depth}",
                        "grover.gate_count": "${circuit.gate_count}",
                        "circuit.label": "%s@${test.case_id}@%s" % (fw_label, rung),
                    },
                    x=X["markmutant"], y=row(base_row + 1 + ri), group_id=group_id,
                    scheduled_state=NOT_RUNNING)
                processors.append(mark_rung)
                connect(mutator_rung, "success", mark_rung)
                connect(mark_rung, "success", submitter)

    processors += [submitter, poller, expander, result_split, result_attrs,
                   result_content, oracle, report]
    if chunk_split is not None:
        processors.append(chunk_split)
    # `waiting` is AUTO-TERMINATED, never looped back: the submitter has
    # already persisted the circuit to its slot file by the time it emits
    # `waiting`, so returning that FlowFile re-appends the SAME circuit on
    # every pass (tools/add_arithmetic_hw_group.py's test_only_pending_loops_back
    # documents the incident this caused there).
    if chunk_split is not None:
        connect(submitter, "submitted", chunk_split)
        connect(chunk_split, "split", poller)
    else:
        connect(submitter, "submitted", poller)
    # `preflight` goes straight to the report: it is a costing, not a result.
    connect(submitter, "preflight", report)
    # `pending` loops back: the job is not terminal yet.
    connect(poller, "pending", poller)
    connect(poller, "success", expander)
    connect(expander, "success", result_split)
    connect(result_split, "split", result_attrs)
    connect(result_attrs, "matched", result_content)
    connect(result_content, "matched", oracle)
    for relationship in ("success", "failure"):
        connect(oracle, relationship, report)

    labels.append(make_label(
        _summary_label(provider, expected, cases, with_mutants, locus, group_name),
        X["trigger"] - 40, -260, 900, 190, group_id))

    return processors, connections, funnels, labels


def _summary_label(provider, expected, cases, with_mutants, locus, group_name):
    lines = [group_name or ("%s-%s" % (GROUP_BASE, provider))]
    lines.append("provider: %s" % provider)
    lines.append("%d cases x (3 builders + 1 readout + 1 null)%s = %d circuits"
                 % (len(cases),
                    " + ladder mutants" if with_mutants else "",
                    expected))
    if with_mutants:
        lines.append("mutants: ladder @ locus=%s" % locus)
    if PROVIDERS.get(provider, {}).get("chunked"):
        # The exact chunk count is a device read (max_jobs_per_batch_job),
        # not something this tool knows offline, so this is worded as a
        # plan, not a promise.
        lines.append("submits in ceil(N / device max_jobs_per_batch_job) batch jobs")
    for case in cases:
        lines.append("  %s: marked=%s iters=%s ideal_success=%.6f"
                     % (case["test.partition"], case["grover.marked_state"],
                        case["grover.iterations"], case["grover.ideal_success"]))
    lines.append("PREFLIGHT — no hardware submission. Arming is a separate, "
                 "deliberate property change.")
    return "\n".join(lines)


def check_not_armed(processors):
    """Refuse to write a group whose submitter would be added ARMED.

    This tool only ever builds ``Submit Mode: preflight`` (there is no CLI
    flag to change that), so this is a defensive check against a future edit
    accidentally wiring in an override -- exactly the guard
    tools/add_arithmetic_hw_group.py carries, copied verbatim in spirit.
    """
    armed = [p["name"] for p in processors
             if p["properties"].get("Submit Mode") == "armed"]
    if armed:
        raise SystemExit(
            "refusing to write: %s would be added ARMED. This tool only "
            "ever adds a group in preflight; arm it by hand, on purpose."
            % (armed,))


def check_valid_states(processors):
    invalid = sorted({p["scheduledState"] for p in processors}
                     - VALID_SCHEDULED_STATES)
    if invalid:
        raise SystemExit("refusing to write: invalid scheduledState %s" % (invalid,))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--flow", required=True, help="path to flow.json.gz (NiFi must be stopped)")
    p.add_argument("--provider", choices=sorted(PROVIDERS), default="quantum-inspire")
    p.add_argument("--device", default=None, help="override the provider default")
    p.add_argument("--shots", type=int, default=1024)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--mutants", default=str(DEFAULT_MUTANTS_FILE),
                   help="path to mutants ladder json (default: experiments/grover_hw_mutants.json). "
                        "Pass 'none' to omit mutants.")
    p.add_argument("--no-mutants", action="store_true", help="build control-side circuits only")
    p.add_argument("--cases", default=None,
                   help="comma-separated partition labels or marked states to include (e.g. asymmetric-2q, 10)")
    p.add_argument("--replicate", type=int, default=1,
                   help="replicate index (>=1) to namespace group and batch label")
    p.add_argument("--locus", choices=LOCI, default="middle",
                   help="fixed mutation locus (default: middle)")
    p.add_argument("--fixed-layout", default="",
                   help="comma-separated physical qubits to pin the batch to. "
                        "Blank searches, which is only safe for a job nothing "
                        "else will be compared against.")
    p.add_argument("--reports-dir", default=str(Path.home() / "quanifi-reports"))
    p.add_argument("--state-dir", default="",
                   help="submitter State Directory. Blank uses a "
                        "provider-namespaced default under reports/tmp/.")
    p.add_argument("--batch-namespace", default="",
                   help="prefix for Batch Label, so two clones' slot files "
                        "stay distinct even in a shared State Directory")
    p.add_argument("--group-name", default=None,
                   help="override the process-group name. Defaults to "
                        "GroverMatrix-HW-<provider>[-rep<N>]")
    p.add_argument("--remove", action="store_true")
    args = p.parse_args()

    mutants_arg = None if (args.no_mutants or (args.mutants and args.mutants.lower() in ("none", "false", "no", ""))) else args.mutants

    if args.replicate > 1 and not args.group_name:
        group_name = "%s-%s-rep%d" % (GROUP_BASE, args.provider, args.replicate)
    else:
        group_name = args.group_name or ("%s-%s" % (GROUP_BASE, args.provider))

    flow_path = Path(args.flow).expanduser()
    if not flow_path.exists():
        raise SystemExit("no such flow file: %s" % flow_path)

    backup = flow_path.with_name(
        "%s.bak_grover_hw_%s"
        % (flow_path.name, datetime.now().strftime("%Y%m%d_%H%M%S_%f")))
    shutil.copy2(flow_path, backup)
    with gzip.open(flow_path, "rt", encoding="utf-8") as handle:
        flow = json.load(handle)

    root = flow["rootGroup"]
    groups = root.setdefault("processGroups", [])
    existing = [g for g in groups if g.get("name") == group_name]

    if args.remove:
        if not existing:
            raise SystemExit("group not present: %s" % group_name)
        groups[:] = [g for g in groups if g.get("name") != group_name]
        action = "removed"
    else:
        if existing:
            raise SystemExit("group already exists: %s (use --remove first)" % group_name)
        group_id = uid()
        device = args.device or PROVIDERS[args.provider]["device"]
        processors, connections, funnels, labels = build_group(
            group_id, args.provider, device, args.shots, args.seed,
            args.reports_dir, fixed_layout=args.fixed_layout,
            mutants=mutants_arg, locus=args.locus,
            state_dir=args.state_dir, batch_namespace=args.batch_namespace,
            group_name=group_name, cases_filter=args.cases,
            replicate=args.replicate)
        check_valid_states(processors)
        check_not_armed(processors)
        gx, gy = _group_position(args.provider)
        groups.append({
            "identifier": group_id, "instanceIdentifier": uid(),
            "name": group_name, "comments": GROUP_COMMENT,
            "position": {"x": gx, "y": gy},
            "processGroups": [], "remoteProcessGroups": [],
            "processors": processors, "connections": connections,
            "inputPorts": [], "outputPorts": [], "labels": labels,
            "funnels": funnels,
            "controllerServices": [],
            "defaultFlowFileExpiration": "0 sec",
            "defaultBackPressureObjectThreshold": 10000,
            "defaultBackPressureDataSizeThreshold": "1 GB",
            "scheduledState": "ENABLED", "executionEngine": "INHERITED",
            "maxConcurrentTasks": 1, "statelessFlowTimeout": "1 min",
            "flowFileOutboundPolicy": "STREAM_WHEN_AVAILABLE",
            "flowFileConcurrency": "UNBOUNDED",
            "componentType": "PROCESS_GROUP",
            "groupIdentifier": root["identifier"],
        })
        action = "added %d processors, %d connections" % (len(processors), len(connections))

    with gzip.open(flow_path, "wt", encoding="utf-8") as handle:
        json.dump(flow, handle)

    print("%s: %s" % (group_name, action))
    print("flow  : %s" % flow_path)
    print("backup: %s" % backup)
    if not args.remove:
        print("\nAll processors are STOPPED and the submitter is in PREFLIGHT mode.")
        print("Start the group, trigger it once, and read the preflight report:")
        print("  batch.estimated_usage_seconds, batch.two_qubit_gates,")
        print("  batch.padded_width, batch.device_qubits")
        print("Nothing is submitted until Submit Mode is set to 'armed'.")


if __name__ == "__main__":
    main()
