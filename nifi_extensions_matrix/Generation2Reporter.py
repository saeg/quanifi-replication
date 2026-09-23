"""Write Generation-2 evaluation records in JSON, CSV, Markdown and HTML.

The HTML report carries a **traceability** section: one row per circuit the
quantum computer actually ran, and a chart of what came back against what the
arithmetic says should have come back. A report that shows only conclusions
cannot be reviewed -- a coauthor has to be able to go from "cdkm passed 3+1"
back to the 512 shots that claim rests on.
"""
import csv
import html
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from generation2.core import RESPONSE_FIELDS, metrics, response_rows  # noqa: E402

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import PropertyDescriptor, StandardValidators, ExpressionLanguageScope
from nifiapi.relationship import Relationship
from nifiapi.__jvm__ import JvmHolder


#: Qualification and calibration do not produce localization records; they
#: produce a per-version eligibility verdict. Reporting them through the
#: validation shape silently rendered `n: 0, records: []` and destroyed the only
#: copy of the voter table -- the evidence for an infeasibility claim. On
#: 2026-08-29 the IQM calibration verdict survived only because `rejected` goes
#: to a funnel; the accepted IBM one was lost.
ELIGIBILITY_SCHEMAS = ("quanifi-generation2-qualification-v1",
                       "quanifi-generation2-calibration-v1")

PAGE_CSS = """
:root{--bg:#eef1f3;--card:#fff;--sunk:#e4e9ec;--ink:#12181d;--muted:#5b6771;
 --rule:#d3dbe0;--copper:#a85d22;--teal:#0e6e78;--good:#2e6b3e;--bad:#8c2f2a;
 --good-soft:#e6f0e8;--bad-soft:#f7e9e8;--sel:#dbe7ea;
 --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
 --sans:system-ui,-apple-system,"Segoe UI",sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#0f1418;--card:#161d22;--sunk:#1c242a;
 --ink:#dfe6ea;--muted:#8b9aa4;--rule:#252f36;--copper:#d8873f;--teal:#3fa0ac;
 --good:#7fb98c;--bad:#e09a95;--good-soft:#16241a;--bad-soft:#2a1715;--sel:#1d2f33}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
 font-size:15px;line-height:1.55}
.wrap{max-width:74rem;margin:0 auto;padding:2.5rem 1.25rem 5rem}
h1{font-size:1.7rem;margin:0 0 .3rem;letter-spacing:-.01em}
h2{font-size:1.15rem;margin:2.6rem 0 .4rem;letter-spacing:-.005em}
.sub{color:var(--muted);margin:0 0 1.4rem}
.badge{display:inline-block;font-size:.7rem;font-weight:700;letter-spacing:.09em;
 text-transform:uppercase;padding:.2rem .6rem;border-radius:999px}
.badge.ok{background:var(--good-soft);color:var(--good)}
.badge.no{background:var(--bad-soft);color:var(--bad)}
.card{background:var(--card);border:1px solid var(--rule);border-radius:12px;
 padding:1.1rem 1.25rem;margin:1rem 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.9rem;
 font-variant-numeric:tabular-nums}
th{text-align:left;font-size:.68rem;letter-spacing:.07em;text-transform:uppercase;
 color:var(--muted);padding:0 .7rem .45rem 0;border-bottom:1px solid var(--rule);
 white-space:nowrap}
td{padding:.4rem .7rem .4rem 0;border-bottom:1px solid var(--rule);white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
code,.mono{font-family:var(--mono);font-size:.92em}
.rows tbody tr{cursor:pointer}
.rows tbody tr:hover{background:var(--sunk)}
.rows tbody tr[aria-selected="true"]{background:var(--sel);
 box-shadow:inset 3px 0 0 var(--teal)}
.rows tbody tr:focus-visible{outline:2px solid var(--copper);outline-offset:-2px}
.hit{color:var(--good);font-weight:600}
.miss{color:var(--bad);font-weight:600}
.split{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr);gap:1rem;
 align-items:start}
@media (max-width:900px){.split{grid-template-columns:1fr}}
.detail h3{margin:0 0 .1rem;font-size:1rem;font-family:var(--mono)}
.detail .why{color:var(--muted);font-size:.86rem;margin:0 0 .9rem}
.detail .lead{margin:-.5rem 0 .5rem;font-variant-numeric:tabular-nums}
.kv{display:grid;grid-template-columns:auto 1fr;gap:.2rem .9rem;font-size:.86rem;
 margin:.9rem 0 0}
.kv dt{color:var(--muted)}
.kv dd{margin:0;font-family:var(--mono)}
.legend{display:flex;gap:1rem;flex-wrap:wrap;font-size:.78rem;color:var(--muted);
 margin:.6rem 0 0}
.legend i{display:inline-block;width:.75rem;height:.75rem;border-radius:2px;
 margin-right:.35rem;vertical-align:-.05em}
svg{display:block;width:100%;height:auto}
svg text{font-family:var(--mono);font-size:10px;fill:var(--muted)}
details{margin-top:2.5rem}
summary{cursor:pointer;color:var(--muted);font-size:.85rem}
pre{background:var(--sunk);padding:1rem;border-radius:10px;overflow-x:auto;
 font-family:var(--mono);font-size:.8rem}
.note{border-left:3px solid var(--copper);background:var(--card);
 border-radius:8px;padding:.7rem .95rem;font-size:.87rem;color:var(--muted)}
"""

PAGE_JS = """
(function(){
 var DATA = JSON.parse(document.getElementById("responses").textContent);
 var panel = document.getElementById("detail");
 var rows = Array.prototype.slice.call(
   document.querySelectorAll(".rows tbody tr[data-i]"));
 function esc(s){return String(s).replace(/[&<>]/g,function(c){
   return {"&":"&amp;","<":"&lt;",">":"&gt;"}[c];});}
 function chart(r){
   var counts = r.counts || {}, keys = Object.keys(counts);
   if (r.expected_bits && keys.indexOf(r.expected_bits) < 0) keys.push(r.expected_bits);
   keys.sort();
   var shots = r.shots || keys.reduce(function(a,k){return a+(counts[k]||0);},0) || 1;
   var W = 460, H = 210, base = 160, top = 130, x0 = 34;
   var step = Math.min(52, (W - x0 - 12) / Math.max(keys.length,1));
   var bw = Math.max(10, step - 12), out = [];
   // The verdict is a comparison, not a single height: a mode that wins by 464
   // shots and one that wins by 12 look alike until the gap is drawn.
   var ranked = keys.map(function(k){return {k:k, n:counts[k]||0};})
                    .sort(function(a,b){return b.n - a.n;});
   var leader = ranked[0], runnerUp = ranked[1], leaderX = null;
   function barTop(n){return base - (n ? Math.max(2, n/shots*top) : 0);}
   out.push('<line x1="'+x0+'" y1="'+(base+.5)+'" x2="'+(x0+step*keys.length)+
            '" y2="'+(base+.5)+'" stroke="var(--rule)"/>');
   [0,.5,1].forEach(function(f){
     var y = base - f*top;
     out.push('<line x1="'+x0+'" y1="'+y+'" x2="'+(x0+step*keys.length)+'" y2="'+y+
              '" stroke="var(--rule)" stroke-dasharray="2 4" opacity=".7"/>');
     out.push('<text x="'+(x0-6)+'" y="'+(y+3)+'" text-anchor="end">'+
              Math.round(f*shots)+'</text>');
   });
   keys.forEach(function(k,i){
     var n = counts[k]||0, h = n ? Math.max(2, n/shots*top) : 0;
     var x = x0 + i*step + (step-bw)/2, hit = (k === r.expected_bits);
     if (hit) out.push('<rect x="'+(x-2)+'" y="'+(base-top)+'" width="'+(bw+4)+
                       '" height="'+top+'" fill="none" stroke="var(--teal)" '+
                       'stroke-dasharray="3 3" rx="3"/>');
     if (h) out.push('<rect x="'+x+'" y="'+(base-h)+'" width="'+bw+'" height="'+h+
                     '" rx="2" fill="'+(hit?"var(--teal)":"var(--copper)")+'"/>');
     if (n) out.push('<text x="'+(x+bw/2)+'" y="'+(base-h-5)+'" text-anchor="middle" '+
                     'fill="var(--ink)">'+n+'</text>');
     out.push('<text x="'+(x+bw/2)+'" y="'+(base+15)+'" text-anchor="middle"'+
              (hit?' fill="var(--ink)" font-weight="600"':'')+'>'+esc(k)+'</text>');
     if (leader && k === leader.k) leaderX = x + bw;
   });
   // The lead marker: a capped span from the leading bar down to the runner-up,
   // labelled with the shots between them. Drawn last so it sits over the bars.
   if (leader && leader.n && runnerUp && leaderX !== null) {
     var yTop = barTop(leader.n), yRun = barTop(runnerUp.n);
     var mx = Math.min(leaderX + 5, x0 + step*keys.length - 2);
     out.push('<path d="M'+(mx-3)+' '+yTop+' H'+(mx+3)+' M'+mx+' '+yTop+' V'+yRun+
              ' M'+(mx-3)+' '+yRun+' H'+(mx+3)+'" stroke="var(--muted)" '+
              'fill="none" stroke-width="1"/>');
     out.push('<text x="'+(mx+6)+'" y="'+((yTop+yRun)/2+3)+'" '+
              'fill="var(--muted)">'+(leader.n - runnerUp.n)+'</text>');
   }
   out.push('<text x="'+x0+'" y="'+(base+34)+'">result register, little-endian'+
            '</text>');
   return '<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="counts per '+
          'outcome for '+esc(r.case)+' '+esc(r.version)+'">'+out.join("")+'</svg>';
 }
 function num(v,d){return (v===null||v===undefined)?"&mdash;":
   (typeof v==="number" ? (Number.isInteger(v)?v:v.toFixed(d===undefined?3:d)) : esc(v));}
 function show(i){
   var r = DATA[i];
   rows.forEach(function(tr){tr.setAttribute("aria-selected",
     tr.getAttribute("data-i")===String(i)?"true":"false");});
   var why = r.expected_bits
     ? ('dashed outline = the ideal spike on <b>'+esc(r.expected_bits)+
        '</b>, the answer with no noise and no fault')
     : 'the fault-free answer is still under seal for this artifact';
   // The circuit label IS the name this circuit was submitted under, so it
   // heads the panel: it is the string to grep the job manifest for.
   var named = r.circuit_label ||
               (String(r.case||"?")+" · "+String(r.version||"?"));
   var lead = (r.margin_counts===null||r.margin_counts===undefined) ? ""
     : '<p class="why lead">leads by '+num(r.margin_counts)+' shot'+
       (Math.abs(r.margin_counts)===1?"":"s")+'</p>';
   panel.innerHTML =
     '<h3>'+esc(named)+'</h3>'+ lead +
     '<p class="why">'+esc(String(r.case||""))+' &middot; '+
       esc(String(r.version||""))+' &middot; '+
       esc(String(r.mutation_id||"clean"))+
       ' &middot; '+esc(String(r.treatment||""))+'</p>'+
     chart(r)+
     '<p class="legend"><span><i style="background:var(--teal)"></i>expected outcome'+
       '</span><span><i style="background:var(--copper)"></i>everything else</span>'+
       '<span>'+why+'</span></p>'+
     '<dl class="kv">'+
       '<dt>circuit</dt><dd>'+(r.circuit_label
         ? esc(r.circuit_label)
         : "&mdash; sealed until the truth join")+'</dd>'+
       '<dt>run</dt><dd>'+num(r.job_key)+
         (r.window_id===null||r.window_id===undefined ? ""
          : " · window "+num(r.window_id))+'</dd>'+
       (r.repeats===null||r.repeats===undefined ? "" :
         '<dt>repeats</dt><dd>'+num(r.correct_repeats)+'/'+num(r.repeats)+
         ' modes correct (shots pooled below)</dd>')+
       '<dt>shots</dt><dd>'+num(r.shots)+'</dd>'+
       '<dt>observed mode</dt><dd>'+num(r.observed_mode_bits)+'</dd>'+
       '<dt>expected</dt><dd>'+num(r.expected_bits)+'</dd>'+
       '<dt>on the answer</dt><dd>'+num(r.successes)+' shots ('+num(r.p_hat)+')</dd>'+
       '<dt>lead over runner-up</dt><dd>'+num(r.margin_counts)+' shots ('+
         num(r.margin)+')</dd>'+
       '<dt>mode stable</dt><dd>'+(r.mode_stable===null||r.mode_stable===undefined
         ? "&mdash;" : (r.mode_stable?"yes":"no, within noise"))+
         ' (p='+num(r.mode_stability_p_value_adjusted,4)+')</dd>'+
     '</dl>';
 }
 rows.forEach(function(tr){
   var i = Number(tr.getAttribute("data-i"));
   tr.addEventListener("click", function(){show(i);});
   tr.addEventListener("keydown", function(e){
     if (e.key === "Enter" || e.key === " ") {e.preventDefault(); show(i);}
   });
 });
 if (DATA.length) show(0);
})();
"""


def _fmt(value):
    """Three decimals, or a dash when a margin field is absent (older payload)."""
    return "-" if value is None else "%.3f" % value


def _cell(value, places=3):
    if value is None:
        return "&mdash;"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return "%.*f" % (places, value)
    return html.escape(str(value))


def eligibility_rows(payload):
    """One row per version, in a stable order."""
    return [dict(version=version, **values)
            for version, values in sorted(payload.get("voters", {}).items())]


def responses_table(rows):
    """The traceability table: every circuit the device ran, one row each."""
    if not rows:
        return ('<p class="note">This payload carries no per-circuit histograms, '
                'so there is nothing to trace. Re-run the job with a build that '
                'records <code>version_counts</code>.</p>')
    head = ("<table class=\"rows\"><thead><tr>"
            "<th>Circuit</th><th>Case</th><th>Version</th><th>Ensemble</th>"
            "<th>Treatment</th>"
            "<th>Shots</th><th>Expected</th><th>Observed</th><th>Match</th>"
            "<th>p&#770;</th><th>Lead</th></tr></thead><tbody>")
    body = []
    for index, row in enumerate(rows):
        correct = row.get("mode_correct")
        match = ("&mdash;" if correct is None
                 else ('<span class="hit">yes</span>' if correct
                       else '<span class="miss">no</span>'))
        body.append(
            '<tr data-i="%d" tabindex="0" role="button" aria-selected="false">'
            '<td class="mono">%s</td>'
            '<td class="mono">%s</td><td>%s</td><td class="mono">%s</td><td>%s</td>'
            '<td class="mono">%s</td><td class="mono">%s</td><td class="mono">%s</td>'
            '<td>%s</td><td class="mono">%s</td><td class="mono">%s</td></tr>'
            % (index, _cell(row.get("circuit_label")),
               _cell(row.get("case")), _cell(row.get("version")),
               _cell(row.get("mutation_id")), _cell(row.get("treatment")),
               _cell(row.get("shots")), _cell(row.get("expected_bits")),
               _cell(row.get("observed_mode_bits")),
               match, _cell(row.get("p_hat")), _cell(row.get("margin_counts"))))
    return head + "".join(body) + "</tbody></table>"


def run_strip(rows):
    """Campaign, window and job, stated once: they are constant within a report.

    A per-row column for a value that never varies would only cost width. The
    CSV still carries them on every row, because a CSV gets separated from the
    filename that would otherwise say which run it came from.
    """
    if not rows:
        return ""
    def unique(field):
        seen = [row.get(field) for row in rows if row.get(field) is not None]
        return sorted({str(v) for v in seen})
    parts = []
    for label, field in (("campaign", "campaign_id"), ("window", "window_id"),
                         ("job", "job_key")):
        values = unique(field)
        if values:
            parts.append("%s <code>%s</code>"
                         % (label, html.escape(", ".join(values))))
    if not parts:
        return ""
    return '<p class="sub run-strip">' + " &middot; ".join(parts) + "</p>"


def summary_table(records, eligibility):
    """The verdict a reader needs before the per-circuit detail."""
    if not records:
        return ""
    if eligibility:
        head = ("<table><thead><tr><th>Version</th><th>Repeated modes</th>"
                "<th>Aggregate modes</th><th>Eligible</th><th>min p&#770;</th>"
                "<th>min margin</th><th>Weakest case</th><th>Modes stable</th>"
                "</tr></thead><tbody>")
        body = []
        for row in records:
            body.append(
                "<tr><td class=\"mono\">%s</td><td class=\"mono\">%s/%s</td><td>%s</td>"
                "<td>%s</td><td class=\"mono\">%s</td><td class=\"mono\">%s</td>"
                "<td class=\"mono\">%s</td><td>%s</td></tr>"
                % (_cell(row.get("version")), _cell(row.get("correct_repeated_modes")),
                   _cell(row.get("total_repeated_modes")),
                   "correct" if row.get("all_aggregate_modes_correct")
                   else '<span class="miss">wrong</span>',
                   '<span class="hit">yes</span>' if row.get("eligible")
                   else '<span class="miss">no</span>',
                   _cell(row.get("min_p_hat")), _cell(row.get("min_margin")),
                   _cell(row.get("weakest_case")),
                   "yes" if row.get("all_modes_stable")
                   else '<span class="miss">no: %s</span>'
                        % _cell(", ".join(row.get("unstable_cases") or []) or "—")))
        return ("<h2>Voters</h2><div class=\"card\">" + head +
                "".join(body) + "</tbody></table></div>")
    head = ("<table><thead><tr><th>Case</th><th>Mutation</th><th>Decision</th>"
            "<th>Suspect</th><th>Outcome</th></tr></thead><tbody>")
    body = ["<tr><td class=\"mono\">%s</td><td class=\"mono\">%s</td><td>%s</td>"
            "<td class=\"mono\">%s</td><td>%s</td></tr>"
            % (_cell(row.get("case_id")), _cell(row.get("mutation_id")),
               _cell(row.get("decision")), _cell(row.get("suspect_version")),
               _cell(row.get("outcome"))) for row in records]
    return ("<h2>Decisions</h2><div class=\"card\">" + head +
            "".join(body) + "</tbody></table></div>")


def build_page(title, subtitle, verdict, summary_html, rows, pretty):
    """A self-contained report page. No network, no bundler, no build step."""
    badge = ""
    if verdict is not None:
        badge = ('<span class="badge %s">%s</span>'
                 % ("ok" if verdict else "no", "accepted" if verdict else "rejected"))
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>" + html.escape(title) + "</title><style>" + PAGE_CSS +
        "</style></head><body><div class=\"wrap\">"
        "<h1>" + html.escape(title) + " " + badge + "</h1>"
        "<p class=\"sub\">" + html.escape(subtitle) + "</p>"
        + summary_html +
        "<h2>Quantum responses</h2>"
        "<p class=\"sub\">Every circuit this job ran on the device, one row per "
        "ensemble it voted in. Select a row to see the histogram it returned "
        "against the answer the arithmetic requires. <b>Circuit</b> is the label "
        "the batch carried: the same string indexes this job's manifest under "
        "<code>experiments/results/generation2/manifests/&lt;job_id&gt;.json</code>, "
        "which holds the submitted OpenQASM. A control circuit is run "
        "once and reused across ensembles, so the same shots may appear under "
        "more than one mutation id.</p>"
        + run_strip(rows) +
        "<div class=\"split\"><div class=\"card\">" + responses_table(rows) + "</div>"
        "<div class=\"card detail\" id=\"detail\"></div></div>"
        "<details><summary>Raw summary JSON</summary><pre>"
        + html.escape(pretty) + "</pre></details>"
        "<script type=\"application/json\" id=\"responses\">"
        + json.dumps(rows).replace("<", "\\u003c") + "</script>"
        "<script>" + PAGE_JS + "</script></div></body></html>")


class Generation2Reporter(FlowFileTransform):
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "0.1.0"
        description = "Writes four provenance-preserving Generation-2 report formats."
        tags = ["quantum", "generation2", "report", "html", "csv"]
        dependencies = []

    def __init__(self, **kwargs):
        JvmHolder.jvm = kwargs.get("jvm")
        super().__init__()
        def prop(name, default=""):
            return PropertyDescriptor(
                name=name, description="Generation-2 report configuration.",
                required=True, default_value=default,
                validators=[StandardValidators.NON_EMPTY_VALIDATOR],
                expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
        self.directory = prop("Reports Directory", "./reports/generation2/nifi")
        self.basename = prop("Report Basename", "${generation2.job_key}")
        self.descriptors = [self.directory, self.basename]

    def getPropertyDescriptors(self): return self.descriptors

    def getRelationships(self):
        return [Relationship(name="success", description="All reports written."),
                Relationship(name="failure", description="Report write failed.")]

    def transform(self, context, flowFile):
        raw = bytes(flowFile.getContentsAsBytes())
        def get(prop):
            return context.getProperty(prop).evaluateAttributeExpressions(flowFile).getValue()
        try:
            payload = json.loads(raw.decode())
            eligibility = payload.get("schema") in ELIGIBILITY_SCHEMAS
            if eligibility:
                records = eligibility_rows(payload)
                # `cells` is the per-cell margin evidence: it belongs in its
                # own CSV, not inlined into the summary block a human reads.
                summary = {k: v for k, v in payload.items()
                           if k not in ("voters", "cells")}
            else:
                records = payload.get("records", [])
                summary = payload.get("metrics") or metrics(records)
            directory = Path(get(self.directory)); directory.mkdir(parents=True, exist_ok=True)
            base = get(self.basename).replace("/", "_")
            # For an eligibility verdict the payload IS the result; keep it
            # whole rather than reducing it to a metrics block.
            document = (dict(payload) if eligibility else
                        {"campaign_generation": 2, "metrics": summary,
                         "records": records})
            (directory / (base + ".json")).write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            fields = sorted({key for row in records for key in row})
            stream = io.StringIO(); writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in records:
                writer.writerow({k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list))
                                 else v for k, v in row.items()})
            (directory / (base + ".csv")).write_text(stream.getvalue(), encoding="utf-8")
            cells = payload.get("cells") if eligibility else None
            if cells:
                # One row per (version, case) with the shots behind it. This is
                # the only place the margin survives at full resolution; the
                # voter table above is already a minimum over these rows.
                cell_fields = sorted({key for row in cells for key in row})
                cell_stream = io.StringIO()
                cell_writer = csv.DictWriter(cell_stream, fieldnames=cell_fields)
                cell_writer.writeheader()
                for row in cells:
                    cell_writer.writerow(
                        {k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list))
                         else v for k, v in row.items()})
                (directory / (base + "-cells.csv")).write_text(
                    cell_stream.getvalue(), encoding="utf-8")

            # The device's own answers, in the shape a reviewer reads them.
            # The basename is the job key for payloads that carry no identity of
            # their own (a qualification verdict names a vendor, not a job).
            responses = response_rows(payload, job_key=base)
            if responses:
                response_stream = io.StringIO()
                response_writer = csv.DictWriter(response_stream,
                                                 fieldnames=list(RESPONSE_FIELDS))
                response_writer.writeheader()
                for row in responses:
                    response_writer.writerow(
                        {k: (json.dumps(row.get(k), sort_keys=True)
                             if isinstance(row.get(k), (dict, list)) else row.get(k))
                         for k in RESPONSE_FIELDS})
                (directory / (base + "-responses.csv")).write_text(
                    response_stream.getvalue(), encoding="utf-8")

            pretty = json.dumps(summary, indent=2, sort_keys=True)
            body = "# Generation-2 NiFi report\n\n```json\n%s\n```\n" % pretty
            phase = str(payload.get("phase") or payload.get("schema") or "").strip()
            vendor = str(payload.get("vendor") or payload.get("vendor_condition") or "")
            if eligibility:
                verdict = "ACCEPTED" if payload.get("accepted") else "REJECTED"
                lines = ["# Generation-2 %s — %s" % (phase, vendor.upper()),
                         "", "**%s**" % verdict, "",
                         "| version | repeated modes | aggregate modes | eligible "
                         "| min p&#770; | min margin | weakest case | modes stable |",
                         "| --- | --- | --- | --- | --- | --- | --- | --- |"]
                for row in records:
                    lines.append("| `%s` | %s/%s | %s | %s | %s | %s | %s | %s |" % (
                        row.get("version"), row.get("correct_repeated_modes"),
                        row.get("total_repeated_modes"),
                        "correct" if row.get("all_aggregate_modes_correct") else "**wrong**",
                        "yes" if row.get("eligible") else "**no**",
                        _fmt(row.get("min_p_hat")), _fmt(row.get("min_margin")),
                        row.get("weakest_case") or "-",
                        "yes" if row.get("all_modes_stable") else
                        ("**no: %s**" % ", ".join(row.get("unstable_cases") or [])
                         if row.get("unstable_cases") else "-")))
                lines += ["",
                          "`min p̂` is the lowest per-case success probability "
                          "(shots on the correct answer / shots), `min margin` how far "
                          "the winning outcome led the runner-up in that voter's worst "
                          "case. A mode is *stable* when a conditional binomial test "
                          "rejects the tie at the recorded alpha. Margins are advisory: "
                          "eligibility is still decided by the mode alone.",
                          "", "```json", pretty, "```", ""]
                body = "\n".join(lines)
            if responses:
                body += "\n".join([
                    "", "## Quantum responses", "",
                    "Per-circuit histograms are in `%s-responses.csv`; the HTML "
                    "report charts each one against the expected answer. `circuit` "
                    "is the label the batch carried, which indexes the submitted "
                    "manifest holding that circuit's OpenQASM." % base, "",
                    "| circuit | case | version | ensemble | treatment | shots "
                    "| expected | observed | match |",
                    "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"] + [
                    "| `%s` | `%s` | `%s` | `%s` | %s | %s | `%s` | `%s` | %s |" % (
                        row.get("circuit_label") or "sealed",
                        row.get("case"), row.get("version"), row.get("mutation_id"),
                        row.get("treatment"),
                        row.get("shots"), row.get("expected_bits") or "sealed",
                        row.get("observed_mode_bits"),
                        "—" if row.get("mode_correct") is None
                        else ("yes" if row.get("mode_correct") else "**no**"))
                    for row in responses] + [""])
            (directory / (base + ".md")).write_text(body, encoding="utf-8")

            title = ("Generation-2 %s — %s" % (phase, vendor.upper())).strip(" —")
            subtitle = ("%d circuits on the device · %s"
                        % (len(responses), base)) if responses else base
            page = build_page(title or "Generation-2 report", subtitle,
                              payload.get("accepted") if eligibility else None,
                              summary_table(records, eligibility), responses, pretty)
            (directory / (base + ".html")).write_text(page, encoding="utf-8")
            return FlowFileTransformResult(
                relationship="success", contents=raw,
                attributes={"generation2.report_base": str(directory / base),
                            "generation2.responses": str(len(responses)),
                            "mime.type": "application/json"})
        except Exception as exc:  # noqa: BLE001
            return FlowFileTransformResult(relationship="failure", contents=raw,
                                           attributes={"generation2.error": str(exc)})
