import json
import re

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


def _qubits_from_qasm(qasm):
    return sum(int(m) for m in re.findall(r"qreg\s+\w+\s*\[\s*(\d+)\s*\]", qasm))


class PennylaneExpectation(FlowFileTransform):
    """The PennyLane execution lane. Reads a qasm2 circuit and, on a PennyLane device,
    returns its native QML outputs — per-qubit Pauli expectation values and the full
    probability distribution — rather than shot counts. Establishes the
    sim.framework=pennylane attribute family."""

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Runs a quantum circuit (OpenQASM 2.0, via circuit.format=qasm2) on a "
            "PennyLane device and emits its QML-native outputs: per-qubit Pauli "
            "expectation values (sim.expectations) and the probability distribution "
            "(FlowFile content, JSON state->probability). Unlike the shot-count "
            "simulators this is the expectation-value lane PennyLane is built around. "
            "Set Shots for sampled estimates, or leave blank for an exact (analytic) "
            "result. Accepts qasm2 from any Quanifi circuit builder."
        )
        tags = ["quantum", "pennylane", "qml", "expectation", "probabilities", "simulation"]
        dependencies = ["pennylane>=0.40", "pennylane-qiskit>=0.40",
                        "qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.observable = PropertyDescriptor(
            name="Observable",
            description="Single-qubit Pauli whose expectation value is reported on "
                        "each qubit.",
            required=True,
            default_value="Z",
            allowable_values=["Z", "X", "Y"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description="Number of samples for estimating expectations/probabilities. "
                        "Leave blank for an exact analytic result.",
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.observable, self.shots]

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

        fmt = flowFile.getAttribute("circuit.format")
        if fmt != "qasm2":
            self.logger.error(
                "PennylaneExpectation: needs circuit.format=qasm2, got '{}'".format(fmt))
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"sim.error": (
                    "PennylaneExpectation accepts only qasm2. Set the upstream "
                    "processor's Output Format to qasm2.")},
            )

        qasm = bytes(flowFile.getContentsAsBytes()).decode("utf-8")
        n = _qubits_from_qasm(qasm)
        if n <= 0:
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"sim.error": "could not determine qubit count from qasm2"},
            )

        obs_name = (self._prop(context, self.observable, flowFile) or "Z").strip()
        paulis = {"Z": qml.PauliZ, "X": qml.PauliX, "Y": qml.PauliY}
        if obs_name not in paulis:
            self.logger.error(
                "PennylaneExpectation: unknown Observable '{}'".format(obs_name))
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"sim.error": (
                    "Unknown Observable '{}'. Expected one of Z, X, Y."
                    .format(obs_name))},
            )
        pauli = paulis[obs_name]

        shots_raw = (self._prop(context, self.shots, flowFile) or "").strip()
        shots = int(shots_raw) if shots_raw else None

        try:
            loaded = qml.from_qasm(qasm, measurements=[])
            dev = qml.device("default.qubit", wires=n)

            @qml.qnode(dev)
            def expectations():
                loaded()
                return [qml.expval(pauli(i)) for i in range(n)]

            @qml.qnode(dev)
            def distribution():
                loaded()
                return qml.probs(wires=range(n))

            # Finite shots are applied per-QNode via set_shots (device-level shots
            # are deprecated); None leaves the analytic (exact) result.
            if shots is not None:
                expectations = qml.set_shots(expectations, shots=shots)
                distribution = qml.set_shots(distribution, shots=shots)

            expvals = [float(v) for v in expectations()]
            probs   = [float(p) for p in distribution()]
        except Exception as exc:
            self.logger.error("PennylaneExpectation: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"sim.error": (
                    "PennyLane failed to load or execute the circuit: {}"
                    .format(exc))},
            )

        dist = {format(i, "0{}b".format(n)): probs[i] for i in range(len(probs))}
        dist = dict(sorted(dist.items(), key=lambda kv: kv[1], reverse=True))
        top_state, top_prob = next(iter(dist.items()))

        expectations_map = {"q{}".format(i): round(expvals[i], 6) for i in range(n)}

        attrs = {
            "sim.framework":       "pennylane",
            "report.type":         "simulation",
            "sim.shots":           str(shots) if shots else "analytic",
            "sim.observable":      obs_name,
            "sim.expectations":    json.dumps(expectations_map),
            "sim.top_result":      top_state,
            "sim.top_probability": "{:.4f}".format(top_prob),
        }
        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(dist, indent=2).encode("utf-8"),
            attributes=attrs,
        )
