"""Shared dashboard-data computation (Section 2Z, 2026-09-05).

Pure, read-only aggregation over the existing audit trail
(audits/negotiation.log, audits/dashboard_seed.log) -- no negotiation,
pricing, or guardrail logic lives here, and nothing here ever writes to
those logs. Originally lived inline in scripts/generate_dashboard.py;
extracted here, unchanged in behavior, so BOTH that script (the static-
snapshot fallback, for when the API server isn't running) and
src/api.py's GET /api/dashboard-data (the live view) call the exact same
functions -- one computation, two consumers, never two implementations
that could drift.

compute_dashboard_data() is the one entry point either consumer needs:
returns a JSON-serializable dict (stats, merchant_breakdown, recent,
meta) -- scripts/generate_dashboard.py additionally renders it to HTML
via its own render_html(); src/api.py returns it as-is.
"""
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.agents.audit_logger import DEFAULT_AUDIT_PATH
from src.personalization import DEFAULT_MERCHANTS_PATH

DEFAULT_DASHBOARD_SEED_PATH = "audits/dashboard_seed.log"
DEFAULT_LOG_PATHS = [DEFAULT_AUDIT_PATH, DEFAULT_DASHBOARD_SEED_PATH]
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


def compute_dashboard_data(log_paths=None, recent_count=DEFAULT_RECENT_COUNT, merchants_path=DEFAULT_MERCHANTS_PATH):
    """The one shared entry point: reads the audit log(s), groups by
    negotiation_id, and returns everything a consumer needs to render a
    dashboard -- as a plain, JSON-serializable dict. Called by BOTH
    scripts/generate_dashboard.py (which renders this to static HTML)
    and GET /api/dashboard-data (which returns it as-is)."""
    log_paths = log_paths if log_paths is not None else DEFAULT_LOG_PATHS
    entries, source_counts = load_entries(log_paths)
    groups, ungrouped_count = group_by_negotiation(entries)
    summaries = summarize_all(groups)
    stats = compute_summary_stats(summaries)
    merchant_names = load_merchant_names(merchants_path)
    merchant_breakdown = compute_merchant_breakdown(summaries, merchant_names)
    recent = sorted(summaries, key=lambda s: s["last_timestamp"], reverse=True)[:recent_count]

    meta = {
        "source_counts": source_counts,
        "ungrouped_count": ungrouped_count,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return {
        "stats": stats,
        "merchant_breakdown": merchant_breakdown,
        "recent": recent,
        "merchant_names": merchant_names,
        "meta": meta,
    }
