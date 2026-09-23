import io

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitPhaseEstimation(FlowFileTransform):
    """
    Quantum Phase Estimation (QPE) using Qiskit's PhaseEstimation library circuit.

    Two modes:

      Standalone — no circuit.format attribute on the FlowFile: builds a
        demo QPE circuit using a builtin single-qubit unitary (T, S, or Z gate).
        The eigenstate |1⟩ is prepared automatically so the phase register
        converges to the known eigenphase.

      Compose — circuit.format is set: loads the existing circuit from the
        FlowFile content and uses it as the unitary to estimate.  The circuit
        must represent a unitary acting on some number of qubits; the phase
        register is prepended.

    QPE structure:
      1. Hadamard on all phase-register qubits.
      2. Controlled-U^(2^k) for k = 0…(m-1) where m = Phase Register Size.
      3. Inverse QFT on the phase register.
      4. Measure the phase register.

    The measured bitstring encodes the eigenphase as φ ≈ bitstring / 2^m.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a Quantum Phase Estimation (QPE) circuit using Qiskit's "
            "PhaseEstimation library block. In standalone mode a demo QPE circuit "
            "is constructed for a builtin unitary (T, S, or Z). In compose mode "
            "the unitary is loaded from the FlowFile content. The phase register "
            "size controls estimation precision."
        )
        tags = ["quantum", "qiskit", "qpe", "phase", "estimation", "eigenvalue"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-qasm3-import"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.phase_register_size = PropertyDescriptor(
            name="Phase Register Size",
            description=(
                "Number of qubits in the phase register. "
                "Precision of the phase estimate is 2π/2^m where m is this value. "
                "3 qubits → 1/8 precision; 4 → 1/16; 5 → 1/32."
            ),
            required=True,
            default_value="3",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.builtin_unitary = PropertyDescriptor(
            name="Builtin Unitary",
            description=(
                "Single-qubit unitary used in standalone mode. "
                "T gate: eigenphase = 1/8 (π/4). "
                "S gate: eigenphase = 1/4 (π/2). "
                "Z gate: eigenphase = 1/2 (π). "
                "Ignored when composing onto an existing circuit."
            ),
            required=True,
            default_value="T",
            allowable_values=["T", "S", "Z"],
        )
        self.insert_barriers = PropertyDescriptor(
            name="Insert Barriers",
            description="Add barriers between QPE stages for clearer circuit diagrams.",
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
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
            self.phase_register_size,
            self.builtin_unitary,
            self.insert_barriers,
            self.output_format,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qiskit import QuantumCircuit
        from qiskit.circuit.library import PhaseEstimation

        def get(prop):
            return (
                context.getProperty(prop)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )

        fmt              = context.getProperty(self.output_format).getValue()
        num_phase_qubits = int(get(self.phase_register_size))
        builtin          = context.getProperty(self.builtin_unitary).getValue()
        barriers         = get(self.insert_barriers).lower() == "true"

        incoming_fmt = flowFile.getAttribute("circuit.format")
        raw          = bytes(flowFile.getContentsAsBytes())

        if incoming_fmt and raw:
            # --- Compose mode: use incoming circuit as the unitary ------------
            if incoming_fmt == "qpy":
                from qiskit import qpy
                unitary_circuit = qpy.load(io.BytesIO(raw))[0]
            elif incoming_fmt == "qasm2":
                from qiskit import qasm2
                unitary_circuit = qasm2.loads(
                    raw.decode("utf-8"),
                    custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS,
                )
            else:
                from qiskit import qasm3
                unitary_circuit = qasm3.loads(raw.decode("utf-8"))
            num_unitary_qubits = unitary_circuit.num_qubits
            inherited = {
                "circuit.marked_state":   flowFile.getAttribute("circuit.marked_state") or "",
                "circuit.num_iterations": flowFile.getAttribute("circuit.num_iterations") or "",
                "circuit.state_type":     flowFile.getAttribute("circuit.state_type") or "",
            }
            mode_label = "compose"
        else:
            # --- Standalone mode: demo QPE for a builtin single-qubit gate ----
            num_unitary_qubits = 1
            unitary_circuit = QuantumCircuit(1, name=builtin)
            if builtin == "T":
                unitary_circuit.t(0)
            elif builtin == "S":
                unitary_circuit.s(0)
            else:
                unitary_circuit.z(0)
            inherited = {}
            mode_label = "standalone({})".format(builtin)

        # PhaseEstimation(num_evaluation_qubits, unitary)
        # The library wraps the unitary with controlled gates + inverse QFT.
        qpe = PhaseEstimation(
            num_evaluation_qubits=num_phase_qubits,
            unitary=unitary_circuit,
            iqft=None,   # uses default inverse QFT
        )

        # In standalone mode, prepare eigenstate |1⟩ for single-qubit gates so
        # the phase register converges to the known eigenphase instead of a
        # superposition of both eigenvalues.
        num_total = num_phase_qubits + num_unitary_qubits
        if incoming_fmt and raw:
            circuit = qpe
        else:
            circuit = QuantumCircuit(num_total)
            # |1⟩ on the last (unitary) qubit — eigenstate of T, S, Z with
            # known eigenphase
            circuit.x(num_phase_qubits)
            if barriers:
                circuit.barrier()
            circuit.compose(qpe, inplace=True)

        if barriers and not (incoming_fmt and raw):
            pass  # already inserted above

        # Circuit metrics.
        _ops        = circuit.count_ops()
        _depth      = circuit.depth()
        _gate_count = sum(v for k, v in _ops.items() if k not in ("barrier", "measure"))
        _nonlocal   = circuit.num_nonlocal_gates()
        _t_count    = _ops.get("t", 0) + _ops.get("tdg", 0)

        # No measure_all here — measurement is the simulator's responsibility
        # (QiskitAerSimulator adds it). Self-measuring here would create a
        # second classical register downstream, producing garbled doubled
        # readouts like "1100 1100" instead of a clean single bitstring.

        # --- Serialize ---------------------------------------------------------
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
        self.logger.warn("QPE circuit ({}, {} phase qubits, {} unitary qubits):\n{}".format(
            mode_label, num_phase_qubits, num_unitary_qubits, diagram
        ))

        attrs = {
            "circuit.format":              fmt,
            "circuit.svg": "",  # blank stale Cirq SVG (NiFi merges attrs)
            "circuit.num_qubits":          str(num_total),
            "circuit.phase_register_size": str(num_phase_qubits),
            "circuit.unitary_num_qubits":  str(num_unitary_qubits),
            "circuit.qpe_builtin":         builtin if not (incoming_fmt and raw) else "",
            "circuit.diagram":             diagram,
            "circuit.depth":               str(_depth),
            "circuit.gate_count":          str(_gate_count),
            "circuit.nonlocal_gates":      str(_nonlocal),
            "circuit.t_count":             str(_t_count),
            # Declarative decode hint for QuanifiReport's generic
            # _render_derived_result. The phase register is qubits 0..m-1 with
            # qubit 0 = MSB; the simulators emit canonical q0-left keys
            # (sim.bit_order = q0_left), so phase qubit j lands at string
            # position j — same as CirqPhaseEstimation. Emitting the positions
            # here keeps all framework/layout knowledge in the builder and the
            # report a pure formatter.
            "result.decode":               "phase",
            "result.bit_positions":        ",".join(
                str(j) for j in range(num_phase_qubits)
            ),
            "result.label":                "Estimated phase (φ)",
            **{k: v for k, v in inherited.items() if v},
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
