import json
import os
import sys
import contextlib
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QrispDeutschJozsa(FlowFileTransform):
    """
    Executes the Deutsch-Jozsa quantum algorithm using Qrisp.

    The Deutsch-Jozsa algorithm solves the problem of determining whether an
    oracle function f: {0,1}^n -> {0,1} is constant (f(x)=0 for all x or f(x)=1 for all x)
    or balanced (f(x)=0 for half the inputs, 1 for the other half) in a single
    quantum evaluation, whereas classical algorithms require 2^(n-1) + 1 evaluations.

    A query register of n qubits is initialized to |0...0> and an ancilla qubit to |1>.
    Hadamards are applied to all qubits putting the query register in |+>^n and the ancilla in |->.
    The oracle is evaluated using phase kickback.
    Final Hadamards are applied to the query register before measurement.
    If f is constant, the measurement outcome is |0...0> with probability 1.
    If f is balanced, the probability of measuring |0...0> is 0.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Executes the Deutsch-Jozsa algorithm using Qrisp. Determines whether a boolean "
            "oracle is constant or balanced in a single evaluation. Emits dj.verdict "
            "('constant' or 'balanced') and measurement distributions."
        )
        tags = ["quantum", "qrisp", "deutsch-jozsa", "textbook", "algorithm"]
        dependencies = ["qrisp==0.9.5", "qiskit>=2.0.0,<2.5", "qiskit-qasm3-import"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.function_type = PropertyDescriptor(
            name="Function Type",
            description="Type of oracle function to evaluate: constant_zero, constant_one, or balanced.",
            required=True,
            default_value="balanced",
            allowable_values=["constant_zero", "constant_one", "balanced"],
        )
        self.num_qubits = PropertyDescriptor(
            name="Num Qubits",
            description="Number of query qubits n (excluding the 1 ancilla qubit).",
            required=True,
            default_value="3",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.balanced_mask = PropertyDescriptor(
            name="Balanced Mask",
            description=(
                "Bitmask of length n selecting which query qubits are connected to the ancilla "
                "for the balanced oracle (e.g. '101'). If empty, connects qubit 0."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description="Number of simulation shots passed to get_measurement().",
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.function_type, self.num_qubits, self.balanced_mask, self.shots]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        from qrisp import QuantumVariable, QuantumBool, h, x, cx
        from qiskit import qasm2 as qiskit_qasm2

        def get(prop):
            return (
                context.getProperty(prop)
                .evaluateAttributeExpressions(flowFile)
                .getValue()
            )

        fn_type = get(self.function_type)
        try:
            n = int(get(self.num_qubits))
            shots = int(get(self.shots))
        except (TypeError, ValueError) as exc:
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"dj.error": f"Invalid numeric property: {exc}"},
            )

        if n < 1:
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"dj.error": "Num Qubits must be at least 1"},
            )

        mask_raw = (get(self.balanced_mask) or "").strip()
        if not mask_raw:
            mask = "1" + "0" * (n - 1)
        else:
            mask = mask_raw.zfill(n)[:n]

        # Allocate query register of n qubits and 1 ancilla
        qv = QuantumVariable(n)
        anc = QuantumBool()

        # Ancilla starts in |1> then |->
        x(anc)
        h(anc)

        # Query register in |+>^n
        h(qv)

        # Oracle evaluation
        if fn_type == "constant_zero":
            pass
        elif fn_type == "constant_one":
            x(anc)
        elif fn_type == "balanced":
            for i in range(n):
                if mask[i] == "1":
                    cx(qv[i], anc)

        # Final Hadamards on query register
        h(qv)

        # Simulation
        raw_counts = qv.get_measurement(shots=shots)
        # Canonical bit order: get_measurement on a bare QuantumVariable in Qrisp
        # already decodes qv[0] as the leftmost character (MSB-left).
        counts = {str(k): v for k, v in raw_counts.items()}
        sorted_counts = dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))

        top_state, top_count = next(iter(sorted_counts.items()))
        total_samples = sum(sorted_counts.values())
        top_prob = top_count / total_samples if total_samples > 0 else 0.0

        all_zeros = "0" * n
        is_constant = (all_zeros in counts and counts[all_zeros] == total_samples)
        verdict = "constant" if is_constant else "balanced"

        # Circuit extraction
        qasm2_str = ""
        circuit_diagram = ""
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                qk = qv.qs.compile().to_qiskit()
            qasm2_str = qiskit_qasm2.dumps(qk)
            circuit_diagram = str(qk.draw("text"))
        except Exception as exc:
            self.logger.warn(f"Circuit export skipped: {exc}")

        attrs = {
            "circuit.format": "qasm2",
            "circuit.framework": "qrisp",
            "circuit.num_qubits": str(n + 1),
            "dj.function_type": fn_type,
            "dj.verdict": verdict,
            "dj.is_constant": "true" if is_constant else "false",
            "dj.balanced_mask": mask if fn_type == "balanced" else "",
            "sim.shots": str(shots),
            "sim.top_result": top_state,
            "sim.top_probability": f"{top_prob:.4f}",
            "sim.bit_order": "q0_left",
            "sim.framework": "qrisp",
            "report.type": "simulation",
        }
        if qasm2_str:
            attrs["circuit.qasm2"] = qasm2_str
        if circuit_diagram:
            attrs["circuit.diagram"] = circuit_diagram

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes=attrs,
        )
