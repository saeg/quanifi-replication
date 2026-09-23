import json
import os
import sys
import contextlib
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QrispBernsteinVazirani(FlowFileTransform):
    """
    Executes the Bernstein-Vazirani algorithm using Qrisp.

    The Bernstein-Vazirani algorithm finds a hidden bitstring s in {0,1}^n
    given oracle access to f(x) = s · x (mod 2) in exactly ONE query,
    whereas a classical randomized algorithm requires n queries.

    A query register of n qubits is initialized to |0...0> and an ancilla to |1>.
    Hadamards place the query register in |+>^n and ancilla in |->.
    For each bit s[i] == '1', a CNOT is applied from qv[i] to ancilla (phase kickback).
    Hadamards are applied to the query register before measuring.
    The measurement outcome reveals the secret bitstring s with 100% probability.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Executes the Bernstein-Vazirani algorithm using Qrisp. Finds a hidden bitstring "
            "s in a single quantum query. Emits bv.secret_bitstring, bv.found_bitstring, and "
            "measurement distributions."
        )
        tags = ["quantum", "qrisp", "bernstein-vazirani", "textbook", "algorithm"]
        dependencies = ["qrisp==0.9.5", "qiskit>=2.0.0,<2.5", "qiskit-qasm3-import"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.secret_bitstring = PropertyDescriptor(
            name="Secret Bitstring",
            description="The hidden bitstring s (binary string of 0s and 1s, e.g. '101' or '1101').",
            required=True,
            default_value="101",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
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
        self.descriptors = [self.secret_bitstring, self.shots]

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

        secret = (get(self.secret_bitstring) or "").strip()
        if not secret or not all(c in "01" for c in secret):
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"bv.error": f"Secret Bitstring must be a non-empty binary string of 0s and 1s, got '{secret}'"},
            )

        try:
            shots = int(get(self.shots))
        except (TypeError, ValueError) as exc:
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"bv.error": f"Invalid Shots value: {exc}"},
            )

        n = len(secret)
        qv = QuantumVariable(n)
        anc = QuantumBool()

        # Ancilla in |->
        x(anc)
        h(anc)

        # Query register in |+>^n
        h(qv)

        # Oracle: CNOT from qv[i] to anc where secret[i] == '1'
        # In canonical q0_left bit order, secret[0] is qubit 0.
        for i, bit in enumerate(secret):
            if bit == "1":
                cx(qv[i], anc)

        # Final Hadamards
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

        is_match = (top_state == secret)

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
            "bv.secret_bitstring": secret,
            "bv.found_bitstring": top_state,
            "bv.match": "true" if is_match else "false",
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
