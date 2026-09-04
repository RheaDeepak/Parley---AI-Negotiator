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

Usage:
    python scripts/generate_dashboard.py
        [--logs audits/negotiation.log audits/dashboard_seed.log]
        [--out dashboard.html] [--recent 20]
"""
import argparse
import html
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_LOG_PATHS = ["audits/negotiation.log", "audits/dashboard_seed.log"]
DEFAULT_MERCHANTS_PATH = "data/merchants.json"
DEFAULT_OUT_PATH = "dashboard.html"
DEFAULT_RECENT_COUNT = 20
UNKNOWN_MERCHANT_LABEL = "Unknown / no merchant"

# Priority order matters: a negotiation whose entries include BOTH an
# earlier payment_rollback and a later payment_completed (a failed
# attempt followed by a successful explicit retry_payment() call, both
# sharing the same negotiation_id) is genuinely "Accepted" overall --
# payment_completed is checked first for exactly that reason.
_OUTCOME_ACTION_PRIORITY = [
    ("payment_completed", "Accepted"),
    ("payment_rollback", "Rolled back"),
    ("insufficient_inventory", "Rolled back"),
    ("approval_declined", "Declined"),
    ("reject", "Rejected"),
]


def load_entries(paths):
    """Reads one or more JSONL audit log files. Missing files are
    skipped (a fresh checkout may not have generated dashboard_seed.log
    yet); malformed lines are skipped too rather than aborting the whole
    read. Returns (entries, per_path_line_counts) so the caller can
    report real read counts on the page -- not an estimate."""
    entries = []
    counts = {}
    for path in paths:
        p = Path(path)
        if not p.exists():
            counts[path] = 0
            continue
        n = 0
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                    n += 1
                except json.JSONDecodeError:
                    continue
        counts[path] = n
    return entries, counts


def load_merchant_names(path=DEFAULT_MERCHANTS_PATH):
    """Resolves merchant_id -> merchant_name from data/merchants.json --
    a small, static lookup, so the audit log itself only needs to carry
    the id (NEGOTIATION_SPEC.md Section 4D). Missing file / bad entries
    degrade to an empty map, not a crash -- the dashboard still works,
    just showing raw merchant_ids instead of names."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        merchants = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {m["merchant_id"]: m["merchant_name"] for m in merchants if "merchant_id" in m and "merchant_name" in m}


def group_by_negotiation(entries):
    """Groups entries by their `negotiation_id` field. Entries with no
    negotiation_id (pre-Milestone-7 log data) are returned separately,
    never grouped by a guess (e.g. timestamp proximity) -- see module
    docstring. Each group's entries are sorted by timestamp so
    "last entry" / "last accept" lookups downstream are chronologically
    correct even though the two source files are read in file order, not
    a merged time order."""
    groups = defaultdict(list)
    ungrouped_count = 0
    for entry in entries:
        negotiation_id = entry.get("negotiation_id")
        if negotiation_id is None:
            ungrouped_count += 1
            continue
        groups[negotiation_id].append(entry)
    for group_entries in groups.values():
        group_entries.sort(key=lambda e: e.get("timestamp", ""))
    return groups, ungrouped_count


def classify_outcome(group_entries):
    actions_present = {e.get("action") for e in group_entries}
    for action, label in _OUTCOME_ACTION_PRIORITY:
        if action in actions_present:
            return label
    return "Incomplete"


def _last_accept_offer(group_entries):
    for entry in reversed(group_entries):
        if entry.get("action") == "accept" and entry.get("offer"):
            return entry["offer"]
    return None


def _first_present(group_entries, field):
    for entry in group_entries:
        if entry.get(field) is not None:
            return entry[field]
    return None


def _completed_revenue(group_entries):
    total = 0.0
    for entry in group_entries:
        if entry.get("action") == "payment_completed" and entry.get("payment"):
            amount = entry["payment"].get("amount")
            if amount is not None:
                total += amount / 100
    return total


def summarize_negotiation(negotiation_id, group_entries):
    """Reduces one negotiation's full entry list to the fields the
    dashboard needs. Every value here traces back to a real field on a
    real entry -- see the individual helpers above."""
    outcome = classify_outcome(group_entries)
    rounds = sum(
        1 for e in group_entries
        if e.get("agent") == "merchant-agent" and e.get("action") in ("counter", "accept", "reject")
    )
    product_name = _first_present(group_entries, "product_name")
    list_price = _first_present(group_entries, "list_price")
    merchant_id = _first_present(group_entries, "merchant_id")
    accepted_offer = _last_accept_offer(group_entries)
    final_price = accepted_offer["price"] if accepted_offer else None
    qty = accepted_offer["qty"] if accepted_offer else None
    discount_pct = None
    if final_price is not None and list_price:
        discount_pct = round((list_price - final_price) / list_price * 100, 2)
    revenue = _completed_revenue(group_entries)
    currency = _first_present(group_entries, "payment")
    currency = currency.get("currency") if isinstance(currency, dict) else None
    last_timestamp = group_entries[-1].get("timestamp", "")

    return {
        "negotiation_id": negotiation_id,
        "product_name": product_name,
        "list_price": list_price,
        "merchant_id": merchant_id,
        "final_price": final_price,
        "qty": qty,
        "outcome": outcome,
        "rounds": rounds,
        "discount_pct": discount_pct,
        "revenue": revenue,
        "currency": currency,
        "last_timestamp": last_timestamp,
    }


def summarize_all(groups):
    return [summarize_negotiation(nid, entries) for nid, entries in groups.items()]


def compute_summary_stats(summaries):
    total = len(summaries)
    accepted = sum(1 for s in summaries if s["outcome"] == "Accepted")
    rejected = sum(1 for s in summaries if s["outcome"] == "Rejected")
    rolled_back = sum(1 for s in summaries if s["outcome"] == "Rolled back")
    other = total - accepted - rejected - rolled_back

    avg_rounds = round(sum(s["rounds"] for s in summaries) / total, 2) if total else 0.0

    discounts = [s["discount_pct"] for s in summaries if s["discount_pct"] is not None]
    avg_discount_pct = round(sum(discounts) / len(discounts), 2) if discounts else None

    total_revenue = round(sum(s["revenue"] for s in summaries), 2)
    currency = next((s["currency"] for s in summaries if s["currency"]), None)

    return {
        "total_negotiations": total,
        "accepted": accepted,
        "rejected": rejected,
        "rolled_back": rolled_back,
        "other": other,
        "avg_rounds": avg_rounds,
        "avg_discount_pct": avg_discount_pct,
        "total_revenue": total_revenue,
        "currency": currency,
    }


def compute_merchant_breakdown(summaries, merchant_names):
    """Same shape as compute_summary_stats(), split per merchant_id.
    Negotiations with no merchant_id at all (e.g. the single-SKU
    merchant_policy.json fallback, which predates multi-merchant support)
    are grouped under UNKNOWN_MERCHANT_LABEL rather than dropped -- real,
    if unattributed, data stays visible instead of silently vanishing.
    Sorted by total_negotiations descending, so the busiest merchant
    leads."""
    by_merchant = defaultdict(list)
    for s in summaries:
        by_merchant[s["merchant_id"]].append(s)

    rows = []
    for merchant_id, group in by_merchant.items():
        stats = compute_summary_stats(group)
        rows.append({
            "merchant_id": merchant_id,
            "merchant_name": merchant_names.get(merchant_id, merchant_id) if merchant_id else UNKNOWN_MERCHANT_LABEL,
            **stats,
        })
    rows.sort(key=lambda r: r["total_negotiations"], reverse=True)
    return rows


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


def build_dashboard(log_paths, recent_count, merchants_path=DEFAULT_MERCHANTS_PATH):
    entries, source_counts = load_entries(log_paths)
    groups, ungrouped_count = group_by_negotiation(entries)
    summaries = summarize_all(groups)
    stats = compute_summary_stats(summaries)
    merchant_names = load_merchant_names(merchants_path)
    merchant_breakdown = compute_merchant_breakdown(summaries, merchant_names)
    recent = sorted(summaries, key=lambda s: s["last_timestamp"], reverse=True)[:recent_count]

    from datetime import datetime, timezone
    meta = {
        "source_counts": source_counts,
        "ungrouped_count": ungrouped_count,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    html_out = render_html(stats, recent, meta, merchant_breakdown, merchant_names)
    return html_out, stats, ungrouped_count


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
