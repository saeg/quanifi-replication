from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QrispQFTCircuit(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Applies the Quantum Fourier Transform (or its inverse QFT†) using Qrisp's "
            "built-in QFT primitive. "
            "Outputs qasm2 compatible with QrispSimulator, QiskitAerSimulator, and CirqSimulator. "
            "No measurement is added — connect to a simulator to run the circuit."
        )
        tags = ["quantum", "qrisp", "qft", "fourier", "transform", "circuit"]
        dependencies = ["qrisp==0.9.5", "qiskit>=2.0.0,<2.5"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.qubit_count = PropertyDescriptor(
            name="Qubit Count",
            description="Number of qubits to apply the QFT to.",
            required=True,
            default_value="3",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.inverse = PropertyDescriptor(
            name="Inverse",
            description=(
                "Apply the inverse QFT (QFT†). "
                "Set to true for the readout stage of Phase Estimation."
            ),
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
        )
        self.do_swaps = PropertyDescriptor(
            name="Do Swaps",
            description=(
                "Include the bit-reversal SWAP network at the end of the QFT "
                "(standard mathematical convention)."
            ),
            required=True,
            default_value="true",
            allowable_values=["true", "false"],
        )
        self.descriptors = [self.qubit_count, self.inverse, self.do_swaps]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qrisp import QuantumVariable, QFT, x
        from qiskit import qasm2 as qiskit_qasm2
        from qiskit.converters import dag_to_circuit, circuit_to_dag
        from qiskit.compiler import transpile

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        try:
            n        = int(get(self.qubit_count))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QrispQFTCircuit: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"circuit.error": msg},
            )
        inverse  = get(self.inverse).lower() == "true"
        do_swaps = get(self.do_swaps).lower() == "true"

        # Apply X on qubit 0 before QFT so Qrisp generates the full gate_QFT/gate_QFT_dg
        # definition in the compiled output. Without a non-|0> input, Qrisp optimises
        # QFT on |0...0> to H⊗n, which is mathematically correct but lacks the CP gates
        # needed for non-zero inputs.  The X gate is stripped after compilation.
        qv = QuantumVariable(n)
        x(qv[0])
        QFT(qv, inv=inverse, exec_swap=do_swaps)

        qk = qv.qs.compile().to_qiskit()

        dag = circuit_to_dag(qk)
        for node in list(dag.topological_op_nodes()):
            if node.op.name == 'x':
                dag.remove_op_node(node)
        qk_clean = dag_to_circuit(dag)

        content = qiskit_qasm2.dumps(qk_clean).encode("utf-8")

        # Decompose the custom QFT gate to primitive gates for accurate metrics.
        qk_decomp = transpile(
            qk_clean,
            basis_gates=['h', 'cx', 'p', 'cp', 'swap', 'x', 'rz', 's', 't', 'sdg', 'tdg', 'id'],
            optimization_level=0,
        )
        ops = qk_decomp.count_ops()
        depth = qk_decomp.depth()
        gate_count = sum(ops.values())
        two_qubit_names = {'cx', 'cz', 'cy', 'ch', 'cp', 'crz', 'crx', 'cry', 'cu', 'ccx', 'swap'}
        nonlocal_gates = sum(count for gate, count in ops.items() if gate in two_qubit_names)
        t_count = ops.get('t', 0)

        diagram = str(qk_clean.draw('text'))
        self.logger.warn("QrispQFTCircuit (n={}, inverse={}, do_swaps={}):\n{}".format(
            n, inverse, do_swaps, diagram
        ))

        attrs = {
            "circuit.format":         "qasm2",
            "circuit.svg": "",  # blank stale Cirq SVG (NiFi merges attrs)
            "circuit.num_qubits":     str(n),
            "circuit.qft_inverse":    str(inverse).lower(),
            "circuit.qft_do_swaps":   str(do_swaps).lower(),
            "circuit.framework":      "qrisp",
            "circuit.diagram":        diagram,
            "circuit.depth":          str(depth),
            "circuit.gate_count":     str(gate_count),
            "circuit.nonlocal_gates": str(nonlocal_gates),
            "circuit.t_count":        str(t_count),
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=content,
            attributes=attrs,
        )
