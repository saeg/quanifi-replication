"""Build blinded one-faulty-version ensembles from one expanded G2 job."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from generation2.core import ContractError, assemble_ensembles  # noqa: E402

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder


class Generation2EnsembleBuilder(FlowFileTransform):
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "0.1.0"
        description = ("Builds strict three-version clean and one-faulty-version "
                       "ensembles from a complete Generation-2 expanded job.")
        tags = ["quantum", "generation2", "n-version", "ensemble", "blinded"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get("jvm")
        super().__init__()
        self.descriptors = []

    def getPropertyDescriptors(self):
        return self.descriptors

    def getRelationships(self):
        return [Relationship(name="success", description="Complete blinded ensembles."),
                Relationship(name="failure", description="Incomplete, duplicate, or truth-leaking input.")]

    def transform(self, context, flowFile):
        raw = bytes(flowFile.getContentsAsBytes())
        try:
            payload = json.loads(raw.decode("utf-8"))
            if payload.get("campaign_generation") != 2:
                raise ContractError("wrong-generation job refused")
            ensembles = assemble_ensembles(payload["records"])
        except Exception as exc:  # noqa: BLE001
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"generation2.error": str(exc)})
        metadata = {k: payload.get(k) for k in
                    ("campaign_id", "campaign_design_id", "execution_path",
                     "job_key", "manifest_sha256",
                     "vendor_condition", "window_id", "matched_block_id")
                    if payload.get(k) is not None}
        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps({"campaign_generation": 2, **metadata,
                                 "ensembles": ensembles}, indent=2).encode(),
            attributes={"generation2.ensembles": str(len(ensembles)),
                        "mime.type": "application/json"})
