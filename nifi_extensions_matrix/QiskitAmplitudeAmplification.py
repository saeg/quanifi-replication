import io

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitAmplitudeAmplification(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Generalised Grover amplitude amplification using the qiskit-algorithms "
            "AmplificationProblem / Grover.construct_circuit() framework (Brassard et al., 2000). "
            "Standalone mode: builds a phase oracle for the Marked State bitstring and wraps it in "
            "a uniform-superposition (H⊗n) Grover circuit. "
            "Compose mode: the incoming FlowFile is treated as the oracle circuit; "
            "amplitude amplification is built around it. "
            "No measurement is added — connect to QiskitAerSimulator to run. "
            "Q = A·S₀·A†·Sᶠ is applied Num Iterations times."
        )
        tags = ["quantum", "qiskit", "amplitude-amplification", "grover", "qaa", "circuit"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-aer>=0.13.0", "qiskit-algorithms>=0.3.0",
                        "qiskit-qasm3-import"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.marked_state = PropertyDescriptor(
            name="Marked State",
            description=(
                "Target bitstring for the standalone phase oracle (e.g. '101' marks |101⟩). "
                "Ignored in compose mode. Each character must be '0' or '1'; "
                "the length determines the qubit count. Bit order is left-to-right "
                "(qubit 0 = leftmost character), matching the other oracle builders."
            ),
            required=True,
            default_value="11",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.num_iterations = PropertyDescriptor(
            name="Num Iterations",
            description=(
                "Number of Grover operator applications Q^k. "
                "For one marked state in 2^n total: optimal k = round(π/(4·arcsin(1/√(2^n)))). "
                "Set to 0 to output only the state-preparation layer (no amplification)."
            ),
            required=True,
            default_value="1",
            validators=[StandardValidators.NON_NEGATIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.insert_barriers = PropertyDescriptor(
            name="Insert Barriers",
            description="Add barriers between state-preparation, oracle, and diffuser layers.",
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description="Serialisation format for the output circuit.",
            required=True,
            default_value="qasm3",
            allowable_values=["qasm3", "qpy", "qasm2"],
        )
        self.descriptors = [
            self.marked_state, self.num_iterations, self.insert_barriers, self.output_format,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qiskit import QuantumCircuit
        from qiskit.circuit.library import grover_operator as qiskit_grover_op
        from qiskit.compiler import transpile
        from qiskit_algorithms.amplitude_amplifiers import AmplificationProblem
        from qiskit_algorithms.amplitude_amplifiers.grover import Grover

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        marked_state    = get(self.marked_state).strip()
        num_iterations  = int(get(self.num_iterations))
        insert_barriers = get(self.insert_barriers).lower() == "true"
        fmt             = get(self.output_format)

        # ── Mode detection ────────────────────────────────────────────────────
        incoming_fmt = flowFile.getAttribute("circuit.format")
        compose_mode = incoming_fmt is not None

        if compose_mode:
            raw = bytes(flowFile.getContentsAsBytes())
            if incoming_fmt == "qpy":
                from qiskit import qpy
                oracle = qpy.load(io.BytesIO(raw))[0]
            elif incoming_fmt == "qasm2":
                from qiskit import qasm2
                oracle = qasm2.loads(raw.decode("utf-8"),
                                     custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
            else:
                from qiskit import qasm3
                oracle = qasm3.loads(raw.decode("utf-8"))
            n = oracle.num_qubits
            mode_label = "compose"
        else:
            # Standalone: build a phase oracle for the marked bitstring.
            # Pattern: flip |0⟩ bits → H·MCX·H (= multi-controlled Z) → undo flips.
            n = len(marked_state)
            oracle = QuantumCircuit(n, name="phase_oracle")

            for i, bit in enumerate(marked_state):
                if bit == '0':
                    oracle.x(i)

            if insert_barriers:
                oracle.barrier()

            oracle.h(n - 1)
            if n > 1:
                oracle.mcx(list(range(n - 1)), n - 1)
            else:
                oracle.x(0)  # single qubit: H·X·H = Z
            oracle.h(n - 1)

            if insert_barriers:
                oracle.barrier()

            for i, bit in enumerate(marked_state):
                if bit == '0':
                    oracle.x(i)

            mode_label = "standalone"

        # ── AmplificationProblem + Grover operator ───────────────────────────
        # state_preparation defaults to H⊗n inside AmplificationProblem when None.
        problem = AmplificationProblem(
            oracle=oracle,
            is_good_state=[marked_state] if not compose_mode else None,
        )
        # The Grover operator Q must be built from a barrier-free oracle:
        # qiskit_grover_op inlines the oracle, and if it contains barriers
        # then grover_op.power() produces an Instruction (non-unitary) that
        # the QASM3 exporter cannot serialise.  Strip barriers before Q-building;
        # the barriers stay in the display oracle and show up in circuit.diagram.
        oracle_clean = oracle.copy()
        oracle_clean.data = [
            inst for inst in oracle_clean.data if inst.operation.name != "barrier"
        ]
        grover_op = qiskit_grover_op(
            oracle=oracle_clean,
            state_preparation=problem.state_preparation,
            insert_barriers=False,
        )
        problem.grover_operator = grover_op

        # ── Full circuit: A + Q^k ─────────────────────────────────────────────
        grover = Grover(iterations=num_iterations)
        circuit = grover.construct_circuit(problem, power=num_iterations, measurement=False)
        circuit.name = f"QAA_{mode_label}_{n}q_{num_iterations}iter"

        # ── Metrics ───────────────────────────────────────────────────────────
        qk_decomp = transpile(
            circuit,
            basis_gates=['h', 'cx', 'p', 'cp', 'swap', 'x', 'rz', 's', 't', 'sdg', 'tdg', 'id'],
            optimization_level=0,
        )
        ops = qk_decomp.count_ops()
        depth      = qk_decomp.depth()
        gate_count = sum(ops.values())
        two_q      = {'cx', 'cz', 'cy', 'ch', 'cp', 'crz', 'crx', 'cry', 'cu', 'ccx', 'swap'}
        nonlocal_gates = sum(c for g, c in ops.items() if g in two_q)
        t_count    = ops.get('t', 0)

        diagram = str(circuit.draw('text'))
        self.logger.warn(
            "QiskitAmplitudeAmplification (n={}, iters={}, mode={}):\n{}".format(
                n, num_iterations, mode_label, diagram
            )
        )

        # ── Serialise ─────────────────────────────────────────────────────────
        if fmt == "qpy":
            from qiskit import qpy
            buf = io.BytesIO()
            qpy.dump(circuit, buf)
            content = buf.getvalue()
        elif fmt == "qasm2":
            from qiskit import qasm2
            content = qasm2.dumps(circuit).encode("utf-8")
        else:
            from qiskit import qasm3
            content = qasm3.dumps(circuit).encode("utf-8")

        attrs = {
            "circuit.format":         fmt,
            # NiFi merges attributes downstream; blank the Cirq-only SVG so a
            # cross-framework oracle (compose mode) can't leave a stale drawing.
            "circuit.svg":            "",
            "circuit.num_qubits":     str(n),
            "circuit.framework":      "qiskit",
            "circuit.algorithm":      "amplitude_amplification",
            "circuit.num_iterations": str(num_iterations),
            "circuit.mode":           mode_label,
            "circuit.diagram":        diagram,
            "circuit.depth":          str(depth),
            "circuit.gate_count":     str(gate_count),
            "circuit.nonlocal_gates": str(nonlocal_gates),
            "circuit.t_count":        str(t_count),
        }
        if not compose_mode:
            attrs["circuit.marked_state"] = marked_state
        if fmt == "qasm3":
            attrs["circuit.qasm3"] = content.decode("utf-8")

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
