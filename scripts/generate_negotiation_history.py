"""Generates bulk synthetic negotiation history for Parley Milestone 7
(dashboard seed data).

Runs many negotiations end-to-end (scripted buyer, rules-only merchant --
BUYER_MODE=scripted/MERCHANT_MODE=rules equivalent, no LLM, no live
Razorpay calls anywhere) across a random mix of real products/buyers/
quantities from data/catalog.json and data/buyers.json, and appends every
resulting audit entry to a SEPARATE log file (default
audits/dashboard_seed.log) -- never audits/negotiation.log, which stays
reserved for real debugging/demo history (see NEGOTIATION_SPEC.md Section
4D).

Deterministic: the same --seed always produces the same run mix (product/
buyer/qty/budget draws, and which runs get a forced payment failure).
Real catalog data (data/catalog.json) is read-only here -- every run is
called with catalog_path=None, so run_full_transaction() never writes
back a current_inventory decrement; only its in-memory snapshot is used
for the (real, not forced) insufficient-inventory check. No cleanup step
is required after running this script.

Payments run against an in-process fake Razorpay client (same pattern as
tests/e2e/negotiation/test_payment.py's FakeRazorpayClient) -- zero live
API calls, zero cost, matching the "no live API calls needed" requirement
for this milestone.

Usage:
    python scripts/generate_negotiation_history.py [--n 130] [--seed 42]
        [--out audits/dashboard_seed.log] [--truncate/--append]
"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import personalization  # noqa: E402
from src.agents.buyer_agent import BuyerAgent  # noqa: E402
from src.negotiation_loop import (  # noqa: E402
    DEFAULT_MAX_NEGOTIATION_ROUNDS,
    DEFAULT_TRANSACTION_APPROVAL_THRESHOLD,
    run_full_transaction,
)

DEFAULT_OUT_PATH = "audits/dashboard_seed.log"

# 12% of runs that reach the payment phase get a forced (simulated)
# payment failure -- higher than a real-world payment failure rate would
# be, deliberately, so a 100-150-run batch reliably produces several
# "Rolled back" outcomes (not just a lucky draw) alongside whatever real
# insufficient-inventory rollbacks occur naturally from the
# qty-vs-current_inventory draws below.
FORCE_PAYMENT_FAILURE_RATE = 0.12


class FakeOrderAPI:
    """Same shape as tests/e2e/negotiation/test_payment.py's fixture --
    an in-process stand-in for razorpay.Client().order, never touching
    the network."""

    def __init__(self):
        self._next_id = 1

    def create(self, data):
        order_id = f"order_seed_{self._next_id:06d}"
        self._next_id += 1
        return {"id": order_id, "amount": data["amount"], "currency": data["currency"], "status": "created"}


class FakeRazorpayClient:
    def __init__(self):
        self.order = FakeOrderAPI()


def _draw_qty(rng):
    """Mostly small orders, sometimes qty_breaks/large-request territory,
    occasionally large enough to naturally exceed a product's
    current_inventory (a real, not forced, insufficient-inventory
    rollback)."""
    roll = rng.random()
    if roll < 0.60:
        return rng.randint(1, 5)
    if roll < 0.85:
        return rng.randint(6, 15)
    return rng.randint(16, 40)


def run_one(rng, catalog, buyers, orders, merchants, audit_path, payment_client):
    product = rng.choice(catalog)
    buyer = rng.choice(buyers)
    qty = _draw_qty(rng)

    base_policy = personalization.product_to_policy(
        product, DEFAULT_MAX_NEGOTIATION_ROUNDS, DEFAULT_TRANSACTION_APPROVAL_THRESHOLD,
    )
    ltv = personalization.compute_ltv(buyer["buyer_id"], orders)
    ltv_bonus_pct = personalization.ltv_discount_bonus(ltv)
    policy = personalization.apply_ltv_bonus(base_policy, ltv_bonus_pct)

    risk_approval_tier = "standard"
    if product.get("merchant_id"):
        merchant = personalization.find_merchant(merchants, product["merchant_id"])
        if merchant is not None:
            risk_approval_tier = merchant["risk_approval_tier"]

    # Section 2AG (2026-09-05): derives the buyer's ceiling as a persona-
    # appropriate discount off THIS drawn product's real list_price,
    # rather than a fixed absolute budget_range with no relation to
    # whichever product/qty this run happens to draw -- see
    # personalization.PERSONA_DISCOUNT_BANDS for the rationale and the
    # per-persona bands.
    max_acceptable_price = personalization.budget_from_list_price(
        buyer["persona"], policy["list_price"], rng,
    )
    opening_discount_pct = rng.randint(10, 22)
    buyer_agent = BuyerAgent(
        qty=qty, opening_discount_pct=opening_discount_pct,
        max_acceptable_price=max_acceptable_price, list_price=policy["list_price"],
    )

    force_payment_failure = rng.random() < FORCE_PAYMENT_FAILURE_RATE

    outcome = run_full_transaction(
        policy, buyer_agent, audit_path=audit_path,
        force_payment_failure=force_payment_failure,
        approval_confirm=lambda message: True,
        payment_client=payment_client,
        product=product, catalog_path=None,
        buyer_id=buyer["buyer_id"], orders=orders, risk_approval_tier=risk_approval_tier,
    )
    return outcome


def classify(outcome):
    state = outcome["state"]
    if state == "COMPLETED":
        return "Accepted"
    if state == "REJECTED":
        return "Rejected"
    if state == "ROLLBACK":
        return "Rolled back"
    return state  # APPROVAL_DECLINED / BUYER_UNAVAILABLE -- not expected in this script


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=130, help="Number of negotiations to run (default 130).")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for reproducible runs (default 42).")
    parser.add_argument("--out", default=DEFAULT_OUT_PATH, help=f"Audit log path to write (default {DEFAULT_OUT_PATH}).")
    truncate_group = parser.add_mutually_exclusive_group()
    truncate_group.add_argument("--truncate", action="store_true", default=True, help="Overwrite --out before writing (default).")
    truncate_group.add_argument("--append", dest="truncate", action="store_false", help="Append to --out instead of overwriting it.")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    catalog = personalization.load_json(personalization.DEFAULT_CATALOG_PATH)
    buyers = personalization.load_json(personalization.DEFAULT_BUYERS_PATH)
    orders = personalization.load_json(personalization.DEFAULT_ORDERS_PATH)
    merchants = personalization.load_json(personalization.DEFAULT_MERCHANTS_PATH)
    payment_client = FakeRazorpayClient()

    out_path = Path(args.out)
    if args.truncate:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("", encoding="utf-8")

    counts = {}
    rollback_reasons = {}
    for _ in range(args.n):
        outcome = run_one(rng, catalog, buyers, orders, merchants, str(out_path), payment_client)
        label = classify(outcome)
        counts[label] = counts.get(label, 0) + 1
        if label == "Rolled back":
            reason = outcome.get("reason", "unknown")
            rollback_reasons[reason] = rollback_reasons.get(reason, 0) + 1

    print(f"Generated {args.n} negotiations -> {out_path} (seed={args.seed})")
    print()
    print("Outcome breakdown:")
    for label in ("Accepted", "Rejected", "Rolled back"):
        print(f"  {label:12s}: {counts.get(label, 0)}")
    other = {k: v for k, v in counts.items() if k not in ("Accepted", "Rejected", "Rolled back")}
    for label, n in other.items():
        print(f"  {label:12s}: {n} (unexpected)")
    if rollback_reasons:
        print("  Rolled back breakdown:")
        for reason, n in rollback_reasons.items():
            print(f"    {reason:22s}: {n}")


if __name__ == "__main__":
    main()
