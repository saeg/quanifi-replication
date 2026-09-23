import base64
import io

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


def _build_ansatz(atype, num_qubits, reps, entanglement):
    """Build a parameterised ansatz circuit using the Qiskit 2.x function builders
    (the class forms TwoLocal/EfficientSU2/RealAmplitudes are deprecated)."""
    from qiskit.circuit.library import efficient_su2, real_amplitudes, n_local

    if atype == "real_amplitudes":
        return real_amplitudes(num_qubits, reps=reps, entanglement=entanglement)
    if atype == "two_local":
        return n_local(
            num_qubits,
            rotation_blocks="ry",
            entanglement_blocks="cx",
            entanglement=entanglement,
            reps=reps,
        )
    # default: hardware-efficient SU2 (Ry/Rz rotations + entangling layer)
    return efficient_su2(num_qubits, reps=reps, entanglement=entanglement)


class QiskitAnsatz(FlowFileTransform):
    """
    Builds a parameterised ansatz (trial-state circuit) for variational
    algorithms and attaches it to the FlowFile.

    This is the *state-preparation* stage of
    QiskitHamiltonian -> QiskitAnsatz -> QiskitVQE -> QuanifiReport. The ansatz
    keeps its free parameters, so it is carried as base64-encoded QPY in the
    ``ansatz.qpy_b64`` attribute (QASM cannot round-trip a ParameterVector as
    cleanly). QiskitVQE reads that attribute and binds the parameters during
    optimisation.

    Two modes, chosen from the incoming FlowFile:

      Chain mode — a Hamiltonian is present (``hamiltonian.format`` set): the
        Hamiltonian content passes through untouched and the ansatz rides along
        only in ``ansatz.*`` attributes, so a single FlowFile carries both the
        operator (content) and the ansatz (attribute) into QiskitVQE. The qubit
        count is taken from ``hamiltonian.num_qubits``.

      Standalone mode — no Hamiltonian: behaves like an ordinary circuit builder,
        writing the ansatz as the FlowFile content with the usual ``circuit.*``
        contract (so it can feed QuanifiUnitary / QuanifiReport directly). The
        qubit count comes from the ``Num Qubits`` property.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a parameterised variational ansatz (efficient_su2, real_amplitudes, "
            "or two_local) and attaches it to the FlowFile as base64 QPY in the "
            "ansatz.qpy_b64 attribute. In chain mode the incoming Hamiltonian content "
            "passes through unchanged; in standalone mode the ansatz is written as the "
            "circuit content with the circuit.* contract."
        )
        tags = ["quantum", "qiskit", "vqe", "ansatz", "circuit", "variational"]
        dependencies = ["qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.ansatz_type = PropertyDescriptor(
            name="Ansatz Type",
            description=(
                "efficient_su2 = hardware-efficient Ry/Rz layers + entanglers (default). "
                "real_amplitudes = Ry layers only, real-valued states. "
                "two_local = Ry rotations with CX entanglers."
            ),
            required=True,
            default_value="efficient_su2",
            allowable_values=["efficient_su2", "real_amplitudes", "two_local"],
        )
        self.reps = PropertyDescriptor(
            name="Reps",
            description="Number of ansatz layers (repetitions of rotation + entanglement blocks). More reps = more parameters and expressivity.",
            required=True,
            default_value="2",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.entanglement = PropertyDescriptor(
            name="Entanglement",
            description="Entanglement pattern between qubits in each entangling layer.",
            required=True,
            default_value="full",
            allowable_values=["full", "linear", "reverse_linear", "circular", "pairwise"],
        )
        self.num_qubits = PropertyDescriptor(
            name="Num Qubits",
            description=(
                "Qubit count used in standalone mode. Ignored in chain mode, where the "
                "count is taken from the incoming hamiltonian.num_qubits attribute so the "
                "ansatz always matches the operator."
            ),
            required=True,
            default_value="2",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.ansatz_type, self.reps, self.entanglement, self.num_qubits]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qiskit import qpy, qasm3, transpile

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        atype = get(self.ansatz_type)
        try:
            reps = int(get(self.reps))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QiskitAnsatz: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"ansatz.error": msg},
            )
        entanglement = get(self.entanglement)

        # Mode detection: a Hamiltonian travelling on the FlowFile pins the qubit count.
        ham_format = flowFile.getAttribute("hamiltonian.format")
        ham_qubits = flowFile.getAttribute("hamiltonian.num_qubits")
        chain_mode = ham_format is not None and ham_qubits is not None

        if chain_mode:
            n = int(ham_qubits)
        else:
            try:
                n = int(get(self.num_qubits))
            except (TypeError, ValueError) as exc:
                msg = "bad numeric property value: {}".format(exc)
                self.logger.error("QiskitAnsatz: " + msg)
                return FlowFileTransformResult(
                    relationship="failure", contents=b"",
                    attributes={"ansatz.error": msg},
                )

        try:
            ansatz = _build_ansatz(atype, n, reps, entanglement)
        except Exception as exc:
            self.logger.error("QiskitAnsatz failed to build {} on {} qubits: {}".format(atype, n, exc))
            return FlowFileTransformResult(
                relationship="failure",
                contents=bytes(flowFile.getContentsAsBytes() or b""),
                attributes={"ansatz.error": str(exc)},
            )

        # Carry the parameterised ansatz losslessly as base64 QPY.
        buf = io.BytesIO()
        qpy.dump(ansatz, buf)
        qpy_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        diagram = str(ansatz.draw("text"))
        self.logger.warn(
            "QiskitAnsatz ({}, {} qubits, {} reps, {} params):\n{}".format(
                atype, n, reps, ansatz.num_parameters, diagram
            )
        )

        attrs = {
            "ansatz.format": "qpy_b64",
            "ansatz.qpy_b64": qpy_b64,
            "ansatz.type": atype,
            "ansatz.reps": str(reps),
            "ansatz.entanglement": entanglement,
            "ansatz.num_qubits": str(n),
            "ansatz.num_parameters": str(ansatz.num_parameters),
            "ansatz.framework": "qiskit",
            "ansatz.diagram": diagram,
        }

        if chain_mode:
            # Pass the Hamiltonian content through unchanged and re-emit its
            # attributes so the operator survives to QiskitVQE.
            content = bytes(flowFile.getContentsAsBytes() or b"")
            for key in ("hamiltonian.format", "hamiltonian.num_qubits",
                        "hamiltonian.num_terms", "hamiltonian.framework",
                        "hamiltonian.expression"):
                val = flowFile.getAttribute(key)
                if val is not None:
                    attrs[key] = val
        else:
            # Standalone: emit the ansatz as the circuit payload (circuit.* contract).
            tc = transpile(ansatz, basis_gates=["rz", "ry", "rx", "cx", "h", "x"], optimization_level=0)
            content = qasm3.dumps(tc).encode("utf-8")
            ops = ansatz.count_ops()
            attrs.update({
                "circuit.format": "qasm3",
                "circuit.qasm3": content.decode("utf-8"),
                "circuit.num_qubits": str(n),
                "circuit.framework": "qiskit",
                "circuit.algorithm": "ansatz",
                "circuit.num_parameters": str(ansatz.num_parameters),
                "circuit.diagram": diagram,
                "circuit.depth": str(ansatz.depth()),
                "circuit.gate_count": str(sum(v for k, v in ops.items() if k not in ("barrier", "measure"))),
                "circuit.nonlocal_gates": str(ansatz.num_nonlocal_gates()),
                "circuit.t_count": str(ops.get("t", 0) + ops.get("tdg", 0)),
            })

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
