import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QrispPhaseEstimation(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Quantum Phase Estimation (QPE) using Qrisp. Estimates the eigenphase φ of a "
            "unitary operator U such that U|ψ⟩ = exp(2πiφ)|ψ⟩. "
            "Uses Qrisp's QPE primitive with iter_spec=True for efficient "
            "controlled-U^(2^k) application. "
            "Built-in unitaries: T (φ=1/8), S (φ=1/4), Z (φ=1/2). "
            "The target register is prepared in the |1⟩ eigenstate. "
            "Outputs a probability distribution over estimated phase values (as floats 0–1). "
            "Sets qpe.top_phase to the most likely phase; also exports the circuit as qasm2."
        )
        tags = ["quantum", "qrisp", "qpe", "phase-estimation", "circuit"]
        dependencies = ["qrisp==0.9.5", "qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.phase_register_size = PropertyDescriptor(
            name="Phase Register Size",
            description=(
                "Number of qubits in the phase register. Precision = 1 / 2^m. "
                "3 qubits gives 1/8 precision; 4 qubits gives 1/16."
            ),
            required=True,
            default_value="3",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.builtin_unitary = PropertyDescriptor(
            name="Builtin Unitary",
            description=(
                "Gate to use as the unitary. "
                "T: eigenphase = 1/8 (0.125).  "
                "S: eigenphase = 1/4 (0.25).  "
                "Z: eigenphase = 1/2 (0.5)."
            ),
            required=True,
            default_value="T",
            allowable_values=["T", "S", "Z"],
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description="Number of measurement shots.",
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.phase_register_size, self.builtin_unitary, self.shots]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        import contextlib
        import io
        import numpy as np
        from qrisp import QuantumVariable, QPE, x, p

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        try:
            m       = int(get(self.phase_register_size))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QrispPhaseEstimation: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"qpe.error": msg},
            )
        builtin = get(self.builtin_unitary)
        try:
            shots   = int(get(self.shots))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QrispPhaseEstimation: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"qpe.error": msg},
            )

        # Phase shifts for each builtin gate:
        # T: Rz(π/4) eigenphase = 1/8   (p(π/4)|1⟩ = e^(iπ/4)|1⟩, φ = 1/8)
        # S: Rz(π/2) eigenphase = 1/4
        # Z: Rz(π)   eigenphase = 1/2
        gate_phases = {"T": np.pi / 4, "S": np.pi / 2, "Z": np.pi}
        phase_angle = gate_phases[builtin]

        # iter_spec=True: Qrisp calls U(args, iter=2**k) instead of U(args) 2**k times —
        # scaling the phase angle avoids exponentially many gate calls for large k.
        def U(qv, iter=1):
            p(phase_angle * iter, qv[0])

        target = QuantumVariable(1)
        x(target)  # Prepare |1⟩ eigenstate
        res = QPE(target, U, precision=m, iter_spec=True)

        # Suppress Qrisp's tqdm progress bar.
        with contextlib.redirect_stdout(io.StringIO()):
            measurement = res.get_measurement(shots=shots)

        sorted_m = dict(sorted(measurement.items(), key=lambda kv: kv[1], reverse=True))
        top_phase, top_prob = next(iter(sorted_m.items()))

        # JSON keys must be strings.
        result_json = {str(k): v for k, v in sorted_m.items()}

        # Export circuit as qasm2 for diagram and downstream inspection.
        qasm2_str = ""
        diagram   = ""
        try:
            from qiskit import qasm2 as qiskit_qasm2
            from qiskit.compiler import transpile as qk_transpile
            qk = res.qs.compile().to_qiskit()
            # Transpile to portable qelib1.inc basis — avoids `p`/`cp` in the dump
            # which QiskitAerSimulator and CirqSimulator cannot parse.
            qk_export = qk_transpile(
                qk,
                basis_gates=['h', 'cx', 'rz', 'x', 'swap', 's', 't', 'sdg', 'tdg'],
                optimization_level=0,
            )
            qasm2_str = qiskit_qasm2.dumps(qk_export)
            diagram   = str(qk.draw('text'))
        except Exception as exc:
            self.logger.warn("QPE QASM2 export skipped: {}".format(exc))

        self.logger.warn(
            "QrispPhaseEstimation (m={}, builtin={}, shots={}): top_phase={} p={:.4f}{}".format(
                m, builtin, shots, top_phase, top_prob,
                "\n" + diagram if diagram else ""
            )
        )

        attrs = {
            "qpe.top_phase":       str(top_phase),
            "qpe.top_probability": f"{top_prob:.4f}",
            "qpe.precision":       str(m),
            "qpe.builtin":         builtin,
            "qpe.framework":       "qrisp",
            "qpe.shots":           str(shots),
            # Blank stale cross-framework presentation attrs (NiFi merges attrs).
            "circuit.svg":         "",
            "circuit.qasm3":       "",
        }
        if qasm2_str:
            attrs["circuit.format"] = "qasm2"
            attrs["circuit.qasm2"]  = qasm2_str
        if diagram:
            attrs["circuit.diagram"] = diagram

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(result_json, indent=2).encode("utf-8"),
            attributes=attrs,
        )
