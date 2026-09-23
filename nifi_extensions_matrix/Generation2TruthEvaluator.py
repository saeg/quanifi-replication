"""Join frozen Generation-2 decisions to a separately stored truth table."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from generation2.core import artifact_digest, metrics  # noqa: E402

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder


class Generation2TruthEvaluator(FlowFileTransform):
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "0.1.0"
        description = ("Evaluates already-frozen Generation-2 decisions against "
                       "truth supplied only after the oracle stage.")
        tags = ["quantum", "generation2", "evaluation", "unblinding"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get("jvm")
        super().__init__()
        self.truth_file = PropertyDescriptor(
            name="Sealed Truth File", description="Reviewed Generation-2 truth JSON.",
            required=True, validators=[StandardValidators.NON_EMPTY_VALIDATOR])
        self.descriptors = [self.truth_file]

    def getPropertyDescriptors(self):
        return self.descriptors

    def getRelationships(self):
        return [Relationship(name="success", description="Evaluated decisions."),
                Relationship(name="failure", description="Missing or mismatched truth.")]

    def transform(self, context, flowFile):
        raw = bytes(flowFile.getContentsAsBytes())
        try:
            decision_doc = json.loads(raw.decode("utf-8"))
            if artifact_digest(decision_doc, "decisions_sha256") != decision_doc.get("decisions_sha256"):
                raise ValueError("decision artifact changed after freezing")
            decisions = decision_doc["decisions"]
            path = context.getProperty(self.truth_file).getValue()
            truth_doc = json.loads(open(path, encoding="utf-8").read())
            truth = {(r.get("job_key"), r["case_id"], r["mutation_id"]): r
                     for r in truth_doc["records"]}
            evaluated = []
            for d in decisions:
                key = (d.get("job_key"), d["case_id"], d["mutation_id"])
                # Canvas decisions may omit job_key when one job is evaluated
                # at a time; require a unique case/mutation fallback.
                row = truth.get(key)
                if row is None:
                    matches = [r for r in truth_doc["records"]
                               if r["case_id"] == d["case_id"] and
                               r["mutation_id"] == d["mutation_id"]]
                    if len(matches) != 1:
                        raise ValueError("truth join is not unique for %s" % (key,))
                    row = matches[0]
                faulty = row["true_faulty_version"]
                if d["decision"] == "abstain": outcome = "abstention"
                elif faulty is None: outcome = "correct_clean" if d["decision"] == "clean" else "clean_false_alarm"
                elif d["decision"] == "clean": outcome = "missed_mutant"
                elif d["suspect_version"] == faulty: outcome = "correctly_localized"
                else: outcome = "wrong_attribution"
                evaluated.append({**d, "outcome": outcome,
                                  "true_faulty_version": faulty,
                                  "mutation_activation": row.get("mutation_activation"),
                                  "operator": row.get("operator"),
                                  "logical_locus": row.get("logical_locus")})
        except Exception as exc:  # noqa: BLE001
            return FlowFileTransformResult(relationship="failure", contents=raw,
                                           attributes={"generation2.error": str(exc)})
        out = {"campaign_generation": 2, "records": evaluated,
               "metrics": metrics(evaluated)}
        return FlowFileTransformResult(
            relationship="success", contents=json.dumps(out, indent=2).encode(),
            attributes={"generation2.evaluated": str(len(evaluated)),
                        "mime.type": "application/json"})
