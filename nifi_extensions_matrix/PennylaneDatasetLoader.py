import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class PennylaneDatasetLoader(FlowFileTransform):
    """Source processor for QML pipelines. Emits a JSON array of labelled feature
    records — [{"features": [...], "label": k}, ...] — to feed a feature embedding
    and a trainable model downstream. 'synthetic' generates a separable toy dataset
    offline; 'inline' validates and passes through a dataset supplied as a property.

    Fan the array out with SplitJson ($) so each record drives one embedding."""

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Emits a labelled classical dataset as a JSON array of records "
            "({\"features\": [...], \"label\": k}) to drive a QML pipeline. 'synthetic' "
            "generates a separable two-class Gaussian-blob dataset (numpy only, fully "
            "offline and seeded); 'inline' validates and emits a dataset provided in "
            "the Records property. Feed into SplitJson to fan out one FlowFile per "
            "sample, then into PennylaneFeatureEmbedding."
        )
        tags = ["quantum", "pennylane", "qml", "dataset", "source", "data"]
        dependencies = ["numpy"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.mode = PropertyDescriptor(
            name="Mode",
            description="'synthetic' generates a toy two-class dataset; 'inline' emits "
                        "the dataset given in the Records property.",
            required=True,
            default_value="synthetic",
            allowable_values=["synthetic", "inline"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.num_samples = PropertyDescriptor(
            name="Num Samples",
            description="Total number of samples to generate (synthetic mode).",
            required=True,
            default_value="20",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.num_features = PropertyDescriptor(
            name="Num Features",
            description="Feature-vector dimension (synthetic mode).",
            required=True,
            default_value="3",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.class_separation = PropertyDescriptor(
            name="Class Separation",
            description="Distance between the two class centroids (synthetic mode); "
                        "larger = easier to classify.",
            required=False,
            default_value="2.0",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.seed = PropertyDescriptor(
            name="Seed",
            description="RNG seed for reproducible synthetic data.",
            required=True,
            default_value="0",
            validators=[StandardValidators.NON_NEGATIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.records = PropertyDescriptor(
            name="Records",
            description="Dataset for inline mode: a JSON array of "
                        "{\"features\": [...], \"label\": k} objects.",
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.mode, self.num_samples, self.num_features,
            self.class_separation, self.seed, self.records,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _prop(self, context, descriptor, flowFile):
        return (
            context.getProperty(descriptor)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

    def _synthetic(self, context, flowFile):
        import numpy as np

        n_samples = int(self._prop(context, self.num_samples, flowFile))
        n_feat    = int(self._prop(context, self.num_features, flowFile))
        sep       = float(self._prop(context, self.class_separation, flowFile) or 2.0)
        seed      = int(self._prop(context, self.seed, flowFile))
        rng = np.random.default_rng(seed)

        records = []
        for i in range(n_samples):
            label = i % 2
            centre = (sep / 2.0) if label == 1 else (-sep / 2.0)
            vec = rng.normal(loc=centre, scale=1.0, size=n_feat)
            records.append({
                "features": [round(float(v), 6) for v in vec],
                "label": label,
            })
        rng.shuffle(records)
        return records

    def _inline(self, context, flowFile):
        raw = self._prop(context, self.records, flowFile) or ""
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("Records must be a non-empty JSON array")
        for i, rec in enumerate(parsed):
            if not isinstance(rec, dict) or "features" not in rec:
                raise ValueError("record {} must be an object with a 'features' key".format(i))
        return parsed

    def transform(self, context, flowFile):
        mode = (self._prop(context, self.mode, flowFile) or "synthetic").strip()

        try:
            records = self._inline(context, flowFile) if mode == "inline" \
                else self._synthetic(context, flowFile)
        except (ValueError, json.JSONDecodeError) as exc:
            self.logger.error("PennylaneDatasetLoader: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"dataset.error": str(exc)},
            )

        labels = [r.get("label") for r in records if r.get("label") is not None]
        num_features = len(records[0].get("features", []))

        attrs = {
            "dataset.count":        str(len(records)),
            "dataset.num_features": str(num_features),
            "dataset.num_classes":  str(len(set(labels))) if labels else "0",
            "dataset.mode":         mode,
            "dataset.framework":    "pennylane",
            "mime.type":            "application/json",
        }
        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(records, indent=2).encode("utf-8"),
            attributes=attrs,
        )
