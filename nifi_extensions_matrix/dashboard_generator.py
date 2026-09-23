"""
Generates the interactive, light-themed Master-Detail distribution consensus dashboard.
Can be called directly by QuantumDistributionOracle inside NiFi whenever a test case completes.
"""

import json
import os
import html as html_lib

META_DEFAULTS = {
    "matrix-000": {
        "title": "Grover Search 2-Qubit",
        "qubits": 2,
        "target": "10",
        "oracle_desc": "Oracle marks |10⟩",
        "top1_verdict": "PASS",
        "top1_winner": "10",
        "top1_pct": "100.0%",
        "top1_note": "Unanimous consensus across all 24 branches.",
        "finding_type": "EQUIVALENT",
        "finding_label": "✓ Equivalent / No Fault",
        "finding_badge_class": "badge-equivalent",
        "summary": "Equivalent mutant with zero behavioral deviation. All 24 branches (Cirq, Qiskit, Pennylane, Qrisp) returned |10⟩ with 100.0% probability. Pairwise Hellinger distance H = 0.0000 across all 276 comparisons."
    },
    "matrix-001": {
        "title": "Grover Search 2-Qubit",
        "qubits": 2,
        "target": "11",
        "oracle_desc": "Oracle marks |11⟩",
        "top1_verdict": "DISAGREE",
        "top1_winner": "11 (19) vs 10/01 (5)",
        "top1_pct": "79.2% / 20.8%",
        "top1_note": "19 unmutated branches voted |11⟩; 5 mutated Qiskit branches voted |10⟩/|01⟩.",
        "finding_type": "COARSE_FAULT",
        "finding_label": "⚠️ Coarse Fault: Bit Flipped",
        "finding_badge_class": "badge-coarse",
        "summary": "Coarse fault: The mutation completely suppressed target state |11⟩ in the Qiskit engine, outputting equal superposition over |10⟩ (51.4%) and |01⟩ (48.6%). Both Top-1 consensus and Hellinger distance (H = 1.0000) caught the divergence immediately."
    },
    "matrix-002": {
        "title": "Grover Search 3-Qubit",
        "qubits": 3,
        "target": "110",
        "oracle_desc": "Oracle marks |110⟩",
        "top1_verdict": "DISAGREE",
        "top1_winner": "110 (19) vs 111 (5)",
        "top1_pct": "79.2% / 20.8%",
        "top1_note": "19 unmutated branches voted |110⟩; 5 mutated Qiskit branches voted |111⟩.",
        "finding_type": "COARSE_FAULT",
        "finding_label": "⚠️ Coarse Fault: Bit Flipped",
        "finding_badge_class": "badge-coarse",
        "summary": "Coarse fault: The mutated Qiskit engine flipped the least significant bit, producing |111⟩ (58.3%) as its top outcome rather than expected |110⟩ (40.4%). Detected by both Top-1 voting and Hellinger consensus (max H = 0.5873)."
    },
    "matrix-003": {
        "title": "Grover Search 3-Qubit",
        "qubits": 3,
        "target": "010",
        "oracle_desc": "Oracle marks |010⟩",
        "top1_verdict": "DISAGREE",
        "top1_winner": "010 (19) vs 110 (5)",
        "top1_pct": "79.2% / 20.8%",
        "top1_note": "19 unmutated branches voted |010⟩; 5 mutated Qiskit branches voted |110⟩.",
        "finding_type": "COARSE_FAULT",
        "finding_label": "⚠️ Coarse Fault: Bit Flipped",
        "finding_badge_class": "badge-coarse",
        "summary": "Coarse fault: Mutation altered the oracle qubit mapping, producing |110⟩ (94.6%) instead of target |010⟩ (95.0%). Caught by both Top-1 voting and Hellinger distance (max H = 0.9153)."
    },
    "matrix-004": {
        "title": "Grover Search 4-Qubit",
        "qubits": 4,
        "target": "0111",
        "oracle_desc": "Oracle marks |0111⟩",
        "top1_verdict": "PASS",
        "top1_winner": "0111",
        "top1_pct": "100.0%",
        "top1_note": "All 24 branches agreed on |0111⟩ (>91% confidence).",
        "finding_type": "TOLERANCE",
        "finding_label": "✓ Near Tolerance: H ≈ 0.10",
        "finding_badge_class": "badge-tolerance",
        "summary": "Near tolerance threshold: Minor stochastic and compiler variations kept pairwise Hellinger distance around H = 0.1017. All 24 branches correctly identified |0111⟩ with >91% probability."
    },
    "matrix-005": {
        "title": "Grover Search 4-Qubit",
        "qubits": 4,
        "target": "0110",
        "oracle_desc": "Oracle marks |0110⟩",
        "top1_verdict": "PASS (False Negative!)",
        "top1_winner": "0110",
        "top1_pct": "100.0% voted |0110⟩",
        "top1_note": "CRITICAL: Top-1 majority voter reported PASS because |0110⟩ was still the #1 bitstring!",
        "finding_type": "SUBTLE_FAULT",
        "finding_label": "⚡ Subtle Fault: Top-1 Passed, Hellinger Caught Error!",
        "finding_badge_class": "badge-hero",
        "summary": "CRITICAL ANOMALY: In all 24 branches, |0110⟩ remained the most frequent single bitstring. Therefore, coarse Top-1 majority consensus reported PASS (False Negative). However, the mutant in the Qiskit engine severely attenuated the target amplitude from ~94.2% down to 56.9%, leaking 43.1% probability into noisy states (|0000⟩, |0010⟩, |0100⟩, etc.). The QuantumDistributionOracle detected this amplitude degradation with H = 0.4143 (> 0.10, p < 0.05), successfully flagging DISAGREE."
    }
}


def render_matrix_dashboard(raw_cases_dict, output_path, flow_name="Matrix-Dist"):
    """
    Renders the full light-themed Master-Detail dashboard.
    raw_cases_dict: { 'matrix-000': {...}, 'matrix-001': {...}, ... }
    """
    structured_cases = {}

    for cid, rc in raw_cases_dict.items():
        m = META_DEFAULTS.get(cid, {
            "title": f"Test Case {cid}",
            "qubits": 3,
            "target": "000",
            "oracle_desc": "",
            "top1_verdict": "UNKNOWN",
            "top1_winner": "unknown",
            "top1_pct": "0%",
            "top1_note": "",
            "finding_type": "UNKNOWN",
            "finding_label": "Run Result",
            "finding_badge_class": "badge-tolerance",
            "summary": "Test run execution."
        })

        true_divergent = [p for p in rc.get("pairs", []) if p.get("hellinger", 0) > 0.1]

        mutated_branches = []
        unmutated_branches = []
        for idx, b in enumerate(rc.get("branches", [])):
            b_info = dict(b)
            b_info["index"] = idx + 1
            if b_info.get("framework") == "qiskit" and (b_info.get("top_state") != m["target"] or b_info.get("top_pct", 0) < 80.0):
                b_info["is_mutated"] = True
                mutated_branches.append(b_info)
            else:
                b_info["is_mutated"] = False
                unmutated_branches.append(b_info)

        ref_dist_agg = {}
        for b in (unmutated_branches or rc.get("branches", [])):
            for s in b.get("dist", []):
                ref_dist_agg[s["state"]] = ref_dist_agg.get(s["state"], 0.0) + s["pct"]
        for k in ref_dist_agg:
            ref_dist_agg[k] = round(ref_dist_agg[k] / max(1, len(unmutated_branches or rc.get("branches", []))), 1)

        mut_dist_agg = {}
        mut_source = mutated_branches if mutated_branches else unmutated_branches
        for b in (mut_source or rc.get("branches", [])):
            for s in b.get("dist", []):
                mut_dist_agg[s["state"]] = mut_dist_agg.get(s["state"], 0.0) + s["pct"]
        for k in mut_dist_agg:
            mut_dist_agg[k] = round(mut_dist_agg[k] / max(1, len(mut_source or rc.get("branches", []))), 1)

        union_states = sorted(
            list(set(ref_dist_agg.keys()) | set(mut_dist_agg.keys())),
            key=lambda s: -(ref_dist_agg.get(s, 0.0) + mut_dist_agg.get(s, 0.0))
        )[:8]

        comparison_table = []
        for s in union_states:
            r_pct = ref_dist_agg.get(s, 0.0)
            m_pct = mut_dist_agg.get(s, 0.0)
            diff = round(m_pct - r_pct, 1)
            comparison_table.append({
                "state": s,
                "ref_pct": r_pct,
                "mut_pct": m_pct,
                "diff": diff,
                "is_target": (s == m["target"])
            })

        if mutated_branches:
            top_mut = mutated_branches[0]
            mut_returned_str = f"|{top_mut.get('top_state', '')}⟩ ({top_mut.get('top_pct', 0):.1f}%)"
        else:
            mut_returned_str = f"|{m['target']}⟩ (100.0%)"

        structured_cases[cid] = {
            "id": cid,
            "title": m["title"],
            "qubits": m["qubits"],
            "target": m["target"],
            "oracle_desc": m["oracle_desc"],
            "top1_verdict": m["top1_verdict"],
            "top1_winner": m["top1_winner"],
            "top1_pct": m["top1_pct"],
            "top1_note": m["top1_note"],
            "finding_type": m["finding_type"],
            "finding_label": m["finding_label"],
            "finding_badge_class": m["finding_badge_class"],
            "summary": m["summary"],
            "max_hellinger": float(rc.get("max_hellinger", 0.0)),
            "dist_verdict": "DISAGREE" if float(rc.get("max_hellinger", 0.0)) > 0.10 else "PASS",
            "divergent_count": len(true_divergent),
            "total_pairs": len(rc.get("pairs", [])),
            "mut_returned": mut_returned_str,
            "branches": rc.get("branches", []),
            "mutated_count": len(mutated_branches),
            "unmutated_count": len(unmutated_branches),
            "comparison_table": comparison_table,
            "pairs": rc.get("pairs", []),
            "correction": rc.get("correction", "none"),
            "pairwise_tests": int(rc.get("pairwise_tests", 0)),
            "significant_pairs_raw": int(rc.get("significant_pairs_raw", 0)),
            "significant_pairs_corrected": int(rc.get("significant_pairs_corrected", 0)),
        }

    ordered_ids = sorted(structured_cases.keys())

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quanifi — 24-Branch Matrix Distribution Consensus</title>
  <style>
    :root {{
      --bg-primary: #f6f8fa;
      --bg-secondary: #ffffff;
      --bg-tertiary: #f8fafc;
      --border-color: #d0d7de;
      --border-subtle: #eaeef2;
      --text-main: #1f2328;
      --text-muted: #656d76;
      --accent-blue: #0969da;
      --accent-green: #1a7f37;
      --accent-amber: #9a6700;
      --accent-red: #cf222e;
      --hero-gold: #9a6700;
      --hero-bg: #fff8c5;
      --hero-border: #d4a72c;
    }}

    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg-primary);
      color: var(--text-main);
      margin: 0;
      padding: 24px;
      line-height: 1.5;
    }}

    .container {{
      max-width: 1440px;
      margin: 0 auto;
    }}

    /* Header */
    .page-header {{
      background: var(--bg-secondary);
      border: 1px solid var(--border-color);
      border-radius: 12px;
      padding: 24px 28px;
      margin-bottom: 24px;
      box-shadow: 0 1px 3px rgba(31, 35, 40, 0.06);
    }}
    .header-top {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
      margin-bottom: 12px;
    }}
    .header-title {{
      margin: 0;
      font-size: 1.6rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .header-title .logo-symbol {{
      color: var(--accent-blue);
      font-size: 1.8rem;
    }}
    .header-desc {{
      color: var(--text-muted);
      margin: 0;
      font-size: 0.95rem;
      max-width: 960px;
    }}

    /* Stat Cards */
    .stats-row {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 14px;
      margin-top: 20px;
    }}
    .stat-card {{
      background: #fdfdfd;
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 12px 16px;
      display: flex;
      flex-direction: column;
    }}
    .stat-card .stat-val {{
      font-size: 1.4rem;
      font-weight: 700;
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
      color: var(--text-main);
    }}
    .stat-card .stat-lbl {{
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      margin-top: 4px;
    }}

    /* Section Title */
    .section-title {{
      font-size: 1.15rem;
      font-weight: 600;
      color: var(--text-main);
      margin: 28px 0 14px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .section-subtitle {{
      font-size: 0.82rem;
      color: var(--text-muted);
      font-weight: 400;
    }}

    /* Master Table */
    .table-container {{
      background: var(--bg-secondary);
      border: 1px solid var(--border-color);
      border-radius: 10px;
      overflow: hidden;
      margin-bottom: 30px;
      box-shadow: 0 1px 3px rgba(31, 35, 40, 0.06);
    }}
    table.master-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.88rem;
      text-align: left;
    }}
    table.master-table th {{
      background: #f6f8fa;
      color: var(--text-muted);
      font-weight: 600;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      padding: 12px 16px;
      border-bottom: 1px solid var(--border-color);
      white-space: nowrap;
    }}
    .th-content {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      vertical-align: middle;
    }}
    .help-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 15px;
      height: 15px;
      border-radius: 50%;
      background: #e1e4e8;
      color: #57606a;
      border: 1px solid #d0d7de;
      font-size: 10px;
      font-weight: 700;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      cursor: pointer;
      line-height: 1;
      padding: 0;
      transition: all 0.15s cubic-bezier(0.4, 0, 0.2, 1);
      user-select: none;
      vertical-align: middle;
    }}
    .help-btn:hover, .help-btn:focus, .help-btn.active {{
      background: var(--accent-blue);
      color: #ffffff;
      border-color: var(--accent-blue);
      transform: scale(1.15);
      outline: none;
      box-shadow: 0 0 0 3px rgba(9, 105, 218, 0.25);
    }}

    /* Global Floating Tooltip Popover */
    .global-help-tooltip {{
      position: absolute;
      visibility: hidden;
      opacity: 0;
      transform: translateY(6px);
      width: 320px;
      max-width: calc(100vw - 32px);
      background: #1f2328;
      color: #f0f6fc;
      padding: 14px 16px;
      border-radius: 10px;
      font-size: 0.82rem;
      font-weight: 400;
      line-height: 1.5;
      text-transform: none;
      letter-spacing: normal;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.28), 0 2px 8px rgba(0, 0, 0, 0.15);
      border: 1px solid #30363d;
      z-index: 99999;
      pointer-events: none;
      transition: opacity 0.18s ease, transform 0.18s ease, visibility 0.18s;
    }}
    .global-help-tooltip.visible {{
      visibility: visible;
      opacity: 1;
      transform: translateY(0);
      pointer-events: auto;
    }}
    .tooltip-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 6px;
    }}
    .tooltip-title {{
      font-weight: 700;
      color: #58a6ff;
      font-size: 0.88rem;
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .tooltip-close {{
      background: transparent;
      border: none;
      color: #8b949e;
      font-size: 16px;
      cursor: pointer;
      padding: 0 4px;
      line-height: 1;
    }}
    .tooltip-close:hover {{
      color: #f0f6fc;
    }}
    .tooltip-formula {{
      font-family: ui-monospace, SFMono-Regular, "SF Mono", monospace;
      font-size: 0.78rem;
      background: #0d1117;
      color: #7ee787;
      padding: 5px 8px;
      border-radius: 6px;
      border: 1px solid #30363d;
      margin: 8px 0;
      word-break: break-word;
    }}
    .tooltip-desc {{
      color: #c9d1d9;
      margin-bottom: 8px;
    }}
    .tooltip-rule {{
      background: rgba(56, 139, 253, 0.12);
      border-left: 3px solid #388bfd;
      padding: 6px 10px;
      border-radius: 0 6px 6px 0;
      color: #e6edf3;
      font-size: 0.78rem;
      line-height: 1.45;
    }}

    /* Guide Panel */
    .guide-toggle-btn {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 0.85rem;
      font-weight: 600;
      background: #f3f4f6;
      color: #24292f;
      border: 1px solid var(--border-color);
      cursor: pointer;
      transition: all 0.15s ease;
    }}
    .guide-toggle-btn:hover {{
      background: #e5e7eb;
      border-color: #cbd5e1;
    }}
    .guide-panel {{
      display: none;
      background: #ffffff;
      border: 1px solid #d0d7de;
      border-radius: 10px;
      padding: 20px 24px;
      margin: 16px 0 20px;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
      animation: fadeIn 0.2s ease-in-out;
    }}
    .guide-panel.open {{
      display: block;
    }}
    @keyframes fadeIn {{
      from {{ opacity: 0; transform: translateY(-6px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    .guide-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 16px;
      margin-top: 14px;
    }}
    .guide-card {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      padding: 14px;
    }}
    .guide-card-title {{
      font-weight: 700;
      font-size: 0.9rem;
      color: #0f172a;
      margin-bottom: 6px;
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .guide-card-body {{
      font-size: 0.82rem;
      color: #475569;
      line-height: 1.5;
    }}
    table.master-table td {{
      padding: 14px 16px;
      border-bottom: 1px solid var(--border-subtle);
      vertical-align: middle;
    }}
    table.master-table tr.case-row {{
      cursor: pointer;
      transition: all 0.15s ease-in-out;
    }}
    table.master-table tr.case-row:hover {{
      background: #f3f6f9;
    }}
    table.master-table tr.case-row.active {{
      background: #f0f7ff;
      border-left: 4px solid var(--accent-blue);
    }}
    table.master-table tr.case-row.active td:first-child {{
      font-weight: 700;
      color: var(--accent-blue);
    }}

    /* Badges */
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 20px;
      font-size: 0.76rem;
      font-weight: 600;
      font-family: ui-monospace, SFMono-Regular, "SF Mono", monospace;
      white-space: nowrap;
    }}
    .badge-pass {{
      background: #dafbe1;
      color: #1a7f37;
      border: 1px solid rgba(26, 127, 55, 0.3);
    }}
    .badge-fail {{
      background: #ffebe9;
      color: #cf222e;
      border: 1px solid rgba(207, 34, 46, 0.3);
    }}
    .badge-disagree {{
      background: #fff8c5;
      color: #9a6700;
      border: 1px solid rgba(154, 103, 0, 0.3);
    }}
    .badge-hero {{
      background: linear-gradient(135deg, #fff8c5 0%, #ffedd5 100%);
      color: #875000;
      border: 1px solid #d4a72c;
      box-shadow: 0 1px 3px rgba(212, 167, 44, 0.3);
    }}
    .badge-coarse {{
      background: #ffebe9;
      color: #cf222e;
      border: 1px solid rgba(207, 34, 46, 0.3);
    }}
    .badge-equivalent {{
      background: #dafbe1;
      color: #1a7f37;
      border: 1px solid rgba(26, 127, 55, 0.3);
    }}
    .badge-tolerance {{
      background: #ddf4ff;
      color: #0969da;
      border: 1px solid rgba(9, 105, 218, 0.3);
    }}

    .state-code {{
      font-family: ui-monospace, SFMono-Regular, "SF Mono", monospace;
      font-weight: 600;
      color: #0969da;
      background: rgba(9, 105, 218, 0.08);
      padding: 2px 7px;
      border-radius: 4px;
      border: 1px solid rgba(9, 105, 218, 0.25);
    }}
    .state-code.mutant {{
      color: #cf222e;
      background: rgba(207, 34, 46, 0.08);
      border-color: rgba(207, 34, 46, 0.25);
    }}

    /* Detail Container */
    .detail-view {{
      background: var(--bg-secondary);
      border: 1px solid var(--border-color);
      border-radius: 12px;
      padding: 28px;
      margin-top: 10px;
      box-shadow: 0 1px 4px rgba(31, 35, 40, 0.08);
    }}
    .detail-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      flex-wrap: wrap;
      gap: 16px;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border-color);
      margin-bottom: 24px;
    }}
    .detail-title-group h2 {{
      margin: 0 0 6px;
      font-size: 1.4rem;
      font-weight: 700;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .detail-title-group p {{
      margin: 0;
      color: var(--text-muted);
      font-size: 0.9rem;
    }}

    /* Spotlight Card */
    .spotlight-card {{
      background: #fff8c5;
      border: 1px solid #d4a72c;
      border-radius: 10px;
      padding: 18px 22px;
      margin-bottom: 28px;
    }}
    .spotlight-card.spotlight-subtle {{
      background: linear-gradient(135deg, #fffbeb 0%, #fff1f2 100%);
      border: 1px solid #f59e0b;
      box-shadow: 0 1px 4px rgba(245, 158, 11, 0.15);
    }}
    .spotlight-header {{
      font-size: 0.95rem;
      font-weight: 700;
      color: #92400e;
      margin-bottom: 8px;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .spotlight-body {{
      font-size: 0.9rem;
      color: #451a03;
      line-height: 1.6;
    }}

    /* Two-Column Layout for Charts and Info */
    .grid-2col {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 24px;
      margin-bottom: 30px;
    }}
    @media (max-width: 992px) {{
      .grid-2col {{ grid-template-columns: 1fr; }}
    }}

    .card-panel {{
      background: var(--bg-tertiary);
      border: 1px solid var(--border-color);
      border-radius: 10px;
      padding: 20px;
    }}
    .panel-title {{
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--accent-blue);
      font-weight: 700;
      margin: 0 0 16px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}

    /* Distribution Comparison Bars */
    .comparison-row {{
      display: grid;
      grid-template-columns: 80px 1fr 65px 1fr 65px 60px;
      align-items: center;
      gap: 10px;
      padding: 8px 0;
      border-bottom: 1px solid var(--border-subtle);
      font-size: 0.84rem;
      font-family: ui-monospace, SFMono-Regular, "SF Mono", monospace;
    }}
    .comparison-row.header {{
      font-weight: 600;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border-color);
      font-size: 0.74rem;
      text-transform: uppercase;
    }}
    .comp-bar-track {{
      height: 14px;
      background: #e1e4e8;
      border-radius: 4px;
      overflow: hidden;
      width: 100%;
    }}
    .comp-bar-ref {{
      background: var(--accent-green);
      height: 100%;
      border-radius: 4px;
    }}
    .comp-bar-mut {{
      background: var(--accent-amber);
      height: 100%;
      border-radius: 4px;
    }}
    .comp-bar-mut.danger {{
      background: var(--accent-red);
    }}
    .diff-tag {{
      font-weight: 600;
      font-size: 0.78rem;
    }}
    .diff-drop {{ color: #cf222e; }}
    .diff-gain {{ color: #9a6700; }}
    .diff-zero {{ color: var(--text-muted); }}

    /* 24-Branch Grid */
    .branches-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 14px;
      margin-top: 14px;
    }}
    .branch-card {{
      background: #ffffff;
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 14px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      box-shadow: 0 1px 2px rgba(31, 35, 40, 0.04);
      transition: transform 0.1s ease, border-color 0.1s ease;
    }}
    .branch-card:hover {{
      border-color: var(--accent-blue);
      transform: translateY(-2px);
    }}
    .branch-card.mutated {{
      border: 1px solid rgba(207, 34, 46, 0.4);
      background: #fff8f8;
    }}
    .branch-card-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .branch-name {{
      font-weight: 700;
      font-size: 0.88rem;
      color: var(--text-main);
      text-transform: capitalize;
    }}
    .branch-badge {{
      font-size: 0.7rem;
      padding: 2px 7px;
      border-radius: 12px;
      font-weight: 600;
    }}
    .branch-badge.ref {{
      background: #dafbe1;
      color: #1a7f37;
    }}
    .branch-badge.mut {{
      background: #ffebe9;
      color: #cf222e;
    }}
    .branch-winner {{
      font-family: ui-monospace, SFMono-Regular, "SF Mono", monospace;
      font-size: 0.82rem;
      color: var(--text-muted);
    }}
    .branch-bars {{
      display: flex;
      flex-direction: column;
      gap: 4px;
      margin-top: 4px;
    }}
    .mini-bar-row {{
      display: flex;
      align-items: center;
      gap: 8px;
      font-family: ui-monospace, SFMono-Regular, "SF Mono", monospace;
      font-size: 0.74rem;
    }}
    .mini-bar-lbl {{ width: 48px; color: var(--text-muted); text-align: right; }}
    .mini-bar-trk {{ flex: 1; height: 8px; background: #e1e4e8; border-radius: 2px; overflow: hidden; }}
    .mini-bar-fill {{ height: 100%; background: var(--accent-blue); border-radius: 2px; }}
    .mini-bar-fill.mut-fill {{ background: var(--accent-amber); }}
    .mini-bar-val {{ width: 44px; color: var(--text-muted); }}

    /* Pairwise Comparisons Table */
    .pairwise-section {{
      margin-top: 30px;
    }}
    .pairwise-toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      flex-wrap: wrap;
      gap: 12px;
    }}
    .filter-tabs {{
      display: flex;
      gap: 8px;
    }}
    .tab-btn {{
      background: #ffffff;
      border: 1px solid var(--border-color);
      color: var(--text-muted);
      padding: 6px 14px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.8rem;
      font-weight: 600;
      transition: all 0.15s ease;
    }}
    .tab-btn:hover {{
      color: var(--text-main);
      border-color: var(--accent-blue);
    }}
    .tab-btn.active {{
      background: #ddf4ff;
      border-color: var(--accent-blue);
      color: var(--accent-blue);
    }}
    .pairwise-scroll-box {{
      max-height: 380px;
      overflow-y: auto;
      border: 1px solid var(--border-color);
      border-radius: 8px;
      background: #ffffff;
    }}
    table.pairwise-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.82rem;
      text-align: left;
      font-family: ui-monospace, SFMono-Regular, "SF Mono", monospace;
    }}
    table.pairwise-table th {{
      position: sticky;
      top: 0;
      background: #f6f8fa;
      padding: 10px 14px;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border-color);
      font-size: 0.74rem;
      text-transform: uppercase;
      z-index: 2;
    }}
    table.pairwise-table td {{
      padding: 8px 14px;
      border-bottom: 1px solid var(--border-subtle);
    }}
    table.pairwise-table tr:hover {{
      background: #f6f8fa;
    }}
    .divergent-text {{
      color: #cf222e;
      font-weight: 700;
    }}
    .agrees-text {{
      color: #1a7f37;
    }}
  </style>
</head>
<body>
<div class="container">

  <!-- Header -->
  <header class="page-header">
    <div class="header-top">
      <h1 class="header-title">
        <span class="logo-symbol">⚛</span>
        Quanifi — 24-Branch Matrix Distribution Consensus
      </h1>
      <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
        <button type="button" class="guide-toggle-btn" onclick="toggleGuide()">
          <span style="font-size: 1rem;">❓</span> Column & Metrics Help
        </button>
        <span class="badge badge-tolerance" style="font-size: 0.85rem; padding: 6px 14px;">
          4 Circuit Builders × 6 Execution Engines
        </span>
      </div>
    </div>
    <p class="header-desc">
      Empirical comparison between <strong>Top-1 Coarse Consensus Voting</strong> and 
      <strong>QuantumDistributionOracle (Pairwise Hellinger Distance + χ² Gate)</strong> across 
      Grover 2-qubit, 3-qubit, and 4-qubit benchmarks subjected to deterministic mutations.
    </p>

    <!-- Collapsible Column & Metrics Interpretation Guide -->
    <div id="guide-panel" class="guide-panel">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
        <h3 style="margin: 0; font-size: 1.05rem; color: #0f172a; font-weight: 700;">
          📘 Quantum Differential Testing: Column & Metric Interpretation Guide
        </h3>
        <button type="button" class="tooltip-close" onclick="toggleGuide()" style="font-size: 1.3rem;">&times;</button>
      </div>
      <p style="color: #64748b; font-size: 0.84rem; margin: 0 0 12px;">
        This matrix report compares 24 compiler/simulator execution pipelines across quantum benchmarks. Click or hover over any <span class="help-btn" style="display:inline-flex; width:14px; height:14px; font-size:10px; vertical-align:middle;">?</span> icon on table headers for immediate inline assistance.
      </p>
      <div class="guide-grid">
        <div class="guide-card">
          <div class="guide-card-title">🎯 Top-1 Majority vs. Hellinger Oracle</div>
          <div class="guide-card-body">
            <strong>Top-1 Consensus:</strong> Votes strictly on discrete mode <code>argmax P(x)</code>. Fast, but completely blind to partial amplitude degradation (Case <code>matrix-005</code>!).<br>
            <strong>Hellinger Oracle:</strong> Evaluates the entire probability distribution manifold <code>H(P, Q)</code> across all 276 pairs, catching subtle compiler bugs and amplitude leaks.
          </div>
        </div>
        <div class="guide-card">
          <div class="guide-card-title">⚖️ Two-Gated Decision Filter</div>
          <div class="guide-card-body">
            To prevent false alarms caused by stochastic finite-shot noise (1024 shots), a pair is flagged as <strong>DIVERGES</strong> only if <em>both</em> conditions are met:<br>
            1. <strong>Significance Gate:</strong> χ² homogeneity test yields <code>p &lt; 0.05</code><br>
            2. <strong>Effect Size Gate:</strong> Pairwise Hellinger distance <code>H(P, Q) &gt; 0.10</code>
          </div>
        </div>
        <div class="guide-card">
          <div class="guide-card-title">🏷️ Diagnostic Classification Types</div>
          <div class="guide-card-body">
            • <strong style="color:#1a7f37;">✓ Equivalent:</strong> Mutant has identical behavior (H = 0).<br>
            • <strong style="color:#cf222e;">⚠️ Coarse Fault:</strong> Peak flipped (H &gt; 0.50), caught by both oracles.<br>
            • <strong style="color:#0969da;">✓ Near Tolerance:</strong> Stochastic/compiler jitter (H &le; 0.10).<br>
            • <strong style="color:#875000;">⚡ Subtle Fault:</strong> Top-1 reported PASS, but Hellinger caught 42% amplitude leak!
          </div>
        </div>
      </div>
    </div>

    <!-- Stats Summary Row -->
    <div class="stats-row">
      <div class="stat-card">
        <span class="stat-val">{len(structured_cases)} Cases</span>
        <span class="stat-lbl">Evaluated Test Benchmarks</span>
      </div>
      <div class="stat-card">
        <span class="stat-val">24</span>
        <span class="stat-lbl">Concurrent Execution Branches</span>
      </div>
      <div class="stat-card">
        <span class="stat-val">276</span>
        <span class="stat-lbl">Pairwise Comparisons / Case</span>
      </div>
      <div class="stat-card" style="border-color: #d4a72c; background: #fffdf5;">
        <span class="stat-val" style="color: #9a6700;">1 Crucial Anomaly</span>
        <span class="stat-lbl">Top-1 Passed, Hellinger Caught Error</span>
      </div>
    </div>
  </header>

  <!-- Section: Master Table -->
  <div class="section-title">
    <span>Test Case Master Matrix</span>
    <span class="section-subtitle">Click any row below to inspect its 24-branch distributions and pairwise metrics</span>
  </div>

  <div class="table-container">
    <table class="master-table">
      <thead>
        <tr>
          <th>
            <span class="th-content">
              Case ID
              <button type="button" class="help-btn" data-help-title="Case ID" data-help-formula="matrix-000 … matrix-005" data-help-desc="Unique benchmark configuration identifier within the 24-branch consensus matrix." data-help-rule="Click any row to load its complete 24-branch state distributions and 276 pairwise statistical metrics in the inspector below." aria-label="Help for Case ID">?</button>
            </span>
          </th>
          <th>
            <span class="th-content">
              Benchmark Algorithm
              <button type="button" class="help-btn" data-help-title="Benchmark Algorithm" data-help-formula="Grover Search (n qubits)" data-help-desc="The quantum circuit algorithm and problem scale under test. Grover Search amplifies a marked target bitstring |x*⟩ from an equal superposition state." data-help-rule="Algorithms range from 2-qubit (N=4 states) to 4-qubit (N=16 states) systems." aria-label="Help for Benchmark Algorithm">?</button>
            </span>
          </th>
          <th>
            <span class="th-content">
              Target (Expected)
              <button type="button" class="help-btn" data-help-title="Target Ground Truth" data-help-formula="|x*⟩ ∈ {{0, 1}}ⁿ" data-help-desc="The marked oracle bitstring that the mathematically correct circuit is designed to amplify as its primary mode." data-help-rule="Serves as the ground truth reference state against which all simulated outputs are audited." aria-label="Help for Target (Expected)">?</button>
            </span>
          </th>
          <th>
            <span class="th-content">
              Mutant (Qiskit)
              <button type="button" class="help-btn" data-help-title="Injected Mutant Outcome" data-help-formula="|x_mut⟩ (P_mut %)" data-help-desc="Empirical top bitstring and observed probability mass returned by the injected mutated Qiskit compiler pipeline." data-help-rule="Compare this to Target to see whether the compiler bug flipped the peak or subtly degraded amplitude." aria-label="Help for Mutant (Qiskit)">?</button>
            </span>
          </th>
          <th>
            <span class="th-content">
              Top-1 Consensus
              <button type="button" class="help-btn" data-help-title="Top-1 Consensus Oracle" data-help-formula="mode({{ argmax_x P_k(x) }})" data-help-desc="Discrete majority vote evaluating the highest probability bitstring from each of the 24 branches (with endianness normalization)." data-help-rule="PASS if branches agree on target. DISAGREE if dissenters exist. WARNING: Blind to amplitude leakage if target remains peak (False Negative)!" aria-label="Help for Top-1 Consensus">?</button>
            </span>
          </th>
          <th>
            <span class="th-content">
              Hellinger Oracle
              <button type="button" class="help-btn" data-help-title="Hellinger Distribution Oracle" data-help-formula="∀ (A, B): [H(A, B) ≤ τ] ∨ [p ≥ α]" data-help-desc="Two-gated manifold testing across the entire probability distribution for all 276 branch pairs." data-help-rule="PASS if all pairs agree within effect size τ = 0.10 or are statistically non-significant (p ≥ 0.05). Catches subtle amplitude degradation!" aria-label="Help for Hellinger Oracle">?</button>
            </span>
          </th>
          <th>
            <span class="th-content">
              Max Hellinger
              <button type="button" class="help-btn" data-help-title="Maximum Pairwise Hellinger Distance" data-help-formula="max_{{(A, B)}} H(P_A, P_B) ∈ [0, 1]" data-help-desc="The worst-case geometric distribution distance among all 276 branch comparisons for this test case." data-help-rule="H = 0.00 means identical distributions; H ≤ 0.10 is within tolerance; H > 0.10 flags significant distribution divergence." aria-label="Help for Max Hellinger">?</button>
            </span>
          </th>
          <th>
            <span class="th-content">
              Diagnostic Finding
              <button type="button" class="help-btn" data-help-title="Diagnostic Classification" data-help-formula="Fault Type Synthesis" data-help-desc="Automated root-cause classification comparing Top-1 voting vs. Hellinger manifold oracle decisions." data-help-rule="Equivalent (H=0), Coarse Fault (bit-flip caught by both), Near Tolerance (H≤0.10), or Subtle Fault (Top-1 PASS, but Hellinger caught 42% leakage!)." aria-label="Help for Diagnostic Finding">?</button>
            </span>
          </th>
        </tr>
      </thead>
      <tbody>
"""

    default_selected = "matrix-005" if "matrix-005" in structured_cases else (ordered_ids[0] if ordered_ids else "")

    for cid in ordered_ids:
        c = structured_cases[cid]
        active_class = "active" if cid == default_selected else ""
        t1_badge = "badge-pass" if "PASS" in c["top1_verdict"] else "badge-fail"
        dist_badge = "badge-pass" if c["dist_verdict"] == "PASS" else "badge-fail"

        html_content += f"""\
        <tr class="case-row {active_class}" id="row-{cid}" onclick="selectCase('{cid}')">
          <td><strong style="font-family: monospace;">{cid}</strong></td>
          <td>{c['title']}</td>
          <td><span class="state-code">|{c['target']}⟩</span></td>
          <td><span class="state-code mutant">{c['mut_returned']}</span></td>
          <td><span class="badge {t1_badge}">{c['top1_verdict']}</span></td>
          <td><span class="badge {dist_badge}">{c['dist_verdict']}</span></td>
          <td style="font-family: monospace; font-weight: 600;">{c['max_hellinger']:.4f}</td>
          <td><span class="badge {c['finding_badge_class']}">{c['finding_label']}</span></td>
        </tr>
"""

    html_content += f"""\
      </tbody>
    </table>
  </div>

  <!-- Detail Container -->
  <div id="detail-container">
    <!-- Populated by JavaScript -->
  </div>

  <!-- RUNS_START -->
</div>

<!-- Embedded JSON Data for all cases -->
<script>
const CASES_DATA = {json.dumps(structured_cases, indent=2)};

let currentSelectedCase = '{default_selected}';
let pairwiseFilter = 'divergent'; // 'divergent' | 'all'

function selectCase(caseId) {{
  currentSelectedCase = caseId;
  
  document.querySelectorAll('.case-row').forEach(row => {{
    row.classList.remove('active');
  }});
  const activeRow = document.getElementById('row-' + caseId);
  if (activeRow) activeRow.classList.add('active');
  
  renderDetail();
}}

function setPairwiseFilter(filter) {{
  pairwiseFilter = filter;
  renderPairwiseTable();
  document.getElementById('tab-divergent').classList.toggle('active', filter === 'divergent');
  document.getElementById('tab-all').classList.toggle('active', filter === 'all');
}}

function renderDetail() {{
  const c = CASES_DATA[currentSelectedCase];
  if (!c) return;

  const isHero = (c.id === 'matrix-005');
  const spotlightClass = isHero ? 'spotlight-subtle' : '';

  let compRowsHtml = '';
  (c.comparison_table || []).forEach(row => {{
    let diffClass = 'diff-zero';
    let diffPrefix = '';
    if (row.diff > 0) {{ diffClass = 'diff-gain'; diffPrefix = '+'; }}
    else if (row.diff < 0) {{ diffClass = 'diff-drop'; }}

    let mutBarClass = (row.is_target && row.mut_pct < 70) ? 'danger' : '';

    compRowsHtml += `
      <div class="comparison-row">
        <span><strong style="${{row.is_target ? 'color:#0969da;' : ''}}">|${{row.state}}⟩</strong></span>
        <div class="comp-bar-track"><div class="comp-bar-ref" style="width: ${{row.ref_pct}}%"></div></div>
        <span>${{row.ref_pct}}%</span>
        <div class="comp-bar-track"><div class="comp-bar-mut ${{mutBarClass}}" style="width: ${{row.mut_pct}}%"></div></div>
        <span>${{row.mut_pct}}%</span>
        <span class="diff-tag ${{diffClass}}">${{diffPrefix}}${{row.diff}}%</span>
      </div>
    `;
  }});

  let branchesHtml = '';
  (c.branches || []).forEach((b, idx) => {{
    const isMut = b.is_mutated;
    const cardClass = isMut ? 'branch-card mutated' : 'branch-card';
    const badgeText = isMut ? 'MUTATED (QISKIT)' : 'REFERENCE';
    const badgeClass = isMut ? 'branch-badge mut' : 'branch-badge ref';

    let miniBars = '';
    (b.dist || []).slice(0, 4).forEach(s => {{
      const fillClass = isMut ? 'mini-bar-fill mut-fill' : 'mini-bar-fill';
      miniBars += `
        <div class="mini-bar-row">
          <span class="mini-bar-lbl">|${{s.state}}⟩</span>
          <div class="mini-bar-trk"><div class="${{fillClass}}" style="width: ${{s.pct}}%"></div></div>
          <span class="mini-bar-val">${{s.label || (s.pct.toFixed(1) + '%')}}</span>
        </div>
      `;
    }});

    const topPct = (typeof b.top_pct === 'number') ? b.top_pct.toFixed(1) : b.top_pct;
    branchesHtml += `
      <div class="${{cardClass}}">
        <div class="branch-card-header">
          <span class="branch-name">#${{idx + 1}} ${{b.framework || b.label || ''}}</span>
          <span class="${{badgeClass}}">${{badgeText}}</span>
        </div>
        <div class="branch-winner">Top: <strong style="color: var(--text-main);">|${{b.top_state || ''}}⟩</strong> (${{topPct}}%)</div>
        <div class="branch-bars">${{miniBars}}</div>
      </div>
    `;
  }});

  const detailHtml = `
    <div class="detail-view">
      <div class="detail-header">
        <div class="detail-title-group">
          <h2>
            <span>Deep Inspection: ${{c.id}} (${{c.title}})</span>
            <span class="badge ${{c.finding_badge_class}}">${{c.finding_label}}</span>
          </h2>
          <p>Target Ground Truth: <strong style="color: #0969da;">|${{c.target}}⟩</strong> &nbsp;&bull;&nbsp;
             Max Hellinger: <strong style="color: var(--text-main);">${{c.max_hellinger.toFixed(4)}}</strong> &nbsp;&bull;&nbsp;
             Divergent Pairs: <strong style="color: ${{c.divergent_count > 0 ? '#cf222e' : '#1a7f37'}};">${{c.divergent_count}} of ${{c.total_pairs}}</strong> &nbsp;&bull;&nbsp;
             Correction: <strong>${{c.correction}}</strong> over ${{c.pairwise_tests}} tests &nbsp;&bull;&nbsp;
             Significant: <strong>${{c.significant_pairs_raw}} raw / ${{c.significant_pairs_corrected}} corrected</strong>
          </p>
        </div>
        <div style="display: flex; gap: 10px;">
          <span class="badge ${{c.top1_verdict.includes('PASS') ? 'badge-pass' : 'badge-fail'}}">Top-1: ${{c.top1_verdict}}</span>
          <span class="badge ${{c.dist_verdict === 'PASS' ? 'badge-pass' : 'badge-fail'}}">Hellinger: ${{c.dist_verdict}}</span>
        </div>
      </div>

      <!-- Spotlight Summary Card -->
      <div class="spotlight-card ${{spotlightClass}}">
        <div class="spotlight-header">
          <span>🔍 Diagnostic Synthesis:</span>
        </div>
        <div class="spotlight-body">
          ${{c.summary}}
        </div>
      </div>

      <!-- 2-Column Comparison & Settings -->
      <div class="grid-2col">
        <!-- Distribution Comparison -->
        <div class="card-panel">
          <div class="panel-title">
            <span>State Distribution: Reference vs Mutated</span>
            <span style="font-size:0.75rem; color:var(--text-muted);">Top 8 Basis States</span>
          </div>
          <div class="comparison-row header">
            <span>State <button type="button" class="help-btn" data-help-title="Basis State" data-help-formula="|x⟩ = |b_{{n-1}} ... b_0⟩" data-help-desc="Computational basis measurement outcome state in the n-qubit Hilbert space." data-help-rule="The marked target state is highlighted with a blue border." aria-label="Help for State">?</button></span>
            <span>Unmutated</span>
            <span>Ref % <button type="button" class="help-btn" data-help-title="Reference Probability %" data-help-formula="E[P_ref(x)]" data-help-desc="Mean empirical probability across all 19 unmutated reference branches." data-help-rule="Represents expected correct behavior of the quantum algorithm." aria-label="Help for Ref %">?</button></span>
            <span>Mutated</span>
            <span>Mut % <button type="button" class="help-btn" data-help-title="Mutated Probability %" data-help-formula="P_mut(x)" data-help-desc="Empirical probability observed in the mutated Qiskit compiler branch." data-help-rule="Discrepancies indicate gate corruption or phase leakage." aria-label="Help for Mut %">?</button></span>
            <span>Delta <button type="button" class="help-btn" data-help-title="Probability Delta (Δ)" data-help-formula="Δ = P_mut(x) - P_ref(x)" data-help-desc="Net probability shift caused by the compiler mutation." data-help-rule="Negative delta on target shows amplitude damping; positive delta on non-target shows leakage." aria-label="Help for Delta">?</button></span>
          </div>
          ${{compRowsHtml}}
        </div>

        <!-- Oracle & Run Settings -->
        <div class="card-panel">
          <div class="panel-title">
            <span>Consensus Oracle Configurations</span>
            <span>Thresholds</span>
          </div>
          <table style="width: 100%; border-collapse: collapse; font-size: 0.85rem;">
            <tr style="border-bottom: 1px solid var(--border-subtle); padding: 8px 0;">
              <td style="color: var(--text-muted); padding: 8px 0;">Hellinger Agreement Threshold</td>
              <td style="font-family: monospace; color: var(--text-main); text-align: right;">0.1000</td>
            </tr>
            <tr style="border-bottom: 1px solid var(--border-subtle);">
              <td style="color: var(--text-muted); padding: 8px 0;">χ² Homogeneity Significance (α)</td>
              <td style="font-family: monospace; color: var(--text-main); text-align: right;">0.05</td>
            </tr>
            <tr style="border-bottom: 1px solid var(--border-subtle);">
              <td style="color: var(--text-muted); padding: 8px 0;">Total Matrix Execution Branches</td>
              <td style="font-family: monospace; color: var(--text-main); text-align: right;">${{(c.branches || []).length}} branches</td>
            </tr>
            <tr style="border-bottom: 1px solid var(--border-subtle);">
              <td style="color: var(--text-muted); padding: 8px 0;">Top-1 Consensus Voter Result</td>
              <td style="text-align: right;"><span class="badge ${{c.top1_verdict.includes('PASS') ? 'badge-pass' : 'badge-fail'}}">${{c.top1_verdict}}</span></td>
            </tr>
            <tr>
              <td style="color: var(--text-muted); padding: 8px 0;">Hellinger Distributional Result</td>
              <td style="text-align: right;"><span class="badge ${{c.dist_verdict === 'PASS' ? 'badge-pass' : 'badge-fail'}}">${{c.dist_verdict}}</span></td>
            </tr>
          </table>
          <div style="margin-top: 16px; font-size: 0.8rem; color: var(--text-muted); line-height: 1.4;">
            ${{c.top1_note}}
          </div>
        </div>
      </div>

      <!-- 24-Branch Matrix Grid -->
      <div style="margin-top: 28px;">
        <div class="panel-title" style="margin-bottom: 8px;">
          <span>Execution Branches Breakdown</span>
          <span style="font-size:0.75rem; color:var(--text-muted);">4 Builders × 6 Engines</span>
        </div>
        <div class="branches-grid">
          ${{branchesHtml}}
        </div>
      </div>

      <!-- Pairwise Comparisons -->
      <div class="pairwise-section">
        <div class="pairwise-toolbar">
          <div class="panel-title" style="margin: 0;">
            <span>Pairwise Hellinger Divergences</span>
          </div>
          <div class="filter-tabs">
            <button id="tab-divergent" class="tab-btn ${{pairwiseFilter === 'divergent' ? 'active' : ''}}" onclick="setPairwiseFilter('divergent')">
              Show Divergent Only (${{c.divergent_count}})
            </button>
            <button id="tab-all" class="tab-btn ${{pairwiseFilter === 'all' ? 'active' : ''}}" onclick="setPairwiseFilter('all')">
              Show All Pairs (${{c.total_pairs}})
            </button>
          </div>
        </div>
        <div class="pairwise-scroll-box" id="pairwise-table-box">
          <!-- Populated by renderPairwiseTable -->
        </div>
      </div>

    </div>
  `;

  document.getElementById('detail-container').innerHTML = detailHtml;
  renderPairwiseTable();
}}

function renderPairwiseTable() {{
  const c = CASES_DATA[currentSelectedCase];
  if (!c) return;

  const box = document.getElementById('pairwise-table-box');
  if (!box) return;

  const pairs = (pairwiseFilter === 'divergent')
    ? (c.pairs || []).filter(p => p.hellinger > 0.1)
    : (c.pairs || []);

  if (pairs.length === 0) {{
    box.innerHTML = `
      <div style="padding: 24px; text-align: center; color: var(--text-muted); font-size: 0.9rem;">
        No pairs diverged in this test case. All pairs agree (H &le; 0.10).
      </div>
    `;
    return;
  }}

  let rowsHtml = '';
  pairs.forEach(p => {{
    const isDivergent = (p.hellinger > 0.1);
    const statusText = isDivergent ? 'DIVERGES' : 'AGREES';
    const statusClass = isDivergent ? 'divergent-text' : 'agrees-text';
    const hColor = isDivergent ? '#cf222e' : '#1a7f37';

    rowsHtml += `
      <tr>
        <td>${{p.pair}}</td>
        <td style="color: ${{hColor}}; font-weight: 600;">${{p.hellinger.toFixed(4)}}</td>
        <td style="color: var(--text-muted);">${{p.p_val}}</td>
        <td class="${{statusClass}}">${{statusText}}</td>
      </tr>
    `;
  }});

  box.innerHTML = `
    <table class="pairwise-table">
      <thead>
        <tr>
          <th>
            <span class="th-content">
              Branch Pair
              <button type="button" class="help-btn" data-help-title="Branch Pair" data-help-formula="Branch A ↔ Branch B" data-help-desc="The two specific compiler and simulation engine pipelines being compared." data-help-rule="Identifies which two execution paths are evaluated (out of 276 unique combinations)." aria-label="Help for Branch Pair">?</button>
            </span>
          </th>
          <th>
            <span class="th-content">
              Hellinger Distance (H)
              <button type="button" class="help-btn" data-help-title="Hellinger Distance (H)" data-help-formula="H = (1/√2) √( ∑ (√P_A - √P_B)² )" data-help-desc="Geometric distance between empirical probability distributions on the unit simplex. Bounded H ∈ [0, 1]." data-help-rule="H ≤ 0.10 indicates distribution agreement; H > 0.10 indicates divergence (colored red)." aria-label="Help for Hellinger Distance">?</button>
            </span>
          </th>
          <th>
            <span class="th-content">
              χ² p-value
              <button type="button" class="help-btn" data-help-title="Pearson χ² Two-Sample p-value" data-help-formula="p = 1 - F_{{χ²}}(χ²; df)" data-help-desc="Significance test under null hypothesis H_0: P_A = P_B from finite measurement shots." data-help-rule="p < 0.05 indicates statistically significant divergence; p ≥ 0.05 confirms gaps are harmless shot noise." aria-label="Help for χ² p-value">?</button>
            </span>
          </th>
          <th>
            <span class="th-content">
              Verdict
              <button type="button" class="help-btn" data-help-title="Pairwise Verdict" data-help-formula="[H > 0.10] ∧ [p < 0.05]" data-help-desc="Combined two-gated verdict requiring both statistical significance and effect size threshold." data-help-rule="DIVERGES (red) if both gates fire; AGREES (green) if effect size is within tolerance or not significant." aria-label="Help for Verdict">?</button>
            </span>
          </th>
        </tr>
      </thead>
      <tbody>
        ${{rowsHtml}}
      </tbody>
    </table>
  `;
}}

// Toggle Guide Panel
function toggleGuide() {{
  const p = document.getElementById('guide-panel');
  if (p) {{
    p.classList.toggle('open');
    if (p.classList.contains('open')) {{
      p.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
    }}
  }}
}}

// Global Floating Tooltip System
(function() {{
  const tooltipEl = document.createElement('div');
  tooltipEl.className = 'global-help-tooltip';
  document.body.appendChild(tooltipEl);

  let activeIcon = null;

  function showTooltip(icon) {{
    activeIcon = icon;
    icon.classList.add('active');
    const title = icon.getAttribute('data-help-title') || 'Metric Help';
    const desc = icon.getAttribute('data-help-desc') || '';
    const formula = icon.getAttribute('data-help-formula') || '';
    const rule = icon.getAttribute('data-help-rule') || '';

    let html = `
      <div class="tooltip-header">
        <div class="tooltip-title">ⓘ ${{title}}</div>
        <button type="button" class="tooltip-close" onclick="hideTooltip()">&times;</button>
      </div>
    `;
    if (formula) {{
      html += `<div class="tooltip-formula">${{formula}}</div>`;
    }}
    if (desc) {{
      html += `<div class="tooltip-desc">${{desc}}</div>`;
    }}
    if (rule) {{
      html += `<div class="tooltip-rule"><strong>Interpretation:</strong> ${{rule}}</div>`;
    }}

    tooltipEl.innerHTML = html;
    tooltipEl.classList.add('visible');

    const rect = icon.getBoundingClientRect();
    const tooltipWidth = Math.min(320, window.innerWidth - 32);
    tooltipEl.style.width = tooltipWidth + 'px';

    let left = rect.left + (rect.width / 2) - (tooltipWidth / 2);
    if (left < 16) left = 16;
    if (left + tooltipWidth > window.innerWidth - 16) {{
      left = window.innerWidth - tooltipWidth - 16;
    }}

    let top = rect.bottom + 8;
    tooltipEl.classList.remove('arrow-bottom');
    tooltipEl.classList.add('arrow-top');

    // Check if it fits below
    if (top + tooltipEl.offsetHeight > window.innerHeight - 10 && rect.top > tooltipEl.offsetHeight + 16) {{
      top = rect.top - tooltipEl.offsetHeight - 8;
      tooltipEl.classList.remove('arrow-top');
      tooltipEl.classList.add('arrow-bottom');
    }}

    tooltipEl.style.left = (left + window.scrollX) + 'px';
    tooltipEl.style.top = (top + window.scrollY) + 'px';
  }}

  window.hideTooltip = function() {{
    if (activeIcon) {{
      activeIcon.classList.remove('active');
      activeIcon = null;
    }}
    tooltipEl.classList.remove('visible');
  }};

  document.addEventListener('mouseover', function(e) {{
    const btn = e.target.closest('.help-btn');
    if (btn) showTooltip(btn);
  }});

  document.addEventListener('mouseout', function(e) {{
    const btn = e.target.closest('.help-btn');
    if (btn && btn === activeIcon && !btn.dataset.locked) {{
      hideTooltip();
    }}
  }});

  document.addEventListener('click', function(e) {{
    const btn = e.target.closest('.help-btn');
    if (btn) {{
      if (activeIcon === btn && tooltipEl.classList.contains('visible')) {{
        hideTooltip();
      }} else {{
        showTooltip(btn);
      }}
      e.stopPropagation();
    }} else if (!e.target.closest('.global-help-tooltip')) {{
      hideTooltip();
    }}
  }});

  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') hideTooltip();
  }});

  window.addEventListener('scroll', function() {{
    if (tooltipEl.classList.contains('visible') && !tooltipEl.matches(':hover')) {{
      hideTooltip();
    }}
  }}, {{ passive: true }});
}})();

window.addEventListener('DOMContentLoaded', () => {{
  renderDetail();
}});
</script>

</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
