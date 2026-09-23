from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class CirqStatePreparation(FlowFileTransform):
    """
    Prepares a quantum state from a classical description, using Cirq.

    Cross-framework mirror of QiskitStatePreparation — same property names and
    the same measured-string convention, so the two are swappable on the canvas.

    Four modes (set via 'State Type'):

      uniform  — equal superposition H⊗n |0⟩  =  (1/√2ⁿ) Σ|x⟩
      ghz      — GHZ entangled state  (|00…0⟩ + |11…1⟩) / √2
      basis    — single computational basis state |k⟩  (X gates where bit=1)
      custom   — arbitrary statevector loaded from a JSON amplitude array

    The output FlowFile carries the circuit as cirq_json or qasm2. Connect to
    CirqSimulator / CirqStatevectorSimulator to run it.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Prepares a quantum state circuit from a classical description, using Cirq. "
            "Supports uniform superposition, GHZ, a specific basis state, or an "
            "arbitrary normalized amplitude vector (custom mode). Cross-framework "
            "mirror of QiskitStatePreparation."
        )
        tags = ["quantum", "cirq", "state-preparation", "superposition", "ghz", "circuit"]
        dependencies = ["cirq>=1.0.0", "numpy"]

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
                "'cirq_json' is Cirq's native lossless format (recommended for Cirq-to-Cirq pipelines). "
                "'qasm2' is OpenQASM 2.0 (use this to feed QiskitAerSimulator or QrispSimulator)."
            ),
            required=True,
            default_value="cirq_json",
            allowable_values=["cirq_json", "qasm2"],
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

    @staticmethod
    def _state_prep_unitary(amps):
        """Build a unitary whose first column is `amps` (an L2-normalised vector).

        Completes `amps` to an orthonormal basis via QR with a phase correction so
        the |0…0⟩ column maps exactly to the requested state. Used with a
        big-endian MatrixGate so amplitude index i ↔ basis string format(i)."""
        import numpy as np
        n = len(amps)
        m = np.eye(n, dtype=complex)
        m[:, 0] = amps
        q, r = np.linalg.qr(m)
        # QR fixes Q[:,0] only up to the phase of R[0,0]; rephase every column so
        # the first column equals `amps` exactly (and the rest stay orthonormal).
        diag = np.diagonal(r).copy()
        diag[np.abs(diag) < 1e-12] = 1.0
        q = q * (diag / np.abs(diag))
        return q

    def transform(self, context, flowFile):
        import json
        import numpy as np
        import cirq
        from cirq.contrib.svg import circuit_to_svg

        def get(prop):
            return (
                context.getProperty(prop)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )

        state_type = context.getProperty(self.state_type).getValue()
        fmt        = context.getProperty(self.output_format).getValue()
        n          = int(get(self.qubit_count))

        extra_attrs = {}

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

        qubits = cirq.LineQubit.range(n)
        circuit = cirq.Circuit()

        if state_type == "uniform":
            circuit.append(cirq.H.on_each(*qubits))

        elif state_type == "ghz":
            circuit.append(cirq.H(qubits[0]))
            for target in qubits[1:]:
                circuit.append(cirq.CNOT(qubits[0], target))

        elif state_type == "basis":
            k = int(get(self.target_basis_state))
            if k < 0 or k >= 2 ** n:
                raise ValueError(
                    f"Target Basis State {k} is out of range for {n} qubits "
                    f"(must be 0–{2**n - 1})."
                )
            # Cirq is MSB-first (qubit 0 = leftmost in the measured string), so
            # qubit i carries digit i of format(k, '0nb'). This yields the same
            # readout string as QiskitStatePreparation for the same k.
            bitstring = format(k, f'0{n}b')
            for i, bit in enumerate(bitstring):
                if bit == '1':
                    circuit.append(cirq.X(qubits[i]))
                else:
                    # Cirq circuits are defined by the qubits their gates touch;
                    # an idle qubit would vanish, so the downstream simulator (which
                    # reads sorted(circuit.all_qubits())) would emit a too-short
                    # string. Pin every zero-bit qubit into the circuit with I.
                    circuit.append(cirq.I(qubits[i]))
            extra_attrs = {"state_prep.basis_state": str(k)}

        elif state_type == "custom":
            u = self._state_prep_unitary(amps)
            # Big-endian MatrixGate: qubit 0 is the most-significant index, so the
            # first column maps |0…0⟩ to Σ amps[i] |format(i)⟩.
            circuit.append(cirq.MatrixGate(u).on(*qubits))
            extra_attrs = {"state_prep.amplitudes": json.dumps(amps.real.tolist())}

        else:
            raise ValueError(f"Unknown State Type: {state_type!r}")

        if fmt == "cirq_json":
            content = cirq.to_json(circuit).encode("utf-8")
        else:
            opt = cirq.optimize_for_target_gateset(circuit, gateset=cirq.CZTargetGateset())
            # The gateset optimisation drops identity-pinned idle qubits (e.g.
            # the zero bits of a basis state), which would shrink the declared
            # qasm register. Re-pin them so the register keeps its full width.
            dropped = set(circuit.all_qubits()) - set(opt.all_qubits())
            if dropped:
                opt.append(cirq.I.on_each(*sorted(dropped)))
            content = opt.to_qasm().encode("utf-8")

        diagram = str(circuit)
        self.logger.warn(
            "CirqStatePreparation ({}, {} qubits):\n{}".format(state_type, n, diagram)
        )

        try:
            svg = circuit_to_svg(circuit)
        except Exception as exc:
            self.logger.warn("circuit_to_svg failed: {}".format(exc))
            svg = ""

        # Metrics on the decomposed circuit so MatrixGate/CNOT counts are real.
        expanded = cirq.Circuit(cirq.decompose(circuit))
        all_ops = list(expanded.all_operations())
        t_count = sum(
            1 for op in all_ops
            if hasattr(op.gate, 'exponent') and abs(abs(op.gate.exponent) - 0.25) < 1e-9
        )

        attrs = {
            "circuit.format":          fmt,
            "circuit.qasm3": "",  # Cirq emits svg/qasm2, not qasm3 (blank stale)
            "circuit.num_qubits":      str(n),
            "circuit.state_type":      state_type,
            "circuit.diagram":         diagram,
            "circuit.svg":             svg,
            "circuit.depth":           str(len(expanded)),
            "circuit.gate_count":      str(len(all_ops)),
            "circuit.nonlocal_gates":  str(sum(1 for op in all_ops if len(op.qubits) >= 2)),
            "circuit.t_count":         str(t_count),
            **extra_attrs,
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
