import json
import math
import os

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.__jvm__ import JvmHolder


class QuantumUnitaryComparison(FlowFileTransform):
    """
    Compare two unitary matrices for equivalence up to a global phase.

    Pairs two FlowFiles (both produced by QuanifiUnitary processors) by a static
    Comparison Label. The first arriving FlowFile is held in a state file; the
    second triggers the comparison.

    Computes the process fidelity
        F = |tr(U_A^dagger . U_B)|^2 / d^2     (d = 2^n)
    F = 1 means the two circuits are equivalent up to a global phase (i.e. they
    produce the same output state for *every possible* input state, not just
    |0...0>). F < 1 means they differ in a way no global phase can fix.

    Routes to:
      - success: comparison complete; result JSON written; metrics in attributes.
      - failure: first of pair (state stored, awaiting second);
                 or input not produced by QuanifiUnitary.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Compares two unitary matrices for equivalence up to a global phase. Pairs "
            "two FlowFiles produced by QuanifiUnitary using a static Comparison Label, "
            "then computes the process fidelity F = |tr(U_A^dagger . U_B)|^2 / d^2. "
            "F = 1 (within tolerance) means the circuits are equivalent on every "
            "possible input, not just |0...0>. The first arrival is held in a state "
            "file; the second triggers the comparison. Routes the first of a pair to "
            "failure (auto-terminate it) and the completed comparison to success."
        )
        tags = ["quantum", "unitary", "equivalence", "comparison", "validation"]
        dependencies = ["numpy"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.state_dir = PropertyDescriptor(
            name="State Directory",
            description=(
                "Directory where the first arrived unitary is persisted between "
                "FlowFile arrivals. Must be writable by NiFi."
            ),
            required=True,
            default_value="reports/tmp/quanifi_unitary_state",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.comparison_label = PropertyDescriptor(
            name="Comparison Label",
            description=(
                "Identifier for this comparison slot. Both unitaries you want to "
                "compare must route into this processor instance. The label is used "
                "as the state filename — set a unique value per processor instance if "
                "you have multiple comparison processors on the canvas. "
                "Example: 'grover-3q-equiv', 'qft-vs-cirq-qft'."
            ),
            required=True,
            default_value="unitary-comparison",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.framework_label = PropertyDescriptor(
            name="Framework Label",
            description=(
                "Label used to identify this FlowFile's framework in the output. "
                "Supports NiFi Expression Language. Defaults to reading "
                "unitary.source_format (so a Cirq circuit reads 'cirq_json', a Qiskit "
                "circuit reads 'qasm3', etc.). Type a literal like 'cirq' or 'qiskit' "
                "to override."
            ),
            required=False,
            default_value="${unitary.source_format}",
            expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES,
        )
        self.tolerance = PropertyDescriptor(
            name="Tolerance",
            description=(
                "Maximum |1 - F| considered equivalent. Default 1e-9 (essentially "
                "exact for hand-built circuits). Loosen to 1e-6 if you expect small "
                "numerical noise from transpilation or unitary decomposition."
            ),
            required=True,
            default_value="1e-9",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.descriptors = [
            self.state_dir,
            self.comparison_label,
            self.framework_label,
            self.tolerance,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    # -----------------------------------------------------------------------

    def transform(self, context, flowFile):
        import numpy as np

        state_dir = context.getProperty(self.state_dir).getValue()
        label     = context.getProperty(self.comparison_label).getValue()
        tolerance = float(context.getProperty(self.tolerance).getValue())

        framework = (
            context.getProperty(self.framework_label)
            .evaluateAttributeExpressions(flowFile)
            .getValue()
        ) or ""

        raw = bytes(flowFile.getContentsAsBytes())

        # ----- Guard: input must come from QuanifiUnitary -----
        if flowFile.getAttribute("unitary.format") != "json_real_imag":
            error_msg = (
                "Input FlowFile does not carry a unitary in json_real_imag format. "
                "QuantumUnitaryComparison must be fed by QuanifiUnitary processors, "
                "not directly by circuit builders, simulators, or other data sources. "
                "Insert a QuanifiUnitary processor between each circuit builder and "
                "this comparison processor."
            )
            self.logger.error("QuantumUnitaryComparison: " + error_msg)
            return FlowFileTransformResult(
                relationship="failure",
                contents=raw,
                attributes={
                    "equiv.error":      error_msg,
                    "equiv.error_type": "not_a_unitary_flowfile",
                },
            )

        try:
            payload = json.loads(raw.decode("utf-8"))
            U = np.array(payload["real"], dtype=np.float64) + 1j * np.array(payload["imag"], dtype=np.float64)
        except Exception as exc:
            error_msg = f"Failed to parse unitary JSON content: {exc}"
            self.logger.error("QuantumUnitaryComparison: " + error_msg)
            return FlowFileTransformResult(
                relationship="failure",
                contents=raw,
                attributes={
                    "equiv.error":      error_msg,
                    "equiv.error_type": "json_parse_failure",
                },
            )

        meta = {
            "num_qubits":       flowFile.getAttribute("unitary.num_qubits")    or "",
            "dimension":        flowFile.getAttribute("unitary.dimension")     or "",
            "source_format":    flowFile.getAttribute("unitary.source_format") or "",
            "marked_state":     flowFile.getAttribute("circuit.marked_state")  or "",
            "num_iterations":   flowFile.getAttribute("circuit.num_iterations") or "",
            "depth":            flowFile.getAttribute("circuit.depth")         or "",
        }

        os.makedirs(state_dir, exist_ok=True)
        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        state_path = os.path.join(state_dir, f"{safe_label}.npz")
        meta_path  = os.path.join(state_dir, f"{safe_label}.meta.json")

        # ----- First arrival: persist and wait -----
        if not os.path.exists(state_path):
            np.savez(state_path, real=U.real, imag=U.imag)
            with open(meta_path, "w") as f:
                json.dump({"framework": framework, "meta": meta}, f)
            self.logger.warn(
                "QuantumUnitaryComparison [{}]: stored first unitary "
                "(framework='{}', n={}). Waiting for second.".format(
                    label, framework, meta["num_qubits"]
                )
            )
            return FlowFileTransformResult(
                relationship="failure",
                contents=raw,
                attributes={
                    "equiv.status": "waiting",
                    "equiv.label":  label,
                },
            )

        # ----- Second arrival: load A, compare to B -----
        try:
            data_a = np.load(state_path)
            U_a = data_a["real"] + 1j * data_a["imag"]
            with open(meta_path) as f:
                stored = json.load(f)
            meta_a      = stored.get("meta", {})
            framework_a = stored.get("framework", "")
        except Exception as exc:
            error_msg = f"Failed to load stored first unitary: {exc}"
            self.logger.error("QuantumUnitaryComparison: " + error_msg)
            self._cleanup(state_path, meta_path)
            return FlowFileTransformResult(
                relationship="failure",
                contents=raw,
                attributes={
                    "equiv.error":      error_msg,
                    "equiv.error_type": "state_load_failure",
                },
            )

        # Clear state files now so the next pair starts fresh, regardless of result.
        self._cleanup(state_path, meta_path)

        # ----- Dimension check -----
        if U_a.shape != U.shape:
            msg = (
                f"Unitary dimensions mismatch: {U_a.shape} vs {U.shape}. "
                "The two circuits act on different numbers of qubits, so they "
                "cannot be equivalent."
            )
            self.logger.warn("QuantumUnitaryComparison: " + msg)
            return FlowFileTransformResult(
                relationship="success",
                contents=json.dumps({
                    "label":      label,
                    "equivalent": False,
                    "error":      msg,
                    "shape_a":    list(U_a.shape),
                    "shape_b":    list(U.shape),
                }, indent=2).encode("utf-8"),
                attributes={
                    "equiv.status":      "complete",
                    "equiv.label":       label,
                    "equiv.equivalent":  "false",
                    "equiv.error":       msg,
                    "equiv.error_type":  "dimension_mismatch",
                    "equiv.shape_a":     str(U_a.shape),
                    "equiv.shape_b":     str(U.shape),
                },
            )

        # ----- Process fidelity: F = |tr(U_A^dagger . U_B)|^2 / d^2 -----
        d = U.shape[0]
        inner             = np.trace(U_a.conj().T @ U)
        process_fidelity  = float((abs(inner) ** 2) / (d * d))
        global_phase_deg  = math.degrees(math.atan2(inner.imag, inner.real))
        equivalent        = abs(1.0 - process_fidelity) <= tolerance

        n  = int(round(math.log2(d)))
        fa = framework_a or "run-1"
        fb = framework   or "run-2"
        if fa == fb:
            fa = fa + " (1)"
            fb = fb + " (2)"

        result = {
            "label":                label,
            "equivalent":           equivalent,
            "process_fidelity":     process_fidelity,
            "global_phase_degrees": global_phase_deg,
            "num_qubits":           n,
            "dimension":            d,
            "tolerance":            tolerance,
            "framework_a":          fa,
            "framework_b":          fb,
            "meta_a":               meta_a,
            "meta_b":               meta,
        }

        out_attrs = {
            "equiv.status":               "complete",
            "equiv.label":                label,
            "equiv.equivalent":           "true" if equivalent else "false",
            "equiv.process_fidelity":     f"{process_fidelity:.10f}",
            "equiv.global_phase_degrees": f"{global_phase_deg:.4f}",
            "equiv.num_qubits":           str(n),
            "equiv.dimension":            str(d),
            "equiv.tolerance":            str(tolerance),
            "equiv.framework_a":          fa,
            "equiv.framework_b":          fb,
        }

        if equivalent:
            self.logger.info(
                "QuantumUnitaryComparison [{}]: EQUIVALENT (F={:.10f}, global phase={:.4f} deg)".format(
                    label, process_fidelity, global_phase_deg
                )
            )
        else:
            self.logger.warn(
                "QuantumUnitaryComparison [{}]: NOT equivalent (F={:.10f}, |1-F|={:.2e} > tol={})".format(
                    label, process_fidelity, abs(1.0 - process_fidelity), tolerance
                )
            )

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(result, indent=2).encode("utf-8"),
            attributes=out_attrs,
        )

    @staticmethod
    def _cleanup(*paths):
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass
