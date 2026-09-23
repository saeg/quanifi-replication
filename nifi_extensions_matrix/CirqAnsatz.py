import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


def _entangle_pairs(n, pattern):
    if n < 2:
        return []
    if pattern == "linear":
        return [(i, i + 1) for i in range(n - 1)]
    if pattern == "circular":
        return [(i, (i + 1) % n) for i in range(n)]
    return [(i, j) for i in range(n) for j in range(i + 1, n)]  # full


def _build_ansatz(atype, n, reps, entanglement):
    """Hand-rolled parameterised Cirq ansatz (Cirq has no high-level VQE ansatz).

    Returns (circuit, param_names) where param_names is the ordered list of the
    sympy symbol names ('theta_0', 'theta_1', ...) used as free parameters.
    """
    import cirq
    import sympy

    qubits = cirq.LineQubit.range(n)
    rotations = ["ry", "rz"] if atype == "efficient_su2" else ["ry"]  # real_amplitudes/two_local: RY only
    ent_gate = cirq.CNOT if atype == "two_local" else cirq.CZ
    pairs = _entangle_pairs(n, entanglement)

    total = n * len(rotations) * (reps + 1)
    syms = [sympy.Symbol("theta_{}".format(i)) for i in range(total)]
    gate_of = {"ry": cirq.ry, "rz": cirq.rz}

    circuit = cirq.Circuit()
    k = 0
    for _layer in range(reps):
        for q in qubits:
            for r in rotations:
                circuit.append(gate_of[r](syms[k])(q))
                k += 1
        for a, b in pairs:
            circuit.append(ent_gate(qubits[a], qubits[b]))
    # final rotation layer
    for q in qubits:
        for r in rotations:
            circuit.append(gate_of[r](syms[k])(q))
            k += 1

    return circuit, [s.name for s in syms]


class CirqAnsatz(FlowFileTransform):
    """
    Builds a parameterised Cirq ansatz (trial-state circuit) for variational
    algorithms and attaches it to the FlowFile.

    State-preparation stage of CirqHamiltonian -> CirqAnsatz -> CirqVQE ->
    QuanifiReport. Cirq has no high-level ansatz, so it is hand-rolled with
    ``sympy.Symbol`` free parameters and carried as Cirq JSON in the
    ``ansatz.cirq_json`` attribute (Cirq JSON round-trips parameterised circuits);
    ``ansatz.param_names`` records the parameter order so CirqVQE can bind them.

    Two modes:
      Chain mode — a Hamiltonian is on the FlowFile: its content passes through
        untouched and the qubit count is taken from ``hamiltonian.num_qubits``.
      Standalone — no Hamiltonian: writes the ansatz as the circuit content with
        the ``circuit.*`` contract (parameterised), sized by ``Num Qubits``.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Builds a parameterised Cirq variational ansatz (efficient_su2, real_amplitudes, "
            "or two_local) with sympy free parameters and attaches it as Cirq JSON in the "
            "ansatz.cirq_json attribute. In chain mode the incoming Hamiltonian passes through; "
            "standalone it writes the ansatz as circuit content."
        )
        tags = ["quantum", "cirq", "vqe", "ansatz", "circuit", "variational"]
        dependencies = ["cirq-core>=1.0"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.ansatz_type = PropertyDescriptor(
            name="Ansatz Type",
            description=(
                "efficient_su2 = RY+RZ rotations + entanglers (default). "
                "real_amplitudes = RY rotations only. "
                "two_local = RY rotations with CNOT entanglers."
            ),
            required=True,
            default_value="efficient_su2",
            allowable_values=["efficient_su2", "real_amplitudes", "two_local"],
        )
        self.reps = PropertyDescriptor(
            name="Reps",
            description="Number of ansatz layers (rotation + entanglement blocks).",
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
            allowable_values=["full", "linear", "circular"],
        )
        self.num_qubits = PropertyDescriptor(
            name="Num Qubits",
            description=(
                "Qubit count in standalone mode. Ignored in chain mode, where the count comes "
                "from the incoming hamiltonian.num_qubits attribute."
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
        import cirq

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
            self.logger.error("CirqAnsatz: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"ansatz.error": msg},
            )
        entanglement = get(self.entanglement)

        ham_format = flowFile.getAttribute("hamiltonian.format")
        ham_qubits = flowFile.getAttribute("hamiltonian.num_qubits")
        chain_mode = ham_format is not None and ham_qubits is not None
        try:
            n = int(ham_qubits) if chain_mode else int(get(self.num_qubits))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("CirqAnsatz: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"ansatz.error": msg},
            )

        try:
            circuit, param_names = _build_ansatz(atype, n, reps, entanglement)
        except Exception as exc:
            self.logger.error("CirqAnsatz failed to build {} on {} qubits: {}".format(atype, n, exc))
            return FlowFileTransformResult(
                relationship="failure",
                contents=bytes(flowFile.getContentsAsBytes() or b""),
                attributes={"ansatz.error": str(exc)},
            )

        cirq_json = cirq.to_json(circuit)
        diagram = str(circuit)
        self.logger.warn(
            "CirqAnsatz ({}, {} qubits, {} reps, {} params):\n{}".format(
                atype, n, reps, len(param_names), diagram))

        attrs = {
            "ansatz.format": "cirq_json",
            "ansatz.cirq_json": cirq_json,
            "ansatz.param_names": json.dumps(param_names),
            "ansatz.type": atype,
            "ansatz.reps": str(reps),
            "ansatz.entanglement": entanglement,
            "ansatz.num_qubits": str(n),
            "ansatz.num_parameters": str(len(param_names)),
            "ansatz.framework": "cirq",
            "ansatz.diagram": diagram,
        }

        if chain_mode:
            content = bytes(flowFile.getContentsAsBytes() or b"")
            for key in ("hamiltonian.format", "hamiltonian.num_qubits",
                        "hamiltonian.num_terms", "hamiltonian.framework",
                        "hamiltonian.expression"):
                val = flowFile.getAttribute(key)
                if val is not None:
                    attrs[key] = val
        else:
            content = cirq_json.encode("utf-8")
            attrs.update({
                "circuit.format": "cirq_json",
                "circuit.num_qubits": str(n),
                "circuit.framework": "cirq",
                "circuit.algorithm": "ansatz",
                "circuit.num_parameters": str(len(param_names)),
                "circuit.diagram": diagram,
            })

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
