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


class BraketDevice(FlowFileTransform):
    """
    Executes a circuit on Amazon Braket managed devices — real QPUs (IonQ,
    Rigetti, IQM) and managed simulators (SV1, DM1) — or on the local
    simulator when Device = 'local' (no AWS account needed).

    This is the hardware gateway for *every* framework: it consumes the shared
    circuit contract (circuit.format = qasm3 or qasm2), so a circuit built by
    any Qiskit, Cirq, Qrisp or PennyLane builder runs on real hardware through
    this one processor. The qasm handling mirrors BraketSimulator (stdgates
    inlining, full-register measurement, qasm2 translation via Qiskit).

    Long QPU queues do not block the canvas: with Wait For Results = false the
    task is submitted asynchronously and the FlowFile continues with
    hw.task_arn + hw.status, to be fetched later. With true, the processor
    polls up to Poll Timeout Seconds. AWS credentials come from the standard
    boto3 chain (env vars / ~/.aws); missing credentials route to failure
    with a clear hw.error.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Runs a circuit (circuit.format = qasm3 or qasm2, from any framework's "
            "builder) on an Amazon Braket device: a QPU / managed-simulator ARN, or "
            "'local' for the offline LocalSimulator. Supports fire-and-forget "
            "submission (Wait For Results = false -> hw.task_arn) for long QPU "
            "queues. Credentials via the standard AWS chain."
        )
        tags = ["quantum", "braket", "aws", "hardware", "qpu", "ionq", "rigetti"]
        dependencies = ["amazon-braket-sdk", "qiskit>=2.0.0,<2.5", "qiskit-qasm3-import"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.device = PropertyDescriptor(
            name="Device",
            description=(
                "'local' for the offline LocalSimulator, or an AWS Braket device "
                "ARN, e.g. arn:aws:braket:::device/quantum-simulator/amazon/sv1 "
                "or a QPU ARN (IonQ / Rigetti / IQM)."
            ),
            required=True,
            default_value="local",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.shots = PropertyDescriptor(
            name="Shots",
            description="Number of shots on the device.",
            required=True,
            default_value="1024",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.s3_bucket = PropertyDescriptor(
            name="S3 Bucket",
            description=(
                "Optional 'bucket/prefix' for task results. Empty uses the "
                "account's default Braket bucket."
            ),
            required=False,
            default_value="",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.wait = PropertyDescriptor(
            name="Wait For Results",
            description=(
                "true: poll until the task completes (up to Poll Timeout Seconds) "
                "and emit counts. false: submit and continue immediately with "
                "hw.task_arn (QPU queues can be hours)."
            ),
            required=True,
            default_value="true",
            allowable_values=["true", "false"],
        )
        self.poll_timeout = PropertyDescriptor(
            name="Poll Timeout Seconds",
            description="Maximum seconds to wait for results when Wait For Results = true.",
            required=True,
            default_value="300",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.descriptors = [self.device, self.shots, self.s3_bucket,
                            self.wait, self.poll_timeout]

    def getPropertyDescriptors(self):
        return self.descriptors

    # --- qasm handling shared with BraketSimulator (braket_qasm.py) ----------

    def _program_from_flowfile(self, flowFile):
        from braket_qasm import ensure_full_register_measure, to_braket_qasm3

        fmt = flowFile.getAttribute("circuit.format")
        raw = bytes(flowFile.getContentsAsBytes())
        qasm3_src, note = to_braket_qasm3(fmt, raw.decode("utf-8"))
        from braket.ir.openqasm import Program
        return (Program(source=ensure_full_register_measure(qasm3_src)),
                {"sim.translation": note})

    def transform(self, context, flowFile):
        get = lambda prop: (
            context.getProperty(prop)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        )

        device_spec = get(self.device).strip()
        shots = int(get(self.shots))
        bucket = (get(self.s3_bucket) or "").strip()
        wait = get(self.wait).lower() == "true"
        poll_timeout = int(get(self.poll_timeout))

        try:
            program, extra = self._program_from_flowfile(flowFile)
        except Exception as exc:
            self.logger.error("BraketDevice: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure", contents=b"", attributes={"hw.error": str(exc)},
            )

        base_attrs = {
            "hw.provider": "aws-braket",
            "hw.device": device_spec,
            "sim.shots": str(shots),
            "sim.framework": "braket",
            "sim.bit_order": "q0_left",
            **extra,
        }

        t0 = time.time()
        if device_spec == "local":
            try:
                from braket.devices import LocalSimulator
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    result = LocalSimulator().run(program, shots=shots).result()
                counts = result.measurement_counts
            except Exception as exc:
                msg = "Local Braket simulation failed: {}".format(exc)
                self.logger.error("BraketDevice: " + msg)
                return FlowFileTransformResult(
                    relationship="failure", contents=b"", attributes={"hw.error": msg},
                )
        else:
            try:
                from braket.aws import AwsDevice
                run_kwargs = {"shots": shots, "poll_timeout_seconds": poll_timeout}
                if bucket:
                    parts = bucket.split("/", 1)
                    run_kwargs["s3_destination_folder"] = (
                        parts[0], parts[1] if len(parts) > 1 else "quanifi")
                task = AwsDevice(device_spec).run(program, **run_kwargs)
                if not wait:
                    arn = getattr(task, "id", "") or getattr(task, "arn", "")
                    self.logger.warn(
                        "BraketDevice: submitted task {} to {} (not waiting)".format(
                            arn, device_spec))
                    return FlowFileTransformResult(
                        relationship="success",
                        contents=json.dumps({"task_arn": arn}, indent=2).encode("utf-8"),
                        attributes={**base_attrs, "hw.task_arn": arn,
                                    "hw.status": str(task.state())},
                    )
                counts = task.result().measurement_counts
            except Exception as exc:
                msg = ("Braket task on '{}' failed: {}. Check AWS credentials "
                       "(env/~/.aws), region, and the device ARN.".format(device_spec, exc))
                self.logger.error("BraketDevice: " + msg)
                return FlowFileTransformResult(
                    relationship="failure", contents=b"", attributes={"hw.error": msg},
                )

        sorted_counts = dict(
            sorted(((k, int(v)) for k, v in counts.items()),
                   key=lambda x: x[1], reverse=True))
        top_state, top_count = next(iter(sorted_counts.items()))

        self.logger.warn(
            "BraketDevice ({}): top |{}> p={:.4f}".format(
                device_spec, top_state, top_count / shots))

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(sorted_counts, indent=2).encode("utf-8"),
            attributes={
                **base_attrs,
                "sim.top_result": top_state,
                "sim.top_probability": "{:.4f}".format(top_count / shots),
                "report.type": "simulation",
                "perf.elapsed_seconds": "{:.4f}".format(time.time() - t0),
            },
        )
