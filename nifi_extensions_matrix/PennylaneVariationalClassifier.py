import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class PennylaneVariationalClassifier(FlowFileTransform):
    """
    Trains a variational quantum classifier on a labelled dataset — the
    flagship of the QML lane: PennylaneDatasetLoader -> this -> QuanifiReport
    is machine learning on a quantum circuit with zero equations.

    Model: <data encoding>(features) -> <ansatz>(weights) -> <Z0>, plus a
    trainable bias; binary labels {0, 1} are mapped to {+1, -1} and trained
    with a square loss by full-batch gradient descent (Adam by default,
    analytic gradients via backprop on default.qubit). The dataset is
    shuffled and split (Train Split) so the report shows honest train and
    validation accuracies.

    The Embedding and Ansatz properties use the same names and values as the
    circuit-building processors PennylaneFeatureEmbedding and
    PennylaneVariationalAnsatz, so the model trained here is the same family
    of circuits those builders emit for the Tier-1 lane: angle/iqp use one
    qubit per feature, amplitude packs the features into ceil(log2(n))
    qubits.

    Input: FlowFile content = JSON array of {"features": [...], "label": 0|1}
    records (exactly what PennylaneDatasetLoader emits). The number of qubits
    equals the number of features.

    Emits train.* attributes and report.type = "training": QuanifiReport
    renders the loss curve and the training summary panel.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Trains a variational quantum classifier (AngleEmbedding + "
            "StronglyEntanglingLayers + trainable bias) on the labelled JSON "
            "dataset in the FlowFile content (from PennylaneDatasetLoader). "
            "Emits train.* attributes (final loss, train/validation accuracy), "
            "the loss history and trained weights as JSON, and report.type = "
            "training for the loss-curve report card."
        )
        tags = ["quantum", "pennylane", "qml", "classifier", "training", "variational"]
        dependencies = ["pennylane>=0.40"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.layers = PropertyDescriptor(
            name="Layers",
            description="Number of StronglyEntanglingLayers blocks (model capacity).",
            required=True,
            default_value="2",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.epochs = PropertyDescriptor(
            name="Epochs",
            description="Full-batch training epochs.",
            required=True,
            default_value="30",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.learning_rate = PropertyDescriptor(
            name="Learning Rate",
            description="Optimizer step size.",
            required=True,
            default_value="0.1",
            validators=[StandardValidators.NUMBER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.optimizer = PropertyDescriptor(
            name="Optimizer",
            description="Classical optimizer for the training loop.",
            required=True,
            default_value="adam",
            allowable_values=["adam", "gradient_descent"],
        )
        self.embedding = PropertyDescriptor(
            name="Embedding",
            description=(
                "Data-encoding template (same vocabulary as "
                "PennylaneFeatureEmbedding). 'angle' and 'iqp' use one qubit per "
                "feature; 'amplitude' packs the features into ceil(log2(n)) qubits."
            ),
            required=True,
            default_value="angle",
            allowable_values=["angle", "iqp", "amplitude"],
        )
        self.ansatz = PropertyDescriptor(
            name="Ansatz",
            description=(
                "Trainable-model template (same vocabulary as "
                "PennylaneVariationalAnsatz)."
            ),
            required=True,
            default_value="strongly_entangling",
            allowable_values=["strongly_entangling", "basic_entangler"],
        )
        self.train_split = PropertyDescriptor(
            name="Train Split",
            description="Fraction of the dataset used for training; the rest validates.",
            required=True,
            default_value="0.8",
            validators=[StandardValidators.NUMBER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.seed = PropertyDescriptor(
            name="Seed",
            description="Random seed for the shuffle and the initial weights.",
            required=True,
            default_value="42",
            validators=[StandardValidators.NON_NEGATIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.embedding, self.ansatz, self.layers,
                            self.epochs, self.learning_rate, self.optimizer,
                            self.train_split, self.seed]

    def getPropertyDescriptors(self):
        return self.descriptors

    @staticmethod
    def _parse_dataset(raw):
        records = json.loads(raw.decode("utf-8"))
        if not isinstance(records, list) or len(records) < 4:
            raise ValueError("expected a JSON array of at least 4 labelled records")
        feats, labels = [], []
        width = None
        for i, rec in enumerate(records):
            if not isinstance(rec, dict) or "features" not in rec or "label" not in rec:
                raise ValueError(
                    "record {} must be {{'features': [...], 'label': 0|1}}".format(i))
            f = [float(v) for v in rec["features"]]
            width = width if width is not None else len(f)
            if len(f) != width:
                raise ValueError(
                    "record {} has {} features, expected {}".format(i, len(f), width))
            lab = int(rec["label"])
            if lab not in (0, 1):
                raise ValueError(
                    "record {} label {} is not binary (0/1)".format(i, lab))
            feats.append(f)
            labels.append(lab)
        if len(set(labels)) < 2:
            raise ValueError("dataset contains a single class; nothing to learn")
        return feats, labels, width

    def transform(self, context, flowFile):
        import math

        import numpy as base_np
        import pennylane as qml
        from pennylane import numpy as pnp

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        try:
            layers = int(get(self.layers))
            epochs = int(get(self.epochs))
            lr = float(get(self.learning_rate))
            opt_name = get(self.optimizer)
            embedding_kind = get(self.embedding)
            ansatz_kind = get(self.ansatz)
            split = float(get(self.train_split))
            seed = int(get(self.seed))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("PennylaneVariationalClassifier: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"train.error": msg},
            )

        try:
            raw = bytes(flowFile.getContentsAsBytes() or b"")
            feats, labels, n_features = self._parse_dataset(raw)
            if not 0.0 < split < 1.0:
                raise ValueError("Train Split must be in (0, 1), got {}".format(split))
        except Exception as exc:
            msg = "invalid training input: {}".format(exc)
            self.logger.error("PennylaneVariationalClassifier: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"train.error": msg},
            )

        rng = base_np.random.default_rng(seed)
        order = rng.permutation(len(feats))
        n_train = max(2, int(round(split * len(feats))))
        n_train = min(n_train, len(feats) - 1)
        train_idx, val_idx = order[:n_train], order[n_train:]

        x = pnp.array(feats, requires_grad=False)
        # labels {0,1} -> targets {+1,-1} to match the <Z> range
        y = pnp.array([1.0 - 2.0 * l for l in labels], requires_grad=False)

        # wire budget follows the embedding, matching PennylaneFeatureEmbedding:
        # angle/iqp use one qubit per feature, amplitude packs into log2(n)
        if embedding_kind == "amplitude":
            n_wires = max(1, math.ceil(math.log2(n_features)))
        else:
            n_wires = n_features

        template = qml.BasicEntanglerLayers if ansatz_kind == "basic_entangler" \
            else qml.StronglyEntanglingLayers

        dev = qml.device("default.qubit", wires=n_wires)

        @qml.qnode(dev)
        def circuit(weights, features):
            if embedding_kind == "amplitude":
                qml.AmplitudeEmbedding(features, wires=range(n_wires),
                                       normalize=True, pad_with=0.0)
            elif embedding_kind == "iqp":
                qml.IQPEmbedding(features, wires=range(n_wires))
            else:
                qml.AngleEmbedding(features, wires=range(n_wires))
            template(weights, wires=range(n_wires))
            return qml.expval(qml.PauliZ(0))

        def predict(weights, bias, features):
            return circuit(weights, features) + bias

        def cost(weights, bias):
            preds = pnp.stack([predict(weights, bias, x[i]) for i in train_idx])
            return pnp.mean((preds - y[train_idx]) ** 2)

        shape = template.shape(n_layers=layers, n_wires=n_wires)
        weights = pnp.array(0.1 * rng.standard_normal(shape), requires_grad=True)
        bias = pnp.array(0.0, requires_grad=True)
        opt = qml.AdamOptimizer(lr) if opt_name == "adam" \
            else qml.GradientDescentOptimizer(lr)

        try:
            loss_history = []
            for _ in range(epochs):
                (weights, bias), loss = opt.step_and_cost(cost, weights, bias)
                loss_history.append(float(loss))

            def accuracy(idx):
                if len(idx) == 0:
                    return None
                hits = 0
                for i in idx:
                    label_pred = 0 if float(predict(weights, bias, x[i])) > 0 else 1
                    hits += int(label_pred == labels[i])
                return hits / len(idx)

            train_acc = accuracy(train_idx)
            val_acc = accuracy(val_idx)
        except Exception as exc:
            self.logger.error("PennylaneVariationalClassifier training failed: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"train.error": "training failed: {}".format(exc)},
            )

        content = {
            "loss_history": [round(v, 8) for v in loss_history],
            "trained_weights": base_np.asarray(weights).tolist(),
            "bias": float(bias),
            "train_accuracy": train_acc,
            "val_accuracy": val_acc,
        }
        attrs = {
            "train.framework": "pennylane",
            "train.model": "{} embedding + {}".format(embedding_kind, ansatz_kind),
            "train.embedding": embedding_kind,
            "train.ansatz": ansatz_kind,
            "train.num_features": str(n_features),
            "train.num_qubits": str(n_wires),
            "train.layers": str(layers),
            "train.epochs": str(epochs),
            "train.optimizer": opt_name,
            "train.learning_rate": "{:g}".format(lr),
            "train.samples_train": str(len(train_idx)),
            "train.samples_val": str(len(val_idx)),
            "train.final_loss": "{:.6f}".format(loss_history[-1]),
            "train.train_accuracy": "{:.4f}".format(train_acc),
            "report.type": "training",
        }
        if val_acc is not None:
            attrs["train.val_accuracy"] = "{:.4f}".format(val_acc)

        self.logger.warn(
            "PennylaneVariationalClassifier: {} epochs, final loss {:.4f}, "
            "train acc {:.2f}, val acc {}".format(
                epochs, loss_history[-1], train_acc,
                "{:.2f}".format(val_acc) if val_acc is not None else "n/a"))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(content, indent=2).encode("utf-8"),
            attributes=attrs,
        )
