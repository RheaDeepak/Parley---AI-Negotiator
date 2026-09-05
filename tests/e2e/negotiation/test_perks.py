"""Milestone 9 (Section 4E): multi-dimensional negotiation -- perk
eligibility, and the margin-floor check that guards against the "only
one pathway touched" bug class (the same class of bug this project has
already hit once for liquidation and once for the Risk Agent's
qty_breaks scaling)."""
import json

import pytest

from src import personalization
from src.agents.buyer_agent import BuyerAgent
from src.negotiation_loop import run_negotiation

# min_price (800) and the discount-cap floor (list_price*(1-max_discount_pct/100)
# = 1000*0.80 = 800) are DELIBERATELY equal here -- ties resolve to the
# min_price branch in merchant_agent._floor_price(), which is the branch
# granted_perk_cost actually extends. If the discount-cap floor were the
# higher (binding) term instead, adding perk cost to min_price wouldn't
# move the effective floor at all -- exactly the kind of accidentally-
# non-binding setup that let the liquidation bug hide for months.
POLICY = {
    "sku_id": "SKU-PERK-001", "product_name": "Test Widget", "currency": "INR",
    "list_price": 1000.0, "min_price": 800.0, "max_discount_pct": 20,
    "qty_breaks": [], "max_negotiation_rounds": 5, "transaction_approval_threshold": 20000,
    "inventory_floor": 1, "shipping_cost": 50.0, "warranty_cost": 40.0,
}


def _orders(buyer_id, count):
    return [{"buyer_id": buyer_id, "order_id": f"O{i}"} for i in range(count)]


def _read_log(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _perk_review(entries):
    return next(e for e in entries if e["action"] == "perk_review")


def test_new_buyer_free_delivery_granted_extended_warranty_declined_ineligible(tmp_path):
    orders = _orders("BUYER-NEW", 0)  # 0 prior orders -> eligible for free_delivery only

    # Sub-case A: requests the ELIGIBLE perk, comfortably affordable.
    audit_a = tmp_path / "a.log"
    # opening_discount_pct=0 -> offers full list price. A 0-prior-order
    # buyer also trips the Risk Agent's "new_buyer" factor (MODERATE risk,
    # even at qty=1), which tightens max_discount_pct to 5% of normal --
    # floor becomes 950.0, not the nominal 800.0. Offering list price
    # sidesteps needing to track that interaction here; it's covered
    # directly by test_perk_at_exact_margin_floor_is_declined_not_silently_granted.
    buyer_a = BuyerAgent(qty=1, opening_discount_pct=0, max_acceptable_price=1000.0, list_price=POLICY["list_price"])
    outcome_a = run_negotiation(
        POLICY, buyer_a, audit_path=str(audit_a),
        buyer_id="BUYER-NEW", orders=orders, persona="Bargain Hunter", requested_perks=["free_delivery"],
    )
    assert outcome_a["state"] == "AGREEMENT_RECORDED"
    assert outcome_a["granted_perks"] == ["free_delivery"]
    assert outcome_a["declined_perks"] == []
    review_a = _perk_review(_read_log(audit_a))
    assert review_a["evidence_paths"] == ["perk_eligibility.new_buyer"]

    # Sub-case B: requests the INELIGIBLE perk -- auto-declined up front,
    # no LLM/margin discretion involved at all.
    audit_b = tmp_path / "b.log"
    buyer_b = BuyerAgent(qty=1, opening_discount_pct=0, max_acceptable_price=1000.0, list_price=POLICY["list_price"])
    outcome_b = run_negotiation(
        POLICY, buyer_b, audit_path=str(audit_b),
        buyer_id="BUYER-NEW", orders=orders, persona="Bargain Hunter", requested_perks=["extended_warranty"],
    )
    assert outcome_b["state"] == "AGREEMENT_RECORDED"
    assert outcome_b["granted_perks"] == []
    assert outcome_b["declined_perks"] == []  # ineligible, never even a margin "candidate"
    review_b = _perk_review(_read_log(audit_b))
    assert "declined as ineligible: extended_warranty" in review_b["rationale"]
    assert review_b["evidence_paths"] == ["perk_eligibility.new_buyer"]


def test_established_non_window_shopper_reverse_of_new_buyer(tmp_path):
    orders = _orders("BUYER-EST", 3)  # >0 prior orders -> eligible for extended_warranty only

    audit_a = tmp_path / "a.log"
    # opening_discount_pct=0 -> offers full list price. A 0-prior-order
    # buyer also trips the Risk Agent's "new_buyer" factor (MODERATE risk,
    # even at qty=1), which tightens max_discount_pct to 5% of normal --
    # floor becomes 950.0, not the nominal 800.0. Offering list price
    # sidesteps needing to track that interaction here; it's covered
    # directly by test_perk_at_exact_margin_floor_is_declined_not_silently_granted.
    buyer_a = BuyerAgent(qty=1, opening_discount_pct=0, max_acceptable_price=1000.0, list_price=POLICY["list_price"])
    outcome_a = run_negotiation(
        POLICY, buyer_a, audit_path=str(audit_a),
        buyer_id="BUYER-EST", orders=orders, persona="Loyal Regular", requested_perks=["extended_warranty"],
    )
    assert outcome_a["state"] == "AGREEMENT_RECORDED"
    assert outcome_a["granted_perks"] == ["extended_warranty"]
    assert outcome_a["declined_perks"] == []

    audit_b = tmp_path / "b.log"
    buyer_b = BuyerAgent(qty=1, opening_discount_pct=10, max_acceptable_price=900.0, list_price=POLICY["list_price"])
    outcome_b = run_negotiation(
        POLICY, buyer_b, audit_path=str(audit_b),
        buyer_id="BUYER-EST", orders=orders, persona="Loyal Regular", requested_perks=["free_delivery"],
    )
    assert outcome_b["state"] == "AGREEMENT_RECORDED"
    assert outcome_b["granted_perks"] == []
    review_b = _perk_review(_read_log(audit_b))
    assert "declined as ineligible: free_delivery" in review_b["rationale"]
    assert review_b["evidence_paths"] == ["perk_eligibility.established_buyer"]


@pytest.mark.parametrize("prior_orders, qty", [
    (0, 1),   # new buyer, normal qty -- the original case
    (5, 1),   # established buyer, normal qty -- the original case
    # Section 2AH (2026-09-05): established buyer AND qty=11 -- WITHOUT the
    # Window Shopper override, this exact combination would now be
    # eligible for BOTH perks (see
    # test_established_buyer_large_qty_eligible_for_both_perks_simultaneously).
    # Confirms the persona override still wins over the new qty-based rule
    # too, not just over order history. (0, 11) is deliberately not
    # included here -- that combination trips the Risk Agent's HIGH risk
    # instead (new_buyer + large_request), a different override, already
    # covered by test_high_risk_declines_all_perk_requests_regardless_of_eligibility.
    (5, 11),
])
def test_window_shopper_both_perks_declined_regardless_of_order_history(tmp_path, prior_orders, qty):
    orders = _orders("BUYER-WS", prior_orders)
    audit_path = tmp_path / "negotiation.log"
    # Full-price offer -- safe regardless of whether this parametrization's
    # prior_orders also happens to trip the Risk Agent's new_buyer factor
    # (prior_orders=0 does; =5 doesn't) -- see test 1's comment for why.
    buyer = BuyerAgent(qty=qty, opening_discount_pct=0, max_acceptable_price=1000.0, list_price=POLICY["list_price"])
    outcome = run_negotiation(
        POLICY, buyer, audit_path=str(audit_path),
        buyer_id="BUYER-WS", orders=orders, persona="Window Shopper",
        requested_perks=["free_delivery", "extended_warranty"],
    )
    assert outcome["state"] == "AGREEMENT_RECORDED"
    assert outcome["granted_perks"] == []
    assert outcome["declined_perks"] == []
    review = _perk_review(_read_log(audit_path))
    assert review["evidence_paths"] == ["perk_eligibility.window_shopper"]
    assert "declined as ineligible: free_delivery, extended_warranty" in review["rationale"]


def test_high_risk_declines_all_perk_requests_regardless_of_eligibility(tmp_path):
    # 0 prior orders (normally -> eligible for free_delivery) AND qty=10
    # (>= RISK_LARGE_QTY_FALLBACK_THRESHOLD, since POLICY.qty_breaks is
    # empty) together trigger HIGH risk, which overrides persona/order-
    # count eligibility entirely -- no perks at all, same as the zeroed
    # discount ceiling.
    orders = _orders("BUYER-RISKY", 0)
    audit_path = tmp_path / "negotiation.log"
    # HIGH risk zeroes max_discount_pct -> floor becomes list_price itself
    # (1000.0, "full price only"); buyer must be willing to pay it in full
    # to reach agreement at all (perks are only ever resolved at acceptance).
    buyer = BuyerAgent(qty=10, opening_discount_pct=0, max_acceptable_price=1000.0, list_price=POLICY["list_price"])
    outcome = run_negotiation(
        POLICY, buyer, audit_path=str(audit_path),
        buyer_id="BUYER-RISKY", orders=orders, persona="Bargain Hunter", requested_perks=["free_delivery"],
    )
    assert outcome["state"] == "AGREEMENT_RECORDED"
    assert outcome["risk_level"] == "high"
    assert outcome["granted_perks"] == []
    review = _perk_review(_read_log(audit_path))
    assert review["evidence_paths"] == ["perk_eligibility.risk_override"]
    assert "High risk flagged by the Risk Agent" in review["rationale"]


def test_established_buyer_large_qty_eligible_for_both_perks_simultaneously(tmp_path):
    """Section 2AH (2026-09-05): qty > PERK_LARGE_QTY_THRESHOLD (10) is a
    SECOND, independent basis for free_delivery eligibility, on top of --
    not instead of -- the existing "0 prior orders" rule. An established
    buyer (>0 prior orders, normally eligible for extended_warranty only)
    placing a qty=11 order is now eligible for BOTH perks at once: this
    breaks the original "eligible for at most one perk by construction"
    invariant, and is the intended behavior, not a bug -- confirmed with
    the user before implementing.

    qty=11 (> the RISK_LARGE_QTY_FALLBACK_THRESHOLD-equivalent of 10 that
    POLICY's empty qty_breaks falls back to) ALSO trips the Risk Agent's
    own "large_request" factor -- but this buyer has 3 prior orders, so
    "new_buyer" does NOT also fire, so risk_level lands on MODERATE, not
    HIGH. This is deliberately the buyer profile the user asked to see:
    an established, non-Window-Shopper buyer who is NOT HIGH risk, so
    perk_eligibility() is actually reached (a HIGH-risk buyer would get
    nothing at all via the risk_override rule, before either perk rule is
    even considered)."""
    orders = _orders("BUYER-BULK", 3)  # established, but not so many orders it changes anything else
    audit_path = tmp_path / "negotiation.log"

    # MODERATE risk scales max_discount_pct to 25% of normal (20% -> 5%),
    # so the discount-tier floor becomes list_price*0.95 = 950.0 -- still
    # comfortably below list_price even with both perks' combined cost
    # (shipping_cost 50.0 + warranty_cost 40.0 = 90.0) folded into the
    # OTHER floor candidate (min_price + 90.0 = 890.0). Offering full list
    # price (1000.0) clears 950.0 either way, so both perks are affordable
    # together, not just individually.
    buyer = BuyerAgent(qty=11, opening_discount_pct=0, max_acceptable_price=1000.0, list_price=POLICY["list_price"])
    outcome = run_negotiation(
        POLICY, buyer, audit_path=str(audit_path),
        buyer_id="BUYER-BULK", orders=orders, persona="Bulk Buyer",
        requested_perks=["free_delivery", "extended_warranty"],
    )

    assert outcome["state"] == "AGREEMENT_RECORDED"
    assert outcome["risk_level"] == "moderate"  # confirms NOT high -- see docstring above for why that matters
    assert outcome["granted_perks"] == ["free_delivery", "extended_warranty"]
    assert outcome["declined_perks"] == []

    review = _perk_review(_read_log(audit_path))
    assert review["evidence_paths"] == ["perk_eligibility.established_buyer+large_qty"]
    assert "eligible for both" in review["rationale"]
    assert "Requested and eligible: free_delivery, extended_warranty" in review["rationale"]


def test_high_risk_override_still_wins_for_established_buyer_at_large_qty():
    """Direct unit test of personalization.perk_eligibility() itself (not
    through run_negotiation()): an established buyer (>0 prior orders)
    can never actually REACH risk_level="high" via risk_assessment() --
    HIGH requires BOTH "new_buyer" and "large_request" factors together,
    and an established buyer never trips "new_buyer" (Section 2AH's own
    docstring notes this: the "new buyer + qty>10 = HIGH risk, gets
    nothing" case only applies to genuinely new buyers). So this exact
    combination -- established buyer, qty > PERK_LARGE_QTY_THRESHOLD,
    risk_level explicitly "high" -- can't be produced by a real
    negotiation today. Tested directly anyway: perk_eligibility()'s own
    precedence (risk_level=="high" checked FIRST, before either perk
    rule) must not silently depend on that being unreachable -- a future
    change to what counts as "new buyer" could make it reachable, and
    this guarantees the override still wins if it ever is."""
    orders = _orders("BUYER-EST-RISKY", 5)
    result = personalization.perk_eligibility(
        "BUYER-EST-RISKY", orders, "Loyal Regular", "high", 11,
    )
    assert result["eligible"] == []
    assert result["rule"] == "risk_override"


def test_perk_at_exact_margin_floor_is_declined_not_silently_granted(tmp_path):
    """THE test that specifically guards against the "only one pathway
    touched" bug class (the exact class this project already hit once
    for liquidation, once for the Risk Agent's qty_breaks scaling): a
    buyer eligible for a perk negotiates price down to EXACTLY the
    combined min_price/discount-cap floor (800.0, see POLICY's comment
    above for why the two terms are deliberately tied). Granting the
    requested perk (warranty_cost=40.0) on top would push the effective
    price 40.0 below the true floor -- it must be declined, not silently
    granted just because the bare price cleared the non-perk floor.

    An ESTABLISHED buyer (>0 prior orders, eligible for extended_warranty)
    at qty=1 deliberately, not a new buyer: a 0-prior-order buyer also
    trips the Risk Agent's new_buyer factor at any qty, which would
    tighten max_discount_pct and shift the floor to a DIFFERENT number
    than the plain min_price/discount-cap tie this test is built around
    -- see test 1's comment. Using an established buyer keeps the two
    concerns (risk-driven repricing vs. perk-driven repricing) isolated,
    so this test proves the perk mechanism specifically, not their
    interaction."""
    orders = _orders("BUYER-TIGHT", 3)  # established -- no risk factors at qty=1
    audit_path = tmp_path / "negotiation.log"

    # opening_discount_pct=20 -> initial offer 800.0 = list_price*0.80 --
    # EXACTLY the floor (both min_price and the discount-cap term are
    # 800.0 by construction). The merchant accepts it immediately, zero
    # margin headroom left for any perk cost.
    buyer = BuyerAgent(qty=1, opening_discount_pct=20, max_acceptable_price=800.0, list_price=POLICY["list_price"])
    outcome = run_negotiation(
        POLICY, buyer, audit_path=str(audit_path),
        buyer_id="BUYER-TIGHT", orders=orders, persona="Loyal Regular", requested_perks=["extended_warranty"],
    )

    assert outcome["state"] == "AGREEMENT_RECORDED"
    assert outcome["risk_level"] == "none"  # confirms this is a pure perk-margin case, not risk-driven
    assert outcome["offer"]["price"] == 800.0  # landed exactly at the floor -- zero headroom
    assert outcome["granted_perks"] == []
    assert outcome["declined_perks"] == ["extended_warranty"]

    entries = _read_log(audit_path)
    accept_entry = next(e for e in entries if e["action"] == "accept")
    assert "margin floor" in accept_entry["rationale"].lower()
