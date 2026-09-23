"""Evaluate a complete clean qualification or calibration hardware job."""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from arithmetic_spec import marginalize  # noqa: E402

from generation2.core import (  # noqa: E402
    CASES, CLEAN_CONTROL_WILSON_FLOOR, DEFAULT_ALPHA, cell_statistics,
    voter_margin_summary)

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder


class Generation2CalibrationEvaluator(FlowFileTransform):
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "0.1.0"
        description = ("Checks Generation-2 clean qualification/calibration modes "
                       "and emits voter eligibility without entering confirmatory data.")
        tags = ["quantum", "generation2", "qualification", "calibration"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get("jvm")
        super().__init__()
        self.phase = PropertyDescriptor(
            name="Phase", description="qualification or calibration", required=True,
            default_value="qualification",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR])
        self.vendor = PropertyDescriptor(
            name="Vendor", description="ibm or iqm", required=True,
            validators=[StandardValidators.NON_EMPTY_VALIDATOR])
        self.descriptors = [self.phase, self.vendor]

    def getPropertyDescriptors(self): return self.descriptors

    def getRelationships(self):
        return [Relationship(name="accepted", description="All required voters pass."),
                Relationship(name="rejected", description="Qualification or eligibility failed."),
                Relationship(name="failure", description="Malformed complete job.")]

    def transform(self, context, flowFile):
        raw = bytes(flowFile.getContentsAsBytes())
        try:
            phase = context.getProperty(self.phase).getValue()
            vendor = context.getProperty(self.vendor).getValue()
            if phase not in ("qualification", "calibration"):
                raise ValueError("phase must be qualification or calibration")
            payload = json.loads(raw.decode())
            rows = payload if isinstance(payload, list) else payload["rows"]
            design_ids = {
                (row.get("attributes") or {}).get("arithmetic.campaign_design_id")
                for row in rows
            } - {None, ""}
            if len(design_ids) != 1:
                raise ValueError("qualification/calibration needs one campaign design id")
            campaign_design_id = design_ids.pop()
            expected_n = 21 if phase == "qualification" else 84
            if len(rows) != expected_n:
                raise ValueError("%s needs %d rows, received %d" %
                                 (phase, expected_n, len(rows)))
            repeated, aggregate = defaultdict(list), defaultdict(lambda: defaultdict(int))
            truth = {}
            for row in rows:
                attrs = row["attributes"]
                version = attrs["arithmetic.implementation"]
                cid = "%s+%s" % (attrs["arithmetic.operand_a"], attrs["arithmetic.operand_b"])
                # The layout-aware bitstring the circuit builder derived, not a
                # reconstruction from the integer. Re-deriving with format(n,
                # "03b")[::-1] assumes three contiguous result qubits in a fixed
                # order, duplicating logic that arithmetic_spec already owns --
                # and it read arithmetic.expected_result, which was constant
                # across a whole job in every canvas archive to 2026-08-25.
                expected = attrs["arithmetic.expected_result_bits"]
                qubits = [int(q) for q in attrs["arithmetic.result_qubits"].split(",") if q]
                counts = marginalize(row["counts"], qubits)
                winner = max(counts, key=counts.get)
                truth[(version, cid)] = expected
                repeated[(version, cid)].append(winner == expected)
                for bits, count in counts.items():
                    aggregate[(version, cid)][bits] += int(count)
            versions = ("cdkm", "qft", "semiadder")
            # How safely each cell was won. The gate below still decides by
            # argmax, exactly as preregistered -- these numbers do not move it.
            # They are the evidence for whether a winning mode leads by a
            # comfortable margin or by a handful of shots, which is what tells
            # a later mutant apart from a voter that flips on its own noise.
            cells, per_version = [], defaultdict(list)
            for (version, cid), histogram in sorted(aggregate.items()):
                expected = truth.get((version, cid))
                if expected is None:
                    continue
                stats = cell_statistics(dict(histogram), expected)
                per_version[version].append((cid, stats))
                cells.append({"version": version, "case": cid,
                              "repeats": len(repeated[(version, cid)]),
                              "correct_repeats": sum(repeated[(version, cid)]),
                              # The histogram itself, so a report can show what
                              # the device returned rather than only what was
                              # concluded from it.
                              "counts": {str(k): int(v)
                                         for k, v in sorted(histogram.items())},
                              **stats})
            voters = {}
            for version in versions:
                keys = [(version, "%d+%d" % case) for case in CASES]
                aggregate_ok = all(key in truth and
                                   max(aggregate[key], key=aggregate[key].get) == truth[key]
                                   for key in keys)
                correct = sum(sum(repeated[key]) for key in keys)
                total = sum(len(repeated[key]) for key in keys)
                version_cells = per_version[version]
                floor_ok = (len(version_cells) == len(CASES) and all(
                    stats.get("wilson_low") is not None and
                    stats["wilson_low"] >= CLEAN_CONTROL_WILSON_FLOOR
                    for _, stats in version_cells))
                minimum_wilson = min(
                    (stats["wilson_low"] for _, stats in version_cells
                     if stats.get("wilson_low") is not None), default=None)
                voters[version] = {"correct_repeated_modes": correct,
                                   "total_repeated_modes": total,
                                   "all_aggregate_modes_correct": aggregate_ok,
                                   "control_floor": CLEAN_CONTROL_WILSON_FLOOR,
                                   "minimum_wilson_low": minimum_wilson,
                                   "all_cells_above_control_floor": floor_ok,
                                   "eligible": aggregate_ok and floor_ok and
                                   (phase == "qualification" or correct >= 25),
                                   **voter_margin_summary(per_version[version])}
            accepted = all(v["eligible"] for v in voters.values())
            # Advisory only, and deliberately not folded into `accepted`:
            # changing the acceptance rule mid-campaign would make today's runs
            # incomparable with the ones already on disk.
            all_modes_stable = all(v["all_modes_stable"] for v in voters.values())
            margins = [v["min_margin"] for v in voters.values()
                       if v["min_margin"] is not None]
            out = {"schema": "quanifi-generation2-%s-v1" % phase,
                   "campaign_generation": 2, "execution_path": "nifi",
                   "campaign_design_id": campaign_design_id,
                   "vendor": vendor, "accepted": accepted, "voters": voters,
                   "margin_alpha": DEFAULT_ALPHA,
                   "all_modes_stable": all_modes_stable, "cells": cells}
            relationship = "accepted" if accepted else "rejected"
            return FlowFileTransformResult(
                relationship=relationship, contents=json.dumps(out, indent=2).encode(),
                attributes={"generation2.phase": phase,
                            "generation2.accepted": str(accepted).lower(),
                            "generation2.all_modes_stable": str(all_modes_stable).lower(),
                            "generation2.min_margin": ("%.6f" % min(margins)) if margins else "",
                            "mime.type": "application/json"})
        except Exception as exc:  # noqa: BLE001
            return FlowFileTransformResult(relationship="failure", contents=raw,
                                           attributes={"generation2.error": str(exc)})
