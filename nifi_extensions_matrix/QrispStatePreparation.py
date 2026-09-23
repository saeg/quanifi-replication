import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QrispStatePreparation(FlowFileTransform):
    """
    Prepares a quantum state from a classical description, using Qrisp.

    Cross-framework mirror of QiskitStatePreparation / CirqStatePreparation —
    same property names and the same canonical bit order (qubit 0 = leftmost
    char / MSB, so basis state k measures as format(k, '0nb') and custom
    amplitude index i maps to basis string format(i, '0nb')), so the three are
    swappable on the canvas and their distributions compare directly.

    Four modes (set via 'State Type'):

      uniform  — equal superposition H⊗n |0⟩  =  (1/√2ⁿ) Σ|x⟩
      ghz      — GHZ entangled state  (|00…0⟩ + |11…1⟩) / √2
      basis    — single computational basis state |k⟩  (X gates where bit=1)
      custom   — arbitrary statevector loaded from a JSON amplitude array

    Output is OpenQASM 2.0 (Qrisp's neutral wire format, matching QrispQFTCircuit).

    NOTE: validate with QiskitAerSimulator or CirqSimulator. Qrisp's own native
    QrispSimulatorBackend (QrispSimulator) re-orders and drops idle qubits when re-parsing
    sparse qasm2 circuits, so it mis-reads basis/custom states — the emitted circuit
    itself is correct (verified against Aer/Cirq). Custom-state synthesis uses
    Qrisp's prepare() with method='qiskit', the only deterministic backend for it
    (method='auto' picks varying syntheses with unstable qubit order).
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Prepares a quantum state circuit from a classical description, using Qrisp. "
            "Supports uniform superposition, GHZ, a specific basis state, or an arbitrary "
            "normalized amplitude vector (custom mode). Emits OpenQASM 2.0 compatible with "
            "QiskitAerSimulator and CirqSimulator. Cross-framework mirror of "
            "QiskitStatePreparation."
        )
        tags = ["quantum", "qrisp", "state-preparation", "superposition", "ghz", "circuit"]
        dependencies = ["qrisp==0.9.5", "qiskit>=2.0.0,<2.5", "numpy"]

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
        self.descriptors = [
            self.state_type,
            self.qubit_count,
            self.target_basis_state,
            self.amplitudes,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        import numpy as np
        from qrisp import QuantumVariable, h, x, cx, prepare
        from qiskit import qasm2 as qiskit_qasm2
        from qiskit.compiler import transpile

        def get(prop):
            return (
                context.getProperty(prop)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )

        state_type = context.getProperty(self.state_type).getValue()
        n          = int(get(self.qubit_count))

        extra_attrs = {}

        # ------------------------------------------------------------------ #
        #  Build circuit (Qrisp QuantumVariable)                              #
        # ------------------------------------------------------------------ #

        if state_type == "custom":
            raw_amps = context.getProperty(self.amplitudes).getValue() or ""
            if not raw_amps:
                raise ValueError("Amplitudes must be provided for custom mode.")
            amps = np.array(json.loads(raw_amps), dtype=complex)
            norm = np.linalg.norm(amps)
            if norm == 0:
                raise ValueError("Amplitude vector has zero norm.")
            amps = amps / norm
            size = len(amps)
            if size == 0 or (size & (size - 1)) != 0:
                raise ValueError(f"Amplitudes length ({size}) must be a power of 2.")
            n = int(np.log2(size))

        qv = QuantumVariable(n)

        if state_type == "uniform":
            h(qv)

        elif state_type == "ghz":
            h(qv[0])
            for target in range(1, n):
                cx(qv[0], qv[target])

        elif state_type == "basis":
            k = int(get(self.target_basis_state))
            if k < 0 or k >= 2 ** n:
                raise ValueError(
                    f"Target Basis State {k} is out of range for {n} qubits "
                    f"(must be 0–{2**n - 1})."
                )
            # Canonical bit order: qubit i carries digit i of format(k, '0nb')
            # (qubit 0 = MSB), so |k⟩ measures as format(k, '0nb') under the
            # simulators' q0-left keys — matching Qiskit/CirqStatePreparation.
            for i, bit in enumerate(format(k, f'0{n}b')):
                if bit == '1':
                    x(qv[i])
            extra_attrs = {"state_prep.basis_state": str(k)}

        elif state_type == "custom":
            # method='qiskit' is the only deterministic synthesis (auto varies its
            # choice run-to-run, giving unstable qubit order); reversed=True yields
            # amps[i] ↔ canonical key format(i, '0nb') (qubit 0 = leftmost), the
            # cross-framework convention.
            prepare(qv, amps, reversed=True, method="qiskit")
            extra_attrs = {"state_prep.amplitudes": json.dumps(amps.real.tolist())}

        else:
            raise ValueError(f"Unknown State Type: {state_type!r}")

        qk = qv.qs.compile().to_qiskit()
        content = qiskit_qasm2.dumps(qk).encode("utf-8")

        # Decompose to primitive gates for accurate metrics (matches QrispQFTCircuit).
        qk_decomp = transpile(
            qk,
            basis_gates=['h', 'cx', 'p', 'cp', 'swap', 'x', 'rz', 'ry', 'rx',
                         's', 't', 'sdg', 'tdg', 'u', 'id'],
            optimization_level=0,
        )
        ops = qk_decomp.count_ops()
        depth = qk_decomp.depth()
        gate_count = sum(ops.values())
        two_qubit_names = {'cx', 'cz', 'cy', 'ch', 'cp', 'crz', 'crx', 'cry', 'cu', 'ccx', 'swap'}
        nonlocal_gates = sum(count for gate, count in ops.items() if gate in two_qubit_names)
        t_count = ops.get('t', 0)

        diagram = str(qk.draw('text'))
        self.logger.warn(
            "QrispStatePreparation ({}, {} qubits):\n{}".format(state_type, n, diagram)
        )

        attrs = {
            "circuit.format":         "qasm2",
            "circuit.svg": "",  # blank stale Cirq SVG (NiFi merges attrs)
            "circuit.num_qubits":     str(n),
            "circuit.state_type":     state_type,
            "circuit.framework":      "qrisp",
            "circuit.diagram":        diagram,
            "circuit.depth":          str(depth),
            "circuit.gate_count":     str(gate_count),
            "circuit.nonlocal_gates": str(nonlocal_gates),
            "circuit.t_count":        str(t_count),
            **extra_attrs,
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
