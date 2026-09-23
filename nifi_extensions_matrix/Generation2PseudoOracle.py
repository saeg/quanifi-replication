"""Blinded Generation-2 modal-majority pseudo-oracle."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from generation2.core import ORACLE_VERSION, artifact_digest, majority_decision  # noqa: E402

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder


class Generation2PseudoOracle(FlowFileTransform):
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "0.1.0"
        description = ("Applies the reference-free Generation-2 modal-majority "
                       "rule and emits hash-frozen decisions.")
        tags = ["quantum", "generation2", "pseudo-oracle", "majority", "blinded"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get("jvm")
        super().__init__()
        self.descriptors = []

    def getPropertyDescriptors(self):
        return self.descriptors

    def getRelationships(self):
        return [Relationship(name="success", description="Frozen blinded decisions."),
                Relationship(name="failure", description="Invalid ensemble or truth leakage.")]

    def transform(self, context, flowFile):
        raw = bytes(flowFile.getContentsAsBytes())
        try:
            payload = json.loads(raw.decode("utf-8"))
            if payload.get("campaign_generation") != 2:
                raise ValueError("wrong-generation ensembles refused")
            decisions = []
            common = {k: payload.get(k) for k in
                      ("campaign_id", "campaign_design_id", "execution_path",
                       "job_key", "manifest_sha256",
                       "vendor_condition", "window_id", "matched_block_id")
                      if payload.get(k) is not None}
            for ensemble in payload["ensembles"]:
                decisions.append(majority_decision(
                    ensemble["versions"],
                    {**common, "case_id": ensemble["case_id"],
                     "mutation_id": ensemble["mutation_id"]}))
        except Exception as exc:  # noqa: BLE001
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"generation2.error": str(exc)})
        out = {"campaign_generation": 2, "oracle_version": ORACLE_VERSION,
               "decisions": decisions}
        out["decisions_sha256"] = artifact_digest(out, "decisions_sha256")
        return FlowFileTransformResult(
            relationship="success", contents=json.dumps(out, indent=2).encode(),
            attributes={"generation2.decisions": str(len(decisions)),
                        "generation2.oracle_version": ORACLE_VERSION,
                        "mime.type": "application/json"})
