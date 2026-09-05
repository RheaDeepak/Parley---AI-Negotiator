"""Generates dashboard.html from Parley's audit trail (Milestone 7).

Reads audits/negotiation.log (real debugging/demo history) AND
audits/dashboard_seed.log (bulk synthetic history from
scripts/generate_negotiation_history.py) together, groups entries by
`negotiation_id` (NEGOTIATION_SPEC.md Section 4D), and writes a single
self-contained dashboard.html: a summary-stats row plus a "Recent
Negotiations" table. Plain CSS, no framework, no build step -- opens
directly via file://.

Every number on the page is computed from the entries actually present in
the two log files; nothing is hardcoded or estimated. An entry with no
`negotiation_id` (any real negotiation.log entry logged before this
milestone) cannot be attributed to a specific negotiation without a
heuristic -- per an explicit decision NOT to use one, such entries are
counted and reported on the page as excluded, never silently dropped or
guessed into a group.

Section 2Z (2026-09-05): the actual log-reading/grouping/stat-computation
logic now lives in src/dashboard.py (compute_dashboard_data()), shared
with src/api.py's GET /api/dashboard-data -- this script is a thin
consumer of that same function, rendering its result to static HTML.
This script's own behavior/output/CLI are unchanged; it remains the
fallback for generating a one-shot snapshot without the API server
running (the live view is dashboard.html itself, which now fetches from
the API directly -- see frontend note there).

Usage:
    python scripts/generate_dashboard.py
        [--logs audits/negotiation.log audits/dashboard_seed.log]
        [--out dashboard.html] [--recent 20]
"""
import argparse
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import DEFAULT_LOG_PATHS, DEFAULT_RECENT_COUNT, UNKNOWN_MERCHANT_LABEL, compute_dashboard_data

DEFAULT_OUT_PATH = "dashboard.html"


def _fmt_price(value, currency):
    if value is None:
        return "—"
    prefix = f"{currency} " if currency else ""
    return f"{prefix}{value:,.2f}"


def _fmt_pct(value):
    return "—" if value is None else f"{value:.1f}%"


_OUTCOME_CSS_CLASS = {
    "Accepted": "outcome-accepted",
    "Rejected": "outcome-rejected",
    "Rolled back": "outcome-rolledback",
}


def render_html(stats, recent_summaries, meta, merchant_breakdown, merchant_names):
    rows = []
    for s in recent_summaries:
        css_class = _OUTCOME_CSS_CLASS.get(s["outcome"], "outcome-other")
        merchant_label = merchant_names.get(s["merchant_id"], s["merchant_id"]) if s["merchant_id"] else UNKNOWN_MERCHANT_LABEL
        rows.append(
            "<tr>"
            f'<td class="mono" title="{html.escape(s["negotiation_id"])}">{html.escape(s["negotiation_id"][:12])}…</td>'
            f'<td>{html.escape(s["product_name"] or "—")}</td>'
            f'<td>{html.escape(merchant_label)}</td>'
            f'<td class="num">{html.escape(_fmt_price(s["final_price"], s["currency"]))}</td>'
            f'<td><span class="badge {css_class}">{html.escape(s["outcome"])}</span></td>'
            "</tr>"
        )
    rows_html = "\n".join(rows) if rows else '<tr><td colspan="5" class="empty">No negotiations found.</td></tr>'

    merchant_rows = []
    for m in merchant_breakdown:
        merchant_rows.append(
            "<tr>"
            f'<td>{html.escape(m["merchant_name"])}</td>'
            f'<td class="num">{m["total_negotiations"]}</td>'
            f'<td class="num">{m["accepted"]}</td>'
            f'<td class="num">{m["rejected"]}</td>'
            f'<td class="num">{m["rolled_back"]}</td>'
            f'<td class="num">{_fmt_pct(m["avg_discount_pct"])}</td>'
            f'<td class="num">{html.escape(_fmt_price(m["total_revenue"], m["currency"]))}</td>'
            "</tr>"
        )
    merchant_rows_html = "\n".join(merchant_rows) if merchant_rows else '<tr><td colspan="7" class="empty">No negotiations found.</td></tr>'

    other_note = ""
    if stats["other"]:
        other_note = f'<p class="note">{stats["other"]} negotiation(s) had an outcome other than Accepted/Rejected/Rolled back (e.g. approval declined or incomplete) and are excluded from the three counts above but included in avg rounds.</p>'

    excluded_note = ""
    if meta["ungrouped_count"]:
        excluded_note = (
            f'<p class="note">{meta["ungrouped_count"]} log entr'
            f'{"y" if meta["ungrouped_count"] == 1 else "ies"} had no negotiation_id (pre-Milestone-7 data) '
            "and could not be attributed to a specific negotiation, so they are excluded from every stat "
            "and table row on this page.</p>"
        )

    sources_html = ", ".join(f"{html.escape(p)} ({n} entries)" for p, n in meta["source_counts"].items())

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Parley Negotiation Dashboard</title>
<style>
  :root {{
    --bg: #f6f7f9;
    --card-bg: #ffffff;
    --text: #1a1d23;
    --muted: #6b7280;
    --border: #e5e7eb;
    --accent: #2563eb;
    --green-bg: #dcfce7; --green-text: #166534;
    --red-bg: #fee2e2; --red-text: #991b1b;
    --amber-bg: #fef3c7; --amber-text: #92400e;
    --gray-bg: #e5e7eb; --gray-text: #374151;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px; background: var(--bg); color: var(--text);
    font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
  }}
  h1 {{ margin: 0 0 4px 0; font-size: 22px; }}
  .subtitle {{ color: var(--muted); font-size: 13px; margin: 0 0 28px 0; }}
  .stats {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 14px; margin-bottom: 32px;
  }}
  .stat-card {{
    background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px 18px;
  }}
  .stat-label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }}
  .stat-value {{ font-size: 24px; font-weight: 600; margin-top: 6px; }}
  .table-scroll {{
    overflow-x: auto; background: var(--card-bg);
    border: 1px solid var(--border); border-radius: 10px;
  }}
  table {{
    width: 100%; min-width: 560px; border-collapse: collapse;
  }}
  th, td {{ text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--border); font-size: 14px; }}
  th {{ background: #fafbfc; font-size: 12px; text-transform: uppercase; color: var(--muted); letter-spacing: 0.03em; }}
  tr:last-child td {{ border-bottom: none; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.mono {{ font-family: ui-monospace, Consolas, monospace; font-size: 13px; color: var(--muted); }}
  td.empty {{ text-align: center; color: var(--muted); padding: 24px; }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }}
  .outcome-accepted {{ background: var(--green-bg); color: var(--green-text); }}
  .outcome-rejected {{ background: var(--red-bg); color: var(--red-text); }}
  .outcome-rolledback {{ background: var(--amber-bg); color: var(--amber-text); }}
  .outcome-other {{ background: var(--gray-bg); color: var(--gray-text); }}
  h2 {{ font-size: 16px; margin: 0 0 12px 0; }}
  .note {{ font-size: 12px; color: var(--muted); margin: 10px 2px 0 2px; }}
  footer {{ margin-top: 28px; font-size: 12px; color: var(--muted); }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #16181d; --card-bg: #1e2128; --text: #e6e8eb; --muted: #9aa1ac; --border: #2b2f38;
      --green-bg: #113321; --green-text: #4ade80;
      --red-bg: #3a1414; --red-text: #f87171;
      --amber-bg: #3a2c0f; --amber-text: #fbbf24;
      --gray-bg: #2b2f38; --gray-text: #b8bec9;
    }}
    th {{ background: #1a1d23; }}
  }}
</style>
</head>
<body>
  <h1>Parley Negotiation Dashboard</h1>
  <p class="subtitle">Generated from {html.escape(sources_html)}. {stats["total_negotiations"]} negotiations grouped by negotiation_id.</p>

  <div class="stats">
    <div class="stat-card"><div class="stat-label">Total negotiations</div><div class="stat-value">{stats["total_negotiations"]}</div></div>
    <div class="stat-card"><div class="stat-label">Accepted</div><div class="stat-value">{stats["accepted"]}</div></div>
    <div class="stat-card"><div class="stat-label">Rejected</div><div class="stat-value">{stats["rejected"]}</div></div>
    <div class="stat-card"><div class="stat-label">Rolled back</div><div class="stat-value">{stats["rolled_back"]}</div></div>
    <div class="stat-card"><div class="stat-label">Avg rounds</div><div class="stat-value">{stats["avg_rounds"]}</div></div>
    <div class="stat-card"><div class="stat-label">Avg discount</div><div class="stat-value">{_fmt_pct(stats["avg_discount_pct"])}</div></div>
    <div class="stat-card"><div class="stat-label">Total revenue</div><div class="stat-value">{_fmt_price(stats["total_revenue"], stats["currency"])}</div></div>
  </div>
  {other_note}
  {excluded_note}

  <h2>By Merchant</h2>
  <div class="table-scroll">
    <table>
      <thead>
        <tr><th>Merchant</th><th>Total</th><th>Accepted</th><th>Rejected</th><th>Rolled back</th><th>Avg discount</th><th>Revenue</th></tr>
      </thead>
      <tbody>
        {merchant_rows_html}
      </tbody>
    </table>
  </div>

  <h2 style="margin-top: 32px;">Recent Negotiations</h2>
  <div class="table-scroll">
    <table>
      <thead>
        <tr><th>Negotiation ID</th><th>Product</th><th>Merchant</th><th>Final price</th><th>Outcome</th></tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </div>

  <footer>Generated by scripts/generate_dashboard.py &middot; {html.escape(meta["generated_at"])}</footer>
</body>
</html>
"""


def build_dashboard(log_paths, recent_count, merchants_path=None):
    kwargs = {"merchants_path": merchants_path} if merchants_path else {}
    data = compute_dashboard_data(log_paths, recent_count, **kwargs)
    html_out = render_html(data["stats"], data["recent"], data["meta"], data["merchant_breakdown"], data["merchant_names"])
    return html_out, data["stats"], data["meta"]["ungrouped_count"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--logs", nargs="+", default=DEFAULT_LOG_PATHS, help=f"Audit log paths to read (default {DEFAULT_LOG_PATHS}).")
    parser.add_argument("--out", default=DEFAULT_OUT_PATH, help=f"Output HTML path (default {DEFAULT_OUT_PATH}).")
    parser.add_argument("--recent", type=int, default=DEFAULT_RECENT_COUNT, help=f"Rows in Recent Negotiations (default {DEFAULT_RECENT_COUNT}).")
    args = parser.parse_args()

    html_out, stats, ungrouped_count = build_dashboard(args.logs, args.recent)
    Path(args.out).write_text(html_out, encoding="utf-8")

    print(f"Wrote {args.out}")
    print(f"  total_negotiations={stats['total_negotiations']} accepted={stats['accepted']} "
          f"rejected={stats['rejected']} rolled_back={stats['rolled_back']} other={stats['other']}")
    print(f"  avg_rounds={stats['avg_rounds']} avg_discount_pct={stats['avg_discount_pct']} "
          f"total_revenue={stats['total_revenue']} currency={stats['currency']}")
    if ungrouped_count:
        print(f"  NOTE: {ungrouped_count} entries had no negotiation_id and were excluded (see dashboard page note).")


if __name__ == "__main__":
    main()
