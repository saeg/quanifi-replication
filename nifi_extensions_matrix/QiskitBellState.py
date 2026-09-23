import time
import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitBellState(FlowFileTransform):
    """
    Prepares and measures one of the four Bell states — the canonical
    entanglement demo and the natural first step after QiskitQuantumHelloWorld.

    All four states come from H(0); CX(0,1) plus a local flip:
      phi_plus  = (|00> + |11>)/sqrt(2)
      phi_minus = (|00> - |11>)/sqrt(2)   (Z on qubit 0)
      psi_plus  = (|01> + |10>)/sqrt(2)   (X on qubit 1)
      psi_minus = (|01> - |10>)/sqrt(2)   (Z and X)

    The report shows the two-outcome histogram; bell.correlation is the
    measured probability mass on the correlated pair (1.0 ideally), which is
    the "entanglement worked" number a learner should look at.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Prepares one of the four Bell states (H + CNOT + optional local "
            "flips), samples it, and reports the two-outcome histogram plus "
            "bell.correlation (probability mass on the correlated pair, 1.0 "
            "ideal). The canonical entanglement demo."
        )
        tags = ["quantum", "qiskit", "bell", "entanglement", "education"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-aer>=0.13.0"]

    _FLIPS = {
        "phi_plus":  (False, False),
        "phi_minus": (True,  False),
        "psi_plus":  (False, True),
        "psi_minus": (True,  True),
    }

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.bell_state = PropertyDescriptor(
            name="Bell State",
            description="Which of the four Bell states to prepare.",
            required=True,
            default_value="phi_plus",
            allowable_values=["phi_plus", "phi_minus", "psi_plus", "psi_minus"],
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description="Number of measurement shots.",
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
        self.descriptors = [self.bell_state, self.shots, self.random_seed]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qiskit import QuantumCircuit, qasm3, transpile
        from qiskit_aer import AerSimulator

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        state = get(self.bell_state)
        if state not in self._FLIPS:
            msg = "unknown Bell State {!r}; expected one of {}".format(
                state, sorted(self._FLIPS))
            self.logger.error("QiskitBellState: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"bell.error": msg},
            )
        shots = int(get(self.shots))
        seed_raw = (get(self.random_seed) or "").strip()
        seed = int(seed_raw) if seed_raw else None
        z_flip, x_flip = self._FLIPS[state]

        qc = QuantumCircuit(2)
        qc.h(0)
        qc.cx(0, 1)
        if z_flip:
            qc.z(0)
        if x_flip:
            qc.x(1)
        measured = qc.copy()
        measured.measure_all()

        sim = AerSimulator()
        run_kwargs = {"shots": shots}
        if seed is not None:
            run_kwargs["seed_simulator"] = seed
        t0 = time.time()
        counts = sim.run(transpile(measured, sim), **run_kwargs).result().get_counts()
        elapsed = time.time() - t0
        # canonical q0-left keys (Aer keys are little-endian: qubit 0 rightmost)
        counts = {k.replace(" ", "")[::-1]: v for k, v in counts.items()}
        sorted_counts = dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        pair = ("00", "11") if state.startswith("phi") else ("01", "10")
        correlation = sum(v for k, v in counts.items() if k in pair) / shots

        ops = qc.count_ops()
        attrs = {
            "bell.state": state,
            "bell.pair": "|{}>, |{}>".format(*pair),
            "bell.correlation": "{:.4f}".format(correlation),
            "bell.framework": "qiskit",
            "circuit.format": "qasm3",
            "circuit.svg": "",  # blank stale Cirq SVG (NiFi merges attrs)
            "circuit.qasm3": qasm3.dumps(qc),
            "circuit.num_qubits": "2",
            "circuit.diagram": str(qc.draw("text")),
            "circuit.depth": str(qc.depth()),
            "circuit.gate_count": str(sum(v for k, v in ops.items()
                                          if k not in ("barrier", "measure"))),
            "circuit.nonlocal_gates": str(qc.num_nonlocal_gates()),
            "circuit.t_count": str(ops.get("t", 0) + ops.get("tdg", 0)),
            "sim.shots": str(shots),
            "sim.top_result": top_state,
            "sim.top_probability": "{:.4f}".format(top_count / shots),
            "sim.framework": "qiskit",
            "sim.bit_order": "q0_left",
            "report.type": "simulation",
            "perf.elapsed_seconds": "{:.4f}".format(elapsed),
            **({"run.seed": str(seed)} if seed is not None else {}),
        }

        self.logger.warn(
            "QiskitBellState: {} -> correlation {:.4f} on pair {}".format(
                state, correlation, pair))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes=attrs,
        )
