import io
import json
import os
import sys
import html as html_lib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reporting import page_template, write_card, bar_rows, now  # noqa: E402

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators
from nifiapi.__jvm__ import JvmHolder

_EXTRA_CSS = """\
    .diagram-panel { border-right: 1px solid #d0d7de; overflow: auto; }
    .diagram-panel svg { max-width: 100%; height: auto; border-radius: 6px; display: block; }
    .code-panel { overflow: auto; }
    .code-block {
      background: #ffffff;
      border: 1px solid #d8dee4;
      border-radius: 6px;
      padding: 14px;
      font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
      font-size: 0.8rem;
      color: #24292f;
      margin: 0;
      overflow-x: auto;
      white-space: pre;
      line-height: 1.65;
    }
    .attrs-panel { border-right: 1px solid #d0d7de; }
    .bar-fill { background: #0969da; height: 100%; border-radius: 4px; }"""


class QiskitCircuitReport(FlowFileTransform):
    """DEPRECATED — use ``QuanifiReport`` instead.

    This is the original Qiskit-only, Grover-flavoured reporter: it renders a
    circuit diagram from ``circuit.qasm3`` via matplotlib and shows Grover-style
    attributes (``circuit.marked_state``/``num_iterations``). It is superseded by
    the generic, framework-agnostic ``QuanifiReport``, which keys off
    ``report.type`` and ``sim.framework``, prefers ``circuit.svg`` (Cirq) over a
    re-render, and carries the decoded-result / VQE / statevector panels. New
    flows should wire simulators into ``QuanifiReport``. Retained only because an
    existing saved flow (``flow.json.gz``) still references this processor; do not
    add new dependencies on it.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.1.0"
        description = (
            "[DEPRECATED — use QuanifiReport] Legacy Qiskit-only reporter. "
            "Appends an HTML card to a local report file for each FlowFile, "
            "showing the Grover circuit diagram (SVG re-rendered from QASM3), "
            "OpenQASM 3 source, simulation attributes, and a measurement-count "
            "bar chart. Superseded by the generic, framework-agnostic "
            "QuanifiReport (report.type/sim.framework-driven); kept only for "
            "backward compatibility with existing saved flows."
        )
        tags = ["quantum", "report", "html", "visualization", "grover"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-qasm3-import", "matplotlib", "pylatexenc"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.reports_dir = PropertyDescriptor(
            name="Reports Directory",
            description="Folder where HTML report files are written. Shared across all CircuitReport instances.",
            required=True,
            default_value="reports",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.flow_name = PropertyDescriptor(
            name="Flow Name",
            description=(
                "Identifies this flow. Used as the HTML filename ({flow_name}.html) "
                "and as the page title. Set a unique value per CircuitReport instance, "
                "e.g. 'grover', 'hadamard'."
            ),
            required=True,
            default_value="circuit",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.descriptors = [self.reports_dir, self.flow_name]

    def getPropertyDescriptors(self):
        return self.descriptors

    def transform(self, context, flowFile):
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from qiskit import qasm3 as qasm3_module

        reports_dir = context.getProperty(self.reports_dir).getValue()
        flow_name   = context.getProperty(self.flow_name).getValue()
        output_path = os.path.join(reports_dir, f"{flow_name}.html")
        os.makedirs(reports_dir, exist_ok=True)

        qasm3_code    = flowFile.getAttribute("circuit.qasm3") or ""
        marked_state  = flowFile.getAttribute("circuit.marked_state") or "?"
        num_qubits    = flowFile.getAttribute("circuit.num_qubits") or "?"
        num_iters     = flowFile.getAttribute("circuit.num_iterations") or "?"
        fmt           = flowFile.getAttribute("circuit.format") or "qasm3"
        sim_shots     = flowFile.getAttribute("sim.shots") or "?"
        sim_top       = flowFile.getAttribute("sim.top_result") or "?"
        sim_prob      = flowFile.getAttribute("sim.top_probability") or "?"

        raw = bytes(flowFile.getContentsAsBytes())
        counts = {}
        try:
            counts = json.loads(raw.decode("utf-8"))
        except Exception:
            pass

        # --- Circuit SVG ---
        svg_html = "<p><em>Circuit diagram unavailable (QPY or missing QASM3).</em></p>"
        if qasm3_code:
            try:
                circuit = qasm3_module.loads(qasm3_code)
                fig = circuit.draw('mpl', fold=-1, style={'backgroundcolor': '#ffffff'})
                buf = io.BytesIO()
                fig.savefig(buf, format='svg', bbox_inches='tight', facecolor='white')
                plt.close(fig)
                raw_svg = buf.getvalue().decode('utf-8')
                start = raw_svg.find('<svg')
                svg_html = raw_svg[start:] if start >= 0 else raw_svg
            except Exception as exc:
                svg_html = f"<pre>Render error: {html_lib.escape(str(exc))}</pre>"

        # --- Attributes table rows ---
        attrs = [
            ("circuit.marked_state",   f"|{html_lib.escape(marked_state)}&#x27E9;"),
            ("circuit.num_qubits",     num_qubits),
            ("circuit.num_iterations", num_iters),
            ("circuit.format",         fmt),
            ("sim.shots",              sim_shots),
            ("sim.top_result",         f"|{html_lib.escape(sim_top)}&#x27E9;"),
            ("sim.top_probability",    sim_prob),
        ]
        rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in attrs)

        bars = bar_rows(counts, "bar-fill") if counts else ""

        card = f"""\
<section class="run-card">
  <header class="run-header">
    <span class="run-title">target |{html_lib.escape(marked_state)}&#x27E9; &nbsp;&middot;&nbsp; {html_lib.escape(num_qubits)} qubits &nbsp;&middot;&nbsp; {html_lib.escape(num_iters)} iteration(s)</span>
    <span class="run-time">{now()}</span>
  </header>
  <div class="run-body">
    <div class="panel diagram-panel"><h3>Circuit Diagram</h3>{svg_html}</div>
    <div class="panel code-panel"><h3>OpenQASM 3</h3><pre class="code-block">{html_lib.escape(qasm3_code)}</pre></div>
  </div>
  <div class="run-footer">
    <div class="panel attrs-panel">
      <h3>Attributes</h3>
      <table><tr><th>Key</th><th>Value</th></tr>{rows}</table>
    </div>
    <div class="panel counts-panel">
      <h3>Measurement Counts</h3>
      <div class="bar-chart">{bars}</div>
    </div>
  </div>
</section>"""

        page = page_template(html_lib.escape(flow_name), _EXTRA_CSS)
        write_card(output_path, card, page)

        return FlowFileTransformResult(
            relationship="success",
            attributes={"report.path": output_path, "report.flow_name": flow_name},
        )
