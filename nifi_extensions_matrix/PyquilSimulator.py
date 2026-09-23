import json
import os
import sys
import time
from collections import Counter

# NiFi runs each processor in its own module context where the extensions
# directory is not on sys.path, so the sibling-module import (quil_qasm)
# fails unless we add this file's directory explicitly. (Tests pass without
# it only because conftest puts the directory on sys.path.)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class PyquilSimulator(FlowFileTransform):
    """Run OpenQASM circuits on Rigetti's pyquil PyQVM -- a seventh
    independently-implemented counts engine for cross-framework differential
    testing, alongside QiskitAerSimulator, CirqSimulator, QrispSimulator,
    PennylaneSimulator, BraketSimulator, and QSharpSimulator.

    PyQVM is pure Python and needs no external quilc/qvm server: it runs
    fully offline, matching every other engine in this repo. Deliberately
    NOT used: `pyquil.get_qc(...)` and `compiler.transpile_qasm_2(...)`, both
    of which require a running Forest server binary.

    qasm2/qasm3 input is re-based onto Quil-native gates via a Qiskit
    parse-and-re-emit step (translation only, see quil_qasm.py) -- the
    simulation itself is pure pyquil.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Reads a quantum circuit from the FlowFile content (OpenQASM 2 "
            "or OpenQASM 3, determined by the 'circuit.format' attribute; "
            "both are re-based onto Quil-native gates via Qiskit's parser), "
            "samples it on Rigetti's pyquil PyQVM (no external quilc/qvm "
            "server required), and writes shot counts as JSON. Bit order is "
            "q0-left. Optionally applies pyquil's built-in Kraus noise "
            "channels via PyQVM's ReferenceDensitySimulator."
        )
        tags = ["quantum", "pyquil", "rigetti", "simulation", "measurement", "noise"]
        dependencies = ["pyquil>=4.18", "qiskit>=2.0.0,<2.5", "qiskit-qasm3-import>=0.6"]

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
        self.random_seed = PropertyDescriptor(
            name="Random Seed",
            description="Seed for reproducible PyQVM sampling. Empty = nondeterministic.",
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.noise_model = PropertyDescriptor(
            name="Noise Model",
            description=(
                "pyquil post-gate Kraus noise channel applied by PyQVM's "
                "ReferenceDensitySimulator after every gate. 'none' runs the "
                "ideal NumpyWavefunctionSimulator instead."
            ),
            required=True,
            default_value="none",
            allowable_values=[
                "none", "relaxation", "dephasing", "depolarizing",
                "phase_flip", "bit_flip", "bitphase_flip",
            ],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.error_probability = PropertyDescriptor(
            name="Error Probability",
            description="Probability for the selected Noise Model channel.",
            required=False,
            default_value="0.01",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.shots,
            self.random_seed,
            self.noise_model,
            self.error_probability,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def _prop(self, context, descriptor, flowFile):
        return (
            context.getProperty(descriptor)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

    def _fail(self, message):
        self.logger.error("PyquilSimulator: " + message)
        return FlowFileTransformResult(
            relationship="failure",
            contents=b"",
            attributes={"sim.error": message},
        )

    def _build_noise(self, context, flowFile, kind):
        if kind == "none":
            return None, {"sim.noise_model": "none"}

        p = float(self._prop(context, self.error_probability, flowFile) or 0.0)
        if not 0.0 <= p <= 1.0:
            raise ValueError("Error Probability must be between 0 and 1")

        params = {"probability": p}
        noise = {kind: p}
        return noise, {
            "sim.noise_model": kind,
            "sim.noise_params": json.dumps(params, sort_keys=True),
        }

    def transform(self, context, flowFile):
        started = time.perf_counter()
        try:
            from quil_qasm import to_quil_program

            shots = int(self._prop(context, self.shots, flowFile))
            seed_raw = (self._prop(context, self.random_seed, flowFile) or "").strip()
            seed = int(seed_raw) if seed_raw else None
            noise_kind = (self._prop(context, self.noise_model, flowFile) or "none").strip()

            fmt = flowFile.getAttribute("circuit.format")
            if not fmt:
                raise ValueError("missing circuit.format (expected qasm2 or qasm3)")
            raw = bytes(flowFile.getContentsAsBytes()).decode("utf-8")

            program = to_quil_program(fmt, raw)
            num_qubits = program.num_qubits

            noise, noise_attrs = self._build_noise(context, flowFile, noise_kind)

            from pyquil.gates import MEASURE
            from pyquil.quilbase import Declare

            run_program = program.copy()
            run_program += Declare("ro", "BIT", num_qubits)
            for q in range(num_qubits):
                run_program += MEASURE(q, ("ro", q))

            from pyquil.pyqvm import PyQVM

            qvm = PyQVM(
                n_qubits=num_qubits,
                seed=seed,
                post_gate_noise_probabilities=noise,
            )
            qvm.execute(run_program.wrap_in_numshots_loop(shots))
            readout = qvm.read_memory(region_name="ro")

            # PyQVM's `ro` columns are already in qubit-index order (column i
            # = qubit i), which is exactly the q0-left convention this repo
            # uses -- verified empirically (an X on qubit i flips only
            # column i, for every i), so no reversal/normalisation is needed
            # here the way QiskitAerSimulator must reverse Aer's
            # little-endian keys. Declaring `ro` at the full num_qubits width
            # above (not derived from which qubits the program happens to
            # gate) and measuring every qubit explicitly is what stops an
            # idle qubit from silently vanishing, the same bug documented
            # for Braket at tests/test_braket.py:105.
            counts = Counter(
                "".join(str(int(bit)) for bit in row) for row in readout
            )
            if not counts:
                raise ValueError("PyQVM simulation returned no measurement results")

            sorted_counts = dict(
                sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            )
            top_state, top_count = next(iter(sorted_counts.items()))
            elapsed = time.perf_counter() - started

            return FlowFileTransformResult(
                relationship="success",
                contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
                attributes={
                    "sim.shots": str(shots),
                    "sim.top_result": top_state,
                    "sim.top_probability": "{:.4f}".format(top_count / shots),
                    "sim.framework": "pyquil",
                    "sim.component": "PyquilSimulator",
                    "sim.backend": "PyQVM",
                    "sim.bit_order": "q0_left",
                    "report.type": "simulation",
                    "perf.elapsed_seconds": "{:.4f}".format(elapsed),
                    **({"run.seed": str(seed)} if seed is not None else {}),
                    **noise_attrs,
                },
            )
        except Exception as exc:
            return self._fail(str(exc))
