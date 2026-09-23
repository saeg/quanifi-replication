import time
import json
import contextlib
import io

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QrispGroverSearch(FlowFileTransform):
    """
    Runs Grover's search algorithm using Qrisp.

    Builds a phase oracle for the marked bitstring via tag_state(), runs
    grovers_alg() (auto-iteration by default), measures with get_measurement(),
    and writes the probability distribution as JSON.

    The output attributes mirror the Qiskit pipeline (circuit.*, sim.*) so
    QuanifiReport can display the results unchanged.  A QASM3 export is
    attempted via qv.qs.compile().to_qiskit() — if it succeeds the circuit
    diagram appears in the report; if anything goes wrong it is silently skipped.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Runs Grover's search algorithm using the Qrisp framework. "
            "Accepts a target bitstring, builds a phase oracle with tag_state(), "
            "runs grovers_alg(), and outputs the measurement probability distribution "
            "as JSON with sim.* and circuit.* attributes compatible with QuanifiReport."
        )
        tags = ["quantum", "qrisp", "grover", "search", "amplitude-amplification"]
        dependencies = ["qrisp==0.9.5", "qiskit>=2.0.0,<2.5", "qiskit-qasm3-import"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.marked_state = PropertyDescriptor(
            name="Marked State",
            description=(
                "Target bitstring Grover will search for, e.g. '110'. "
                "Length determines the number of qubits."
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
                "Set to 0 to let Qrisp calculate the optimal count automatically "
                "(floor(π/4 · √(2ⁿ)) for one marked state)."
            ),
            required=True,
            default_value="0",
            validators=[StandardValidators.INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description=(
                "Number of shots passed to get_measurement(). "
                "Controls sampling resolution. Qrisp returns probabilities; "
                "higher shots give more accurate estimates."
            ),
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.random_seed = PropertyDescriptor(
            name="Random Seed",
            description=(
                "Seed for reproducible sampling. Empty = nondeterministic. Best-effort via the global NumPy seed: Qrisp's sampler has no seed API."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.marked_state, self.num_iterations, self.shots, self.random_seed]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qrisp import QuantumVariable
        from qrisp.grover import grovers_alg, tag_state

        def get(prop):
            return (
                context.getProperty(prop)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )

        target         = get(self.marked_state)
        try:
            seed_raw = (get(self.random_seed) or "").strip()
            seed = int(seed_raw) if seed_raw else None
            num_iterations = int(get(self.num_iterations))
            shots          = int(get(self.shots))
        except (TypeError, ValueError) as exc:
            msg = "bad numeric property value: {}".format(exc)
            self.logger.error("QrispGroverSearch: " + msg)
            return FlowFileTransformResult(
                relationship="failure", contents=b"",
                attributes={"grover.error": msg},
            )
        n              = len(target)

        # --- Build circuit and run Grover -----------------------------------

        if seed is not None:
            import numpy as np
            np.random.seed(seed)  # best-effort: Qrisp has no seed API
        t0 = time.time()
        qv = QuantumVariable(n)

        def oracle(qv):
            tag_state({qv: target}, binary_values=True)

        # Suppress Qrisp's tqdm progress bar: NiFi's py4j bridge uses stdout, so
        # anything printed there corrupts the channel ("null response" crash).
        # get_measurement() returns {bitstring: probability}; QuanifiReport's bar
        # chart normalises by sum, so probabilities work without conversion.
        with contextlib.redirect_stdout(io.StringIO()):
            if num_iterations == 0:
                grovers_alg(qv, oracle)
            else:
                grovers_alg(qv, oracle, iterations=num_iterations)
            results = qv.get_measurement(shots=shots)
        elapsed = time.time() - t0
        # Canonical bit order: qubit 0 = leftmost char. Qrisp decodes a raw
        # QuantumVariable little-endian (qv[0] = rightmost char), so reverse.
        results = {k[::-1]: v for k, v in results.items()}
        sorted_results = dict(
            sorted(results.items(), key=lambda x: x[1], reverse=True)
        )
        top_state, top_prob = next(iter(sorted_results.items()))

        # --- Export circuit to QASM3 (for QuanifiReport diagram) ------

        qasm3_code    = ""
        circuit_diagram = ""
        try:
            from qiskit import qasm3 as qasm3_module
            with contextlib.redirect_stdout(io.StringIO()):
                qiskit_circuit = qv.qs.compile().to_qiskit()
            qasm3_code     = qasm3_module.dumps(qiskit_circuit)
            circuit_diagram = str(qiskit_circuit.draw("text"))
        except Exception as exc:
            self.logger.warn("QASM3 export skipped: {}".format(exc))

        # --- Build attributes -----------------------------------------------

        attrs = {
            "circuit.marked_state":  target,
            "circuit.num_qubits":    str(n),
            "circuit.num_iterations": str(num_iterations if num_iterations else "auto"),
            "sim.shots":             str(shots),
            "sim.top_result":        top_state,
            "sim.top_probability":   f"{top_prob:.4f}",
            "sim.bit_order":         "q0_left",
            "sim.framework":         "qrisp",
            "grover.framework":      "qrisp",
            "perf.elapsed_seconds":  "{:.4f}".format(elapsed),
            **({"run.seed": str(seed)} if seed is not None else {}),
        }
        if qasm3_code:
            attrs["circuit.format"] = "qasm3"
            attrs["circuit.qasm3"]  = qasm3_code
        if circuit_diagram:
            attrs["circuit.diagram"] = circuit_diagram

        self.logger.warn(
            "QrispGroverSearch: target=|{}⟩  top=|{}⟩  p={:.4f}{}".format(
                target, top_state, top_prob,
                "\n" + circuit_diagram if circuit_diagram else ""
            )
        )

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_results, indent=2).encode("utf-8"),
            attributes=attrs,
        )
