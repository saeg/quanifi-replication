import io
import json
import os
import sys
import html as html_lib
import hashlib
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reporting import page_template, write_card, bar_rows, now  # noqa: E402

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators
from nifiapi.__jvm__ import JvmHolder

_EXTRA_CSS = """\
    .panel + .panel { border-left: 1px solid #d0d7de; }
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
    .bar-fill   { background: #0969da; height: 100%; border-radius: 4px; }
    .bar-fill-a { background: #0969da; height: 100%; border-radius: 4px; }
    .bar-fill-b { background: #1a7f37; height: 100%; border-radius: 4px; }
    .badge {
      display: inline-block;
      padding: 2px 10px;
      border-radius: 12px;
      font-size: 0.78rem;
      font-weight: 600;
      font-family: monospace;
    }
    .badge-agree  { background: #dafbe1; color: #1a7f37; }
    .badge-differ { background: #ffebe9; color: #cf222e; }
    .badge-noise  { background: #fff8c5; color: #9a6700; }
    .metric-val   { color: #1f2328 !important; }
    .tag-a { color: #0969da; }
    .tag-b { color: #1a7f37; }
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
    /* Circuit SVGs keep their natural width and scroll inside their panel
       instead of bleeding over the neighbouring panels. */
    .svg-scroll { overflow-x: auto; max-width: 100%; }
    .svg-scroll svg { display: block; }
    /* Footers with many panels wrap onto extra rows instead of squeezing
       every panel into one narrow strip. */
    .run-footer { flex-wrap: wrap; }
    .run-footer .panel { flex: 1 1 340px; }"""

def _qasm_source(attrs):
    """Return (code, dialect) for whichever QASM dialect the flow published.

    Builders set ``circuit.qasm3`` or ``circuit.qasm2`` depending on their
    Output Format property, never both, so every consumer has to check for
    each. QASM 3 wins when both are somehow present.
    """
    qasm3_code = attrs.get("circuit.qasm3", "")
    if qasm3_code:
        return qasm3_code, "qasm3"
    return attrs.get("circuit.qasm2", ""), "qasm2"


# ---------------------------------------------------------------------------
# Registry: maps report.type → ordered list of (zone, section_name) pairs.
# "body" panels are rendered in run-body; "footer" panels in run-footer.
# To support a new processor type, add an entry here and, if needed, a
# _render_<section_name> method below.
# ---------------------------------------------------------------------------

_SECTION_REGISTRY = {
    "simulation": [
        ("body",   "circuit_diagram"),
        ("body",   "qasm_source"),
        ("footer", "derived_result"),
        ("footer", "counts_chart"),
        ("footer", "vqe_attrs"),
        ("footer", "qaoa_attrs"),
        ("footer", "ae_attrs"),
        ("footer", "noise_model"),
        ("footer", "circuit_attrs"),
        ("footer", "sim_attrs"),
        ("footer", "hardware_attrs"),
    ],
    "statevector": [
        ("body",   "circuit_diagram"),
        ("body",   "statevector_table"),
        ("footer", "qasm_source"),
        ("footer", "noise_model"),
        ("footer", "circuit_attrs"),
        ("footer", "sim_attrs"),
    ],
    "comparison": [
        ("body",   "comparison_metrics"),
        ("footer", "dual_distribution_chart"),
    ],
    "consensus": [
        ("body",   "consensus_verdict"),
        ("body",   "consensus_branches"),
        ("body",   "counts_chart"),
        ("footer", "circuit_diagram"),
        ("footer", "qasm_source"),
        ("footer", "mutation_attrs"),
        ("footer", "circuit_attrs"),
        ("footer", "sim_attrs"),
    ],
    "training": [
        ("body",   "loss_curve"),
        ("footer", "train_attrs"),
        ("footer", "all_attrs"),
    ],
    "_default": [
        ("body",   "raw_content"),
        ("footer", "all_attrs"),
    ],
}


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
    return f"hsl({deg % 360:.0f}, 65%, 62%)"


# Friendly labels for the keys carried in the sim.noise_params JSON blob.
_NOISE_PARAM_LABELS = {
    "error_1q":     "1-qubit error rate",
    "error_2q":     "2-qubit error rate",
    "t1_us":        "T1 (\xb5s)",
    "t2_us":        "T2 (\xb5s)",
    "gate_time_ns": "Gate time (ns)",
    "readout_error": "Readout error rate",
    "backend":      "Backend",
    "probability":  "Error probability",   # Cirq depolarizing / bit-flip
    "gamma":        "Damping γ",       # Cirq amplitude damping
}


class QuanifiReport(FlowFileTransform):
    """
    Generic reporting processor for the Quanifi pipeline.

    Reads the report.type FlowFile attribute to select which sections to render,
    then writes a self-contained HTML card to a local report file using the
    shared reporting module.

    Supported types and their sections
    -----------------------------------
    simulation  — circuit diagram, QASM source, counts chart, noise model
                  (when non-ideal), circuit attrs, sim attrs
    statevector — circuit diagram, statevector amplitude/phase table, QASM
                  source, noise model (when non-ideal), circuit attrs, sim attrs
    comparison  — distance metrics table, dual distribution bar charts
    consensus   — verdict panel (PASS/FAIL/DISAGREE + majority/dissenters),
                  counts chart, circuit diagram, QASM source, mut.* panel
                  (mutation runs only), circuit attrs, sim attrs
    (default)   — raw content code block, all FlowFile attributes

    Adding a new type
    -----------------
    1. Add an entry to _SECTION_REGISTRY above.
    2. Add a _render_<section_name>(self, data) method if using a new section.
       Return None to skip the panel; return an HTML string (panel inner HTML)
       to include it.
    """

    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']

    class ProcessorDetails:
        version = "0.3.1"
        description = (
            "Generic HTML report writer for Quanifi processors.  Reads report.type "
            "from the FlowFile attribute to select which sections to render (circuit "
            "diagram, counts, statevector, comparison metrics, etc.) and appends a "
            "card to a local HTML file.  Extend _SECTION_REGISTRY to support new "
            "processor output types."
        )
        tags = ["quantum", "report", "html", "visualization", "qiskit", "comparison", "statevector"]
        dependencies = ["qiskit>=2.0.0,<2.5", "qiskit-qasm3-import", "matplotlib", "pylatexenc"]

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get('jvm')
        super().__init__()

        self.reports_dir = PropertyDescriptor(
            name="Reports Directory",
            description="Folder where the HTML report file is written.",
            required=True,
            default_value="reports",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.flow_name = PropertyDescriptor(
            name="Flow Name",
            description=(
                "Report filename: {flow_name}.html.  Also used as the page title. "
                "Set a unique value per QuanifiReport instance, e.g. 'grover', "
                "'statevector-debug', 'grover-comparison'."
            ),
            required=True,
            default_value="report",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.output_mode = PropertyDescriptor(
            name="Output Mode",
            description=(
                "Local HTML writes the existing report file; Web API submits structured "
                "report data; Both does both during migration."
            ),
            required=True,
            default_value="Local HTML",
            validators=[StandardValidators.NON_EMPTY_VALIDATOR],
        )
        self.api_url = PropertyDescriptor(
            name="Reports API URL",
            description=(
                "Full Django ingestion endpoint, for example "
                "https://reports.example/api/v1/report-runs/."
            ),
            required=False,
            default_value="",
        )
        self.api_token = PropertyDescriptor(
            name="Reports API Token",
            description="Bearer token used only for authenticated report ingestion.",
            required=False,
            default_value="",
            sensitive=True,
        )
        self.api_timeout = PropertyDescriptor(
            name="Reports API Timeout Seconds",
            description="Maximum time to wait for the reports web application.",
            required=True,
            default_value="10",
            validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR],
        )
        self.descriptors = [
            self.reports_dir,
            self.flow_name,
            self.output_mode,
            self.api_url,
            self.api_token,
            self.api_timeout,
        ]

    def getPropertyDescriptors(self):
        return self.descriptors

    # -----------------------------------------------------------------------

    # Footer sections that take a full row of their own (their tables are too
    # wide to share a 340px flex slot with a neighbour).
    _WIDE_SECTIONS = {"qaoa_attrs", "vqe_attrs", "ae_attrs", "train_attrs"}

    @staticmethod
    def _idempotency_key(flow_name, attrs, raw):
        for key in ("uuid", "report.run_id", "assert.run_id", "test.run_id"):
            if attrs.get(key):
                return f"{key}:{attrs[key]}"
        digest = hashlib.sha256()
        digest.update(flow_name.encode("utf-8"))
        digest.update(json.dumps(attrs, sort_keys=True).encode("utf-8"))
        digest.update(raw)
        return f"sha256:{digest.hexdigest()}"

    def _submit_web_report(self, api_url, api_token, timeout, flow_name,
                           report_type, attrs, content, raw):
        if not api_url or not api_token:
            raise ValueError("Reports API URL and Reports API Token are required")
        payload = {
            "flow_name": flow_name,
            "report_type": report_type or "_default",
            "attributes": attrs,
            "status": "completed",
        }
        source_timestamp = (
            attrs.get("report.triggered_at")
            or attrs.get("run.started_at")
            or attrs.get("timestamp")
        )
        if source_timestamp:
            payload["timestamp"] = source_timestamp
        if isinstance(content, (dict, list)):
            payload["payload"] = content
        else:
            payload["raw_payload"] = raw.decode("utf-8", errors="replace")
        key = self._idempotency_key(flow_name, attrs, raw)
        request = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
                "Idempotency-Key": key,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status not in (200, 201):
                raise RuntimeError(f"reports API returned HTTP {response.status}")
            result = json.loads(response.read().decode("utf-8"))
        return result, key

    def transform(self, context, flowFile):
        reports_dir = context.getProperty(self.reports_dir).getValue()
        flow_name   = context.getProperty(self.flow_name).getValue()
        output_mode = context.getProperty(self.output_mode).getValue()

        raw     = bytes(flowFile.getContentsAsBytes())
        content = {}
        try:
            content = json.loads(raw.decode("utf-8"))
        except Exception:
            pass

        try:
            attrs = dict(flowFile.getAttributes())
        except Exception:
            attrs = {}

        report_type = attrs.get("report.type", "")
        mode = (output_mode or "Local HTML").strip().lower()
        if mode not in {"local html", "web api", "both"}:
            return FlowFileTransformResult(
                relationship="failure",
                attributes={"report.error": f"Unsupported Output Mode: {output_mode}"},
            )

        web_attributes = {}
        if mode in {"web api", "both"}:
            api_url = context.getProperty(self.api_url).getValue()
            api_token = context.getProperty(self.api_token).getValue()
            try:
                timeout = int(context.getProperty(self.api_timeout).getValue())
                result, key = self._submit_web_report(
                    api_url, api_token, timeout, flow_name, report_type,
                    attrs, content, raw,
                )
                web_attributes = {
                    "report.web_run_id": result["id"],
                    "report.idempotency_key": key,
                }
            except Exception as exc:
                self.logger.error(f"QuanifiReport: web submission failed: {exc}")
                return FlowFileTransformResult(
                    relationship="failure",
                    attributes={"report.error": f"Web submission failed: {exc}"},
                )

        if mode == "web api":
            return FlowFileTransformResult(
                relationship="success",
                attributes={
                    "report.flow_name": flow_name,
                    "report.type": report_type or "_default",
                    **web_attributes,
                },
            )

        sections    = _SECTION_REGISTRY.get(report_type, _SECTION_REGISTRY["_default"])
        data        = {"content": content, "attrs": attrs, "raw": raw}

        body_panels   = []
        footer_panels = []

        for loc, name in sections:
            renderer = getattr(self, f"_render_{name}", None)
            if renderer is None:
                self.logger.warn(f"QuanifiReport: no renderer for section '{name}' — skipped")
                continue
            try:
                inner = renderer(data)
            except Exception as exc:
                self.logger.warn(f"QuanifiReport: section '{name}' raised {exc}")
                inner = None
            if inner is None:
                continue
            # Styles are inlined (not only in the page CSS) so cards appended
            # to report files created before these rules existed still render
            # correctly — the page stylesheet is baked in at file creation.
            if loc == "body":
                body_panels.append(
                    f'<div class="panel" style="min-width:0;overflow-x:auto">{inner}</div>')
            else:
                # Wide sections get a whole footer row to themselves instead of
                # squeezing next to (and overflowing into) a neighbour panel;
                # overflow-x keeps any over-wide table scrolling inside its own
                # panel rather than invading the next one.
                basis = "1 1 100%" if name in self._WIDE_SECTIONS else "1 1 340px"
                footer_panels.append(
                    f'<div class="panel" style="flex:{basis};min-width:0;overflow-x:auto">{inner}</div>')

        header_title   = self._build_header(report_type, attrs)
        body_section   = f'<div class="run-body">{"".join(body_panels)}</div>'   if body_panels   else ""
        footer_section = (
            f'<div class="run-footer" style="flex-wrap:wrap">{"".join(footer_panels)}</div>'
            if footer_panels else ""
        )

        card = (
            f'<section class="run-card">\n'
            f'  <header class="run-header">\n'
            f'    <span class="run-title">{header_title}</span>\n'
            f'    <span class="run-time">{now()}</span>\n'
            f'  </header>\n'
            f'  {body_section}\n'
            f'  {footer_section}\n'
            f'</section>'
        )

        output_path = os.path.join(reports_dir, f"{flow_name}.html")
        os.makedirs(reports_dir, exist_ok=True)
        page = page_template(html_lib.escape(flow_name), _EXTRA_CSS)
        write_card(output_path, card, page)

        return FlowFileTransformResult(
            relationship="success",
            attributes={
                "report.path":      output_path,
                "report.flow_name": flow_name,
                "report.type":      report_type or "_default",
                **web_attributes,
            },
        )

    # -----------------------------------------------------------------------
    # Header builder

    _VERDICT_BADGES = {
        "PASS":     '<span class="badge badge-agree">&#x2713; PASS</span>',
        "FAIL":     '<span class="badge badge-differ">&#x2717; FAIL</span>',
        "DISAGREE": '<span class="badge badge-noise">&#x26A0; DISAGREE</span>',
    }

    def _build_header(self, report_type, attrs):
        if report_type == "consensus":
            parts = []
            case_id = attrs.get("assert.case_id") or attrs.get("test.case_id", "")
            if case_id:
                parts.append(html_lib.escape(case_id))
            target = attrs.get("circuit.marked_state", "")
            if target:
                parts.append(f"target |{html_lib.escape(target)}&#x27E9;")
            branches = attrs.get("consensus.branches", "")
            if branches:
                parts.append(f"{html_lib.escape(branches)} branch(es)")
            if attrs.get("mut.applied") == "true":
                op = attrs.get("mut.operator", "?")
                parts.append(
                    f'<span class="badge badge-noise">mutant: {html_lib.escape(op)}</span>')
            verdict = attrs.get("assert.verdict", "")
            badge = self._VERDICT_BADGES.get(verdict)
            if badge:
                parts.append(badge)
            elif verdict:
                parts.append(html_lib.escape(verdict))
            return " &middot; ".join(parts) if parts else "consensus"

        if report_type == "comparison":
            fa    = attrs.get("compare.framework_a", "A")
            fb    = attrs.get("compare.framework_b", "B")
            h     = attrs.get("compare.hellinger_distance", "")
            agree = attrs.get("compare.agreement", "false") == "true"
            badge = (
                '<span class="badge badge-agree">&#x2713; agree</span>' if agree
                else '<span class="badge badge-differ">&#x2717; differ</span>'
            )
            h_part = f" &nbsp;&middot;&nbsp; H&nbsp;=&nbsp;{h}" if h else ""
            return (
                f'<span class="tag-a">{html_lib.escape(fa)}</span>'
                f' &nbsp;vs&nbsp; '
                f'<span class="tag-b">{html_lib.escape(fb)}</span>'
                f'{h_part} &nbsp;&middot;&nbsp; {badge}'
            )

        parts = []
        for key, fmt in [
            ("circuit.marked_state",   lambda v: f"target |{html_lib.escape(v)}&#x27E9;"),
            ("circuit.num_qubits",     lambda v: f"{html_lib.escape(v)} qubits"),
            ("circuit.num_iterations", lambda v: f"{html_lib.escape(v)} iteration(s)"),
            ("circuit.depth",          lambda v: f"depth {html_lib.escape(v)}"),
            ("sim.simulator",          lambda v: html_lib.escape(v)),
        ]:
            val = attrs.get(key, "")
            if val:
                parts.append(fmt(val))

        noise = attrs.get("sim.noise_model", "")
        if noise and noise != "none":
            parts.append(
                f'<span class="badge badge-noise">noisy: {html_lib.escape(noise)}</span>'
            )

        return " &middot; ".join(parts) if parts else html_lib.escape(report_type or "report")

    # -----------------------------------------------------------------------
    # Section renderers — return None to skip the panel entirely

    @staticmethod
    def _diagram_panel(svg_html, attrs, large):
        """Wrap a diagram in a height-capped scroll box; very large circuits
        start collapsed behind a <details> click so they don't dominate the
        card (the drawing is still fully scrollable once expanded)."""
        scroll = (
            "<div class='svg-scroll' style='overflow:auto;max-width:100%;"
            f"max-height:520px'>{svg_html}</div>"
        )
        if not large:
            return f"<h3>Circuit Diagram</h3>{scroll}"
        depth = attrs.get("circuit.depth", "?")
        gates = attrs.get("circuit.gate_count", "?")
        return (
            "<h3>Circuit Diagram</h3>"
            "<details><summary style='cursor:pointer;color:#0969da'>"
            f"Large circuit (depth {html_lib.escape(str(depth))}, "
            f"{html_lib.escape(str(gates))} gates) — click to expand"
            f"</summary>{scroll}</details>"
        )

    def _render_circuit_diagram(self, data):
        # Prefer a pre-rendered SVG (Cirq builders emit circuit.svg directly).
        svg_attr = data["attrs"].get("circuit.svg", "")
        if svg_attr:
            start = svg_attr.find("<svg")
            svg_html = svg_attr[start:] if start >= 0 else svg_attr
            return self._diagram_panel(svg_html, data["attrs"],
                                       large=len(svg_html) > 120_000)

        # Otherwise render from the QASM the builder published. Builders set
        # circuit.qasm3 or circuit.qasm2 according to their Output Format, so
        # both dialects must be handled or a qasm2 flow silently loses its
        # diagram (and the ASCII circuit.diagram is the last-resort fallback).
        qasm_code, dialect = _qasm_source(data["attrs"])
        if not qasm_code:
            ascii_diagram = data["attrs"].get("circuit.diagram", "")
            if not ascii_diagram:
                return None
            return (
                "<h3>Circuit Diagram</h3>"
                "<pre class='code-block' style='max-height:520px;overflow:auto'>"
                f"{html_lib.escape(ascii_diagram)}</pre>"
            )
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        if dialect == "qasm3":
            from qiskit import qasm3 as qasm_module
        else:
            from qiskit import qasm2 as qasm_module
        large = False
        try:
            if dialect == "qasm3":
                circuit = qasm_module.loads(qasm_code)
            else:
                circuit = qasm_module.loads(
                    qasm_code,
                    custom_instructions=qasm_module.LEGACY_CUSTOM_INSTRUCTIONS,
                )
            large    = len(circuit.data) > 80
            # fold the drawing into stacked rows instead of one endless
            # horizontal strip (fold=-1) — long circuits grow downward and
            # stay inside the height-capped scroll box.
            fig      = circuit.draw("mpl", fold=40, style={"backgroundcolor": "#ffffff"})
            buf      = io.BytesIO()
            fig.savefig(buf, format="svg", bbox_inches="tight", facecolor="white")
            plt.close(fig)
            raw_svg  = buf.getvalue().decode("utf-8")
            start    = raw_svg.find("<svg")
            svg_html = raw_svg[start:] if start >= 0 else raw_svg
        except Exception as exc:
            svg_html = f"<pre>Render error: {html_lib.escape(str(exc))}</pre>"
        return self._diagram_panel(svg_html, data["attrs"], large=large)

    def _render_qasm_source(self, data):
        qasm_code, dialect = _qasm_source(data["attrs"])
        if not qasm_code:
            return None
        heading = "OpenQASM 3" if dialect == "qasm3" else "OpenQASM 2"
        # Height-capped and scrollable so a long program can't stretch the
        # card; long listings additionally start collapsed behind a click.
        pre = (
            "<pre class='code-block' style='max-height:420px;overflow:auto'>"
            f"{html_lib.escape(qasm_code)}</pre>"
        )
        lines = qasm_code.count("\n") + 1
        if lines <= 30:
            return f"<h3>{heading}</h3>{pre}"
        return (
            f"<h3>{heading}</h3>"
            "<details><summary style='cursor:pointer;color:#0969da'>"
            f"{lines} lines — click to expand</summary>{pre}</details>"
        )

    def _render_counts_chart(self, data):
        counts = data["content"]
        if not isinstance(counts, dict) or not counts:
            return None
        bars = bar_rows(counts, "bar-fill")
        return f"<h3>Measurement Counts</h3><div class='bar-chart'>{bars}</div>"

    def _render_circuit_attrs(self, data):
        rows = [
            (k, v) for k, v in sorted(data["attrs"].items())
            if k.startswith("circuit.") and k not in (
                "circuit.qasm3", "circuit.qasm2", "circuit.diagram", "circuit.svg"
            )
        ]
        if not rows:
            return None
        trs = "".join(
            f"<tr><td>{k}</td><td>{html_lib.escape(str(v))}</td></tr>"
            for k, v in rows
        )
        return (
            f"<h3>Circuit Attributes</h3>"
            f"<table><tr><th>Key</th><th>Value</th></tr>{trs}</table>"
        )

    def _render_noise_model(self, data):
        """Dedicated panel summarising the noise model; skipped for ideal runs."""
        attrs = data["attrs"]
        model = attrs.get("sim.noise_model", "")
        if not model or model == "none":
            return None

        try:
            params = json.loads(attrs.get("sim.noise_params", "") or "{}")
        except Exception:
            params = {}

        trs = f"<tr><td>Model</td><td class='metric-val'>{html_lib.escape(model)}</td></tr>"
        for key, val in params.items():
            label = _NOISE_PARAM_LABELS.get(key, key)
            trs += (
                f"<tr><td>{html_lib.escape(label)}</td>"
                f"<td class='metric-val'>{html_lib.escape(str(val))}</td></tr>"
            )
        return (
            f"<h3>Noise Model</h3>"
            f"<table><tr><th>Parameter</th><th>Value</th></tr>{trs}</table>"
        )

    def _render_sim_attrs(self, data):
        rows = [
            (k, v) for k, v in sorted(data["attrs"].items())
            if k.startswith("sim.")
            and k not in ("sim.statevector_data", "sim.noise_model", "sim.noise_params")
        ]
        if not rows:
            return None
        trs = "".join(
            f"<tr><td>{k}</td><td>{html_lib.escape(str(v))}</td></tr>"
            for k, v in rows
        )
        return (
            f"<h3>Simulation Attributes</h3>"
            f"<table><tr><th>Key</th><th>Value</th></tr>{trs}</table>"
        )

    def _render_hardware_attrs(self, data):
        """Surfaces hw.* attributes for runs that executed on a real device.

        Framework- and vendor-agnostic: every hardware gateway (BraketDevice,
        QiskitRuntimeSampler, QrispIQMDevice, IQMJobPoller) already emits the
        same hw.* prefix, so one panel serves all of them. Returns None when no
        hw.* keys are present, leaving pure-simulation cards untouched.
        """
        rows = [
            (k, v) for k, v in sorted(data["attrs"].items())
            if k.startswith("hw.") and k != "hw.error"
        ]
        if not rows:
            return None
        trs = "".join(
            f"<tr><td>{k}</td><td>{html_lib.escape(str(v))}</td></tr>"
            for k, v in rows
        )
        return (
            f"<h3>Hardware Execution</h3>"
            f"<table><tr><th>Key</th><th>Value</th></tr>{trs}</table>"
        )

    def _render_derived_result(self, data):
        """Generic decoder for Sampler-algorithm headline results.

        A circuit builder that knows its own readout convention (e.g.
        QiskitPhaseEstimation) emits a *declarative* hint and this one renderer
        formats it — keeping the report a pure formatter with no per-algorithm
        or per-framework decoding logic:

          result.decode        — kind of decode ("phase" today)
          result.bit_positions — comma-separated indices into sim.top_result,
                                  MSB→LSB; the builder computes these from its
                                  circuit layout + its framework's measured-string
                                  convention, so the report never branches on it.
          result.label         — optional display label.

        Returns None (panel skipped) when no result.decode hint is present, so
        plain simulation/VQE cards are unaffected.
        """
        attrs = data["attrs"]
        kind  = attrs.get("result.decode")
        if not kind:
            return None
        top           = attrs.get("sim.top_result")
        positions_raw = attrs.get("result.bit_positions")
        if not top or not positions_raw:
            return None
        try:
            positions = [int(p) for p in positions_raw.split(",")]
            bits      = "".join(top[p] for p in positions)
            value     = int(bits, 2)
        except (ValueError, IndexError):
            return None

        label = attrs.get("result.label", "Decoded value")
        if kind == "phase":
            from fractions import Fraction
            m     = len(positions)
            frac  = Fraction(value, 2 ** m)
            value_html = (
                f"{value / (2 ** m):.4f} "
                f"(= {frac.numerator}/{frac.denominator})"
            )
        else:
            value_html = str(value)

        return (
            f"<h3>Decoded Result</h3>"
            f"<table><tr><th>Key</th><th>Value</th></tr>"
            f"<tr><td>{html_lib.escape(label)}</td><td>{value_html}</td></tr>"
            f"<tr><td>Readout bits (MSB→LSB)</td><td>{html_lib.escape(bits)}</td></tr>"
            f"</table>"
        )

    def _render_vqe_attrs(self, data):
        """Surfaces vqe.* result attributes (optimal energy, convergence, …).

        Only rendered when the FlowFile actually carries vqe.* attributes —
        i.e. for VQE runs — so plain simulation/expectation cards stay as-is.
        """
        rows = [
            (k, v) for k, v in sorted(data["attrs"].items())
            if k.startswith("vqe.")
        ]
        if not rows:
            return None
        trs = "".join(
            f"<tr><td>{k}</td><td>{html_lib.escape(str(v))}</td></tr>"
            for k, v in rows
        )
        return (
            f"<h3>VQE Result</h3>"
            f"<table><tr><th>Key</th><th>Value</th></tr>{trs}</table>"
        )

    @staticmethod
    def _bitstring_with_ruler(bits):
        """A bitstring annotated with its qubit orientation, kept strictly
        inline (no block elements) so it cannot disturb the table row layout.
        Falls back to plain text for non-binary values."""
        if not bits or any(c not in "01" for c in bits):
            return html_lib.escape(str(bits))
        return (
            "<span style='font-family:monospace;letter-spacing:0.15em'>{}</span>"
            "&nbsp;<span style='color:#999;font-size:0.85em;white-space:nowrap'>"
            "(q0&thinsp;&rarr;&thinsp;q{})</span>".format(bits, len(bits) - 1)
        )

    def _render_qaoa_attrs(self, data):
        """Surfaces qaoa.* result attributes (optimal value, best bitstring,
        approximation ratio, …).

        Only rendered when the FlowFile actually carries qaoa.* attributes —
        i.e. for QAOA runs — so plain simulation cards stay as-is.
        """
        rows = [
            (k, v) for k, v in sorted(data["attrs"].items())
            if k.startswith("qaoa.")
        ]
        if not rows:
            return None
        trs = "".join(
            "<tr><td>{}</td><td>{}</td></tr>".format(
                k,
                self._bitstring_with_ruler(str(v)) if k == "qaoa.best_measurement"
                else html_lib.escape(str(v)),
            )
            for k, v in rows
        )
        caption = (
            "<p style='color:#999;font-size:0.85em;margin:4px 0 0'>"
            "Bitstrings read left&nbsp;&rarr;&nbsp;right: character <i>i</i> is "
            "qubit/node <i>i</i> (<code>sim.bit_order = q0_left</code>)."
            "</p>"
        )
        return (
            f"<h3>QAOA Result</h3>"
            f"<table><tr><th>Key</th><th>Value</th></tr>{trs}</table>"
            f"{caption}"
        )

    def _render_loss_curve(self, data):
        """Inline-SVG loss curve for training runs (report.type=training).

        Reads content["loss_history"] (list of floats, one per epoch); returns
        None when absent so non-training cards are unaffected.
        """
        history = (data.get("content") or {}).get("loss_history")
        if not isinstance(history, list) or len(history) < 2:
            return None
        try:
            ys = [float(v) for v in history]
        except (TypeError, ValueError):
            return None

        w, h, pad = 560, 210, 34
        lo, hi = min(ys), max(ys)
        span = (hi - lo) or 1.0
        pts = []
        for i, v in enumerate(ys):
            px = pad + (w - 2 * pad) * i / (len(ys) - 1)
            py = h - pad - (h - 2 * pad) * (v - lo) / span
            pts.append("{:.1f},{:.1f}".format(px, py))
        svg = (
            "<svg viewBox='0 0 {w} {h}' width='100%' role='img'>"
            "<line x1='{pad}' y1='{ybase}' x2='{xmax}' y2='{ybase}' stroke='#d8dee4'/>"
            "<line x1='{pad}' y1='{pad}' x2='{pad}' y2='{ybase}' stroke='#d8dee4'/>"
            "<polyline points='{pts}' fill='none' stroke='#0969da' stroke-width='2'/>"
            "<text x='{pad}' y='{toptxt}' fill='#656d76' font-size='11'>max {hi:.4f}</text>"
            "<text x='{pad}' y='{bottxt}' fill='#656d76' font-size='11'>min {lo:.4f}</text>"
            "<text x='{xmax}' y='{bottxt}' fill='#656d76' font-size='11' "
            "text-anchor='end'>epoch {n}</text>"
            "</svg>"
        ).format(w=w, h=h, pad=pad, ybase=h - pad, xmax=w - pad,
                 pts=" ".join(pts), hi=hi, lo=lo, n=len(ys),
                 toptxt=pad - 8, bottxt=h - pad + 16)
        return "<h3>Training Loss</h3><div class='svg-scroll'>{}</div>".format(svg)

    def _render_train_attrs(self, data):
        """Surfaces train.* result attributes (final loss, accuracies, model).

        Only rendered when the FlowFile actually carries train.* attributes —
        i.e. for training runs — so other cards stay as-is.
        """
        rows = [
            (k, v) for k, v in sorted(data["attrs"].items())
            if k.startswith("train.")
        ]
        if not rows:
            return None
        trs = "".join(
            f"<tr><td>{k}</td><td>{html_lib.escape(str(v))}</td></tr>"
            for k, v in rows
        )
        return (
            f"<h3>Training Result</h3>"
            f"<table><tr><th>Key</th><th>Value</th></tr>{trs}</table>"
        )

    def _render_ae_attrs(self, data):
        """Surfaces ae.* result attributes (amplitude estimate, MLE, method, …).

        Only rendered when the FlowFile actually carries ae.* attributes —
        i.e. for amplitude-estimation runs — so plain simulation cards stay as-is.
        """
        rows = [
            (k, v) for k, v in sorted(data["attrs"].items())
            if k.startswith("ae.")
        ]
        if not rows:
            return None
        trs = "".join(
            f"<tr><td>{k}</td><td>{html_lib.escape(str(v))}</td></tr>"
            for k, v in rows
        )
        return (
            f"<h3>Amplitude Estimation Result</h3>"
            f"<table><tr><th>Key</th><th>Value</th></tr>{trs}</table>"
        )

    def _render_consensus_verdict(self, data):
        """Verdict panel for QuantumConsensusOracle output (report.type=consensus).

        Skipped when the FlowFile carries no assert.verdict — e.g. if a plain
        simulation FlowFile is mislabeled — so the card degrades gracefully.
        """
        attrs   = data["attrs"]
        verdict = attrs.get("assert.verdict", "")
        if not verdict:
            return None

        badge = self._VERDICT_BADGES.get(
            verdict, html_lib.escape(verdict))
        rows = [("Verdict", badge)]

        reason = attrs.get("assert.reason", "")
        if reason:
            rows.append(("Reason", html_lib.escape(reason)))
        expected = attrs.get("consensus.expected", "")
        if expected:
            rows.append(("Expected", f"|{html_lib.escape(expected)}&#x27E9;"))
        majority = attrs.get("consensus.majority_top", "")
        if majority:
            count    = attrs.get("consensus.majority_count", "?")
            branches = attrs.get("consensus.branches", "?")
            rows.append(("Majority", f"|{html_lib.escape(majority)}&#x27E9; "
                                     f"({html_lib.escape(count)}/{html_lib.escape(branches)} branches)"))
        dissenters = attrs.get("consensus.dissenters", "")
        rows.append(("Dissenters", html_lib.escape(dissenters) if dissenters else "none"))
        max_h = attrs.get("consensus.max_hellinger", "")
        if max_h:
            rows.append(("Max Hellinger between branches", html_lib.escape(max_h)))
        case_id = attrs.get("assert.case_id") or attrs.get("test.case_id", "")
        run_id  = attrs.get("assert.run_id") or attrs.get("test.run_id", "")
        if case_id or run_id:
            rows.append(("Case / Run",
                         f"{html_lib.escape(case_id) or '?'} / {html_lib.escape(run_id) or '?'}"))

        trs = "".join(
            f"<tr><td>{label}</td><td class='metric-val'>{value}</td></tr>"
            for label, value in rows
        )
        return (
            f"<h3>Consensus Verdict</h3>"
            f"<table><tr><th>Field</th><th>Value</th></tr>{trs}</table>"
        )

    def _render_consensus_branches(self, data):
        """Table of individual branches for QuantumConsensusOracle output.

        Reads consensus.branches_json (list of {label, top, dissent}). If
        absent, degrades cleanly by returning None.
        """
        raw_json = data["attrs"].get("consensus.branches_json", "")
        if not raw_json:
            return None
        try:
            branches = json.loads(raw_json)
        except Exception:
            return None
        if not branches:
            return None

        trs = ""
        for b in branches:
            label = b.get("label", "")
            top = b.get("top", "")
            is_d = b.get("dissent", False)
            badge = '<span class="badge badge-differ">&#x2717; dissents</span>' if is_d else '<span class="badge badge-agree">&#x2713; majority</span>'
            val_style = "color:#cf222e" if is_d else "color:#1a7f37"
            top_str = f"|{html_lib.escape(top)}&#x27E9;" if top else "—"
            trs += (
                f"<tr><td><code>{html_lib.escape(label)}</code></td>"
                f"<td style='font-family:monospace;{val_style}'>{top_str}</td>"
                f"<td>{badge}</td></tr>\n"
            )

        return (
            f"<h3>Consensus Branches</h3>"
            f"<div style='max-height:360px;overflow-y:auto'>"
            f"<table><thead><tr><th>Branch</th><th>Top Outcome</th><th>Status</th></tr></thead>"
            f"<tbody>{trs}</tbody></table></div>"
        )

    def _render_mutation_attrs(self, data):
        """Surfaces mut.* bookkeeping (operator, mutated value, lineage).

        Only rendered for mutation-pipeline FlowFiles that carry mut.*
        attributes; control rows in unmutated runs and plain consensus cards
        skip it.
        """
        rows = [
            (k, v) for k, v in sorted(data["attrs"].items())
            if k.startswith("mut.")
        ]
        if not rows:
            return None
        trs = "".join(
            f"<tr><td>{k}</td><td>{html_lib.escape(str(v))}</td></tr>"
            for k, v in rows
        )
        return (
            f"<h3>Mutation</h3>"
            f"<table><tr><th>Key</th><th>Value</th></tr>{trs}</table>"
        )

    def _render_statevector_table(self, data):
        sv_json = data["attrs"].get("sim.statevector_data", "")
        if not sv_json:
            return None
        try:
            state_data = json.loads(sv_json)
        except Exception:
            return None
        if not state_data:
            return None

        sv_rows = ""
        for s in state_data:
            pct   = s["prob"] * 100
            amp   = _fmt_amplitude(s["amp_real"], s["amp_imag"])
            color = _phase_color(s["phase_deg"])
            sv_rows += (
                f'<tr>'
                f'<td class="sv-state">|{html_lib.escape(s["state"])}&#x27E9;</td>'
                f'<td class="sv-prob">'
                f'<div class="sv-bar-row">'
                f'<div class="sv-bar-track">'
                f'<div class="sv-bar-fill" style="width:{pct:.1f}%"></div>'
                f'</div>'
                f'<span class="sv-pct">{pct:.1f}%</span>'
                f'</div></td>'
                f'<td class="sv-amp">{html_lib.escape(amp)}</td>'
                f'<td class="sv-phase" style="color:{color}">{s["phase_deg"]:.1f}\xb0</td>'
                f'</tr>\n'
            )

        return (
            f"<h3>Statevector &mdash; amplitude &amp; phase</h3>"
            f"<table class='sv-table'>"
            f"<tr><th>State</th><th>Probability</th><th>Amplitude</th><th>Phase</th></tr>"
            f"{sv_rows}"
            f"</table>"
        )

    def _render_comparison_metrics(self, data):
        attrs = data["attrs"]
        fa    = attrs.get("compare.framework_a", "A")
        fb    = attrs.get("compare.framework_b", "B")
        h     = attrs.get("compare.hellinger_distance", "?")
        tv    = attrs.get("compare.total_variation", "?")
        fid   = attrs.get("compare.fidelity", "?")
        top_a = attrs.get("compare.top_result_a", "?")
        top_b = attrs.get("compare.top_result_b", "?")
        agree = attrs.get("compare.agreement", "false") == "true"
        badge = (
            '<span class="badge badge-agree">&#x2713; agree</span>' if agree
            else '<span class="badge badge-differ">&#x2717; differ</span>'
        )
        trs = (
            f"<tr><td>Hellinger Distance</td>"
            f"<td class='metric-val'>{h}</td>"
            f"<td>0 = identical &nbsp;/&nbsp; 1 = disjoint</td></tr>"
            f"<tr><td>Total Variation</td>"
            f"<td class='metric-val'>{tv}</td>"
            f"<td>half the L1 distance between distributions</td></tr>"
            f"<tr><td>Fidelity</td>"
            f"<td class='metric-val'>{fid}</td>"
            f"<td>1 = identical &nbsp;/&nbsp; 0 = disjoint</td></tr>"
            f"<tr><td>Top result</td>"
            f"<td class='metric-val'>"
            f"<span class='tag-a'>|{html_lib.escape(top_a)}&#x27E9;</span>"
            f" / <span class='tag-b'>|{html_lib.escape(top_b)}&#x27E9;</span>"
            f"</td><td>{badge}</td></tr>"
        )
        return (
            f"<h3>Distance Metrics</h3>"
            f"<table><tr><th>Metric</th><th>Value</th><th>Interpretation</th></tr>{trs}</table>"
        )

    def _render_dual_distribution_chart(self, data):
        content = data["content"]
        if not isinstance(content, dict):
            return None
        dists = content.get("distributions", {})
        if len(dists) < 2:
            return None

        attrs   = data["attrs"]
        fa      = attrs.get("compare.framework_a", "")
        fb      = attrs.get("compare.framework_b", "")
        keys    = list(dists.keys())
        dist_a  = dists.get(fa) if fa else dists.get(keys[0], {})
        dist_b  = dists.get(fb) if fb else dists.get(keys[1], {})
        label_a = fa or keys[0]
        label_b = fb or keys[1]

        bars_a = bar_rows(dist_a, "bar-fill-a")
        bars_b = bar_rows(dist_b, "bar-fill-b")

        return (
            f"<div style='display:flex;gap:0'>"
            f"<div style='flex:1;padding-right:20px;border-right:1px solid #d0d7de'>"
            f"<h3><span class='tag-a'>{html_lib.escape(label_a)}</span></h3>"
            f"<div class='bar-chart'>{bars_a}</div>"
            f"</div>"
            f"<div style='flex:1;padding-left:20px'>"
            f"<h3><span class='tag-b'>{html_lib.escape(label_b)}</span></h3>"
            f"<div class='bar-chart'>{bars_b}</div>"
            f"</div>"
            f"</div>"
        )

    def _render_raw_content(self, data):
        try:
            text = data["raw"].decode("utf-8")
        except Exception:
            text = repr(data["raw"])
        return (
            f"<h3>Content</h3>"
            f"<pre class='code-block'>{html_lib.escape(text)}</pre>"
        )

    def _render_all_attrs(self, data):
        rows = sorted(data["attrs"].items())
        if not rows:
            return None
        trs = "".join(
            f"<tr><td>{k}</td><td>{html_lib.escape(str(v))}</td></tr>"
            for k, v in rows
        )
        return (
            f"<h3>All Attributes</h3>"
            f"<table><tr><th>Key</th><th>Value</th></tr>{trs}</table>"
        )
