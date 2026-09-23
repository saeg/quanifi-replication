#!/usr/bin/env python3
"""Build an offline HTML gallery from recorded NiFi evidence, never invented outputs."""
import html
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
esc = html.escape


def read(path):
    return json.loads((ROOT / path).read_text())


def table(headers, rows):
    return '<div class="table-wrap"><table><thead><tr>' + ''.join('<th scope="col">' + esc(h) + '</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + str(v) + '</td>' for v in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def shell(title, body):
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · Quanifi supplementary flows</title><link rel="stylesheet" href="assets/style.css"></head>
<body><a class="skip" href="#main">Skip to content</a><header><a class="brand" href="index.html">QUANIFI / FLOW EXAMPLES</a>
<nav aria-label="Examples"><a href="deutsch-jozsa.html">Deutsch–Jozsa</a><a href="bernstein-vazirani.html">Bernstein–Vazirani</a><a href="portfolio-qaoa.html">Portfolio QAOA</a><a href="max-independent-set.html">Independent set</a></nav></header>
<main id="main">{body}</main><footer>QSE 2027 supplementary material · Local simulator executions · No NiFi installation needed to inspect these pages.<br>
<a href="README.md">Reproduction instructions</a> · <a href="evidence/checks.json">Independent checks</a> · <a href="evidence/environment.json">Environment and source versions</a></footer></body></html>'''


def distribution(data):
    return ''.join(f'<div class="bar-row"><code>{esc(bits)}</code><div class="bar" aria-hidden="true"><i style="width:{prob*100:.3f}%"></i></div><span>{prob*100:.2f}%</span></div>' for bits, prob in sorted(data.items(), key=lambda item: -item[1]))


def configuration(spec):
    output = ''
    for lane in spec['lanes']:
        output += '<h3>' + esc(lane['label']) + '</h3>'
        for step in lane['steps']:
            output += '<details><summary>' + esc(step['type']) + ' — properties</summary>'
            output += table(['Property', 'Configured value'], [(esc(k), '<code>' + esc(v) + '</code>') for k, v in step['properties'].items()]) + '</details>'
    output += '<p class="muted">Each source creates one JSON input using Run Once (an explicit edge list for MIS, otherwise an empty object). The algorithm parameters are processor properties. PutFile saves the returned probability distribution. The portable flow export uses <code>./supplementary-results</code> as its output directory. Algorithm failures route to visible inspection queues.</p>'
    return output


def main():
    checks = read('evidence/checks.json')
    assert checks['passed'], 'Do not build a verified gallery from failed evidence'
    specs = read('flows/specifications.json')
    by_id = {c['id']: c for c in checks['checks']}
    intro = '''<div class="eyebrow">Supplementary material / RQ1</div><h1>From configured components<br>to complete quantum flows.</h1>
<p class="lead">Inspect four Quanifi examples from input to saved result. Each page connects the problem, the NiFi canvas, its configuration, and an output captured from a real local execution.</p>
<div class="meta"><span class="badge">4 configured flows</span><span class="badge">5 executed cases</span><span class="badge">Offline HTML · no account required</span></div>
<p>These examples supplement the paper’s Grover evaluation. They show which programs can be composed from high-level components; they do not measure usability, development effort, or quantum advantage.</p><div class="cards">'''
    for i, spec in enumerate(specs, 1):
        intro += f'<article class="card"><span class="number">EXAMPLE 0{i}</span><h2>{esc(spec["algorithm"])}</h2><p>{esc(spec["summary"])}</p><span class="badge">Executed and checked</span><a class="cta" href="{spec["id"]}.html">Explore this flow →</a></article>'
    intro += '</div><section><h2>What the evidence establishes</h2>'
    intro += table(['Example', 'Component-level capability', 'Independent output check'], [
        ('Deutsch–Jozsa', 'Configure two oracle behaviours without writing their gates.', 'Balanced → 101; constant → 000.'),
        ('Bernstein–Vazirani', 'Configure a secret and execute its algorithm component.', 'Recovered string equals configured 1011.'),
        ('Portfolio QAOA', 'Pass an encoded optimisation problem into a hybrid solver.', 'Compare the sampled portfolios with exhaustive evaluation of all 8 bitstrings.'),
        ('Maximum independent set', 'Reuse the QAOA solver with a graph-problem encoder.', 'Check all 256 subsets of the Classiq reference graph.'),
    ])
    intro += '''<p>A canvas image shows the actual configured flow. Saved FlowFile content and attributes document its execution. The independent checker evaluates the outputs against each small problem instance. Flow exports and processor snapshots support reproduction.</p>
<p class="note">The algorithm processors encapsulate their internal circuit construction and simulation. In QAOA, the classical optimisation loop also runs inside the solver processor. The diagrams distinguish those internals from the connections that are visible on the NiFi canvas.</p></section>
<section><h2>Review in a few minutes</h2><ol><li>Choose an example and read its input/output contract.</li><li>Follow the stage diagram, then open the actual canvas screenshot.</li><li>Expand the property tables and inspect the saved result and correctness check.</li><li>Download the flow JSON only if you want to reproduce it in NiFi.</li></ol>
<p>All page assets are local. The <a href="README.md">README</a> describes how to import and rerun the flows. These examples use Qrisp. The independent-set flow adapts the Classiq reference problem; no Classiq cloud execution is claimed.</p></section>'''
    (ROOT / 'index.html').write_text(shell('Inspect the supplementary flows', intro))
    for i, spec in enumerate(specs, 1):
        sid = spec['id']
        if sid == 'max-independent-set':
            build_mis(spec, by_id['mis-classiq-reference'])
            continue
        body = f'<div class="eyebrow">Example 0{i} / {esc(spec["algorithm"])}</div><h1>{esc(spec["title"])}</h1><p class="lead">{esc(spec["summary"])}</p><div class="meta"><span class="badge">Executed in Apache NiFi</span><span class="badge">Independent checks passed</span><span class="badge">Qrisp · local simulation</span></div>'
        if sid == 'deutsch-jozsa':
            problem = 'Is the Boolean oracle constant or balanced? The two branches use three query qubits plus one ancilla. One branch implements the balanced parity mask 101; the other always returns zero. For these ideal simulator cases, the expected query-register outcomes are 101 and 000 respectively.'
            stages = [('Create input', 'One empty JSON FlowFile per branch.'), ('Evaluate the oracle', 'QrispDeutschJozsa builds and simulates the configured circuit.'), ('Save distribution', 'PutFile writes the measurement probabilities as JSON.')]
            contract = [('Input', 'Empty JSON content; Function Type, Num Qubits, Balanced Mask and Shots are configured properties.'), ('Output content', 'A JSON mapping from query-register bitstrings to probabilities.'), ('Output attributes', '<code>dj.verdict</code>, <code>dj.function_type</code>, <code>sim.top_result</code>, <code>sim.top_probability</code>.'), ('Qubit convention', 'The leftmost bit represents query qubit 0. The ancilla is not part of the output bitstring.')]
            interpretation = 'The balanced branch returned 101 and verdict balanced; the constant branch returned 000 and verdict constant. Both saved distributions put all observed probability on the expected result. These two configured oracle cases illustrate component-level programming; they are not an exhaustive test of every oracle.'
        elif sid == 'bernstein-vazirani':
            problem = 'Recover a hidden four-bit string from an oracle for its parity function. The Secret Bitstring property configures the oracle as 1011. The algorithm uses four query qubits and one ancilla; the configured string is ground truth for checking the result, not an unknown value supplied by an external oracle.'
            stages = [('Create input', 'One empty JSON FlowFile triggers the case.'), ('Recover the string', 'QrispBernsteinVazirani builds the oracle, applies phase kickback and simulates.'), ('Save distribution', 'PutFile writes the recovered-string probabilities.')]
            contract = [('Input', 'Empty JSON content; Secret Bitstring = 1011 and Shots = 1024 are properties.'), ('Output content', 'A JSON probability distribution over four-bit strings.'), ('Output attributes', '<code>bv.found_bitstring</code>, <code>bv.secret_bitstring</code>, <code>bv.match</code>.'), ('Qubit convention', 'Leftmost bit is query qubit 0; the measured register excludes the ancilla.')]
            interpretation = 'The saved result is 1011 with probability 1. The independent checker compares the measured outcome to the configured secret, and also checks the processor’s match attribute. This demonstrates the configured algorithm pipeline on one input.'
        else:
            problem = 'Select exactly two of three assets. The objective combines expected return, covariance-weighted risk, and a penalty for violating the budget. PortfolioRebalancingProblem encodes that classical objective as a diagonal Hamiltonian. QrispQAOA consumes the Hamiltonian, trains a two-layer circuit with COBYLA, and samples the trained state.'
            stages = [('Create input', 'One empty JSON FlowFile starts the case.'), ('Encode problem', 'PortfolioRebalancingProblem emits a diagonal cost Hamiltonian.'), ('Optimise & sample', 'QrispQAOA runs the hybrid loop and final simulation internally.'), ('Save distribution', 'PutFile writes portfolio probabilities as JSON.')]
            contract = [('Input', 'Returns, covariance matrix, risk factor 0.5, budget 2, and penalty 3.0 are encoder properties.'), ('Encoder → solver', 'Content uses the framework-neutral sparse Pauli JSON format; <code>hamiltonian.format</code> and qubit metadata identify the representation.'), ('Solver output', 'A JSON probability distribution plus <code>qaoa.best_measurement</code>, <code>qaoa.best_value</code>, and mean objective <code>qaoa.optimal_value</code>.'), ('Mixer and budget', 'RX mixer; the budget is encouraged by an objective penalty. Infeasible samples remain possible.'), ('Bitstring meaning', 'For the saved output convention, 011 selects assets 1 and 2, with asset indices starting at 0.')]
            c = by_id['portfolio']
            interpretation = f'The exact optimum is {c["exact_optimum"]["bits"]}, with objective {c["exact_optimum"]["cost"]:.4f}. Its observed probability is {c["optimum_probability"]:.2%}; the total probability of selecting exactly two assets is {c["feasible_probability"]:.2%}. The independently calculated mean objective is {c["mean_cost"]:.6f}. A best sampled solution is different from the mean objective and from the most probable state. This single small run does not establish general optimisation performance.'
        body += f'<section><h2>1. The problem and the dataflow</h2><p>{problem}</p><ol class="flow">'
        for n, (name, text) in enumerate(stages, 1):
            body += f'<li><span class="step-count">0{n}</span><strong>{name}</strong><span>{text}</span></li>'
        body += '</ol>' + table(['Boundary', 'Data and meaning'], contract) + f'<p class="note"><strong>RQ1 capability:</strong> {esc(spec["capability"])}</p></section>'
        body += f'<section><h2>2. The actual NiFi canvas</h2><figure><a href="assets/{sid}.png"><img src="assets/{sid}.png" alt="Actual NiFi canvas for {esc(spec["algorithm"])} with configured processors and connections" loading="lazy"></a><figcaption>Captured from the isolated NiFi instance after execution. Red squares mean intentionally stopped processors, not failed runs. NiFi’s counters cover the last five minutes and can return to zero; the saved evidence below retains the run. Click for the full-resolution image.</figcaption></figure><div class="links"><a href="flows/{sid}.json" download>Download importable flow JSON</a><a href="evidence/validation.json">Inspect processor validation</a></div></section>'
        body += '<section><h2>3. Inspect the configuration</h2><p>The following values come from the same specification used to create the NiFi processors.</p>' + configuration(spec) + '</section>'
        body += '<section><h2>4. Saved execution and independent check</h2><p>' + interpretation + '</p>'
        for lane in spec['lanes']:
            data = read('evidence/' + lane['id'] + '.json')
            body += '<h3>' + esc(lane['label']) + '</h3>' + distribution(data['distribution'])
            body += f'<p class="muted">Captured {esc(data["captured_utc"])}. Values are probabilities returned by Qrisp, not integer shot counts. The Shots property was 1024.</p><div class="links"><a href="evidence/{lane["id"]}.json">FlowFile evidence and attributes</a><a href="evidence/{lane["id"]}-distribution.json" download>Raw output JSON</a></div>'
        if sid == 'portfolio-qaoa':
            c = by_id['portfolio']
            dist = read('evidence/portfolio.json')['distribution']
            body += '<h3>Check every possible portfolio</h3><p>The independent checker evaluates the classical objective directly for all eight bitstrings, without calling the quantum solver.</p><pre>C(x) = 0.5 × xᵀΣx − μᵀx + 3 × (Σx − 2)²</pre>'
            body += table(['Portfolio', 'Selected assets', 'Budget met?', 'Objective', 'Observed probability'], [('<code>' + r['bits'] + '</code>', ', '.join(str(j) for j,b in enumerate(r['bits']) if b == '1') or 'None', 'Yes' if r['feasible'] else 'No', f'{r["cost"]:.4f}', f'{dist.get(r["bits"],0):.2%}') for r in c['states']])
            body += '''<h3>Explore the saved objective</h3><p>Toggle the selected assets to inspect their classical cost. This interaction uses the archived table above; it does not run NiFi or a quantum simulation.</p><div class="checker">'''
            body += ''.join(f'<label><input type="checkbox" id="asset-{j}" {"checked" if j else ""}> Asset {j}</label>' for j in range(3))
            body += '</div><output id="portfolio-cost" aria-live="polite"></output><noscript>The complete objective table above works without JavaScript.</noscript>'
            body += '<script>const states=' + json.dumps(c['states']) + ''';function update(){const bits=[0,1,2].map(i=>document.getElementById('asset-'+i).checked?'1':'0').join('');const row=states.find(r=>r.bits===bits);document.getElementById('portfolio-cost').textContent=bits+' · objective '+row.cost.toFixed(4)+' · '+(row.feasible?'budget met':'budget violated');}document.querySelectorAll('.checker input').forEach(x=>x.addEventListener('change',update));update();</script>'''
            body += '<p class="note">The Random Seed property is best-effort: Qrisp does not expose a complete seed interface for every stage. Reruns may produce different probabilities. The archived output and independent exact-cost checks make this reported run inspectable.</p>'
        body += '<p><a href="evidence/checks.json">Read the machine-readable checks</a> · <a href="tools/verify_evidence.py">Inspect the independent checker</a></p></section>'
        body += '<section><h2>5. Reproduce or inspect the implementation</h2><p>Import the downloadable process group into NiFi with the supplied Python processors installed. Review its output directory, start the downstream processors, and use Run Once on each source. The exported processors are stopped by default.</p><p>For exact commands and version details, see the <a href="README.md">reproduction guide</a>. Installation is optional for reviewing the screenshots, configuration tables, and saved results on this page.</p><ul>'
        for name in sorted({s['type'] for l in spec['lanes'] for s in l['steps']}):
            body += f'<li><a href="processors/{name}.py">{name}.py — executed processor snapshot</a></li>'
        body += '</ul></section>'
        (ROOT / (sid + '.html')).write_text(shell(spec['algorithm'], body))
    print('Built index.html and four example pages')


def build_mis(spec, check):
    reference = read('evidence/classiq-reference.json')
    evidence = read('evidence/mis-classiq-reference.json')
    best = check['best_sample']['bits']
    vertices = [i for i, bit in enumerate(best) if bit == '1']
    source = reference['notebook_url']
    body = '''<div class="eyebrow">Example 04 / Classiq reference problem</div><h1>Find vertices that<br>can coexist.</h1>
<p class="lead">A maximum independent set contains as many vertices as possible, with no edge joining any two selected vertices. Here, the graph from Classiq’s example travels through a Quanifi encoder and a local Qrisp QAOA solver.</p>
<div class="meta"><span class="badge">Executed in NiFi</span><span class="badge">8 vertices · 14 edges</span><span class="badge">Local Qrisp adaptation</span></div>'''
    body += f'<p class="note">This flow adapts the <a href="{source}">Classiq maximum-independent-set notebook</a> at revision <code>{reference["commit"][:12]}</code>. It uses the same graph and problem, but a different implementation and optimisation objective. The saved results are from Quanifi/Qrisp, not Classiq cloud execution.</p>'
    body += '<section><h2>1. One graph, two implementations</h2>'
    body += table(['Aspect', 'Classiq reference notebook', 'This executable Quanifi flow'], [
        ('Graph', '8 nodes; edge probability 0.4; seed 12345.', 'The same 14 edges, stored explicitly as an input fixture.'),
        ('Problem', 'Maximise selected vertices subject to no adjacent selected pair.', 'Minimise −|S| + 2 × selected-edge conflicts; its global minima solve the same MIS problem.'),
        ('Encoder', 'Pyomo model translated by CombinatorialProblem.', 'MaxIndependentSetProblem produces a sparse Pauli Hamiltonian.'),
        ('Solver', 'Classiq synthesis and CVaR optimisation, quantile 0.7.', 'QrispQAOA, RX mixer, COBYLA, mean-cost optimisation.'),
        ('Depth and iterations', '3 QAOA layers; maxiter 60.', '3 QAOA layers; 60 requested optimiser iterations; 1024 final shots.'),
        ('Validation', 'Notebook compares with a classical solver.', 'Independent enumeration of all 256 vertex subsets.'),
    ])
    body += '<p>The solver’s internal circuit synthesis, parameter optimisation, and sampling are encapsulated in a single processor. Quanifi’s visible composition connects graph input, Hamiltonian encoding, the solver, and file output. Reusing the same QAOA processor as the portfolio example illustrates the shared representation between different problem encoders.</p></section>'
    body += '<section><h2>2. Explore the graph</h2><p>The highlighted vertices are the best sampled set from the archived run. Toggle a vertex to see whether your selection is independent. This browser interaction checks the classical graph only; it does not rerun QAOA.</p>'
    coords = [(250 + 190 * math.cos(-math.pi/2 + i*math.pi/4), 250 + 190*math.sin(-math.pi/2 + i*math.pi/4)) for i in range(8)]
    body += '<div class="graph-layout"><svg viewBox="0 0 500 500" role="img" aria-labelledby="graph-title"><title id="graph-title">Classiq reference graph with eight vertices and fourteen edges</title>'
    for u,v in reference['edges']:
        x1,y1=coords[u];x2,y2=coords[v]
        body += f'<line data-edge="{u},{v}" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="#a6b9b4" stroke-width="2"/>'
    for i,(x,y) in enumerate(coords):
        body += f'<g><circle id="node-{i}" cx="{x:.1f}" cy="{y:.1f}" r="22" fill="{"#087d70" if i in vertices else "white"}" stroke="#17343b" stroke-width="2"/><text id="node-label-{i}" x="{x:.1f}" y="{y+6:.1f}" text-anchor="middle" fill="{"white" if i in vertices else "#17343b"}" font-size="18">{i}</text></g>'
    body += '</svg><div><div class="checker">' + ''.join(f'<label><input type="checkbox" id="vertex-{i}" {"checked" if i in vertices else ""}> Vertex {i}</label>' for i in range(8)) + '</div><output id="mis-status" aria-live="polite"></output><noscript>Use the highlighted saved solution and the evidence below; interactive checking requires JavaScript.</noscript></div></div>'
    body += '<script>const edges=' + json.dumps(reference['edges']) + ''';function updateGraph(){const selected=[0,1,2,3,4,5,6,7].filter(i=>document.getElementById('vertex-'+i).checked);let conflicts=0;document.querySelectorAll('[data-edge]').forEach(line=>{const [u,v]=line.dataset.edge.split(',').map(Number);const conflict=selected.includes(u)&&selected.includes(v);if(conflict)conflicts++;line.setAttribute('stroke',conflict?'#bd432e':'#a6b9b4');line.setAttribute('stroke-width',conflict?'4':'2');});for(let i=0;i<8;i++){document.getElementById('node-'+i).setAttribute('fill',selected.includes(i)?'#087d70':'white');document.getElementById('node-label-'+i).setAttribute('fill',selected.includes(i)?'white':'#17343b');}document.getElementById('mis-status').textContent='Selected {'+selected.join(', ')+'} · '+selected.length+' vertices · '+(conflicts===0?'independent':conflicts+' conflicting edges')+' · cost '+(-selected.length+2*conflicts);}document.querySelectorAll('.checker input').forEach(x=>x.addEventListener('change',updateGraph));updateGraph();</script></section>'''
    body += '<section><h2>3. Follow the complete flow</h2><ol class="flow">'
    for i,(name,text) in enumerate([('Read graph','GenerateFlowFile supplies the explicit JSON edge list.'),('Encode constraints','MaxIndependentSetProblem maps the graph to an eight-qubit cost Hamiltonian.'),('Optimise & sample','QrispQAOA trains a three-layer circuit and samples it locally.'),('Save result','PutFile writes the probability distribution.')],1):
        body+=f'<li><span class="step-count">0{i}</span><strong>{name}</strong><span>{text}</span></li>'
    body += '</ol><figure><a href="assets/max-independent-set.png"><img src="assets/max-independent-set.png" alt="Actual NiFi maximum independent set flow: input, encoder, QAOA solver, and file output" loading="lazy"></a><figcaption>Actual NiFi canvas after execution. Processors are intentionally stopped. Rolling five-minute counters may expire; archived FlowFile evidence retains the output.</figcaption></figure><div class="links"><a href="flows/max-independent-set.json" download>Download importable flow</a><a href="evidence/classiq-reference.json">Graph fixture and source provenance</a></div></section>'
    body += '<section><h2>4. Inspect inputs and settings</h2><details><summary>Exact input graph JSON</summary><pre><code>' + esc(json.dumps(spec['lanes'][0]['input'],indent=2)) + '</code></pre></details>' + configuration(spec)
    body += '<p>The MIS source carries the graph in its FlowFile content. The Edges property is a fallback; the incoming graph takes precedence. Num Nodes is explicitly 8. The encoder emits <code>hamiltonian.format=sparse_pauli_op_json</code>, which the same solver used in the portfolio flow understands.</p></section>'
    body += f'<section><h2>5. Saved results and exact validation</h2><div class="metric-grid"><div class="metric"><strong>{check["maximum_size"]}</strong><span>Exact maximum independent-set size</span></div><div class="metric"><strong>{check["optimal_probability"]:.1%}</strong><span>Probability of sampling any maximum set</span></div><div class="metric"><strong>{check["independent_probability"]:.1%}</strong><span>Probability of a conflict-free set</span></div></div>'
    body += f'<p>The recorded best sample is <code>{best}</code>, selecting vertices <strong>{", ".join(map(str,vertices))}</strong>. It contains {check["best_sample"]["size"]} vertices and {check["best_sample"]["conflicts"]} conflicting edges. Its penalised cost is {check["best_sample"]["cost"]}. The independent checker enumerates every subset and verifies the reported best cost, the exact optimum, and the distribution’s mean cost ({check["mean_cost"]:.6f}).</p>'
    body += '<p>Bits in the saved output are interpreted left to right as vertices 0 through 7. An optimal sample is not the same as the most probable sample. The plot below shows the twelve most probable outputs; the downloadable evidence includes the entire distribution.</p>'
    body += distribution(dict(sorted(evidence['distribution'].items(),key=lambda x:-x[1])[:12]))
    body += '<details><summary>All exact maximum independent sets</summary>' + table(['Bitstring','Vertices'],[('<code>'+bits+'</code>',', '.join(str(i) for i,b in enumerate(bits) if b=='1')) for bits in check['optimal_bitstrings']]) + '</details>'
    body += f'<p class="muted">Captured {esc(evidence["captured_utc"])}. The seed is best-effort; new QAOA runs can differ. Probabilities are returned by Qrisp from the configured 1024-shot final sampling.</p><div class="links"><a href="evidence/mis-classiq-reference.json">FlowFile output and attributes</a><a href="evidence/mis-classiq-reference-distribution.json">Complete distribution</a><a href="evidence/checks.json">All 256 subset checks</a></div></section>'
    body += '<section><h2>6. Reproduce the adaptation</h2><p>Follow the <a href="README.md">NiFi import instructions</a>, using the downloaded flow and the supplied <a href="processors/MaxIndependentSetProblem.py">MIS encoder</a>, <a href="processors/QrispQAOA.py">QAOA processor</a>, and <a href="processors/pauli_dsl.py">Hamiltonian helper</a>. No Classiq account is needed for this adaptation.</p><p>The reference graph is fixed as explicit edges, so a future change in a random-graph library cannot silently change the problem. Differences in solver implementation and CVaR versus mean-cost optimisation prevent interpreting these results as a numerical reproduction of Classiq’s notebook.</p></section>'
    (ROOT / 'max-independent-set.html').write_text(shell('Maximum independent set',body))


if __name__ == '__main__':
    main()
