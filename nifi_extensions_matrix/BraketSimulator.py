import time
import contextlib
import io
import json
import os
import sys

# NiFi runs each processor in its own module context where the extensions
# directory is not on sys.path, so the sibling-module import (braket_qasm)
# fails unless we add this file's directory explicitly. (Tests pass without
# it only because conftest puts the directory on sys.path.)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class BraketSimulator(FlowFileTransform):
    """Amazon Braket local simulator. A fifth independently-implemented
    counts engine for cross-framework differential testing, alongside
    QiskitAerSimulator, CirqSimulator, QrispSimulator and PennylaneSimulator.
    Both qasm3 and qasm2 inputs are re-based onto Braket-native gates via a
    Qiskit parse-and-re-emit step (translation only — the simulation itself
    is pure Braket, flagged with sim.translation for traceability; see
    braket_qasm.py for why inlined gate definitions are unsound)."""

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Reads a quantum circuit from the FlowFile content (OpenQASM 3 or "
            "OpenQASM 2.0, determined by the 'circuit.format' attribute; both "
            "are re-based onto Braket-native gates via Qiskit's parser), "
            "samples it on the "
            "Amazon Braket LocalSimulator (no AWS account or network access "
            "required), and writes shot counts as JSON. Bit order is MSB-first "
            "(qubit 0 leftmost, the Cirq convention). Note: the Braket local "
            "simulator here runs ideal (noiseless) — for noisy simulation use "
            "QiskitAerSimulator or CirqSimulator."
        )
        tags = ["quantum", "braket", "aws", "simulation", "measurement"]
        dependencies = ["amazon-braket-sdk", "qiskit>=2.0.0,<2.5", "qiskit-qasm3-import"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.shots = PropertyDescriptor(
            name="Shots",
            description="Number of times to sample the circuit.",
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.shots]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        # Translation only (see braket_qasm.py for why the circuit is
        # re-based onto Braket-native gates): the simulation below is pure
        # Braket; sim.translation flags the shared Qiskit front-end so
        # differential runs can account for it.
        from braket_qasm import ensure_full_register_measure, to_braket_qasm3

        shots = int(
            context.getProperty(self.shots)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        fmt = flowFile.getAttribute("circuit.format")
        raw = bytes(flowFile.getContentsAsBytes())

        try:
            qasm3_src, note = to_braket_qasm3(fmt, raw.decode("utf-8"))
        except Exception as exc:
            self.logger.error("BraketSimulator: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"sim.error": str(exc)},
            )
        extra = {"sim.translation": note}

        from braket.devices import LocalSimulator
        from braket.ir.openqasm import Program

        try:
            program = Program(source=ensure_full_register_measure(qasm3_src))

            # Suppress the "may not be supported on QPUs" advisory print so it
            # doesn't pollute NiFi logs.
            t0 = time.time()
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                result = LocalSimulator().run(program, shots=shots).result()
            elapsed = time.time() - t0
        except Exception as exc:
            self.logger.error("BraketSimulator: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"sim.error": (
                    "Braket simulation failed: {}".format(exc))},
            )
        counts = result.measurement_counts

        sorted_counts = dict(
            sorted(((k, int(v)) for k, v in counts.items()),
                   key=lambda x: x[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes={
                "sim.shots":           str(shots),
                "sim.top_result":      top_state,
                "sim.top_probability": "{:.4f}".format(top_count / shots),
                "sim.framework":       "braket",
                "sim.component":       "BraketSimulator",
                "sim.bit_order":       "q0_left",
                "report.type":         "simulation",
                "perf.elapsed_seconds": "{:.4f}".format(elapsed),
                **extra,
            },
        )
