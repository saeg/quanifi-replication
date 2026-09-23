import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder


#: How a provider hands back its counts keys.
Q0_LEFT, Q0_RIGHT = "q0_left", "q0_right"


def reverse_keys(counts):
    """Flip a counts dict between q0-left and q0-right ordering."""
    out = {}
    for key, value in counts.items():
        flipped = key.replace(" ", "")[::-1]
        out[flipped] = out.get(flipped, 0) + value
    return out


def expand(batch, source_bit_order=Q0_RIGHT):
    """Turn one polled batch into one row per circuit.

    Returns a list of ``{"counts": {...}, "attributes": {...}}``. Counts are
    normalised to the project's canonical ``q0_left`` order, so the rows are
    directly comparable with every simulator in the repo.

    Raises ``ValueError`` when the input is not a polled batch.
    """
    if not isinstance(batch, dict):
        raise ValueError("expected a batch result object, got %s"
                         % type(batch).__name__)
    entries = batch.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("batch has no 'entries' list")

    order = (batch.get("bit_order") or source_bit_order or Q0_RIGHT).strip().lower()
    rows = []
    for index, entry in enumerate(entries):
        counts = entry.get("counts")
        if not isinstance(counts, dict) or not counts:
            raise ValueError("entry %d ('%s') has no counts"
                             % (index, entry.get("label", "?")))
        if order != Q0_LEFT:
            counts = reverse_keys(counts)
        else:
            counts = {k.replace(" ", ""): v for k, v in counts.items()}

        attributes = dict(entry.get("attributes") or {})
        # Bookkeeping the oracle and the report read by name.
        attributes.update({
            "batch.entry_index": str(index),
            "batch.label": str(entry.get("label", "")),
            "batch.kind": str(entry.get("kind", "")),
            "sim.bit_order": Q0_LEFT,
            "sim.shots": str(sum(counts.values())),
        })
        if entry.get("num_qubits") is not None:
            attributes["sim.num_qubits"] = str(entry["num_qubits"])
        for key, name in (("job_id", "batch.job_id"), ("device", "batch.device")):
            if batch.get(key):
                attributes[name] = str(batch[key])
        rows.append({"counts": counts, "attributes": attributes})
    return rows


class QuantumBatchResultExpander(FlowFileTransform):
    """
    Expands a polled hardware batch into one result per circuit.

    A batch poller returns a single FlowFile holding every circuit's counts,
    because a job is one job. Scoring is per circuit. NiFi's FlowFileTransform
    returns exactly one result, so a poller structurally cannot fan out, and
    something has to sit between it and the oracle.

    This processor follows the same idiom ``QuantumTestCaseSource`` uses: emit a
    JSON array and let ``SplitJson`` do the fanning out.

        Poller -> QuantumBatchResultExpander -> SplitJson($)
               -> EvaluateJsonPath (attributes -> flowfile-attribute)
               -> EvaluateJsonPath ($.counts -> flowfile-content)
               -> QuantumSuccessProbabilityOracle

    Each row carries that circuit's counts and the attributes the submitter
    captured for it, so a hardware result can be scored against the classical
    answer instead of against another branch's distribution. That is the whole
    reason this exists: without it the arithmetic hardware lane falls back to a
    distributional oracle and looks like it worked.

    **Bit order.** Qiskit returns counts MSB-first (qubit 0 rightmost); the IQM
    serializer already produces q0-left. This processor normalises to the
    project's canonical ``q0_left`` and advertises ``sim.bit_order``, exactly as
    every simulator processor does. Getting this wrong does not raise, it
    reports 0% success and looks like a dead device.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Expands a polled hardware batch into a JSON array of one row per "
            "circuit, each carrying that circuit's counts (normalised to "
            "q0_left) and the ground-truth attributes recorded at submission, "
            "so per-circuit results can be scored against a known answer. Feed "
            "into SplitJson."
        )
        tags = ["quantum", "hardware", "batch", "counts", "testing"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.source_bit_order = PropertyDescriptor(
            name="Source Bit Order",
            description=(
                "How the provider's counts keys are ordered when the batch does "
                "not say. q0_right (default) is Qiskit's and therefore IBM's: "
                "qubit 0 is the RIGHTMOST character. q0_left is what the IQM "
                "serializer produces. Output is always q0_left. A batch "
                "carrying its own 'bit_order' overrides this."
            ),
            required=True, default_value=Q0_RIGHT,
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.source_bit_order]

    def getPropertyDescriptors(self):
        return self.descriptors

    def getRelationships(self):
        return [Relationship(name="success",
                             description="One row per circuit, ready for SplitJson.",
                             auto_terminated=False),
                Relationship(name="failure",
                             description="Input was not a polled batch result.",
                             auto_terminated=False)]

    def transform(self, context, flowFile):
        raw = bytes(flowFile.getContentsAsBytes())
        order = (context.getProperty(self.source_bit_order)
                 .evaluateAttributeExpressions(flowFile).getValue())
        try:
            batch = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - any decode problem is the same answer
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": "content is not JSON: {}".format(exc)})

        try:
            rows = expand(batch, order)
        except ValueError as exc:
            self.logger.error("QuantumBatchResultExpander: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=raw,
                attributes={"batch.error": str(exc)})

        scorable = sum(1 for r in rows
                       if "arithmetic.expected_result_bits" in r["attributes"]
                       or "arithmetic.expected_bitstring" in r["attributes"])
        attributes = {
            "batch.expanded": str(len(rows)),
            "batch.scorable": str(scorable),
            "batch.bit_order": Q0_LEFT,
            "mime.type": "application/json",
        }
        for key, name in (("job_id", "batch.job_id"), ("device", "batch.device"),
                          ("layout", "batch.layout")):
            if batch.get(key):
                # The layout is job-level provenance: which physical qubits
                # produced these counts. It was being dropped here, so no report
                # recorded it and nothing downstream could publish the qualified
                # layout for the later jobs to pin.
                value = batch[key]
                if isinstance(value, (list, tuple)):
                    value = ",".join(str(int(q)) for q in value)
                attributes[name] = str(value)
        if scorable == 0:
            # Not a failure: a GHZ batch legitimately has no ground truth. But
            # it is exactly the state in which the lane silently degrades to a
            # distributional oracle, so say it out loud.
            attributes["batch.warning"] = (
                "no entry carries ground truth; a success-probability oracle "
                "cannot score this batch")
            self.logger.warn("QuantumBatchResultExpander: " + attributes["batch.warning"])

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(rows, indent=2).encode("utf-8"),
            attributes=attributes)
