"""
The Grover N-M hardware canvas group: it must be correct, and it must not be
able to spend.

Mirrors tests/test_arithmetic_hw_group.py's two classes of check. The first is
the same property-name / bundle-version validation the simulator groups get,
because a property NiFi does not recognise is silently ignored and a wrong
bundle version produces a Ghost component. The second is specific to a
hardware group: every path by which it could submit a job is asserted shut.
"""

import importlib
import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import add_grover_hw_matrix_group as tool


#: Every processor TYPE this tool places on the canvas that is a
#: python-extensions module with a real PropertyDescriptor set to validate
#: against. Standard NiFi processors (GenerateFlowFile, SplitJson,
#: EvaluateJsonPath) and UpdateAttribute (a Java nar, not ours) are excluded.
PROCESSOR_MODULES = {
    "QuantumTestCaseSource", "QuantumSuccessProbabilityOracle",
    "QiskitGroverCircuit", "CirqGroverCircuit", "PennylaneGroverCircuit",
    "QuantumIBMBatchSubmitter", "QuantumIBMBatchPoller",
    "QuantumIQMBatchSubmitter", "QuantumIQMBatchPoller",
    "QuantumInspireBatchSubmitter", "QuantumInspireBatchPoller",
    "QuantumMutator", "QuantumBatchResultExpander", "QuanifiReport",
    "ReadoutBaselineCircuit",
}


@pytest.fixture(params=sorted(tool.PROVIDERS))
def group(request):
    provider = request.param
    processors, connections, funnels, labels = tool.build_group(
        "gid", provider, tool.PROVIDERS[provider]["device"], 1024, 11,
        "/tmp/quanifi-reports")
    return provider, processors, connections


@pytest.fixture(params=sorted(tool.PROVIDERS))
def control_group(request):
    provider = request.param
    processors, connections, funnels, labels = tool.build_group(
        "gid", provider, tool.PROVIDERS[provider]["device"], 1024, 11,
        "/tmp/quanifi-reports", mutants=None)
    return provider, processors, connections


@pytest.fixture(params=sorted(tool.PROVIDERS))
def mutant_group(request):
    provider = request.param
    processors, connections, funnels, labels = tool.build_group(
        "gid", provider, tool.PROVIDERS[provider]["device"], 1024, 11,
        "/tmp/quanifi-reports", mutant="gate.remove", locus="middle")
    return provider, processors, connections


class TestNothingCanSpend:

    def test_submitter_is_added_in_preflight(self, group):
        _, processors, _ = group
        submitter = next(p for p in processors if "Submit Mode" in p["properties"])
        assert submitter["properties"]["Submit Mode"] == "preflight"

    def test_every_processor_is_stopped(self, group):
        _, processors, _ = group
        assert all(p["scheduledState"] != "RUNNING" for p in processors)
        assert {p["scheduledState"] for p in processors} <= tool.VALID_SCHEDULED_STATES

    def test_no_token_is_baked_into_the_flow(self, group):
        """A credential in a flow file is a credential in a git repo."""
        _, processors, _ = group
        for processor in processors:
            assert processor["properties"].get("API Token", "") == ""

    def test_guards_are_configured(self, group):
        provider, processors, _ = group
        submitter = next(p for p in processors if "Submit Mode" in p["properties"])
        assert int(submitter["properties"]["Maximum Circuits"]) > 0
        assert int(submitter["properties"]["Maximum Shots Per Circuit"]) > 0
        assert float(submitter["properties"]["Maximum Estimated Usage Seconds"]) > 0
        if provider == "iqm":
            assert submitter["properties"]["Maximum Circuits"] == "100"
        if provider == "quantum-inspire":
            assert submitter["properties"]["Maximum Circuits"] == "50"
        if provider == "ibm":
            assert submitter["properties"]["Maximum Circuits"] == "256"

    def test_allow_unreadable_credit_balance_is_set_only_on_the_iqm_lane(self, group):
        """The repo owner accepted the risk of submitting to IQM without a
        credit check for this study only -- ibm and quantum-inspire read
        real balances/quotas and must keep the default (unset/false)."""
        provider, processors, _ = group
        submitter = next(p for p in processors if "Submit Mode" in p["properties"])
        if provider == "iqm":
            assert submitter["properties"]["Allow Unreadable Credit Balance"] == "true"
        else:
            assert submitter["properties"].get(
                "Allow Unreadable Credit Balance", "false") == "false"

    def test_tool_refuses_to_write_an_armed_group(self):
        armed = [{"name": "Grover HW — Batch Submitter (PREFLIGHT)",
                 "properties": {"Submit Mode": "armed"}}]
        with pytest.raises(SystemExit, match="ARMED"):
            tool.check_not_armed(armed)

    def test_tool_accepts_a_preflight_group(self, group):
        _, processors, _ = group
        # Should not raise.
        tool.check_not_armed(processors)

    def test_invalid_scheduled_state_is_refused(self):
        with pytest.raises(SystemExit, match="scheduledState"):
            tool.check_valid_states([{"scheduledState": "RUNNING_DISABLED_TYPO"}])


class TestWiring:

    def test_all_three_branches_are_present(self, group):
        _, processors, _ = group
        builder_types = {"QiskitGroverCircuit", "CirqGroverCircuit",
                         "PennylaneGroverCircuit"}
        builders = [p for p in processors if p["type"] in builder_types]
        assert len(builders) == 3

    def test_branches_are_three_distinct_families(self):
        types = {b[1] for b in tool.BRANCHES}
        assert len(types) == 3

    def test_the_scoring_path_is_wired(self, group):
        _, processors, _ = group
        types = {p["type"] for p in processors}
        assert "QuantumBatchResultExpander" in types
        assert "QuantumSuccessProbabilityOracle" in types

    def test_expander_sits_between_poller_and_oracle(self, group):
        provider, processors, connections = group
        by_id = {p["identifier"]: p for p in processors}
        # Some connections terminate in a dead-letter FUNNEL, not a processor;
        # only processor-to-processor edges are relevant here.
        edges = {(by_id[c["source"]["id"]]["type"],
                  by_id[c["destination"]["id"]]["type"]) for c in connections
                 if c["source"]["id"] in by_id and c["destination"]["id"] in by_id}
        poller = tool.PROVIDERS[provider]["poller"]
        assert (poller, "QuantumBatchResultExpander") in edges

    def test_oracle_reads_the_case_expected_outcome(self, group):
        _, processors, _ = group
        oracle = next(p for p in processors
                      if p["type"] == "QuantumSuccessProbabilityOracle")
        assert oracle["properties"]["Expected Outcome"] == "${grover.expected}"
        assert oracle["properties"]["Mode"] == "single"

    def test_bit_order_matches_the_provider(self, group):
        provider, processors, _ = group
        expander = next(p for p in processors
                        if p["type"] == "QuantumBatchResultExpander")
        expected = tool.PROVIDERS[provider]["bit_order"]
        assert expander["properties"]["Source Bit Order"] == expected

    def test_expected_circuits_is_fortyfour_unfiltered(self, group):
        _, processors, _ = group
        submitter = next(p for p in processors if "Submit Mode" in p["properties"])
        assert int(submitter["properties"]["Expected Circuits"]) == 44
        assert tool.expected_circuits(tool.CASES, tool.load_ladder(), with_mutants=True) == 44

    def test_expected_circuits_is_twenty_without_mutants(self, control_group):
        _, processors, _ = control_group
        submitter = next(p for p in processors if "Submit Mode" in p["properties"])
        assert int(submitter["properties"]["Expected Circuits"]) == 20
        assert tool.expected_circuits(tool.CASES, {}, with_mutants=False) == 20

    def test_mutant_adds_three_mutators_per_branch(self, group):
        _, processors, _ = group
        mutators = [p for p in processors if p["type"] == "QuantumMutator"]
        assert len(mutators) == 9  # 3 builders x 3 rungs

    def test_no_mutators_in_control_group(self, control_group):
        _, processors, _ = control_group
        mutators = [p for p in processors if p["type"] == "QuantumMutator"]
        assert len(mutators) == 0

    def test_readout_branch_exists(self, group):
        _, processors, connections = group
        readout = next((p for p in processors if p["type"] == "ReadoutBaselineCircuit"), None)
        assert readout is not None, "ReadoutBaselineCircuit must be present"
        assert readout["properties"]["Marked State"] == "${grover.marked_state}"
        assert readout["properties"]["Output Format"] == "qasm2"

        mark_readout = next((p for p in processors if p["name"] == "Grover HW — Mark readout Cell"), None)
        assert mark_readout is not None, "Mark readout Cell must be present"
        assert mark_readout["properties"]["grover.builder"] == "readout"
        assert mark_readout["properties"]["grover.kind"] == "readout"
        assert mark_readout["properties"]["circuit.label"] == "readout@${test.case_id}"

    def test_null_branch_exists(self, group):
        _, processors, connections = group
        mark_null = next((p for p in processors if p["name"] == "Grover HW — Mark qiskit Null"), None)
        assert mark_null is not None, "Mark qiskit Null must be present"
        assert mark_null["properties"]["grover.builder"] == "Qiskit"
        assert mark_null["properties"]["grover.kind"] == "null"
        assert mark_null["properties"]["circuit.label"] == "Qiskit@${test.case_id}@null"

    def test_readout_and_null_branches_are_distinct(self, group):
        _, processors, connections = group
        mark_readout = next(p for p in processors if p["name"] == "Grover HW — Mark readout Cell")
        mark_null = next(p for p in processors if p["name"] == "Grover HW — Mark qiskit Null")
        assert mark_readout["properties"]["grover.kind"] != mark_null["properties"]["grover.kind"]
        assert mark_readout["properties"]["grover.builder"] != mark_null["properties"]["grover.builder"]
        assert mark_readout["properties"]["circuit.label"] != mark_null["properties"]["circuit.label"]

    def test_ibm_default_device_is_kingston(self):
        assert tool.PROVIDERS["ibm"]["device"] == "ibm_kingston"

    def test_ibm_submitter_device_is_kingston(self):
        processors, _, _, _ = tool.build_group(
            "gid", "ibm", tool.PROVIDERS["ibm"]["device"], 1024, 11, "/tmp/quanifi-reports")
        submitter = next(p for p in processors if p["type"] == "QuantumIBMBatchSubmitter")
        assert submitter["properties"]["Device"] == "ibm_kingston"

    def test_cases_filter_asymmetric_2q(self):
        processors, _, _, _ = tool.build_group(
            "gid", "quantum-inspire", "Tuna-17", 1024, 11, "/tmp/quanifi-reports",
            cases_filter="asymmetric-2q")
        submitter = next(p for p in processors if "Submit Mode" in p["properties"])
        # 1 case * (3 builders + 1 readout + 1 null) = 5 control
        # 1 case (2q carries large only) * 3 builders = 3 mutants -> total 8
        assert int(submitter["properties"]["Expected Circuits"]) == 8

    def test_cases_filter_asymmetric_2q_without_mutants(self):
        processors, _, _, _ = tool.build_group(
            "gid", "quantum-inspire", "Tuna-17", 1024, 11, "/tmp/quanifi-reports",
            cases_filter="asymmetric-2q", mutants=None)
        submitter = next(p for p in processors if "Submit Mode" in p["properties"])
        assert int(submitter["properties"]["Expected Circuits"]) == 5

    def test_per_cell_mutator_config_matches_ladder(self, group):
        _, processors, _ = group
        ladder = tool.load_ladder()
        source = next(p for p in processors if p["type"] == "QuantumTestCaseSource")
        matrix = json.loads(source["properties"]["Test Matrix"])
        case_0111 = next(c for c in matrix if c["grover.marked_state"] == "0111")
        for rung in ("small", "medium", "large"):
            entry = ladder[("qiskit", "0111", rung)]
            prefix = "grover.mut_qiskit_%s" % rung
            assert case_0111[prefix + "_operator"] == entry["operator"]
            if entry.get("epsilon") is not None:
                assert case_0111[prefix + "_epsilon"] == "%.6f" % entry["epsilon"]
            assert float(case_0111[prefix + "_delta"]) == pytest.approx(entry["delta_sim"], abs=1e-5)

    def test_replicate_namespaces_batch_label_and_group(self):
        processors, _, _, labels = tool.build_group(
            "gid", "quantum-inspire", "Tuna-17", 1024, 11, "/tmp/quanifi-reports",
            replicate=2)
        submitter = next(p for p in processors if "Submit Mode" in p["properties"])
        assert submitter["properties"]["Batch Label"].startswith("rep2-")
        assert "rep2" in labels[0]["label"]

    def test_only_pending_loops_back(self, group):
        """`pending` loops; `waiting` must NOT -- see
        tests/test_arithmetic_hw_group.py's identical test for the archived
        incident this exact bug caused when it was the other way round.
        """
        _, processors, connections = group
        by_id = {p["identifier"]: p for p in processors}
        loops = {(by_id[c["source"]["id"]]["type"], c["selectedRelationships"][0])
                 for c in connections
                 if c["source"]["id"] == c["destination"]["id"]}
        relationships = {rel for _, rel in loops}
        assert "pending" in relationships, "a non-terminal job would be dropped"
        assert "waiting" not in relationships, (
            "the submitter must not feed itself; it double-counts circuits")
        submitter = next(p for p in processors if "Batch Submitter" in p["name"])
        assert "waiting" in submitter["autoTerminatedRelationships"]

    def test_preflight_is_routed_somewhere_visible(self, group):
        """A costing nobody sees is a costing nobody reads."""
        _, processors, connections = group
        by_id = {p["identifier"]: p for p in processors}
        preflight = [c for c in connections
                     if "preflight" in c["selectedRelationships"]]
        assert preflight
        assert by_id[preflight[0]["destination"]["id"]]["type"] == "QuanifiReport"

    def test_builder_failure_is_never_auto_terminated(self, group):
        """A builder that fails to build must be visible, in a preflight
        group any more than an armed one -- see
        tools/add_arithmetic_hw_group.py's comment on the 2026-08-26 stall."""
        _, processors, connections = group
        builder_types = {"QiskitGroverCircuit", "CirqGroverCircuit",
                         "PennylaneGroverCircuit"}
        builders = [p for p in processors if p["type"] in builder_types]
        for builder in builders:
            assert "failure" not in builder["autoTerminatedRelationships"]
        by_id = {p["identifier"]: p for p in processors}
        failure_edges = {
            (by_id[c["source"]["id"]]["identifier"])
            for c in connections if c["selectedRelationships"] == ["failure"]}
        for builder in builders:
            assert builder["identifier"] in failure_edges, (
                "%s failure must reach a funnel" % builder["name"])


class TestPropertyNamesExist:
    """A misspelled property is silently ignored by NiFi, with no warning."""

    def test_every_property_name_is_real(self, group):
        _, processors, _ = group
        for spec in processors:
            module = spec["type"]
            if module not in PROCESSOR_MODULES:
                continue  # a NiFi standard processor or UpdateAttribute, not ours
            cls = getattr(importlib.import_module(module), module)
            known = {d.name for d in cls().getPropertyDescriptors()}
            unknown = sorted(set(spec["properties"]) - known)
            assert not unknown, "%s: unknown properties %s" % (spec["name"], unknown)

    def test_every_property_name_is_real_with_mutant(self, mutant_group):
        _, processors, _ = mutant_group
        for spec in processors:
            module = spec["type"]
            if module not in PROCESSOR_MODULES:
                continue
            cls = getattr(importlib.import_module(module), module)
            known = {d.name for d in cls().getPropertyDescriptors()}
            unknown = sorted(set(spec["properties"]) - known)
            assert not unknown, "%s: unknown properties %s" % (spec["name"], unknown)

    def test_every_property_name_is_real_in_control_group(self, control_group):
        _, processors, _ = control_group
        for spec in processors:
            module = spec["type"]
            if module not in PROCESSOR_MODULES:
                continue
            cls = getattr(importlib.import_module(module), module)
            known = {d.name for d in cls().getPropertyDescriptors()}
            unknown = sorted(set(spec["properties"]) - known)
            assert not unknown, "%s: unknown properties %s" % (spec["name"], unknown)


class TestBundleVersions:
    """A bundle version that does not match the source is a Ghost component."""

    def test_every_bundle_version_matches_the_source(self, group):
        _, processors, _ = group
        for spec in processors:
            module = spec["type"]
            if module not in PROCESSOR_MODULES:
                continue
            assert spec["bundle"]["version"] == tool.pybundle(module)["version"], (
                "%s: bundle version %s does not match source-declared %s"
                % (spec["name"], spec["bundle"]["version"], tool.pybundle(module)["version"]))

    def test_quanifi_report_is_not_the_ghost_component_version(self, group):
        """tools/add_arithmetic_hw_group.py:567 hardcodes QuanifiReport at
        0.2.0 while the source declares 0.3.1 -- the exact bug this tool's
        pybundle() reads from source specifically to avoid."""
        _, processors, _ = group
        report = next(p for p in processors if p["type"] == "QuanifiReport")
        assert report["bundle"]["version"] == "0.3.1"


class TestRowFieldsCoverage:
    """ROW_FIELDS must cover every grover.-prefixed attribute the Mark
    processors write, or an omitted field is silently replaced by the parent
    FlowFile's value and looks per-circuit."""

    def test_row_fields_cover_every_mark_processor_property(self, group):
        _, processors, _ = group
        mark_processors = [p for p in processors if p["type"] == tool.UPDATE_ATTRIBUTE]
        assert mark_processors, "expected at least one Mark <fw> Cell processor"
        written = set()
        for p in mark_processors:
            for key in p["properties"]:
                if key.startswith("grover."):
                    written.add(key)
        assert written, "Mark processors wrote no grover.* attributes"
        missing = written - set(tool.ROW_FIELDS)
        assert not missing, "ROW_FIELDS is missing %s" % (missing,)

    def test_row_fields_cover_the_case_sourced_attributes(self):
        """grover.marked_state / grover.iterations are hoisted straight from
        the test case and never overwritten, so they too ride the batch under
        the grover. prefix and must be covered."""
        for field in ("grover.marked_state", "grover.iterations"):
            assert field in tool.ROW_FIELDS


class TestIdealSuccessProbability:
    """Each case's analytic ideal must match sin^2((2k+1)*asin(2^(-n/2)))."""

    @pytest.mark.parametrize("case", tool.CASES, ids=lambda c: c["test.partition"])
    def test_ideal_success_matches_the_formula(self, case):
        n = len(case["grover.marked_state"])
        k = int(case["grover.iterations"])
        theta = math.asin(2.0 ** (-n / 2.0))
        expected = math.sin((2 * k + 1) * theta) ** 2
        assert case["grover.ideal_success"] == pytest.approx(expected, abs=1e-9)

    def test_two_qubit_one_iteration_is_exact_resonance(self):
        two_q_cases = [c for c in tool.CASES if len(c["grover.marked_state"]) == 2]
        assert two_q_cases
        for case in two_q_cases:
            assert round(case["grover.ideal_success"], 6) == 1.000000

    def test_four_qubit_two_iterations_matches_the_documented_value(self):
        four_q_cases = [c for c in tool.CASES if len(c["grover.marked_state"]) == 4]
        assert four_q_cases
        for case in four_q_cases:
            assert round(case["grover.ideal_success"], 6) == 0.908447

    def test_ideal_success_never_reaches_the_flowfile(self):
        """See CASES' own docstring: this field is analytic bookkeeping for
        this tool and the test suite, not a runtime input, and must not leak
        onto the canvas under the grover. prefix (it isn't in
        CASE_ATTRIBUTES, so it is never hoisted)."""
        assert "grover.ideal_success" not in tool.CASE_ATTRIBUTES
        for case in tool.CASES:
            wire = tool._case_wire_fields(case)
            assert "grover.ideal_success" not in wire


class TestCaseTable:

    def test_four_cases_two_widths_each_with_a_palindrome_and_an_asymmetric(self):
        assert len(tool.CASES) == 4
        partitions = {c["test.partition"] for c in tool.CASES}
        assert partitions == {"asymmetric-2q", "palindrome-2q",
                              "asymmetric-4q", "palindrome-4q"}

    def test_expected_outcome_equals_the_marked_state(self):
        """Grover with the calibrated iteration count returns the marked
        state; the oracle's ground truth must agree."""
        for case in tool.CASES:
            assert case["test.expected"] == case["grover.marked_state"]


class TestChunkSplitter:
    """QuantumInspireBatchSubmitter partitions a batch bigger than the
    device's max_jobs_per_batch_job into several chunks and emits a JSON
    array; only the quantum-inspire lane needs a SplitJson between the
    submitter and the poller to fan that back out. IBM and IQM are never
    chunked and must be provably untouched."""

    SPLIT_TYPE = "org.apache.nifi.processors.standard.SplitJson"

    @staticmethod
    def _edges(processors, connections):
        by_id = {p["identifier"]: p for p in processors}
        return {(by_id[c["source"]["id"]]["type"], by_id[c["destination"]["id"]]["type"])
                for c in connections
                if c["source"]["id"] in by_id and c["destination"]["id"] in by_id}

    @staticmethod
    def _build(provider):
        processors, connections, funnels, labels = tool.build_group(
            "gid", provider, tool.PROVIDERS[provider]["device"], 1024, 11,
            "/tmp/quanifi-reports")
        return processors, connections

    def test_quantum_inspire_lane_has_a_split_between_submitter_and_poller(self):
        processors, connections = self._build("quantum-inspire")
        submitter_type = tool.PROVIDERS["quantum-inspire"]["submitter"]
        poller_type = tool.PROVIDERS["quantum-inspire"]["poller"]
        edges = self._edges(processors, connections)
        assert (submitter_type, self.SPLIT_TYPE) in edges
        assert (self.SPLIT_TYPE, poller_type) in edges
        assert (submitter_type, poller_type) not in edges

    @pytest.mark.parametrize("provider", ["ibm", "iqm"])
    def test_ibm_and_iqm_lanes_go_straight_from_submitter_to_poller(self, provider):
        processors, connections = self._build(provider)
        submitter_type = tool.PROVIDERS[provider]["submitter"]
        poller_type = tool.PROVIDERS[provider]["poller"]
        edges = self._edges(processors, connections)
        assert (submitter_type, poller_type) in edges
        splits = [p for p in processors if p["type"] == self.SPLIT_TYPE]
        assert len(splits) == 2, "ibm/iqm keep only the pre-existing splits"

    def test_quantum_inspire_lane_has_three_splitjsons(self):
        processors, _ = self._build("quantum-inspire")
        splits = [p for p in processors if p["type"] == self.SPLIT_TYPE]
        assert len(splits) == 3

    def test_chunk_splitter_is_configured_like_the_other_splitjsons(self):
        processors, _ = self._build("quantum-inspire")
        chunk_split = next(p for p in processors
                           if p["name"] == "Grover HW — Split Chunk Manifests")
        assert chunk_split["properties"]["JsonPath Expression"] == "$"
        assert "original" in chunk_split["autoTerminatedRelationships"]
        assert "failure" not in chunk_split["autoTerminatedRelationships"]

    def test_preflight_still_lands_on_report_not_the_splitter(self):
        processors, connections = self._build("quantum-inspire")
        by_id = {p["identifier"]: p for p in processors}
        preflight = [c for c in connections
                    if "preflight" in c["selectedRelationships"]]
        assert preflight
        destination = by_id[preflight[0]["destination"]["id"]]
        assert destination["type"] == "QuanifiReport"
        assert destination["name"] != "Grover HW — Split Chunk Manifests"

    def test_row_fields_hoist_the_chunk_provenance(self):
        for field in ("batch.batch_index", "batch.chunk_index",
                     "batch.chunk_count", "batch.job_id"):
            assert field in tool.ROW_FIELDS
