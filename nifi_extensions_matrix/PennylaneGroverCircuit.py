from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


def _strip_measurements(qasm):
    """Drop the trailing measure/creg lines PennyLane's to_openqasm always emits.

    Same rule as ``PennylaneFeatureEmbedding``: measurement belongs to the
    engine, not the builder. Duplicated here rather than imported, because NiFi
    loads each processor in its own module context and a sibling import makes
    the processor a ghost component on the canvas.
    """
    kept = [ln for ln in qasm.splitlines()
            if not ln.strip().startswith(("measure", "creg"))]
    return "\n".join(kept) + "\n"


def _strip_global_phase(qasm):
    """Remove OpenQASM 3 ``gphase`` statements, which OpenQASM 2.0 does not define.

    PennyLane's ``GroverOperator`` export emits one ``gphase(pi)`` per iteration.
    A global phase multiplies the whole state vector by a scalar of modulus one,
    so it changes no measurement probability and dropping it is sound *for a
    circuit that is sampled directly*. It would not be sound if the circuit were
    later placed under a control, because a control turns a global phase into a
    relative one — that is exactly the mechanism behind the Braket phase defect
    this project has already hit once.

    Returns the cleaned source and the number of statements removed.
    """
    kept, dropped = [], 0
    for ln in qasm.splitlines():
        if ln.strip().startswith("gphase"):
            dropped += 1
            continue
        kept.append(ln)
    return "\n".join(kept) + "\n", dropped


class PennylaneGroverCircuit(FlowFileTransform):
    """Build-only PennyLane Grover builder.

    PennyLane is the QML lane and its execution processors are deliberately not
    interchangeable with the counts engines, but its *circuit builders* emit
    OpenQASM 2.0 and are. This processor gives the builder axis a fourth
    independent implementation, built from ``qml.FlipSign`` and
    ``qml.GroverOperator`` rather than from a hand-written oracle and diffuser.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a PennyLane Grover search circuit for a given target bitstring "
            "and outputs the unmeasured circuit as OpenQASM 2.0, so it can be "
            "executed by any Quanifi counts engine. PennyLane's export emits an "
            "OpenQASM 3 'gphase' statement that OpenQASM 2.0 does not define; it is "
            "removed, and circuit.global_phase_dropped records that it was."
        )
        tags = ["quantum", "pennylane", "grover", "circuit"]
        # qiskit is needed for the foreign-parser self-check and the circuit
        # metrics below, not for building the circuit. NiFi installs ONLY what
        # is declared here into the processor's isolated venv, so omitting it
        # makes every FlowFile fail with "not parseable by a foreign parser:
        # No module named 'qiskit'" -- while the processor still loads and
        # validates. Changing this list requires deleting the processor's
        # cached venv under <nifi>/work/python/extensions/ before restart.
        dependencies = ["pennylane>=0.40", "qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.marked_state = PropertyDescriptor(
            name="Marked State",
            description=(
                "Target bitstring Grover will search for, e.g. '110'. "
                "Length sets the number of qubits. Bit order is left-to-right "
                "(qubit 0 = leftmost character)."
            ),
            required=True,
            default_value="11",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.num_iterations = PropertyDescriptor(
            name="Num Iterations",
            description=(
                "Number of Grover operator applications. "
                "Optimal is roughly floor(pi/4 * sqrt(2^n)) for one marked state."
            ),
            required=True,
            default_value="1",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.output_format = PropertyDescriptor(
            name="Output Format",
            description=(
                "Format used to serialize the circuit into the FlowFile content. "
                "Only 'qasm2' is supported: it is the format every counts engine in "
                "the library accepts, and it is what makes this builder "
                "interchangeable with the Qiskit, Cirq and Qrisp builders."
            ),
            required=True,
            default_value="qasm2",
            allowable_values=["qasm2"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.marked_state,
            self.num_iterations,
            self.output_format,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        import pennylane as qml

        def get(prop):
            return (
                context.getProperty(prop)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )

        target = get(self.marked_state)
        try:
            num_iterations = int(get(self.num_iterations))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("PennylaneGroverCircuit: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"grover.error": msg},
            )
        if set(target) - {"0", "1"}:
            msg = "Marked State must be a bitstring, got {!r}".format(target)
            self.logger.error("PennylaneGroverCircuit: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"grover.error": msg},
            )
        fmt = get(self.output_format)
        n = len(target)
        wires = list(range(n))
        bits = [int(b) for b in target]

        try:
            dev = qml.device("default.qubit", wires=n)

            @qml.qnode(dev)
            def circuit():
                for w in wires:
                    qml.Hadamard(w)
                for _ in range(num_iterations):
                    qml.FlipSign(bits, wires=wires)
                    qml.GroverOperator(wires=wires)
                return qml.expval(qml.PauliZ(0))

            raw = qml.to_openqasm(circuit, measure_all=False)()
            diagram = qml.draw(circuit)()
        except Exception as exc:
            msg = "PennyLane circuit construction failed: {}".format(exc)
            self.logger.error("PennylaneGroverCircuit: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"grover.error": msg},
            )

        qasm, dropped = _strip_global_phase(_strip_measurements(raw))

        # The export is only useful if a foreign parser accepts it, so prove that
        # here rather than letting a downstream engine fail on it.
        try:
            from qiskit import qasm2 as _qasm2
            parsed = _qasm2.loads(
                qasm, custom_instructions=_qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
        except Exception as exc:
            msg = ("emitted OpenQASM 2.0 is not parseable by a foreign parser: "
                   "{}".format(exc))
            self.logger.error("PennylaneGroverCircuit: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"grover.error": msg,
                            "grover.error_type": "unrepresentable_primitive"},
            )

        content = qasm.encode("utf-8")
        ops = parsed.count_ops()
        attrs = {
            "circuit.format":               fmt,
            "circuit.svg":                  "",   # blank stale Cirq SVG
            "circuit.qasm3":                "",   # never emitted here
            "circuit.qasm2":                qasm,
            "circuit.num_qubits":           str(n),
            "circuit.marked_state":         target,
            "circuit.num_iterations":       str(num_iterations),
            "circuit.bit_order":            "canonical",
            "circuit.global_phase_dropped": "true" if dropped else "false",
            "circuit.diagram":              diagram,
            "circuit.depth":                str(parsed.depth()),
            "circuit.gate_count":           str(sum(
                v for k, v in ops.items() if k not in ("barrier", "measure"))),
            "circuit.nonlocal_gates":       str(parsed.num_nonlocal_gates()),
            "circuit.t_count":              str(ops.get("t", 0) + ops.get("tdg", 0)),
            "builder.component":            "PennylaneGrover",
            "builder.framework":            "pennylane",
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
