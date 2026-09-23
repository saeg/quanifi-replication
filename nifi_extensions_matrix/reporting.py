import os
import datetime
import html as html_lib

SENTINEL = "<!-- RUNS_START -->"

_BASE_CSS = """\
    *, *::before, *::after { box-sizing: border-box; }
    body {
      font-family: 'Segoe UI', system-ui, sans-serif;
      background: #ffffff;
      color: #24292f;
      margin: 0;
      padding: 24px;
    }
    h1 {
      color: #0969da;
      font-size: 1.5rem;
      border-bottom: 1px solid #d0d7de;
      padding-bottom: 12px;
      margin-bottom: 24px;
    }
    h3 {
      color: #0969da;
      font-size: 0.85rem;
      margin: 0 0 12px;
      text-transform: uppercase;
      letter-spacing: 0.07em;
    }
    .run-card {
      background: #f6f8fa;
      border: 1px solid #d0d7de;
      border-radius: 10px;
      margin-bottom: 32px;
      overflow: hidden;
    }
    .run-header {
      background: #eaeef2;
      padding: 12px 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid #d0d7de;
    }
    .run-title { color: #0969da; font-weight: 600; font-size: 0.95rem; }
    .run-time  { color: #8c959f; font-size: 0.82rem; font-family: monospace; }
    .run-footer { display: flex; flex-wrap: wrap; gap: 16px; border-top: 1px solid #d0d7de; padding: 16px; }
    .run-footer .panel { flex: 1 1 260px; min-width: 240px; }
    .panel { padding: 16px; flex: 1; min-width: 0; }
    table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
    th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #d0d7de; }
    th { color: #656d76; font-weight: 600; }
    td:first-child { color: #656d76; font-family: monospace; font-size: 0.8rem; }
    td:last-child  { color: #1f2328; font-family: monospace; }
    .bar-chart { display: flex; flex-direction: column; gap: 8px; }
    .bar-row   { display: flex; align-items: center; gap: 10px; }
    .bar-label { font-family: monospace; color: #656d76; width: 56px; text-align: right;
                 flex-shrink: 0; font-size: 0.82rem; }
    .bar-track { flex: 1; background: #d0d7de; border-radius: 4px; height: 18px; overflow: hidden; }
    .bar-count { font-family: monospace; color: #656d76; font-size: 0.78rem;
                 width: 110px; flex-shrink: 0; }"""


def page_template(title: str, extra_css: str = "") -> str:
    escaped = html_lib.escape(title)
    css = _BASE_CSS + ("\n" + extra_css if extra_css else "")
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quanifi &mdash; {escaped}</title>
  <style>
{css}
  </style>
</head>
<body>
  <h1>&#x269B; Quanifi &mdash; {escaped}</h1>
  {SENTINEL}
</body>
</html>"""


def write_card(output_path: str, card_html: str, page_html: str) -> None:
    if os.path.exists(output_path):
        with open(output_path, "r", encoding="utf-8") as f:
            existing = f.read()
        updated = (
            existing.replace(SENTINEL, SENTINEL + "\n" + card_html, 1)
            if SENTINEL in existing
            else page_html.replace(SENTINEL, SENTINEL + "\n" + card_html)
        )
    else:
        updated = page_html.replace(SENTINEL, SENTINEL + "\n" + card_html)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(updated)


def bar_rows(dist: dict, fill_class: str = "bar-fill", limit: int = 12) -> str:
    """Build HTML bar-chart rows from a distribution.

    Integer values are treated as raw counts and displayed as 'N (P%)'.
    Float values are treated as probabilities and displayed as 'P%'.
    Both are normalised before computing widths, so either form works.
    """
    total = sum(dist.values()) or 1
    rows = ""
    for state, val in list(dist.items())[:limit]:
        pct = val / total * 100
        label = f"{val}&nbsp;({pct:.1f}%)" if isinstance(val, int) else f"{pct:.1f}%"
        rows += (
            f'<div class="bar-row">'
            f'<span class="bar-label">|{html_lib.escape(state)}&#x27E9;</span>'
            f'<div class="bar-track">'
            f'<div class="{fill_class}" style="width:{pct:.1f}%"></div>'
            f'</div>'
            f'<span class="bar-count">{label}</span>'
            f'</div>\n'
        )
    return rows


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
