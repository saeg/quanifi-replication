import io
import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitStatePreparation(FlowFileTransform):
    """
    Prepares a quantum state from classical description.

    Four modes (set via 'State Type'):

      uniform  — equal superposition H⊗n |0⟩  =  (1/√2ⁿ) Σ|x⟩
      ghz      — GHZ entangled state  (|00…0⟩ + |11…1⟩) / √2
      basis    — single computational basis state |k⟩  (X gates where bit=1)
      custom   — arbitrary statevector loaded from a JSON amplitude array

    The output FlowFile carries the circuit in QASM3 or QPY format.
    Connect to AerSimulator to run it, or to PhaseEstimation to use it as
    a unitary.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Prepares a quantum state circuit from a classical description. "
            "Supports uniform superposition, GHZ, a specific basis state, or an "
            "arbitrary normalized amplitude vector (custom mode)."
        )
        tags = ["quantum", "qiskit", "state-preparation", "superposition", "ghz", "circuit"]
        dependencies = ["qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.state_type = PropertyDescriptor(
            name="State Type",
            description=(
                "uniform — H⊗n equal superposition.\n"
                "ghz     — (|00…0⟩ + |11…1⟩)/√2 entangled state.\n"
                "basis   — single basis state |k⟩ (set Target Basis State).\n"
                "custom  — arbitrary state from Amplitudes JSON array."
            ),
            required=True,
            default_value="uniform",
            allowable_values=["uniform", "ghz", "basis", "custom"],
        )
        self.qubit_count = PropertyDescriptor(
            name="Qubit Count",
            description=(
                "Number of qubits. Ignored in custom mode — inferred from "
                "the length of the Amplitudes array (must be a power of 2)."
            ),
            required=True,
            default_value="2",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.target_basis_state = PropertyDescriptor(
            name="Target Basis State",
            description=(
                "Index of the basis state to prepare (basis mode only). "
                "E.g. 3 prepares |11⟩ on a 2-qubit register."
            ),
            required=False,
            default_value="0",
            validators=[StandardValidators.INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.amplitudes = PropertyDescriptor(
            name="Amplitudes",
            description=(
                "JSON array of real amplitudes for custom mode only. "
                "Length must equal 2^n (8 values for 3 qubits, 4 for 2, 16 for 4, …). "
                "Values are L2-normalised automatically, so [1,1,1,1,1,1,1,1] "
                "and [0.354,0.354,…] both produce equal superposition. "
                "Leave empty for all other State Types."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.NONE,
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "'qasm3' writes human-readable OpenQASM 3. "
                "'qpy' writes compact Qiskit binary (lossless). "
                "'qasm2' writes OpenQASM 2.0 (use this to feed CirqSimulator or other non-Qiskit tools)."
            ),
            required=True,
            default_value="qasm3",
            allowable_values=["qasm3", "qpy", "qasm2"],
        )
        self.descriptors = [
            self.state_type,
            self.qubit_count,
            self.target_basis_state,
            self.amplitudes,
            self.output_format,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _fail(self, msg):
        self.logger.error("QiskitStatePreparation: " + msg)
        return FlowFileTransformResult(
            relationship="failure",
            attributes={"state_prep.error": msg},
        )

    def transform(self, context, flowFile):
        import numpy as np
        from qiskit import QuantumCircuit

        def get(prop):
            return (
                context.getProperty(prop)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )

        state_type  = context.getProperty(self.state_type).getValue()
        fmt         = context.getProperty(self.output_format).getValue()
        n           = int(get(self.qubit_count))

        # ------------------------------------------------------------------ #
        #  Build circuit                                                       #
        # ------------------------------------------------------------------ #

        if state_type == "uniform":
            circuit = QuantumCircuit(n)
            circuit.h(range(n))
            extra_attrs = {}

        elif state_type == "ghz":
            circuit = QuantumCircuit(n)
            circuit.h(0)
            for target in range(1, n):
                circuit.cx(0, target)
            extra_attrs = {}

        elif state_type == "basis":
            k = int(get(self.target_basis_state))
            if k < 0 or k >= 2 ** n:
                return self._fail(
                    f"Target Basis State {k} is out of range for {n} qubits "
                    f"(must be 0–{2**n - 1})."
                )
            circuit = QuantumCircuit(n)
            # Canonical bit order: qubit i carries digit i of format(k, '0nb')
            # (qubit 0 = MSB), so |k⟩ measures as format(k, '0nb') under the
            # simulators' q0-left keys — same physical circuit as
            # CirqStatePreparation for the same k.
            for i, bit in enumerate(format(k, f'0{n}b')):
                if bit == '1':
                    circuit.x(i)
            extra_attrs = {"state_prep.basis_state": str(k)}

        elif state_type == "custom":
            raw_amps = context.getProperty(self.amplitudes).getValue() or ""
            if not raw_amps:
                return self._fail("Amplitudes must be provided for custom mode.")

            try:
                # json.JSONDecodeError subclasses ValueError; np.array raises
                # ValueError/TypeError on non-numeric entries.
                amps = np.array(json.loads(raw_amps), dtype=complex)
            except (ValueError, TypeError) as exc:
                return self._fail(f"Amplitudes must be a JSON array of numbers: {exc}")
            if amps.ndim != 1:
                return self._fail("Amplitudes must be a flat JSON array of numbers.")
            norm = np.linalg.norm(amps)
            if norm == 0:
                return self._fail("Amplitude vector has zero norm.")
            amps = amps / norm

            size = len(amps)
            if size == 0 or (size & (size - 1)) != 0:
                return self._fail(
                    f"Amplitudes length ({size}) must be a power of 2."
                )
            n = int(np.log2(size))

            # Cross-framework convention: amps[i] ↔ canonical key
            # format(i, '0nb') with qubit 0 = leftmost. Qiskit's StatePreparation
            # is little-endian (basis index bit j = qubit j), so apply the
            # bit-reversal permutation before synthesis.
            perm = [int(format(i, f"0{n}b")[::-1], 2) for i in range(size)]
            synth_amps = amps[perm]

            from qiskit import transpile
            from qiskit.circuit.library import StatePreparation as QiskitStatePrep
            sp_gate = QiskitStatePrep(synth_amps.tolist())
            circuit  = QuantumCircuit(n)
            circuit.append(sp_gate, range(n))
            # Transpile to a primitive basis so Aer and QASM3 see only standard
            # gates — decompose() alone leaves UnitaryGate stubs that Aer cannot
            # assemble.
            circuit = transpile(circuit, basis_gates=['u', 'cx'], optimization_level=0)
            extra_attrs = {"state_prep.amplitudes": json.dumps(amps.real.tolist())}

        else:
            return self._fail(f"Unknown State Type: {state_type!r}")

        # ------------------------------------------------------------------ #
        #  Serialize                                                           #
        # ------------------------------------------------------------------ #

        if fmt == "qpy":
            from qiskit import qpy
            buf = io.BytesIO()
            qpy.dump(circuit, buf)
            content = buf.getvalue()
        elif fmt == "qasm2":
            from qiskit import qasm2, transpile
            tc = transpile(circuit, basis_gates=["h", "cx", "rz", "x"], optimization_level=0)
            content = qasm2.dumps(tc).encode("utf-8")
        else:  # qasm3
            from qiskit import qasm3, transpile
            tc = transpile(circuit, basis_gates=["h", "cx", "rz", "x"], optimization_level=0)
            content = qasm3.dumps(tc).encode("utf-8")

        diagram = str(circuit.draw("text"))
        self.logger.warn(
            "StatePreparation ({}, {} qubits):\n{}".format(state_type, n, diagram)
        )

        ops = circuit.count_ops()
        attrs = {
            "circuit.format":          fmt,
            "circuit.svg": "",  # blank stale Cirq SVG (NiFi merges attrs)
            "circuit.num_qubits":      str(n),
            "circuit.state_type":      state_type,
            "circuit.diagram":         diagram,
            "circuit.depth":           str(circuit.depth()),
            "circuit.gate_count":      str(sum(v for k, v in ops.items() if k not in ("barrier", "measure"))),
            "circuit.nonlocal_gates":  str(circuit.num_nonlocal_gates()),
            "circuit.t_count":         str(ops.get("t", 0) + ops.get("tdg", 0)),
            **extra_attrs,
        }
        if fmt == "qasm3":
            attrs["circuit.qasm3"] = content.decode("utf-8")
        elif fmt == "qasm2":
            attrs["circuit.qasm2"] = content.decode("utf-8")

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
