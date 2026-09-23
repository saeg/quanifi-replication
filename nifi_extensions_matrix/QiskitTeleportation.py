import time
import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitTeleportation(FlowFileTransform):
    """
    Quantum teleportation of an arbitrary single-qubit state, self-verifying.

    Qubit 0 carries the message |psi> = Rz(phi) Ry(theta) |0>; qubits 1 and 2
    share a Bell pair. A Bell measurement on qubits 0 and 1 plus classically
    conditioned X/Z corrections (real mid-circuit measurement + feed-forward,
    via Qiskit dynamic circuits) reconstructs |psi> on qubit 2. The processor
    then applies the inverse preparation to qubit 2 and measures it: ideally
    it always reads 0, so teleport.fidelity = P(qubit2 = 0) is the number to
    look at (1.0 means the state arrived intact).

    No report.type is set: the default report card lists the teleport.*
    attributes, which is exactly the headline this demo needs.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Teleports Rz(Phi)Ry(Theta)|0> from qubit 0 to qubit 2 using a Bell "
            "pair, a Bell measurement, and classically conditioned corrections "
            "(dynamic circuit). Self-verifies by inverting the preparation on the "
            "target: teleport.fidelity = P(target measures 0), ideally 1.0."
        )
        tags = ["quantum", "qiskit", "teleportation", "entanglement", "education"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-aer>=0.13.0"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.theta = PropertyDescriptor(
            name="Theta",
            description="Polar angle of the message state (radians). Default pi/3.",
            required=True,
            default_value="1.0471975512",
            validators=[StandardValidators.NUMBER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.phi = PropertyDescriptor(
            name="Phi",
            description="Azimuthal angle of the message state (radians). Default pi/4.",
            required=True,
            default_value="0.7853981634",
            validators=[StandardValidators.NUMBER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description="Number of protocol runs.",
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.random_seed = PropertyDescriptor(
            name="Random Seed",
            description=(
                "Seed for reproducible sampling. Empty = nondeterministic. Passed to Aer as seed_simulator."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.theta, self.phi, self.shots, self.random_seed]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qiskit import QuantumCircuit, transpile
        from qiskit_aer import AerSimulator

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        try:
            theta = float(get(self.theta))
            phi = float(get(self.phi))
        except (TypeError, ValueError):
            msg = "Theta and Phi must be numbers (radians), got {!r} / {!r}".format(
                get(self.theta), get(self.phi))
            self.logger.error("QiskitTeleportation: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"teleport.error": msg},
            )
        shots = int(get(self.shots))
        seed_raw = (get(self.random_seed) or "").strip()
        seed = int(seed_raw) if seed_raw else None

        qc = QuantumCircuit(3, 3)
        # message state on qubit 0
        qc.ry(theta, 0)
        qc.rz(phi, 0)
        # Bell pair between qubits 1 (Alice) and 2 (Bob)
        qc.h(1)
        qc.cx(1, 2)
        # Bell measurement of message + Alice's half
        qc.cx(0, 1)
        qc.h(0)
        qc.measure(0, 0)
        qc.measure(1, 1)
        # classical feed-forward corrections on Bob's qubit
        with qc.if_test((qc.clbits[1], 1)):
            qc.x(2)
        with qc.if_test((qc.clbits[0], 1)):
            qc.z(2)
        # verification: undo the preparation; ideal readout is always 0
        qc.rz(-phi, 2)
        qc.ry(-theta, 2)
        qc.measure(2, 2)

        sim = AerSimulator()
        run_kwargs = {"shots": shots}
        if seed is not None:
            run_kwargs["seed_simulator"] = seed
        t0 = time.time()
        try:
            counts = sim.run(transpile(qc, sim), **run_kwargs).result().get_counts()
        except Exception as exc:
            self.logger.error("QiskitTeleportation simulation failed: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"teleport.error": "simulation failed: {}".format(exc)},
            )
        # canonical order: leftmost char = clbit 0 (Aer keys are little-endian)
        counts = {k.replace(" ", "")[::-1]: v for k, v in counts.items()}
        sorted_counts = dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        # keys read c0 c1 c2 left->right; fidelity = P(verification bit c2 == 0)
        fidelity = sum(v for k, v in counts.items() if k[2] == "0") / shots

        attrs = {
            "teleport.fidelity": "{:.4f}".format(fidelity),
            "teleport.theta": "{:.6f}".format(theta),
            "teleport.phi": "{:.6f}".format(phi),
            "teleport.framework": "qiskit",
            "teleport.protocol": "bell-measure + classically-conditioned X/Z",
            "circuit.num_qubits": "3",
            "circuit.diagram": str(qc.draw("text")),
            "circuit.depth": str(qc.depth()),
            "sim.shots": str(shots),
            "sim.top_result": top_state,
            "sim.top_probability": "{:.4f}".format(top_count / shots),
            "sim.framework": "qiskit",
            "sim.bit_order": "q0_left",
            "perf.elapsed_seconds": "{:.4f}".format(time.time() - t0),
            **({"run.seed": str(seed)} if seed is not None else {}),
        }

        self.logger.warn(
            "QiskitTeleportation: theta={:.4f} phi={:.4f} -> fidelity {:.4f}".format(
                theta, phi, fidelity))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes=attrs,
        )
