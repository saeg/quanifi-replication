import io
import json
import html as html_lib
import datetime

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators
from nifiapi.__jvm__ import JvmHolder

SENTINEL = "<!-- RUNS_START -->"

TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quanifi &mdash; <!-- FLOW_TITLE --></title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body { font-family: 'Segoe UI', system-ui, sans-serif; background: #ffffff;
           color: #24292f; margin: 0; padding: 24px; }
    h1 { color: #0969da; font-size: 1.5rem; border-bottom: 1px solid #d0d7de;
         padding-bottom: 12px; margin-bottom: 24px; }
    h3 { color: #0969da; font-size: 0.85rem; margin: 0 0 12px;
         text-transform: uppercase; letter-spacing: 0.07em; }
    .run-card { background: #f6f8fa; border: 1px solid #d0d7de;
                border-radius: 10px; margin-bottom: 32px; overflow: hidden; }
    .run-header { background: #eaeef2; padding: 12px 20px; display: flex;
                  justify-content: space-between; align-items: center;
                  border-bottom: 1px solid #d0d7de; }
    .run-title { color: #0969da; font-weight: 600; font-size: 0.95rem; }
    .run-time  { color: #8c959f; font-size: 0.82rem; font-family: monospace; }
    .run-body, .run-footer { display: flex; }
    .run-footer { border-top: 1px solid #d0d7de; }
    .panel { padding: 20px; flex: 1; min-width: 0; }
    .panel + .panel { border-left: 1px solid #d0d7de; }
    .diagram-panel svg { max-width: 100%; height: auto; border-radius: 6px; display: block; }
    .code-block { background: #ffffff; border: 1px solid #d8dee4; border-radius: 6px;
                  padding: 14px; font-family: 'Cascadia Code','Fira Code',monospace;
                  font-size: 0.8rem; color: #24292f; margin: 0; overflow-x: auto;
                  white-space: pre; line-height: 1.65; }
    table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
    th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #d0d7de; }
    th { color: #656d76; font-weight: 600; }
    td:first-child { color: #656d76; font-family: monospace; font-size: 0.8rem; }
    td:last-child  { color: #1f2328; font-family: monospace; }
    /* Statevector table */
    .sv-table th, .sv-table td { padding: 5px 8px; }
    .sv-table td { color: #1f2328; }
    .sv-state { font-family: monospace; color: #656d76 !important; }
    .sv-prob  { width: 160px; }
    .sv-bar-row { display: flex; align-items: center; gap: 8px; }
    .sv-bar-track { flex: 1; background: #d0d7de; border-radius: 3px; height: 12px; overflow: hidden; }
    .sv-bar-fill  { background: #0969da; height: 100%; border-radius: 3px; }
    .sv-pct    { font-family: monospace; font-size: 0.78rem; color: #656d76;
                 width: 46px; text-align: right; flex-shrink: 0; }
    .sv-amp    { font-family: monospace; font-size: 0.82rem; color: #1f2328 !important; }
    .sv-phase  { font-family: monospace; font-size: 0.82rem; font-weight: 600; }
    .note      { color: #8c959f; font-size: 0.78rem; margin-top: 10px; }
  </style>
</head>
<body>
  <h1>&#x269B; Quanifi &mdash; <!-- FLOW_TITLE --></h1>
  <!-- RUNS_START -->
</body>
</html>"""


def _fmt_amplitude(real, imag, tol=1e-6):
    r = abs(real) > tol
    i = abs(imag) > tol
    if not r and not i:
        return "0"
    if not i:
        return f"{real:.4f}"
    if not r:
        return f"{imag:+.4f}i"
    sign = "+" if imag >= 0 else "−"
    return f"{real:.4f} {sign} {abs(imag):.4f}i"


def _phase_color(deg):
    hue = deg % 360
    return f"hsl({hue:.0f}, 65%, 62%)"


class QiskitStatevectorSimulator(FlowFileTransform):
    """
    Computes the exact statevector of a quantum circuit (no measurement noise).

    Unlike QiskitAerSimulator — which samples N shots and returns counts — this
    processor uses qiskit.quantum_info.Statevector to compute the full 2^n-entry
    complex amplitude vector in one pass.  No measurements are added; if the
    incoming circuit already has measurements they are stripped first.

    Output includes:
      • Probability distribution as JSON content (compatible with
        QuanifiReport and QuantumDistributionComparison)
      • sim.* attributes matching AerSimulator convention (top_result, etc.)
      • circuit.depth / gate_count / nonlocal_gates / t_count metrics
      • An HTML card with amplitude + phase table written to the report file

    Best used in a debug branch alongside (not replacing) AerSimulator:

        QiskitGroverCircuit ──► QiskitAerSimulator ──► QuanifiReport
                           └──► QiskitStatevectorSimulator ──► (report/log)
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "Computes the exact statevector of an unmeasured quantum circuit using "
            "qiskit.quantum_info.Statevector.  Outputs probability distribution as JSON "
            "and writes an HTML card with amplitude and phase per basis state."
        )
        tags = ["quantum", "qiskit", "statevector", "debug", "simulation", "amplitude", "phase"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-qasm3-import", "matplotlib", "pylatexenc"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.reports_dir = PropertyDescriptor(
            name="Reports Directory",
            description="Folder where the HTML statevector report is written.",
            required=True,
            default_value="reports",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.flow_name = PropertyDescriptor(
            name="Flow Name",
            description=(
                "Used as the page title and, unless 'Report File Name' is set, "
                "as the report filename: {flow_name}-statevector.html."
            ),
            required=True,
            default_value="circuit",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.report_file_name = PropertyDescriptor(
            name="Report File Name",
            description=(
                "Optional override for the report filename, without the .html "
                "extension. If unset, the filename defaults to "
                "'{flow_name}-statevector.html'."
            ),
            required=False,
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.prob_threshold = PropertyDescriptor(
            name="Probability Threshold",
            description=(
                "States with probability below this value are hidden from the report table. "
                "E.g. 0.001 hides anything below 0.1 %. Set to 0 to show all 2^n states."
            ),
            required=True,
            default_value="0.001",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.max_states = PropertyDescriptor(
            name="Max States",
            description="Maximum rows to display in the statevector table (sorted by probability).",
            required=True,
            default_value="32",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
        )
        self.descriptors = [
            self.reports_dir, self.flow_name, self.report_file_name,
            self.prob_threshold, self.max_states,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        import numpy as np
        from qiskit.quantum_info import Statevector

        reports_dir     = context.getProperty(self.reports_dir).getValue()
        flow_name       = context.getProperty(self.flow_name).getValue()
        report_filename = context.getProperty(self.report_file_name).getValue()
        threshold       = float(context.getProperty(self.prob_threshold).getValue() or 0.001)
        max_states  = int(context.getProperty(self.max_states).getValue())

        fmt        = flowFile.getAttribute("circuit.format") or "qasm3"
        raw        = bytes(flowFile.getContentsAsBytes())
        qasm3_attr = flowFile.getAttribute("circuit.qasm3") or ""

        circuit = None

        # Try to load circuit from content first.
        if fmt == "qpy" and raw:
            try:
                from qiskit import qpy
                circuit = qpy.load(io.BytesIO(raw))[0]
            except Exception:
                pass

        if circuit is None and fmt == "qasm3" and raw:
            raw_str = raw.decode("utf-8").strip()
            # QASM3 files begin with 'OPENQASM' or a comment; JSON results begin
            # with '{'. Skip parsing if the content is clearly JSON (e.g. from
            # AerSimulator output).
            if raw_str.startswith("OPENQASM") or raw_str.startswith("//"):
                try:
                    from qiskit import qasm3
                    circuit = qasm3.loads(raw_str)
                except Exception:
                    pass

        # Fallback: load from the circuit.qasm3 attribute that all circuit
        # processors carry forward even after AerSimulator replaces the content.
        if circuit is None and qasm3_attr:
            try:
                from qiskit import qasm3
                circuit = qasm3.loads(qasm3_attr)
            except Exception as exc:
                self.logger.error("StatevectorSimulator: {}".format(exc))
                return FlowFileTransformResult(
                    relationship="failure",
                    attributes={"sim.error": (
                        "StatevectorSimulator: could not parse circuit from "
                        "content or circuit.qasm3 attribute: {}".format(exc))},
                )

        if circuit is None:
            msg = (
                "StatevectorSimulator: no circuit found in FlowFile content or "
                "circuit.qasm3 attribute. Connect this processor to a circuit "
                "processor output (GroverCircuit, HadamardTransform, …), not "
                "directly to AerSimulator — or ensure circuit.qasm3 is set."
            )
            self.logger.error(msg)
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"sim.error": msg},
            )

        # Strip measurements — statevector requires an unmeasured circuit.
        circuit = circuit.remove_final_measurements(inplace=False)

        # Circuit metrics (computed on the pre-measurement circuit).
        ops        = circuit.count_ops()
        gate_count = sum(v for k, v in ops.items() if k not in ("barrier", "measure", "reset"))
        depth      = circuit.depth()
        n_nonlocal = circuit.num_nonlocal_gates()
        t_count    = ops.get("t", 0) + ops.get("tdg", 0)

        # Statevector. from_instruction raises on mid-circuit measurements
        # (remove_final_measurements only strips trailing ones) and on
        # instructions without a unitary definition.
        try:
            sv = Statevector.from_instruction(circuit)
        except Exception as exc:
            self.logger.error("StatevectorSimulator: {}".format(exc))
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"sim.error": (
                    "StatevectorSimulator: could not compute the statevector "
                    "(mid-circuit measurements or non-unitary instructions are "
                    "not supported): {}".format(exc))},
            )
        n  = circuit.num_qubits

        state_data = []
        for i, amp in enumerate(sv.data):
            prob = float(abs(amp) ** 2)
            if prob < threshold:
                continue
            state_data.append({
                # Canonical bit order: qubit 0 = leftmost char. Qiskit basis
                # index i is little-endian (qubit 0 = LSB), so reverse the
                # binary string to match CirqStatevectorSimulator's labels.
                "state":     format(i, f"0{n}b")[::-1],
                "prob":      prob,
                "amp_real":  float(amp.real),
                "amp_imag":  float(amp.imag),
                "phase_deg": float(np.angle(amp) * 180.0 / np.pi),
            })

        state_data.sort(key=lambda x: x["prob"], reverse=True)
        total_nonzero = len(state_data)
        state_data    = state_data[:max_states]

        top = state_data[0] if state_data else {"state": "?", "prob": 0.0}

        # Probability dict as content — compatible with CircuitReport bar chart
        # and QuantumDistributionComparison.
        probs = {s["state"]: s["prob"] for s in state_data}

        # Inherited circuit attributes to pass downstream.
        inherited = {}
        for key in ("circuit.marked_state", "circuit.num_iterations", "circuit.state_type",
                    "circuit.qasm3", "circuit.diagram", "circuit.qft_inverse",
                    "circuit.phase_register_size"):
            val = flowFile.getAttribute(key)
            if val:
                inherited[key] = val

        qasm3_code = inherited.get("circuit.qasm3", "")

        self.logger.warn(
            "StatevectorSimulator ({} qubits): top=|{}⟩ p={:.4f}  "
            "depth={}  gates={}  nonlocal={}".format(
                n, top["state"], top["prob"], depth, gate_count, n_nonlocal
            )
        )

        self._write_report(
            reports_dir, flow_name, report_filename, n, state_data, total_nonzero,
            depth, gate_count, n_nonlocal, t_count, qasm3_code,
            flowFile.getAttribute("circuit.marked_state") or "",
            flowFile.getAttribute("circuit.state_type") or "",
        )

        attrs = {
            "circuit.format":          fmt,
            "circuit.num_qubits":      str(n),
            "circuit.depth":           str(depth),
            "circuit.gate_count":      str(gate_count),
            "circuit.nonlocal_gates":  str(n_nonlocal),
            "circuit.t_count":         str(t_count),
            "sim.top_result":          top["state"],
            "sim.top_probability":     f"{top['prob']:.4f}",
            "sim.num_nonzero_states":  str(total_nonzero),
            "sim.total_states":        str(2 ** n),
            "sim.simulator":           "statevector",
            "sim.framework":           "qiskit",
            "sim.bit_order":           "q0_left",
            "report.type":             "simulation",
            **inherited,
        }

        return FlowFileTransformResult(
            relationship="success",
            contents=json.dumps(probs, indent=2).encode("utf-8"),
            attributes=attrs,
        )

    # -------------------------------------------------------------------------

    def _write_report(self, reports_dir, flow_name, report_filename, n, state_data,
                      total_nonzero, depth, gate_count, n_nonlocal, t_count,
                      qasm3_code, marked_state, state_type):
        import os
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        os.makedirs(reports_dir, exist_ok=True)
        filename    = report_filename if report_filename else f"{flow_name}-statevector"
        output_path = os.path.join(reports_dir, f"{filename}.html")

        # --- Circuit SVG ---
        svg_html = "<p><em>Circuit diagram unavailable.</em></p>"
        if qasm3_code:
            try:
                from qiskit import qasm3 as qasm3_module
                circ = qasm3_module.loads(qasm3_code)
                fig  = circ.draw("mpl", fold=-1, style={"backgroundcolor": "#ffffff"})
                buf  = io.BytesIO()
                fig.savefig(buf, format="svg", bbox_inches="tight", facecolor="white")
                plt.close(fig)
                raw_svg = buf.getvalue().decode("utf-8")
                start   = raw_svg.find("<svg")
                svg_html = raw_svg[start:] if start >= 0 else raw_svg
            except Exception as exc:
                svg_html = f"<pre>Render error: {html_lib.escape(str(exc))}</pre>"

        # --- Statevector table ---
        sv_rows = ""
        for s in state_data:
            pct   = s["prob"] * 100
            amp   = _fmt_amplitude(s["amp_real"], s["amp_imag"])
            color = _phase_color(s["phase_deg"])
            sv_rows += (
                f'<tr>'
                f'<td class="sv-state">|{html_lib.escape(s["state"])}&#x27E9;</td>'
                f'<td class="sv-prob">'
                f'  <div class="sv-bar-row">'
                f'    <div class="sv-bar-track">'
                f'      <div class="sv-bar-fill" style="width:{pct:.1f}%"></div>'
                f'    </div>'
                f'    <span class="sv-pct">{pct:.1f}%</span>'
                f'  </div>'
                f'</td>'
                f'<td class="sv-amp">{html_lib.escape(amp)}</td>'
                f'<td class="sv-phase" style="color:{color}">{s["phase_deg"]:.1f}°</td>'
                f'</tr>\n'
            )

        shown   = len(state_data)
        note    = ""
        if shown < total_nonzero:
            note = f'<p class="note">Showing {shown} of {total_nonzero} states above threshold (2ⁿ = {2**n}).</p>'
        else:
            note = f'<p class="note">{total_nonzero} state(s) above threshold &nbsp;/&nbsp; {2**n} total.</p>'

        # --- Attributes table ---
        header_parts = [f"{n} qubits"]
        if marked_state:
            header_parts.append(f"target |{html_lib.escape(marked_state)}&#x27E9;")
        if state_type:
            header_parts.append(html_lib.escape(state_type))

        attr_rows = "".join(
            f"<tr><td>{k}</td><td>{v}</td></tr>"
            for k, v in [
                ("circuit.num_qubits",     str(n)),
                ("circuit.depth",          str(depth)),
                ("circuit.gate_count",     str(gate_count)),
                ("circuit.nonlocal_gates", str(n_nonlocal)),
                ("circuit.t_count",        str(t_count)),
                ("sim.simulator",          "statevector"),
                ("sim.total_states",       str(2 ** n)),
                ("sim.nonzero_states",     str(total_nonzero)),
            ]
        )

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        card = f"""\
<section class="run-card">
  <header class="run-header">
    <span class="run-title">
      {' &middot; '.join(header_parts)} &nbsp;&middot;&nbsp; depth {depth}
    </span>
    <span class="run-time">{timestamp}</span>
  </header>
  <div class="run-body">
    <div class="panel diagram-panel"><h3>Circuit Diagram</h3>{svg_html}</div>
    <div class="panel">
      <h3>Statevector &mdash; amplitude &amp; phase</h3>
      <table class="sv-table">
        <tr>
          <th>State</th>
          <th>Probability</th>
          <th>Amplitude</th>
          <th>Phase</th>
        </tr>
        {sv_rows}
      </table>
      {note}
    </div>
  </div>
  <div class="run-footer">
    <div class="panel">
      <h3>Circuit Metrics</h3>
      <table><tr><th>Attribute</th><th>Value</th></tr>{attr_rows}</table>
    </div>
    <div class="panel">
      <h3>OpenQASM 3</h3>
      <pre class="code-block">{html_lib.escape(qasm3_code) if qasm3_code else '(not available)'}</pre>
    </div>
  </div>
</section>"""

        title = html_lib.escape(f"{flow_name} — statevector")
        fresh = TEMPLATE.replace("<!-- FLOW_TITLE -->", title)

        if os.path.exists(output_path):
            with open(output_path, "r", encoding="utf-8") as f:
                existing = f.read()
            new_html = (
                existing.replace(SENTINEL, SENTINEL + "\n" + card, 1)
                if SENTINEL in existing
                else fresh.replace(SENTINEL, SENTINEL + "\n" + card)
            )
        else:
            new_html = fresh.replace(SENTINEL, SENTINEL + "\n" + card)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(new_html)
