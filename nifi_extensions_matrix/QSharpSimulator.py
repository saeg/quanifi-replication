import json
import os
import time
from collections import Counter

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QSharpSimulator(FlowFileTransform):
    """Run OpenQASM circuits with the Microsoft QDK local simulator."""

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.1"
        description = (
            "Reads an OpenQASM 2 or 3 circuit, adds measurements, and runs it "
            "with the Microsoft QDK sparse-state or Clifford simulator. Emits "
            "canonical q0-left counts and optionally applies Pauli noise."
        )
        tags = ["quantum", "qsharp", "qdk", "simulation", "measurement", "noise"]
        dependencies = [
            "qdk>=1.30,<1.31",
            "qiskit>=2.0.0,<2.5",
            "qiskit-qasm3-import>=0.6",
        ]

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
        self.simulator_type = PropertyDescriptor(
            name="Simulator Type",
            description=(
                "QDK simulation engine. 'sparse' supports general circuits; "
                "'clifford' is efficient but accepts only Clifford circuits."
            ),
            required=True,
            default_value="sparse",
            allowable_values=["sparse", "clifford"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.noise_model = PropertyDescriptor(
            name="Noise Model",
            description=(
                "Pauli noise applied after gates and before measurements by the "
                "QDK sparse-state simulator."
            ),
            required=True,
            default_value="none",
            allowable_values=["none", "pauli", "bit_flip", "phase_flip", "depolarizing"],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.error_probability = PropertyDescriptor(
            name="Error Probability",
            description=(
                "Error probability for bit_flip, phase_flip, or depolarizing noise."
            ),
            required=False,
            default_value="0.01",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.pauli_x = PropertyDescriptor(
            name="Pauli X Probability",
            description="Pauli-X probability when Noise Model is pauli.",
            required=False,
            default_value="0.001",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.pauli_y = PropertyDescriptor(
            name="Pauli Y Probability",
            description="Pauli-Y probability when Noise Model is pauli.",
            required=False,
            default_value="0.001",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.pauli_z = PropertyDescriptor(
            name="Pauli Z Probability",
            description="Pauli-Z probability when Noise Model is pauli.",
            required=False,
            default_value="0.001",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.random_seed = PropertyDescriptor(
            name="Random Seed",
            description="Seed for reproducible QDK sampling. Empty = nondeterministic.",
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [
            self.shots,
            self.simulator_type,
            self.random_seed,
            self.noise_model,
            self.error_probability,
            self.pauli_x,
            self.pauli_y,
            self.pauli_z,
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
        self.logger.error("QSharpSimulator: " + message)
        return FlowFileTransformResult(
            relationship="failure",
            contents=b"",
            attributes={"sim.error": message},
        )

    @staticmethod
    def _probability(raw, name):
        value = float(raw)
        if not 0.0 <= value <= 1.0:
            raise ValueError("{} must be between 0 and 1".format(name))
        return value

    def _build_noise(self, context, flowFile, kind):
        from qdk import BitFlipNoise, DepolarizingNoise, PauliNoise, PhaseFlipNoise

        if kind == "none":
            return None, {"sim.noise_model": "none"}

        if kind == "pauli":
            x = self._probability(self._prop(context, self.pauli_x, flowFile), "Pauli X Probability")
            y = self._probability(self._prop(context, self.pauli_y, flowFile), "Pauli Y Probability")
            z = self._probability(self._prop(context, self.pauli_z, flowFile), "Pauli Z Probability")
            if x + y + z > 1.0:
                raise ValueError("Pauli X, Y, and Z probabilities must sum to at most 1")
            params = {"x": x, "y": y, "z": z}
            noise = PauliNoise(x, y, z)
        else:
            p = self._probability(
                self._prop(context, self.error_probability, flowFile),
                "Error Probability",
            )
            params = {"probability": p}
            noise_types = {
                "bit_flip": BitFlipNoise,
                "phase_flip": PhaseFlipNoise,
                "depolarizing": DepolarizingNoise,
            }
            if kind not in noise_types:
                raise ValueError("unsupported noise model '{}'".format(kind))
            noise = noise_types[kind](p)

        return noise, {
            "sim.noise_model": kind,
            "sim.noise_params": json.dumps(params, sort_keys=True),
        }

    @staticmethod
    def _measured_qasm3(raw, fmt, require_clifford=False):
        """Load either QASM version, replace measurements, and emit QASM 3.

        Rebuilding the classical output through Qiskit gives every upstream
        builder the same measure-all behavior as Quanifi's other simulators and
        avoids ambiguous multi-register result tuples at the QDK boundary.
        """
        if fmt == "qasm2":
            from qiskit import qasm2
            circuit = qasm2.loads(
                raw,
                custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS,
            )
        elif fmt == "qasm3":
            from qiskit import qasm3
            circuit = qasm3.loads(raw)
        else:
            raise ValueError(
                "unsupported circuit.format '{}' (expected qasm2 or qasm3)".format(fmt)
            )

        circuit.remove_final_measurements(inplace=True)
        if require_clifford:
            # QDK 1.30's native Clifford simulator can terminate the entire
            # Python process (SIGSEGV) when handed a non-Clifford circuit. That
            # bypasses transform()'s exception handler and appears in NiFi as a
            # Py4J null response. Validate in pure Python before crossing the
            # native QDK boundary so the FlowFile can be routed to failure.
            from qiskit.quantum_info import Clifford
            try:
                Clifford(circuit)
            except Exception as exc:
                raise ValueError(
                    "Simulator Type = clifford requires a Clifford-only circuit "
                    "(H, S, X/Y/Z, CX/CZ/SWAP and measurement); use sparse for "
                    "Grover, arbitrary rotations, T gates, or multi-controlled "
                    "operations"
                ) from exc

        num_qubits = circuit.num_qubits
        circuit.measure_all()

        from qiskit import qasm3
        return qasm3.dumps(circuit), num_qubits

    @staticmethod
    def _result_key(result):
        if isinstance(result, str):
            return result.replace(" ", "")
        if isinstance(result, (list, tuple)):
            return "".join(QSharpSimulator._result_key(item) for item in result)
        if result in (0, 1):
            return str(result)
        raise ValueError("QDK returned an unsupported measurement value: {!r}".format(result))

    def transform(self, context, flowFile):
        started = time.perf_counter()
        try:
            shots = int(self._prop(context, self.shots, flowFile))
            simulator_type = (self._prop(context, self.simulator_type, flowFile) or "sparse").strip()
            noise_kind = (self._prop(context, self.noise_model, flowFile) or "none").strip()
            seed_raw = (self._prop(context, self.random_seed, flowFile) or "").strip()
            seed = int(seed_raw) if seed_raw else None

            fmt = flowFile.getAttribute("circuit.format")
            if not fmt:
                raise ValueError("missing circuit.format (expected qasm2 or qasm3)")
            raw = bytes(flowFile.getContentsAsBytes()).decode("utf-8")
            source, num_qubits = self._measured_qasm3(
                raw,
                fmt,
                require_clifford=simulator_type == "clifford",
            )

            if simulator_type == "clifford" and noise_kind != "none":
                raise ValueError(
                    "Pauli noise modes are supported only by the sparse simulator; "
                    "use Noise Model = none with clifford"
                )

            noise, noise_attrs = self._build_noise(context, flowFile, noise_kind)

            # The core qdk import is intentionally lazy: NiFi installs each
            # processor's dependencies in an isolated environment on first use.
            # Quanifi simulations must not emit package telemetry by default.
            os.environ.setdefault("QDK_PYTHON_TELEMETRY", "none")
            from qdk.openqasm import run

            run_kwargs = {
                "shots": shots,
                "as_bitstring": True,
                "type": simulator_type,
            }
            if simulator_type == "clifford":
                run_kwargs["num_qubits"] = num_qubits
            if seed is not None:
                run_kwargs["seed"] = seed
            if noise is not None:
                run_kwargs["noise"] = noise

            results = run(source, **run_kwargs)
            if isinstance(results, str):
                results = [results]
            counts = Counter(self._result_key(result) for result in results)
            if not counts:
                raise ValueError("QDK simulation returned no measurement results")

            sorted_counts = dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
            top_state, top_count = next(iter(sorted_counts.items()))
            elapsed = time.perf_counter() - started

            return FlowFileTransformResult(
                relationship="success",
                contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
                attributes={
                    "sim.shots": str(shots),
                    "sim.top_result": top_state,
                    "sim.top_probability": "{:.4f}".format(top_count / shots),
                    "sim.framework": "qsharp",
                    "sim.component": "QSharpSimulator",
                    "sim.backend": simulator_type,
                    "sim.bit_order": "q0_left",
                    "report.type": "simulation",
                    "perf.elapsed_seconds": "{:.4f}".format(elapsed),
                    **({"run.seed": str(seed)} if seed is not None else {}),
                    **noise_attrs,
                },
            )
        except Exception as exc:
            return self._fail(str(exc))
