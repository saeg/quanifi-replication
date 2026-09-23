import re

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


def _strip_measurements(qasm):
    kept = [ln for ln in qasm.splitlines()
            if not re.match(r"\s*(measure|creg)\b", ln)]
    return "\n".join(kept) + "\n"


def _qubits_from_qasm(qasm):
    """Total qubit count from the `qreg q[N];` declarations of a QASM 2.0 string."""
    return sum(int(m) for m in re.findall(r"qreg\s+\w+\s*\[\s*(\d+)\s*\]", qasm))


class PennylaneVariationalAnsatz(FlowFileTransform):
    """Build a parameterised variational ansatz (the trainable 'model' block of a QML
    circuit) and emit it as OpenQASM 2.0. Standalone it produces a fresh n-qubit
    ansatz; in compose mode it appends the ansatz onto an incoming qasm2 circuit
    (e.g. a PennylaneFeatureEmbedding), giving an embedding->ansatz model circuit."""

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a PennyLane variational ansatz (StronglyEntanglingLayers or "
            "BasicEntanglerLayers) with reproducible seeded weights and emits it as "
            "OpenQASM 2.0. Standalone mode creates a fresh n-qubit ansatz; when the "
            "incoming FlowFile already carries a qasm2 circuit (circuit.format=qasm2) "
            "the ansatz is appended to it, composing an embedding->ansatz model "
            "circuit. The qasm2 output feeds any Quanifi simulator or "
            "PennylaneExpectation."
        )
        tags = ["quantum", "pennylane", "qml", "ansatz", "variational", "circuit"]
        dependencies = ["pennylane>=0.40", "pennylane-qiskit>=0.40",
                        "qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.ansatz = PropertyDescriptor(
            name="Ansatz",
            description=(
                "Variational template. 'strongly_entangling' = StronglyEntanglingLayers "
                "(Rot + ring of CNOTs per layer); 'basic_entangler' = BasicEntanglerLayers "
                "(one rotation + CNOT ring per layer)."
            ),
            required=True,
            default_value="strongly_entangling",
            allowable_values=["strongly_entangling", "basic_entangler"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.num_qubits = PropertyDescriptor(
            name="Num Qubits",
            description="Number of qubits in standalone mode. Ignored in compose mode, "
                        "where the qubit count is taken from the incoming circuit.",
            required=True,
            default_value="3",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.num_layers = PropertyDescriptor(
            name="Num Layers",
            description="Number of ansatz layers (circuit depth / expressivity).",
            required=True,
            default_value="2",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.weight_seed = PropertyDescriptor(
            name="Weight Seed",
            description="Seed for the random initial weights, so the emitted circuit is "
                        "reproducible.",
            required=True,
            default_value="42",
            validators=[StandardValidators.NON_NEGATIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.ansatz, self.num_qubits, self.num_layers, self.weight_seed,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _prop(self, context, descriptor, flowFile):
        return (
            context.getProperty(descriptor)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

    def transform(self, context, flowFile):
        import numpy as np
        import pennylane as qml

        kind   = (self._prop(context, self.ansatz, flowFile) or "strongly_entangling").strip()
        layers = int(self._prop(context, self.num_layers, flowFile))
        seed   = int(self._prop(context, self.weight_seed, flowFile))

        # Compose mode: an incoming qasm2 circuit becomes the prefix.
        in_fmt   = flowFile.getAttribute("circuit.format")
        prefix_fn = None
        if in_fmt == "qasm2":
            incoming = bytes(flowFile.getContentsAsBytes()).decode("utf-8")
            n = _qubits_from_qasm(incoming)
            prefix_fn = qml.from_qasm(incoming)
            mode = "compose"
        elif in_fmt:
            self.logger.error(
                "PennylaneVariationalAnsatz: compose needs circuit.format=qasm2, got "
                "'{}'".format(in_fmt))
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"ansatz.error": (
                    "compose mode requires circuit.format=qasm2; set the upstream "
                    "processor's Output Format to qasm2")},
            )
        else:
            n = int(self._prop(context, self.num_qubits, flowFile))
            mode = "standalone"

        if kind == "basic_entangler":
            template = qml.BasicEntanglerLayers
            shape = template.shape(n_layers=layers, n_wires=n)
        else:
            template = qml.StronglyEntanglingLayers
            shape = template.shape(n_layers=layers, n_wires=n)

        weights = np.random.default_rng(seed).uniform(0, 2 * np.pi, size=shape)

        dev = qml.device("default.qubit", wires=n)

        @qml.qnode(dev)
        def circuit():
            if prefix_fn is not None:
                prefix_fn()
            template(weights, wires=range(n))
            return qml.expval(qml.PauliZ(0))

        qasm    = _strip_measurements(qml.to_openqasm(circuit, measure_all=False)())
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
            "circuit.ansatz":         kind,
            "circuit.num_layers":     str(layers),
            "circuit.num_params":     str(int(np.asarray(weights).size)),
            "circuit.ansatz_mode":    mode,
            "circuit.framework":      "pennylane",
        }
        return FlowFileTransformResult(
            relationship="success",
            contents=qasm.encode("utf-8"),
            attributes=attrs,
        )
