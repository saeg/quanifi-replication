import time
import json

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QiskitGroverSearch(FlowFileTransform):

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Runs Grover's search algorithm for a given target bitstring. "
            "Builds a phase oracle for the marked state, wraps it with the "
            "grover_operator from qiskit.circuit.library, applies it N times, "
            "simulates with AerSimulator, and writes the measurement counts as JSON."
        )
        tags = ["quantum", "qiskit", "grover", "search", "amplitude-amplification"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-aer>=0.13.0"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.marked_state = PropertyDescriptor(
            name="Marked State",
            description=(
                "The target bitstring Grover's algorithm will search for, e.g. '110'. "
                "Length determines the number of qubits. Bit order is MSB-first: '110' "
                "marks the state where qubit 0=1, qubit 1=1, qubit 2=0."
            ),
            required=True,
            default_value="11",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.num_iterations = PropertyDescriptor(
            name="Num Iterations",
            description=(
                "How many times to apply the Grover operator. "
                "Optimal is roughly floor(pi/4 * sqrt(2^n)) for a single marked state."
            ),
            required=True,
            default_value="1",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description="Number of times to run the simulation circuit.",
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.insert_barriers = PropertyDescriptor(
            name="Insert Barriers",
            description="Add barriers between the oracle, inverse state prep, zero reflection, and state prep stages.",
            required=True,
            default_value="false",
            allowable_values=["true", "false"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.random_seed = PropertyDescriptor(
            name="Random Seed",
            description=(
                "Seed for reproducible sampling. Empty = nondeterministic. Passed to Aer as seed_simulator."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.marked_state,
            self.num_iterations,
            self.shots,
            self.insert_barriers,
            self.random_seed,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qiskit import QuantumCircuit, transpile
        from qiskit.circuit.library import grover_operator
        from qiskit_aer import AerSimulator

        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        target = get(self.marked_state)
        barriers = get(self.insert_barriers).lower() == "true"
        try:
            num_iterations = int(get(self.num_iterations))
            shots = int(get(self.shots))
            seed_raw = (get(self.random_seed) or "").strip()
            seed = int(seed_raw) if seed_raw else None
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QiskitGroverSearch: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"grover.error": msg},
            )

        n = len(target)

        # Build a phase oracle that marks exactly the target bitstring.
        # A phase oracle flips the sign of |target⟩ without touching other states.
        oracle = QuantumCircuit(n)
        # Flip qubits where the target bit is '0' so the all-ones state |1...1⟩ becomes |target⟩.
        for i, bit in enumerate(target):
            if bit == '0':
                oracle.x(i)
        # Apply a multi-controlled phase flip on |1...1⟩.
        if n == 1:
            oracle.z(0)
        else:
            oracle.h(n - 1)
            oracle.mcx(list(range(n - 1)), n - 1)
            oracle.h(n - 1)
        # Undo the flips.
        for i, bit in enumerate(target):
            if bit == '0':
                oracle.x(i)

        # Build the full Grover circuit: H^n + (Grover operator)^k + measure.
        grover_op = grover_operator(oracle, insert_barriers=barriers)

        circuit = QuantumCircuit(n)
        circuit.h(range(n))              # uniform superposition
        for _ in range(num_iterations):
            circuit.compose(grover_op, inplace=True)
        circuit.measure_all()

        run_kwargs = {"shots": shots}
        if seed is not None:
            run_kwargs["seed_simulator"] = seed
        t0 = time.time()
        sim = AerSimulator()
        result = sim.run(transpile(circuit, sim), **run_kwargs).result()
        elapsed = time.time() - t0
        counts = result.get_counts()

        # Canonical bit order: qubit 0 = leftmost char. Aer keys are
        # little-endian (clbit 0 = rightmost), so reverse — this makes the top
        # result directly comparable to the Marked State property.
        normalised = {}
        for key, cnt in counts.items():
            k = key.replace(" ", "")[::-1]
            normalised[k] = normalised.get(k, 0) + cnt

        # Sort by count descending so the top result is first.
        sorted_counts = dict(sorted(normalised.items(), key=lambda x: x[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes={
                "grover.marked_state": target,
                "grover.num_qubits": str(n),
                "grover.num_iterations": str(num_iterations),
                "grover.shots": str(shots),
                "grover.top_result": top_state,
                "grover.top_probability": f"{top_count / shots:.4f}",
                "sim.bit_order": "q0_left",
                "sim.framework": "qiskit",
                "sim.component": "QiskitGroverSearch",
                "builder.component": "QiskitGroverSearch",
                "grover.framework": "qiskit",
                "perf.elapsed_seconds": "{:.4f}".format(elapsed),
                **({"run.seed": str(seed)} if seed is not None else {}),
            },
        )
