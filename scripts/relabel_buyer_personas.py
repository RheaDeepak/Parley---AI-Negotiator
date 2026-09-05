"""Milestone 9 follow-up: re-derives each buyer's `persona` label from
their REAL order history in data/orders.json, instead of the synthetic
generator's original round-robin assignment (persona was assigned by
buyer INDEX, `PERSONAS[i % len(PERSONAS)]` -- decorative, never tied to
actual spend).

Deliberately does NOT touch data/orders.json or any other field of
data/buyers.json (budget_range, category_affinity, negotiation_style) --
every currently-verified test/demo scenario depends on those staying
byte-identical. This is a pure relabeling pass: compute each buyer's
real total order amount (sum of orders.json "amount" for that buyer_id,
0.0 for a buyer with no orders at all), sort ascending, split into 8
equal-as-possible bands, and assign persona labels to bands in the SAME
low-to-high rank order the personas' own (unchanged) budget_range field
already implies -- Window Shopper (lowest) through Whale (highest). Not
an arbitrary new ordering: it's the existing design's own spend-tier
intent, just now driven by real data instead of index position.

Usage:
    python scripts/relabel_buyer_personas.py [--buyers data/buyers.json]
        [--orders data/orders.json] [--dry-run]
"""
import argparse
import json
from pathlib import Path

# Same 8 persona labels the generator already uses (scripts/generate_
# synthetic_data.py's PERSONAS list) -- ordered here by that list's own
# budget_range midpoint, lowest to highest, which this script reuses as
# the real-spend rank order.
PERSONA_RANK_ORDER = [
    "Window Shopper",       # budget_range (1000, 3000)   -- mid 2000
    "Occasional Buyer",     # budget_range (1500, 5000)   -- mid 3250
    "Bargain Hunter",       # budget_range (2000, 6000)   -- mid 4000
    "Stubborn Negotiator",  # budget_range (2000, 8000)   -- mid 5000
    "Loyal Regular",        # budget_range (4000, 12000)  -- mid 8000
    "Premium Customer",     # budget_range (8000, 25000)  -- mid 16500
    "Bulk Buyer",           # budget_range (10000, 40000) -- mid 25000
    "Whale",                # budget_range (30000, 80000) -- mid 55000
]


def compute_totals(orders):
    totals = {}
    for o in orders:
        totals[o["buyer_id"]] = totals.get(o["buyer_id"], 0.0) + o["amount"]
    return totals


def assign_bands(buyer_ids_sorted_ascending, n_personas):
    """Splits into n_personas groups, as equal as possible -- the first
    (total % n_personas) groups get one extra item, matching the
    standard "array_split" convention (no arbitrary favoritism toward
    the low or high end)."""
    n = len(buyer_ids_sorted_ascending)
    base, extra = divmod(n, n_personas)
    bands = []
    start = 0
    for i in range(n_personas):
        size = base + (1 if i < extra else 0)
        bands.append(buyer_ids_sorted_ascending[start:start + size])
        start += size
    return bands


def relabel(buyers, orders):
    totals = compute_totals(orders)
    buyer_ids_sorted = sorted((b["buyer_id"] for b in buyers), key=lambda bid: totals.get(bid, 0.0))
    bands = assign_bands(buyer_ids_sorted, len(PERSONA_RANK_ORDER))

    new_persona = {}
    for persona, band in zip(PERSONA_RANK_ORDER, bands):
        for bid in band:
            new_persona[bid] = persona

    before = {b["buyer_id"]: b["persona"] for b in buyers}
    updated = [{**b, "persona": new_persona[b["buyer_id"]]} for b in buyers]
    return updated, before, totals


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--buyers", default="data/buyers.json")
    parser.add_argument("--orders", default="data/orders.json")
    parser.add_argument("--dry-run", action="store_true", help="Print the before/after table without writing.")
    args = parser.parse_args()

    buyers = json.loads(Path(args.buyers).read_text(encoding="utf-8"))
    orders = json.loads(Path(args.orders).read_text(encoding="utf-8"))

    updated, before, totals = relabel(buyers, orders)

    print(f"{'buyer_id':10s} {'total_amount':>14s}  {'before':22s} {'after':22s}")
    for b in sorted(updated, key=lambda x: totals.get(x["buyer_id"], 0.0)):
        bid = b["buyer_id"]
        changed = " *" if before[bid] != b["persona"] else ""
        print(f"{bid:10s} {totals.get(bid, 0.0):14.2f}  {before[bid]:22s} {b['persona']:22s}{changed}")

    if args.dry_run:
        print("\n--dry-run: not written.")
        return

    Path(args.buyers).write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {args.buyers}")


if __name__ == "__main__":
    main()
