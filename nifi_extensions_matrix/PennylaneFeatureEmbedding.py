import json
import math
import re

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


def _strip_measurements(qasm):
    """Drop the trailing measure/creg lines PennyLane's to_openqasm always emits.

    Circuit builders in Quanifi are measurement-free — measurement is the
    simulator's responsibility — so the exported QASM must contain only the
    encoding gates.
    """
    kept = [ln for ln in qasm.splitlines()
            if not re.match(r"\s*(measure|creg)\b", ln)]
    return "\n".join(kept) + "\n"


class PennylaneFeatureEmbedding(FlowFileTransform):
    """Encode a classical feature vector into a quantum circuit (the QML data-loading
    step). Emits OpenQASM 2.0 so the resulting circuit feeds any Quanifi simulator
    (QiskitAerSimulator / CirqSimulator / QrispSimulator) or PennylaneExpectation."""

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Encodes a classical feature vector into a quantum circuit using a "
            "PennyLane embedding template (angle / amplitude / iqp / basis) and emits "
            "it as OpenQASM 2.0. The feature vector is read from the FlowFile content "
            "(a JSON array of numbers) when present, otherwise from the Features "
            "property. This is the classical->quantum data-loading step of a QML "
            "pipeline; the qasm2 output is interoperable with every Quanifi simulator."
        )
        tags = ["quantum", "pennylane", "qml", "embedding", "feature-map", "circuit"]
        dependencies = ["pennylane>=0.40", "pennylane-qiskit>=0.40",
                        "qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.embedding = PropertyDescriptor(
            name="Embedding",
            description=(
                "Which PennyLane embedding template to use. 'angle' rotates one qubit "
                "per feature; 'amplitude' loads the (normalised, padded) vector into "
                "2^n amplitudes; 'iqp' is an IQP feature map; 'basis' encodes a bit "
                "string into computational-basis states."
            ),
            required=True,
            default_value="angle",
            allowable_values=["angle", "amplitude", "iqp", "basis"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.features = PropertyDescriptor(
            name="Features",
            description=(
                "Fallback feature vector as a JSON array of numbers, used only when "
                "the FlowFile content is not itself a JSON array. For 'basis' the "
                "values are coerced to bits (0/1)."
            ),
            required=False,
            default_value="[0.1, 0.5, 0.9]",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.rotation = PropertyDescriptor(
            name="Rotation",
            description="Rotation axis for the 'angle' embedding (ignored otherwise).",
            required=False,
            default_value="Y",
            allowable_values=["X", "Y", "Z"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.embedding, self.features, self.rotation]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _prop(self, context, descriptor, flowFile):
        return (
            context.getProperty(descriptor)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

    def _read_features(self, context, flowFile):
        """Prefer a JSON-array FlowFile content (data-driven), else the property."""
        raw = bytes(flowFile.getContentsAsBytes()).decode("utf-8", errors="ignore").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
        return json.loads(self._prop(context, self.features, flowFile))

    def transform(self, context, flowFile):
        import numpy as np
        import pennylane as qml

        kind     = (self._prop(context, self.embedding, flowFile) or "angle").strip()
        rotation = (self._prop(context, self.rotation, flowFile) or "Y").strip()

        try:
            features = self._read_features(context, flowFile)
            if not isinstance(features, list) or not features:
                raise ValueError("feature vector must be a non-empty JSON array")
            # Coerce to floats inside the guard: a data-driven FlowFile can carry
            # non-numeric entries, and float() raises ValueError (bad string) or
            # TypeError (dict/list) — both must route to failure, not escape.
            features = [float(v) for v in features]
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.logger.error("PennylaneFeatureEmbedding: bad features: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"embedding.error": str(exc)},
            )

        # Resolve the number of qubits and the embedding op from the chosen template.
        if kind == "amplitude":
            n = max(1, math.ceil(math.log2(len(features))))
            vec = np.array(features, dtype=float)

            def encode():
                qml.AmplitudeEmbedding(vec, wires=range(n), normalize=True, pad_with=0.0)
        elif kind == "basis":
            n = len(features)
            bits = [int(round(float(v))) & 1 for v in features]

            def encode():
                qml.BasisEmbedding(bits, wires=range(n))
        elif kind == "iqp":
            n = len(features)
            vec = np.array(features, dtype=float)

            def encode():
                qml.IQPEmbedding(vec, wires=range(n))
        else:  # angle
            n = len(features)
            vec = np.array(features, dtype=float)

            def encode():
                qml.AngleEmbedding(vec, wires=range(n), rotation=rotation)

        dev = qml.device("default.qubit", wires=n)

        @qml.qnode(dev)
        def circuit():
            encode()
            return qml.expval(qml.PauliZ(0))

        qasm = _strip_measurements(qml.to_openqasm(circuit, measure_all=False)())
        diagram = qml.draw(circuit)()

        res = qml.specs(circuit, level="device")().resources
        nonlocal_gates = sum(c for size, c in res.gate_sizes.items() if size >= 2)

        attrs = {
            "circuit.format":         "qasm2",
            "circuit.svg": "",  # blank stale Cirq SVG (NiFi merges attrs)
            "circuit.qasm2":          qasm,
            "circuit.num_qubits":     str(n),
            "circuit.depth":          str(res.depth),
            "circuit.gate_count":     str(res.num_gates),
            "circuit.nonlocal_gates": str(nonlocal_gates),
            "circuit.t_count":        str(res.gate_counts.get("T", 0)),
            "circuit.diagram":        diagram,
            "circuit.embedding":      kind,
            "circuit.num_features":   str(len(features)),
            "circuit.framework":      "pennylane",
        }
        return FlowFileTransformResult(
            relationship="success",
            contents=qasm.encode("utf-8"),
            attributes=attrs,
        )
