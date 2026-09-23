import json
import itertools
import os
import random
import sys
import time

# NiFi loads each processor in its own module context without the extensions
# directory on sys.path, so the sibling-module import below fails without this.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder

import arithmetic_spec as aspec


# ---------------------------------------------------------------------------
# Layer B mutation operators (config / EL-parameter mutation)
# ---------------------------------------------------------------------------
# Each operator mutates one attribute of a test-case row *before* it is fanned
# out to the framework branches, so the branch reading ${grover.marked_state}
# never knows it is running a mutant.  Detection is by cross-framework
# consensus downstream (see MUTATION_TESTING.md §3).
#
# An operator is `(target_attribute, fn)` where ``fn(value, rng) -> new_value``
# returns the mutated string, or ``None`` if the mutation is inapplicable to
# this value (the mutant is then skipped, not emitted).  Functions take a seeded
# ``random.Random`` so a given Mutation Seed reproduces identical mutants.
#
# To add the Hamiltonian / QPE operators from the catalog, register them here;
# the processor needs no other change.

def _mut_bitflip(value, rng):
    """Flip one character of a bitstring (0<->1)."""
    if not value:
        return None
    i = rng.randrange(len(value))
    flipped = "1" if value[i] == "0" else "0"
    return value[:i] + flipped + value[i + 1:]


def _mut_lenshift(value, rng):
    """Grow or shrink a bitstring by one char (qubit-count mismatch)."""
    if value and len(value) > 1 and rng.random() < 0.5:
        return value[:-1]
    return (value or "") + "0"


def _mut_int_plus_one(value, rng):
    """Off-by-one on an integer-valued attribute (e.g. over-rotation)."""
    try:
        return str(int(value) + 1)
    except (TypeError, ValueError):
        return None


def _mut_to_zero(value, rng):
    return "0"


def _mut_shots_shrink(value, rng):
    """Collapse the shot budget to amplify shot noise."""
    return "8"


def _mut_format_swap(value, rng):
    return "qasm3" if value == "qasm2" else "qasm2"


def _mut_bool_toggle(value, rng):
    truthy = str(value).strip().lower() in ("true", "1", "yes", "on")
    return "false" if truthy else "true"


def _mut_noise_inject(value, rng):
    return "depolarizing" if (value or "none") == "none" else "none"


# name -> (target attribute, transform fn)
_MUTATION_OPERATORS = {
    "marked_state.bitflip":  ("grover.marked_state",    _mut_bitflip),
    "marked_state.lenshift": ("grover.marked_state",    _mut_lenshift),
    "iterations.offbyone":   ("grover.num_iterations",  _mut_int_plus_one),
    "iterations.zero":       ("grover.num_iterations",  _mut_to_zero),
    "shots.shrink":          ("grover.shots",           _mut_shots_shrink),
    "format.swap":           ("circuit.output_format",  _mut_format_swap),
    "barriers.toggle":       ("grover.insert_barriers", _mut_bool_toggle),
    "noise.inject":          ("sim.noise_model",        _mut_noise_inject),
}


# --- case input modes -------------------------------------------------------
# `Arithmetic Suite` states a *rule* for picking operand pairs; a manifest
# states the pairs themselves.  Both end at the same derived cases and the same
# rows -- the difference is only whether the experimental inputs are visible on
# the canvas.  The mode is declared, never inferred: an armed hardware path must
# not switch case definitions because upstream content happened to look like
# JSON, and the current trigger's content is the literal string "go".

MODE_LEGACY    = "legacy-auto"
MODE_GENERATED = "generated-suite"
MODE_INLINE    = "inline-arithmetic-json"
MODE_CONTENT   = "flowfile-content"
CASE_INPUT_MODES = (MODE_LEGACY, MODE_GENERATED, MODE_INLINE, MODE_CONTENT)

#: What `testsource.input_mode` reports when legacy-auto resolved to the
#: generic table/axes path.  Not selectable as a mode: `Test Matrix` stays
#: generic and is never retrofitted with arithmetic semantics.
INPUT_MODE_MATRIX = "test-matrix"


def _expand_arithmetic(spec_text, seed):
    """Turn an ``Arithmetic Suite`` spec into derived arithmetic cases.

    Spec is ``strategy:bit_width`` with an optional ``:operation``, e.g.
    ``exhaustive:2`` or ``boundary:3:add``.  Every operand pair the strategy
    selects becomes one case carrying the operands, the classical answer and
    the boundary conditions it covers.

    The row built from a case deliberately does **not** carry an expected
    *bitstring*: that depends on the register layout, which differs per
    implementation, so each builder computes it from ``arithmetic_spec`` and
    publishes it downstream.  The row carries the operands and the expected
    integer, which is the part that is implementation-independent.

    Returns (cases, description).  Raises ValueError on a malformed spec.
    """
    parts = [p.strip() for p in spec_text.split(":")]
    if len(parts) not in (2, 3):
        raise ValueError(
            "Arithmetic Suite must be 'strategy:bit_width[:operation]', got %r"
            % (spec_text,))
    strategy = parts[0]
    if strategy not in aspec.SUITE_STRATEGIES:
        raise ValueError("unknown suite strategy %r; expected one of %s"
                         % (strategy, ", ".join(aspec.SUITE_STRATEGIES)))
    try:
        bit_width = int(parts[1])
    except ValueError:
        raise ValueError("bit width must be an integer, got %r" % (parts[1],))
    operation = parts[2] if len(parts) == 3 else "add"

    cases = aspec.suite(strategy, bit_width, operation, seed)
    return cases, aspec.describe_suite(cases, strategy, bit_width, seed)


def _derive_seed(base_seed, index):
    """Reproducible per-mutant seed mixed from the run base seed + ordinal."""
    return (base_seed * 1_000_003 + index) & 0x7FFFFFFF


def _expand_matrix(matrix):
    """Turn a Test Matrix spec into an ordered list of test-case row dicts.

    Two shapes are accepted (detected by JSON type):

    * **Explicit table** — a JSON *array* of objects; each object is one case,
      a flat ``{attribute-name: value}`` dict used verbatim.  Full control,
      including a per-row ``test.expected``.

    * **Parameter axes** — a JSON *object* mapping an attribute name to a list
      of values, e.g. ``{"grover.marked_state": ["00", "11"],
      "grover.num_iterations": ["1", "2"]}``.  The Cartesian product of the
      axes is emitted (here: 4 cases).  Axis/insertion order is preserved so
      case ids are deterministic.

    Returns (rows, mode).  Raises ValueError on a malformed spec.
    """
    if isinstance(matrix, list):
        rows = []
        for i, row in enumerate(matrix):
            if not isinstance(row, dict):
                raise ValueError(f"table row {i} is not an object: {row!r}")
            rows.append({k: row[k] for k in row})
        return rows, "table"

    if isinstance(matrix, dict):
        if not matrix:
            return [], "axes"
        names = list(matrix)
        value_lists = []
        for name in names:
            vals = matrix[name]
            if not isinstance(vals, list) or not vals:
                raise ValueError(f"axis '{name}' must be a non-empty list, got {vals!r}")
            value_lists.append(vals)
        rows = [dict(zip(names, combo)) for combo in itertools.product(*value_lists)]
        return rows, "axes"

    raise ValueError(f"Test Matrix must be a JSON array or object, got {type(matrix).__name__}")


class QuantumTestCaseSource(FlowFileTransform):
    """
    Data-driven test-case generator for differential / equivalence-partition
    runs.  Reads a compact test-matrix spec (explicit table or parameter axes)
    and emits a JSON *array* of test-case rows on a single FlowFile.

    Wire it as::

        GenerateFlowFile  ->  QuantumTestCaseSource  ->  SplitJson ($)  ->
        EvaluateJsonPath (row fields -> flowfile attributes)  ->  framework branches

    Every emitted row carries the input parameters as attributes (e.g.
    ``grover.marked_state``) plus bookkeeping fields:

    * ``test.case_id``   — stable id ``{prefix}-{NNN}`` (unless the row sets it)
    * ``test.run_id``    — shared by all rows of one generation, so a downstream
                           consensus oracle can correlate the K framework
                           results belonging to the same run.
    * ``test.partition`` — equivalence-class label (from the row, or the
                           static Partition Label property).

    Because the rows are plain attribute dicts, the same source drives any
    algorithm/framework — the attribute names are the only contract.

    **Arithmetic mode.** Setting ``Arithmetic Suite`` (e.g. ``exhaustive:2``)
    generates the operand pairs itself instead of expanding ``Test Matrix``, so
    the canvas covers exactly the inputs ``experiments/arithmetic_study.py``
    covers headlessly — both call ``arithmetic_spec.suite``. Rows carry the
    ``arithmetic.*`` attributes the builders read, plus a ``test.partition``
    naming the boundary conditions (``carry_out``, ``max_a``, ...) that input
    covers, which is what lets a report say *which* inputs caught a fault.

    **Explicit case manifests.** ``Arithmetic Suite`` states a *rule* for
    picking operands; the canvas then shows ``boundary:2`` and not the seven
    pairs it selects. ``Case Input Mode`` adds two modes that state the pairs
    themselves — ``inline-arithmetic-json`` reads the ``Arithmetic Cases``
    property, ``flowfile-content`` reads the incoming FlowFile (put a reviewed
    file there with a stock ``FetchFile``). Both go through one parser in
    ``arithmetic_spec.parse_manifest`` and one row builder, so the same operand
    pairs emit byte-identical rows whichever way they arrived. The manifest
    supplies only inputs: expected answers, carry classification and partitions
    are always derived, so a hand-edited file cannot become the oracle.

    ``Expected Case Count`` / ``Expected Case Set SHA-256`` /
    ``Expected Manifest SHA-256`` pin a campaign to one case set and fail closed
    before a single row is emitted, because the batch size downstream is a
    static number and a quietly-changed case list leaves the submitter waiting
    for a circuit nobody will build. The default mode is ``legacy-auto``, which
    reproduces the historical precedence exactly, so every existing canvas keeps
    working untouched.

    **Mutation pass (Layer B).** When ``Mutation Operators`` is non-empty, each
    base row is expanded into one unmutated *control* row (``mut.applied=false``)
    plus one or more *mutant* rows (``mut.applied=true``) that rewrite a single
    attribute per the operator catalog. Mutants carry ``mut.operator`` /
    ``mut.target_attr`` / ``mut.original_value`` / ``mut.seed`` /
    ``mut.base_case_id`` so a downstream consensus oracle can score them and so
    control dissents (a real framework discrepancy, never a kill) can be excluded
    from the survival rate. With no operators configured the output is byte-for-
    byte the legacy behaviour. See MUTATION_TESTING.md.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Expands a compact test-matrix spec (explicit JSON table or parameter "
            "axes) into a JSON array of test-case rows for data-driven / differential "
            "testing. Each row carries input parameters plus test.case_id / test.run_id "
            "/ test.partition. Feed into SplitJson to fan out one FlowFile per case."
        )
        tags = ["quantum", "testing", "data-driven", "equivalence-partitioning",
                "differential", "mutation"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.test_matrix = PropertyDescriptor(
            name="Test Matrix",
            description=(
                "The test-case spec as JSON. A JSON ARRAY is an explicit table — each "
                "element is one case, a flat {attribute: value} object (full control, "
                "including a per-row 'test.expected'). A JSON OBJECT is parameter axes — "
                "{attribute: [values...]} — whose Cartesian product is emitted. "
                "Example axes: "
                "{\"grover.marked_state\": [\"00\", \"11\", \"000\"], "
                "\"grover.num_iterations\": [\"1\", \"2\"]}."
            ),
            required=True,
            default_value=(
                '{"grover.marked_state": ["00", "11"], '
                '"grover.num_iterations": ["1"]}'
            ),
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.arithmetic_suite = PropertyDescriptor(
            name="Arithmetic Suite",
            description=(
                "Generate arithmetic test cases instead of expanding Test Matrix. "
                "Format 'strategy:bit_width[:operation]', e.g. 'exhaustive:2' (all 16 "
                "operand pairs at 2 bits) or 'boundary:3:add' (zero, maximum and "
                "carrying operands only). Each row carries arithmetic.operation / "
                ".operand_a / .operand_b / .bit_width / .expected_result and a "
                "test.partition naming the boundary conditions it covers. Leave blank "
                "for the Test Matrix behaviour."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.case_id_prefix = PropertyDescriptor(
            name="Case ID Prefix",
            description=(
                "Prefix for the auto-assigned test.case_id ({prefix}-000, {prefix}-001, "
                "...). Ignored for any row that already sets test.case_id."
            ),
            required=True,
            default_value="case",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.partition_label = PropertyDescriptor(
            name="Partition Label",
            description=(
                "Optional equivalence-class label written to test.partition on every "
                "row that does not set its own. Leave blank to omit."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.run_id = PropertyDescriptor(
            name="Run ID",
            description=(
                "Shared correlation id stamped onto every row as test.run_id so a "
                "downstream consensus oracle can group the K framework results of one "
                "run. Leave blank to auto-generate a timestamp-based id per trigger."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.mutation_operators = PropertyDescriptor(
            name="Mutation Operators",
            description=(
                "Comma-separated Layer-B (config) mutation operators to apply. Leave "
                "blank to disable mutation (legacy behaviour). When set, each base row "
                "yields one unmutated control row plus mutant rows. Available: "
                + ", ".join(sorted(_MUTATION_OPERATORS)) + "."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.mutants_per_row = PropertyDescriptor(
            name="Mutants Per Row",
            description=(
                "How many mutant rows to emit per base row, cycling through the operator "
                "list in order (so N > #operators repeats them with fresh seeds, hitting "
                "different positions). Blank = one mutant per listed operator. 0 = controls "
                "only. Ignored when Mutation Operators is blank."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.mutation_seed = PropertyDescriptor(
            name="Mutation Seed",
            description=(
                "Integer base seed for reproducible mutants (same seed -> identical "
                "mutations). Blank = a time-based seed per trigger. The per-mutant derived "
                "seed is recorded on each mutant as mut.seed."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        # --- explicit case definitions ------------------------------------
        # These are campaign controls, not per-FlowFile parameters, so they are
        # declared with ExpressionLanguageScope.NONE: an incoming attribute must
        # not be able to rewrite which cases an armed hardware batch runs. NiFi
        # Parameter Contexts still substitute into them at deployment time.
        self.case_input_mode = PropertyDescriptor(
            name="Case Input Mode",
            description=(
                "Where the test cases come from. 'legacy-auto' (default) reproduces "
                "the historical precedence exactly: a non-blank Arithmetic Suite wins, "
                "otherwise Test Matrix is expanded. 'generated-suite' requires "
                "Arithmetic Suite. 'inline-arithmetic-json' requires Arithmetic Cases. "
                "'flowfile-content' parses the incoming FlowFile content (put the file "
                "there with a stock NiFi FetchFile). The explicit modes never fall back "
                "to another input on error, so a malformed definition cannot silently "
                "run a stale one."
            ),
            required=True,
            default_value=MODE_LEGACY,
            allowable_values=list(CASE_INPUT_MODES),
            expression_language_scope=ExpressionLanguageScope.NONE,
        )
        self.arithmetic_cases = PropertyDescriptor(
            name="Arithmetic Cases",
            description=(
                "The versioned arithmetic case manifest, inline, used by "
                "'inline-arithmetic-json' mode. A JSON object with schema / suite_id / "
                "operation / bit_width / cases, where each case declares only its "
                "operands: {\"a\": 0, \"b\": 3}. Expected answers, carry classification "
                "and partitions are always DERIVED from those inputs, never read from "
                "the manifest, so a typo cannot become the oracle. Schema: "
                + aspec.MANIFEST_SCHEMA_V1 + "."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.NONE,
        )
        self.expected_case_count = PropertyDescriptor(
            name="Expected Case Count",
            description=(
                "Fail-closed guard: refuse to emit anything unless exactly this many "
                "base cases were produced, counted BEFORE any Layer-B mutation "
                "expansion. Blank disables the check. A hardware campaign should set "
                "it, because the downstream batch size is a static number and a "
                "changed case count leaves the submitter waiting for a circuit that "
                "will never arrive."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.NONE,
        )
        self.expected_case_set_sha = PropertyDescriptor(
            name="Expected Case Set SHA-256",
            description=(
                "Fail-closed treatment-identity guard for any arithmetic mode. Hashes "
                "the operation, bit width and ordered operand pairs only -- not the "
                "schema or suite_id -- so reformatting or relabelling a manifest does "
                "not trip it, while changing or reordering a case does. A different "
                "case-set digest is a different test-case condition. Blank disables."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.NONE,
        )
        self.expected_manifest_sha = PropertyDescriptor(
            name="Expected Manifest SHA-256",
            description=(
                "Fail-closed guard on the normalized manifest: the case set plus its "
                "schema and suite_id. Catches a relabelling that the case-set digest "
                "deliberately ignores. Explicit manifest modes only; blank disables."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.NONE,
        )
        self.maximum_cases = PropertyDescriptor(
            name="Maximum Cases",
            description=(
                "Hard safety limit on how many cases a manifest may declare, checked "
                "before any campaign- or provider-specific circuit-count validation."
            ),
            required=False,
            default_value=str(aspec.MAX_MANIFEST_CASES),
            expression_language_scope=ExpressionLanguageScope.NONE,
        )
        self.maximum_definition_bytes = PropertyDescriptor(
            name="Maximum Definition Bytes",
            description=(
                "Hard size limit on an inline or FlowFile-content manifest, so an "
                "accidental or hostile oversized definition is rejected before it is "
                "parsed."
            ),
            required=False,
            default_value=str(aspec.MAX_MANIFEST_BYTES),
            expression_language_scope=ExpressionLanguageScope.NONE,
        )
        self.descriptors = [
            self.test_matrix,
            self.arithmetic_suite,
            self.case_input_mode,
            self.arithmetic_cases,
            self.expected_case_count,
            self.expected_case_set_sha,
            self.expected_manifest_sha,
            self.maximum_cases,
            self.maximum_definition_bytes,
            self.case_id_prefix,
            self.partition_label,
            self.run_id,
            self.mutation_operators,
            self.mutants_per_row,
            self.mutation_seed,
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

        def raw(prop):
            """Read a campaign-control property WITHOUT expression language.

            These are declared ExpressionLanguageScope.NONE precisely so an
            incoming FlowFile attribute cannot rewrite which cases run; reading
            them through evaluateAttributeExpressions would undo that.
            """
            return context.getProperty(prop).getValue()

        raw_matrix = get(self.test_matrix)
        arith_raw  = (get(self.arithmetic_suite) or "").strip()
        prefix     = get(self.case_id_prefix)
        partition  = (get(self.partition_label) or "").strip()
        run_id     = (get(self.run_id) or "").strip() or f"run-{int(time.time() * 1000)}"
        ops_raw    = (get(self.mutation_operators) or "").strip()
        per_row_raw = (get(self.mutants_per_row) or "").strip()
        seed_raw   = (get(self.mutation_seed) or "").strip()

        input_mode_raw = (raw(self.case_input_mode) or MODE_LEGACY).strip() or MODE_LEGACY
        inline_raw     = raw(self.arithmetic_cases) or ""
        want_count     = (raw(self.expected_case_count) or "").strip()
        want_case_set  = (raw(self.expected_case_set_sha) or "").strip().lower()
        want_manifest  = (raw(self.expected_manifest_sha) or "").strip().lower()
        max_cases_raw  = (raw(self.maximum_cases) or "").strip()
        max_bytes_raw  = (raw(self.maximum_definition_bytes) or "").strip()

        # The suite seed reuses Mutation Seed when it is set, so one property
        # pins the whole generation; 0 keeps it deterministic when it is not.
        suite_seed = int(seed_raw) if seed_raw.lstrip("-").isdigit() else 0
        suite_desc = None

        def fail(error, code, body, log=None):
            """Route to failure with a stable code, emitting no cases at all."""
            self.logger.warn("QuantumTestCaseSource: {}".format(log or error))
            return FlowFileTransformResult(
                relationship="failure",
                contents=body,
                attributes={"testsource.error": str(error),
                            "testsource.error_code": code},
            )

        if input_mode_raw not in CASE_INPUT_MODES:
            return fail("unknown Case Input Mode %r; expected one of %s"
                        % (input_mode_raw, ", ".join(CASE_INPUT_MODES)),
                        "mode.unknown", (raw_matrix or "").encode("utf-8"))
        try:
            max_cases = int(max_cases_raw) if max_cases_raw else aspec.MAX_MANIFEST_CASES
            max_bytes = int(max_bytes_raw) if max_bytes_raw else aspec.MAX_MANIFEST_BYTES
            if max_cases < 1 or max_bytes < 1:
                raise ValueError
        except ValueError:
            return fail("Maximum Cases and Maximum Definition Bytes must be positive "
                        "integers, got %r and %r" % (max_cases_raw, max_bytes_raw),
                        "limit.invalid", (raw_matrix or "").encode("utf-8"))

        # `cases` is set by every arithmetic path and stays None for the generic
        # table/axes path; `manifest` is set only when an explicit definition
        # was supplied. `offending` is the definition a failure should hand back
        # so the dead-letter FlowFile carries the input that was actually wrong.
        cases = manifest = None
        rows = mode = None
        offending = (raw_matrix or "").encode("utf-8")

        if input_mode_raw in (MODE_LEGACY, MODE_GENERATED):
            if inline_raw.strip():
                return fail("Arithmetic Cases is set but Case Input Mode is %r; select "
                            "'inline-arithmetic-json' or blank the property, so the "
                            "canvas states exactly one case definition" % (input_mode_raw,),
                            "mode.conflict", inline_raw.encode("utf-8"))
            if input_mode_raw == MODE_GENERATED and not arith_raw:
                return fail("Case Input Mode is 'generated-suite' but Arithmetic Suite "
                            "is blank", "mode.missing_input", b"")
            if arith_raw:
                try:
                    cases, suite_desc = _expand_arithmetic(arith_raw, suite_seed)
                except ValueError as exc:
                    return fail(exc, "suite.invalid", arith_raw.encode("utf-8"),
                                log="bad Arithmetic Suite: {}".format(exc))
                input_mode = MODE_GENERATED
                offending = arith_raw.encode("utf-8")
            else:
                try:
                    matrix = json.loads(raw_matrix)
                    rows, mode = _expand_matrix(matrix)
                except (ValueError, json.JSONDecodeError) as exc:
                    return fail(exc, "matrix.invalid", (raw_matrix or "").encode("utf-8"),
                                log="bad Test Matrix: {}".format(exc))
                input_mode = INPUT_MODE_MATRIX
        else:
            # Explicit manifest modes never fall back: a malformed file must not
            # silently run whatever the other property still says.
            if arith_raw:
                return fail("Arithmetic Suite is set to %r but Case Input Mode is %r; "
                            "blank it so the canvas states exactly one case definition"
                            % (arith_raw, input_mode_raw),
                            "mode.conflict", arith_raw.encode("utf-8"))
            if input_mode_raw == MODE_INLINE:
                definition = inline_raw.encode("utf-8")
                if not inline_raw.strip():
                    return fail("Case Input Mode is 'inline-arithmetic-json' but "
                                "Arithmetic Cases is blank", "mode.missing_input", b"")
            else:
                definition = bytes(flowFile.getContentsAsBytes())
                if not definition.strip():
                    return fail("Case Input Mode is 'flowfile-content' but the incoming "
                                "FlowFile has no content", "mode.missing_input", definition)
            offending = definition
            try:
                manifest = aspec.parse_manifest(definition, max_bytes=max_bytes,
                                                max_cases=max_cases)
            except aspec.ArithmeticManifestError as exc:
                return fail(exc, getattr(exc, "code", "manifest.invalid"), definition,
                            log="bad arithmetic manifest: {}".format(exc))
            cases = manifest["cases"]
            input_mode = input_mode_raw

        if cases is not None:
            rows = aspec.case_rows(cases)
            mode = "arithmetic"

        if not rows:
            return fail("Test Matrix expanded to zero cases", "matrix.empty", b"[]")

        # --- fail-closed guards, before any row leaves the processor ----------
        base_count = len(rows)
        case_set_sha = (aspec.case_set_digest(cases[0]["operation"], cases[0]["bit_width"],
                                              aspec.case_pairs(cases))
                        if cases is not None else None)

        if want_count:
            try:
                wanted = int(want_count)
            except ValueError:
                return fail("Expected Case Count must be an integer, got %r" % (want_count,),
                            "guard.count_invalid", offending)
            if wanted != base_count:
                return fail("expected %d base cases but the input produced %d; refusing "
                            "to emit a batch the downstream circuit count does not match"
                            % (wanted, base_count), "guard.count", offending)
        if want_case_set:
            if case_set_sha is None:
                return fail("Expected Case Set SHA-256 is set but this input mode "
                            "produces no arithmetic case set",
                            "guard.case_set_unavailable", offending)
            if want_case_set != case_set_sha:
                return fail("case-set digest %s does not match the pinned %s; a different "
                            "case set is a different test-case condition, not a rerun"
                            % (case_set_sha, want_case_set), "guard.case_set_digest",
                            offending)
        if want_manifest:
            if manifest is None:
                return fail("Expected Manifest SHA-256 is set but this input mode has no "
                            "explicit manifest", "guard.manifest_unavailable", offending)
            if want_manifest != manifest["manifest_sha256"]:
                return fail("manifest digest %s does not match the pinned %s"
                            % (manifest["manifest_sha256"], want_manifest),
                            "guard.manifest_digest", offending)

        # --- resolve the mutation config (validate before doing any work) -----
        op_names = [o.strip() for o in ops_raw.split(",") if o.strip()]
        mutate = bool(op_names)
        unknown = [o for o in op_names if o not in _MUTATION_OPERATORS]
        if unknown:
            msg = "unknown mutation operator(s): {}".format(", ".join(unknown))
            return fail(msg, "mutation.unknown_operator", (raw_matrix or "").encode("utf-8"))
        if per_row_raw:
            try:
                n_per_row = int(per_row_raw)
                if n_per_row < 0:
                    raise ValueError
            except ValueError:
                msg = "Mutants Per Row must be a non-negative integer, got {!r}".format(per_row_raw)
                return fail(msg, "mutation.bad_per_row", (raw_matrix or "").encode("utf-8"))
        else:
            n_per_row = len(op_names)
        base_seed = int(seed_raw) if seed_raw.lstrip("-").isdigit() else int(time.time() * 1000)

        # --- expand each base row into (optional) control + mutant cases ------
        # `ctrl_idx[i]` is the emitted-index of the control a mutant derives from
        # (None for non-mutants), resolved to mut.base_case_id after id assignment.
        emitted = []
        ctrl_idx = []
        mutant_ordinal = 0
        for row in rows:
            if not mutate:
                emitted.append(dict(row))
                ctrl_idx.append(None)
                continue
            control_pos = len(emitted)
            control = dict(row)
            control["mut.applied"] = "false"
            emitted.append(control)
            ctrl_idx.append(None)
            for k in range(n_per_row):
                op_name = op_names[k % len(op_names)]
                target, fn = _MUTATION_OPERATORS[op_name]
                if target not in row:
                    self.logger.warn(
                        "QuantumTestCaseSource: operator '{}' inapplicable (no '{}' in row), "
                        "skipping".format(op_name, target)
                    )
                    continue
                seed = _derive_seed(base_seed, mutant_ordinal)
                mutant_ordinal += 1
                new_val = fn(str(row[target]), random.Random(seed))
                if new_val is None:
                    continue
                mutant = dict(row)
                mutant[target] = new_val
                mutant["mut.applied"] = "true"
                mutant["mut.operator"] = op_name
                mutant["mut.target_attr"] = target
                mutant["mut.original_value"] = str(row[target])
                mutant["mut.seed"] = str(seed)
                emitted.append(mutant)
                ctrl_idx.append(control_pos)

        # --- assign bookkeeping (case_id / run_id / partition), then linkage --
        width = max(3, len(str(len(emitted) - 1)))
        for i, case in enumerate(emitted):
            # Bookkeeping fields are defaults — never clobber a value the row set itself.
            case.setdefault("test.case_id", f"{prefix}-{i:0{width}d}")
            case.setdefault("test.run_id", run_id)
            if partition:
                case.setdefault("test.partition", partition)
        for i, case in enumerate(emitted):
            if ctrl_idx[i] is not None:
                case["mut.base_case_id"] = emitted[ctrl_idx[i]]["test.case_id"]

        cases = emitted
        n_mutants = sum(1 for c in cases if c.get("mut.applied") == "true")
        n_controls = sum(1 for c in cases if c.get("mut.applied") == "false")
        self.logger.warn(
            "QuantumTestCaseSource: {} cases ({} mode), run_id={}, mutants={}, controls={}".format(
                len(cases), mode, run_id, n_mutants, n_controls
            )
        )

        attributes = {
            # Unchanged meaning: total rows emitted, AFTER the optional Layer-B
            # expansion. testsource.base_case_count is the pre-expansion count
            # that Expected Case Count validates; they are equal whenever
            # mutation is off, which is the case on the hardware canvas.
            "testsource.count":    str(len(cases)),
            "testsource.mode":     mode,
            "testsource.input_mode": input_mode,
            "testsource.base_case_count": str(base_count),
            "testsource.run_id":   run_id,
            "testsource.mutated":  "true" if mutate else "false",
            "testsource.mutants":  str(n_mutants),
            "testsource.controls": str(n_controls),
            "mime.type":           "application/json",
        }
        if case_set_sha is not None:
            # Twice on purpose. The testsource.* copy is the source-level
            # provenance a canvas reader looks at; the arithmetic.* copy is the
            # one the batch submitter captures onto the manifest entry, so it is
            # the only one that survives into a hardware result.
            attributes["testsource.case_set_sha256"] = case_set_sha
            attributes[aspec.ATTR_CASE_SET_SHA] = case_set_sha
        if manifest is not None:
            attributes.update({
                "testsource.manifest_sha256": manifest["manifest_sha256"],
                # Audit only: the exact supplied bytes. Reformatting the file
                # changes this while leaving both normalized digests alone.
                "testsource.raw_sha256":      manifest["raw_sha256"],
                "testsource.schema":          manifest["schema"],
                "testsource.suite_id":        manifest["suite_id"],
                aspec.ATTR_MANIFEST_SHA:      manifest["manifest_sha256"],
                aspec.ATTR_SUITE_ID:          manifest["suite_id"],
                aspec.ATTR_SUITE_SCHEMA:      manifest["schema"],
            })
        if suite_desc is not None:
            # Recorded so a run is reproducible from the FlowFile alone.
            attributes.update({
                "testsource.suite":            suite_desc["strategy"],
                "testsource.bit_width":        str(suite_desc["bit_width"]),
                "testsource.suite_seed":       str(suite_desc["seed"]),
                "testsource.boundaries":       ",".join(suite_desc["boundaries_covered"]),
                "testsource.carrying_cases":   str(suite_desc["carrying_cases"]),
            })

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(cases, indent=2).encode("utf-8"),
            attributes=attributes,
        )
