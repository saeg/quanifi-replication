"""Project an expanded hardware job into the blinded Generation-2 contract."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from arithmetic_spec import marginalize  # noqa: E402

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder


class Generation2JobAdapter(FlowFileTransform):
    """The one audited boundary allowed to see truth-bearing batch attributes."""
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "0.1.0"
        description = ("Marginalizes expanded arithmetic counts and strips all "
                       "truth/mutation fields before the Generation-2 oracle.")
        tags = ["quantum", "generation2", "blinding", "adapter"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get("jvm")
        super().__init__()
        def prop(name, default=""):
            return PropertyDescriptor(
                name=name, description="Generation-2 immutable campaign metadata.",
                required=True, default_value=default,
                validators=[StandardValidators.NON_EMPTY_VALIDATOR],
                expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
        self.campaign_id = prop("Campaign ID")
        self.campaign_design_id = prop("Campaign Design ID")
        self.execution_path = prop("Execution Path", "nifi")
        self.job_key = prop("Job Key")
        self.vendor = prop("Vendor Condition")
        self.window_id = prop("Window ID", "preparatory")
        self.matched_block = prop("Matched Block ID", "preparatory")
        self.manifest_sha = prop("Manifest SHA-256", "SET-AFTER-PLAN")
        self.descriptors = [self.campaign_id, self.campaign_design_id,
                            self.execution_path, self.job_key,
                            self.vendor, self.window_id, self.matched_block,
                            self.manifest_sha]

    def getPropertyDescriptors(self): return self.descriptors

    def getRelationships(self):
        return [Relationship(name="success", description="Blinded job records."),
                Relationship(name="failure", description="Unsafe or malformed batch.")]

    def transform(self, context, flowFile):
        raw = bytes(flowFile.getContentsAsBytes())
        def get(prop):
            return context.getProperty(prop).evaluateAttributeExpressions(flowFile).getValue()
        try:
            payload = json.loads(raw.decode("utf-8"))
            rows = payload if isinstance(payload, list) else payload["rows"]
            blinded = []
            for row in rows:
                attrs = row.get("attributes") or {}
                label = attrs.get("batch.label") or row.get("label") or ""
                parts = label.split("@")
                version = attrs.get("arithmetic.implementation") or parts[0].split("/")[-1]
                cid = "%s+%s" % (attrs["arithmetic.operand_a"], attrs["arithmetic.operand_b"])
                tail = parts[2] if len(parts) > 2 else "control"
                if tail.startswith("null"):
                    kind, mutation = "null", tail
                elif "#" in tail or tail in ("carry.break", "gate.remove", "rotation.perturb"):
                    kind, mutation = "mutant", tail
                    if tail.count("#") == 1:
                        mutation += "#" + version
                else:
                    kind, mutation = "control", "clean"
                qubits = [int(x) for x in attrs["arithmetic.result_qubits"].split(",") if x]
                counts = marginalize(row["counts"], qubits)
                blinded.append({"case_id": cid, "version_id": version,
                                "treatment_kind": kind, "mutation_id": mutation,
                                "counts": counts})
        except Exception as exc:  # noqa: BLE001
            return FlowFileTransformResult(relationship="failure", contents=raw,
                                           attributes={"generation2.error": str(exc)})
        out = {"schema": "quanifi-generation2-blinded-job-v1",
               "campaign_generation": 2, "campaign_id": get(self.campaign_id),
               "campaign_design_id": get(self.campaign_design_id),
               "execution_path": get(self.execution_path), "job_key": get(self.job_key),
               "vendor_condition": get(self.vendor),
               "window_id": get(self.window_id),
               "matched_block_id": get(self.matched_block),
               "manifest_sha256": get(self.manifest_sha),
               "records": blinded}
        return FlowFileTransformResult(
            relationship="success", contents=json.dumps(out, indent=2).encode(),
            attributes={"generation2.blinded_records": str(len(blinded)),
                        "mime.type": "application/json"})
