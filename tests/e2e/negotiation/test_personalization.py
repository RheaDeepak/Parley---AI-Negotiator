import json

import pytest

from src import personalization
from src.agents.buyer_agent import BuyerAgent
from src.negotiation_loop import run_full_transaction, run_negotiation


def test_ltv_computation_is_deterministic():
    orders = [
        {"buyer_id": "B1", "amount": 1000.0},
        {"buyer_id": "B1", "amount": 2000.0},
        {"buyer_id": "B2", "amount": 50000.0},
    ]
    assert personalization.compute_ltv("B1", orders) == personalization.compute_ltv("B1", orders) == 3000.0
    assert personalization.compute_ltv("B2", orders) == 50000.0
    assert personalization.compute_ltv("B3", orders) == 0  # no orders at all


@pytest.mark.parametrize("ltv,expected_bonus", [
    (0, 0), (4999.99, 0),
    (5000, 2), (19999.99, 2),
    (20000, 5), (49999.99, 5),
    (50000, 8), (10_000_000, 8),
])
def test_ltv_tier_lookup_is_deterministic_and_exact_at_boundaries(ltv, expected_bonus):
    assert personalization.ltv_discount_bonus(ltv) == expected_bonus
    assert personalization.ltv_discount_bonus(ltv) == personalization.ltv_discount_bonus(ltv)  # repeatable


def test_ltv_bonus_never_pushes_effective_discount_past_hard_ceiling():
    policy = {
        "max_discount_pct": 28,
        "qty_breaks": [{"min_qty": 10, "discount_pct": 29}],
    }
    # The real top tier (+8%).
    effective = personalization.apply_ltv_bonus(policy, ltv_bonus_pct=8)
    assert effective["max_discount_pct"] == personalization.HARD_DISCOUNT_CEILING_PCT
    assert effective["qty_breaks"][0]["discount_pct"] == personalization.HARD_DISCOUNT_CEILING_PCT

    # An absurd, far-beyond-any-real-tier bonus must ALSO never cross the
    # ceiling -- "regardless of how high LTV is."
    extreme = personalization.apply_ltv_bonus(policy, ltv_bonus_pct=1000)
    assert extreme["max_discount_pct"] == personalization.HARD_DISCOUNT_CEILING_PCT
    assert extreme["qty_breaks"][0]["discount_pct"] == personalization.HARD_DISCOUNT_CEILING_PCT


def test_apply_ltv_bonus_does_not_mutate_the_input_policy():
    policy = {"max_discount_pct": 10, "qty_breaks": [{"min_qty": 10, "discount_pct": 20}]}
    before = json.loads(json.dumps(policy))

    personalization.apply_ltv_bonus(policy, ltv_bonus_pct=5)

    assert policy == before


# ---------------------------------------------------------------------------
# Margin-aware cost floor + inventory liquidation (Milestone 3c follow-up)
# ---------------------------------------------------------------------------


def test_cost_floor_price_is_cost_plus_two_percent_margin():
    product = {"cost": 1000.0}
    assert personalization.cost_floor_price(product) == 1020.0


@pytest.mark.parametrize("days,expected_fraction", [
    (0, 0.0),       # well under threshold -- untouched
    (180, 0.0),     # exactly at Electronics' threshold -- still untouched (relaxation starts AFTER this)
    (205, 0.25),    # 25/100 into the ramp
    (230, 0.5),     # 50/100 into the ramp
    (280, 1.0),     # exactly threshold(180) + ramp(100) -- fully ramped
    (1000, 1.0),    # extreme outlier -- still exactly 1.0, never overshoots
])
def test_liquidation_relaxation_fraction_ramps_linearly_and_never_overshoots(days, expected_fraction):
    product = {"category": "Electronics", "days_in_inventory": days}
    assert personalization.liquidation_relaxation_fraction(product) == expected_fraction


def test_aged_product_floor_is_lower_than_fresh_but_never_below_min_price():
    """Deliverable #2's explicit test, updated for the 2026-09-02
    structural fix (Section 2J): liquidation now relaxes the discount-cap
    floor toward min_price, not min_price itself -- so the EFFECTIVE
    floor (merchant_agent._floor_price(), not policy["min_price"]) must
    be checked. Same SKU economics (cost, base min_price, category,
    max_discount_pct), only days_in_inventory differs. max_discount_pct is
    deliberately the pre-liquidation-binding term here (discount floor
    900 > min_price 750) -- the realistic case, per the live-catalog
    investigation that motivated this fix (79/80 generated products)."""
    from src.agents.merchant_agent import _floor_price

    base = {
        "sku_id": "SKU-AGE-001", "product_name": "Widget", "currency": "INR", "category": "Electronics",
        "list_price": 1000.0, "cost": 600.0, "min_price": 750.0,
        "max_discount_pct": 10, "qty_breaks": [], "current_inventory": 50, "inventory_floor": 1,
    }
    fresh = {**base, "days_in_inventory": 10}
    partially_aged = {**base, "days_in_inventory": 230}   # Electronics threshold(180) + 50 into the ramp
    fully_aged = {**base, "days_in_inventory": 280}       # Electronics threshold(180) + ramp(100)
    extreme_aged = {**base, "days_in_inventory": 1000}

    fresh_policy = personalization.product_to_policy(fresh, 5, 20000)
    partial_policy = personalization.product_to_policy(partially_aged, 5, 20000)
    full_policy = personalization.product_to_policy(fully_aged, 5, 20000)
    extreme_policy = personalization.product_to_policy(extreme_aged, 5, 20000)

    # min_price itself is IDENTICAL across all four -- liquidation no
    # longer touches it directly.
    assert fresh_policy["min_price"] == partial_policy["min_price"] == full_policy["min_price"] == 750.0

    fresh_floor, _ = _floor_price(fresh_policy, qty=1)
    partial_floor, _ = _floor_price(partial_policy, qty=1)
    full_floor, _ = _floor_price(full_policy, qty=1)
    extreme_floor, _ = _floor_price(extreme_policy, qty=1)

    assert fresh_floor == 900.0    # untouched discount-cap floor: 1000*(1-10/100)
    assert partial_floor == 825.0  # 50% ramped: 900 - 0.5*(900-750)
    assert full_floor == 750.0     # fully ramped -- exactly min_price
    assert extreme_floor == 750.0  # extreme days never overshoots below min_price

    # Aged floors are progressively (measurably) lower than fresh, but
    # never below min_price.
    assert 750.0 <= full_floor < partial_floor < fresh_floor


def test_electronics_needs_meaningfully_longer_days_than_old_flat_threshold_to_trigger_liquidation():
    """Deliverable #5: under the old flat 100-day threshold, 150 days
    would have triggered relaxation. Under Electronics' new 180-day
    threshold, it must NOT -- only a meaningfully longer stay (past 180)
    does."""
    product = {"category": "Electronics"}
    assert personalization.liquidation_relaxation_fraction({**product, "days_in_inventory": 150}) == 0.0  # unchanged
    assert personalization.liquidation_relaxation_fraction({**product, "days_in_inventory": 200}) > 0.0   # now relaxing


def test_books_needs_even_longer_days_than_electronics_to_trigger_liquidation():
    """Deliverable #5: Books & Media's 380-day threshold is longer still
    than Electronics' 180. The same days_in_inventory (200) that already
    triggers relaxation for Electronics must NOT trigger it for Books &
    Media."""
    electronics = {"category": "Electronics", "days_in_inventory": 200}
    books = {"category": "Books & Media", "days_in_inventory": 200}

    assert personalization.liquidation_relaxation_fraction(electronics) > 0.0    # past its 180-day threshold
    assert personalization.liquidation_relaxation_fraction(books) == 0.0         # nowhere near its 380-day threshold

    # Only once Books & Media is genuinely far past ITS OWN threshold does it relax.
    books_aged = {**books, "days_in_inventory": 400}
    assert personalization.liquidation_relaxation_fraction(books_aged) > 0.0


def test_cost_floor_overrides_liquidation_regardless_of_category():
    """Deliverable #5's third check, updated for the 2026-09-02
    structural fix: the ramp now targets min_price, not cost_floor_price()
    directly -- but product_to_policy() still clamps min_price itself up
    to cost_floor_price() when the raw catalog value is unsafely low
    (Section 2E), so a full ramp transitively still lands exactly at
    cost_floor_price(), never below it, regardless of category."""
    from src.agents.merchant_agent import _floor_price

    for category in ("Electronics", "Books & Media", "Home & Kitchen"):
        product = {
            "sku_id": f"SKU-{category[:4].upper()}-COSTFLOOR", "product_name": "Widget", "currency": "INR",
            "list_price": 1000.0, "cost": 500.0, "min_price": 400.0,  # 400 < cost*1.02 = 510 -- unsafe raw value
            "max_discount_pct": 20, "qty_breaks": [], "current_inventory": 50, "inventory_floor": 1,
            "category": category, "days_in_inventory": 5000,  # extreme -- past every category's full ramp
        }
        cost_floor = personalization.cost_floor_price(product)
        assert cost_floor == 510.0

        policy = personalization.product_to_policy(product, 5, 20000)
        assert policy["min_price"] == cost_floor  # clamped up from the raw (unsafe) 400.0

        floor, evidence_path = _floor_price(policy, qty=1)
        assert floor == cost_floor  # fully ramped down to (the clamped) min_price -- never below it
        assert evidence_path == "policy.min_price"


def test_liquidation_rationale_names_the_category_specific_threshold():
    fresh = {"min_price": 750.0, "cost": 600.0, "category": "Electronics", "days_in_inventory": 20}
    assert personalization.liquidation_rationale(fresh) is None  # not aged -- no rationale at all

    aged = {"min_price": 750.0, "cost": 600.0, "category": "Electronics", "days_in_inventory": 215}
    rationale = personalization.liquidation_rationale(aged)
    assert rationale is not None
    assert "215 days in inventory" in rationale
    assert "180-day threshold for Electronics" in rationale


def test_extreme_ltv_bonus_on_low_margin_product_never_crosses_cost_floor():
    """Deliverable #1's explicit test: an extremely high-LTV buyer
    negotiating a low-margin product still cannot get a price below the
    cost-derived floor -- it takes priority over min_price,
    max_discount_pct, AND the LTV bonus combined."""
    # A low-margin product where the raw catalog min_price was set BELOW
    # what the cost floor requires (e.g. a generator/operator mistake) --
    # product_to_policy() must still enforce the real floor regardless.
    product = {
        "sku_id": "SKU-LOWMARGIN-001", "product_name": "Thin-Margin Widget", "currency": "INR",
        "list_price": 1100.0, "cost": 1000.0, "min_price": 800.0,  # 800 < cost*1.02 = 1020
        "max_discount_pct": 40, "qty_breaks": [], "current_inventory": 100, "inventory_floor": 1,
        "days_in_inventory": 5,
    }
    cost_floor = personalization.cost_floor_price(product)
    assert cost_floor == 1020.0

    policy = personalization.product_to_policy(product, max_negotiation_rounds=5, transaction_approval_threshold=20000)
    assert policy["min_price"] == cost_floor  # clamped up from the raw (unsafe) 800.0

    # The maximum possible LTV bonus (top tier, an extremely high LTV) --
    # still only touches max_discount_pct/qty_breaks, never min_price.
    max_bonus = personalization.ltv_discount_bonus(10_000_000)
    assert max_bonus == 8
    effective_policy = personalization.apply_ltv_bonus(policy, ltv_bonus_pct=max_bonus)
    assert effective_policy["min_price"] == cost_floor  # untouched by the bonus

    from src.agents.merchant_agent import evaluate

    # Buyer tries an offer well below even the cost floor.
    offer = {"offer_id": "test-offer", "price": 500.0, "qty": 1, "terms": "", "expiration": "", "timestamp": ""}
    result = evaluate(offer, effective_policy, round=1)

    assert result["decision"] in ("counter", "reject")
    if result["decision"] == "counter":
        assert result["offer"]["price"] >= cost_floor
    # A reject is also a valid way of never crossing the floor -- either
    # way, no offer/counter below cost_floor can ever be produced.


def _demo_product(sku_id, current_inventory, cost=500.0, min_price=700.0, days_in_inventory=10):
    return {
        "sku_id": sku_id, "product_name": "Test Widget", "currency": "INR",
        "list_price": 1000.0, "cost": cost, "min_price": min_price, "max_discount_pct": 10,
        "qty_breaks": [], "current_inventory": current_inventory, "inventory_floor": 1,
        "days_in_inventory": days_in_inventory,
    }


class SpyOrderAPI:
    def __init__(self):
        self.calls = []

    def create(self, data):
        self.calls.append(data)
        return {"id": "order_should_never_happen", "amount": data["amount"], "currency": data["currency"], "status": "created"}


class SpyClient:
    def __init__(self):
        self.order = SpyOrderAPI()


def _read_log(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# Risk Agent (Milestone 5) -- deterministic, no LLM, thresholds confirmed
# with the user 2026-09-02: new_buyer = fewer than 2 prior orders;
# large_request = qty >= the product's lowest qty_breaks tier (10, via
# _demo_product()'s empty qty_breaks -> RISK_LARGE_QTY_FALLBACK_THRESHOLD).
# ---------------------------------------------------------------------------


def test_risk_assessment_pure_function_level_combinations():
    """Direct unit coverage of personalization.risk_assessment()'s four
    combinations, independent of the negotiation loop."""
    orders = [{"buyer_id": "BUYER-EXISTING", "amount": 100.0}] * 5  # 5 prior orders -- established

    # Neither factor: established buyer, small qty.
    none_result = personalization.risk_assessment("BUYER-EXISTING", qty=3, qty_breaks=[], orders=orders)
    assert none_result["level"] == "none"
    assert none_result["factors"] == []
    assert none_result["evidence_paths"] == []

    # Only new_buyer: 0 prior orders, small qty.
    moderate_new_buyer = personalization.risk_assessment("BUYER-NEW", qty=3, qty_breaks=[], orders=orders)
    assert moderate_new_buyer["level"] == "moderate"
    assert moderate_new_buyer["evidence_paths"] == ["buyer.order_history"]

    # Only large_request: established buyer, qty at the threshold.
    moderate_large_qty = personalization.risk_assessment("BUYER-EXISTING", qty=10, qty_breaks=[], orders=orders)
    assert moderate_large_qty["level"] == "moderate"
    assert moderate_large_qty["evidence_paths"] == ["policy.qty_breaks"]

    # Both: new buyer AND large qty.
    high_result = personalization.risk_assessment("BUYER-NEW", qty=10, qty_breaks=[], orders=orders)
    assert high_result["level"] == "high"
    assert high_result["evidence_paths"] == ["buyer.order_history", "policy.qty_breaks"]

    # Exactly 1 prior order is still "new" (< 2, the confirmed threshold).
    one_order = personalization.risk_assessment("BUYER-ONE", qty=3, qty_breaks=[], orders=[{"buyer_id": "BUYER-ONE", "amount": 1.0}])
    assert one_order["level"] == "moderate"

    # qty_breaks, when present, overrides the flat fallback (10) with its
    # own lowest tier.
    tiered = personalization.risk_assessment(
        "BUYER-EXISTING", qty=5, qty_breaks=[{"min_qty": 5, "discount_pct": 15}, {"min_qty": 20, "discount_pct": 25}],
        orders=orders,
    )
    assert tiered["level"] == "moderate"  # qty(5) >= lowest tier(5), even though it's below the flat fallback(10)


def test_new_buyer_large_request_proceeds_with_zero_discount_room_via_run_negotiation(tmp_path):
    """A direct, run_negotiation()-level check (no payment phase) that a
    new buyer requesting a large qty (HIGH risk: both factors) proceeds
    normally rather than being blocked -- reframed 2026-09-02 (Section
    2N, confirmed with the user): HIGH risk is a PRICING-ABUSE signal,
    not a trust/fraud one, so it no longer blocks the negotiation before
    any offer exists (the original NEGOTIATION_DECLINED design). Instead
    max_discount_pct is forced to 0 for this negotiation -- full list
    price only. See test_high_risk_forces_list_price_only_and_still_requires_human_approval
    below for the fuller end-to-end (payment + approval-gate) version of
    this same scenario."""
    audit_path = tmp_path / "negotiation.log"
    product = _demo_product("SKU-RISK-001", current_inventory=50)  # qty_breaks=[] -> fallback threshold 10
    policy = personalization.product_to_policy(product, max_negotiation_rounds=5, transaction_approval_threshold=20000)
    orders = []  # this buyer has never ordered anything

    buyer = BuyerAgent(qty=10, opening_discount_pct=15, max_acceptable_price=1000.0, list_price=policy["list_price"])
    outcome = run_negotiation(policy, buyer, audit_path=str(audit_path), buyer_id="BUYER-BRAND-NEW", orders=orders)

    assert outcome["state"] in ("AGREEMENT_RECORDED", "REJECTED")
    assert outcome["risk_level"] == "high"
    if outcome["state"] == "AGREEMENT_RECORDED":
        assert outcome["offer"]["price"] == policy["list_price"]  # zero discount room

    entries = _read_log(audit_path)
    risk_entries = [e for e in entries if e["action"] == "risk_review"]
    assert len(risk_entries) == 1
    assert "new buyer" in risk_entries[0]["rationale"]
    assert "large request" in risk_entries[0]["rationale"]
    assert "max_discount_pct forced to 0" in risk_entries[0]["rationale"]
    assert any(e["action"] == "offer" for e in entries)  # unlike the old behavior, an offer IS generated


def test_high_risk_forces_list_price_only_and_still_requires_human_approval(tmp_path):
    """Requirement #4's explicit replacement test (Section 2N, confirmed
    with the user 2026-09-02): a HIGH-risk negotiation (new buyer AND
    large qty) reaches a NORMAL outcome -- no block -- but with
    max_discount_pct forced to 0 for this negotiation, so the effective
    floor equals list_price exactly (no negotiation room at all).
    Human-approval is still forced, same mechanism as MODERATE, even for
    a transaction total far below transaction_approval_threshold --
    "the risk hasn't gone away, just the response to it has," per the
    user's own framing, confirmed rather than assumed.

    2026-09-02 gap-closing follow-up: uses a product with REAL qty_breaks
    tiers (mirroring SKU-ELEC-007's shape, where the actual gap was
    reported), not _demo_product()'s empty ones -- the qty (10) used
    below deliberately matches this product's first tier's min_qty, so
    if apply_risk_discount_cap() only zeroed max_discount_pct and left
    qty_breaks untouched, _applicable_tier() would still pick that tier's
    (unzeroed) discount_pct over max_discount_pct and the floor would
    land at 780.00 (1000 * (1 - 22%)), not list_price -- the exact
    silent-failure mode a qty_breaks-less product could never expose."""
    audit_path = tmp_path / "negotiation.log"
    product = _demo_product("SKU-RISK-004", current_inventory=50)
    product["qty_breaks"] = [{"min_qty": 10, "discount_pct": 22}, {"min_qty": 25, "discount_pct": 26}]
    policy = personalization.product_to_policy(product, max_negotiation_rounds=5, transaction_approval_threshold=20000)
    orders = []  # new buyer -- the first of the two HIGH-risk factors

    high_risk = personalization.risk_assessment("BUYER-BRAND-NEW-3", qty=10, qty_breaks=policy["qty_breaks"], orders=orders)
    assert high_risk["level"] == "high"
    tightened_policy = personalization.apply_risk_discount_cap(policy, high_risk)
    assert tightened_policy["qty_breaks"][0]["discount_pct"] == 0  # the tier itself was actually zeroed

    from src.agents.merchant_agent import _floor_price
    floor, evidence_path = _floor_price(tightened_policy, qty=10)
    assert floor == policy["list_price"] == 1000.0  # zero discount room -- full list price only, NOT 780.00
    assert evidence_path == "policy.qty_breaks[0].discount_pct"  # this tier still "wins" the max() -- just at 0%

    # Buyer is willing to pay full list price -- otherwise this negotiation
    # round-caps to REJECTED, which is also a valid "normal outcome" but
    # less useful to demonstrate the approval-gate behavior with here.
    buyer = BuyerAgent(qty=10, opening_discount_pct=15, max_acceptable_price=policy["list_price"], list_price=policy["list_price"])
    approval_calls = []

    def _approve(message):
        approval_calls.append(message)
        return True

    outcome = run_full_transaction(
        policy, buyer, audit_path=str(audit_path), payment_client=SpyClient(),
        product=product, catalog_path=None, approval_confirm=_approve,
        buyer_id="BUYER-BRAND-NEW-3", orders=orders,
    )

    assert outcome["state"] == "COMPLETED"
    assert outcome["offer"]["price"] == policy["list_price"]  # settled at full list price, no discount
    assert len(approval_calls) == 1  # human-approval still forced, same as MODERATE
    assert outcome["offer"]["price"] * outcome["offer"]["qty"] < policy["transaction_approval_threshold"]

    entries = _read_log(audit_path)
    risk_entry = next(e for e in entries if e["action"] == "risk_review")
    assert "new buyer" in risk_entry["rationale"]
    assert "large request" in risk_entry["rationale"]
    # The rationale states the ACTUAL, freshly-recomputed effective floor
    # in the same line as the "no negotiation room" claim -- not just a
    # generic assertion -- so a future regression in the qty_breaks-zeroing
    # would show up here directly (e.g. "780.00" instead of "1000.00").
    assert "Effective floor for this negotiation: 1000.00" in risk_entry["rationale"]
    approval_entry = next(e for e in entries if e["action"] == "approval_requested")
    assert "high" in approval_entry["rationale"].lower()
    assert approval_entry["evidence_paths"] == ["risk_agent.risk_level"]
    assert any(e["action"] == "offer" for e in entries)  # negotiation DID proceed normally -- offers were made


def test_established_buyer_normal_qty_proceeds_completely_unaffected(tmp_path):
    """Requirement #5's second explicit case: an established buyer with
    normal order history proceeds exactly as if the Risk Agent didn't
    exist -- risk_level "none" produces byte-identical behavior to
    omitting buyer_id/orders entirely (the pre-Milestone-5 default)."""
    product = _demo_product("SKU-RISK-002", current_inventory=50)
    policy = personalization.product_to_policy(product, max_negotiation_rounds=5, transaction_approval_threshold=20000)
    orders = [{"buyer_id": "BUYER-ESTABLISHED", "amount": 500.0} for _ in range(5)]  # well-established

    def _run(audit_path, **kwargs):
        buyer = BuyerAgent(qty=3, opening_discount_pct=15, max_acceptable_price=1000.0, list_price=policy["list_price"])
        return run_negotiation(policy, buyer, audit_path=str(audit_path), **kwargs)

    with_risk_check = _run(tmp_path / "with_risk.log", buyer_id="BUYER-ESTABLISHED", orders=orders)
    without_risk_check = _run(tmp_path / "without_risk.log")  # pre-Milestone-5 default -- no buyer_id/orders at all

    assert with_risk_check["state"] == "AGREEMENT_RECORDED"
    assert with_risk_check["risk_level"] == "none"
    # Identical outcome either way -- the risk check made zero difference.
    # (offer_id/expiration/timestamp are freshly generated per call, so
    # compare the negotiated terms, not full dict/offer_id equality.)
    assert with_risk_check["state"] == without_risk_check["state"]
    assert with_risk_check["offer"]["price"] == without_risk_check["offer"]["price"]
    assert with_risk_check["offer"]["qty"] == without_risk_check["offer"]["qty"]


def test_moderate_risk_logs_but_does_not_gate_or_restrict_pricing(tmp_path):
    """Follow-up (2026-09-02, Section 2O, confirmed with the user): drops
    forced human-approval from MODERATE risk entirely. MODERATE (exactly
    one factor -- here, new_buyer alone, since qty stays below the
    large-request threshold) now proceeds with ZERO added friction: no
    approval gate, no discount restriction -- but still logs a
    risk_review audit entry naming the single factor, so it stays
    visible/explainable without adding friction. Distinct from HIGH
    (both factors), which still forces the gate AND caps
    max_discount_pct to 0 -- see
    test_high_risk_forces_list_price_only_and_still_requires_human_approval
    below."""
    audit_path = tmp_path / "negotiation.log"
    product = _demo_product("SKU-RISK-003", current_inventory=50, cost=10.0, min_price=15.0)
    product["list_price"] = 20.0
    product["max_discount_pct"] = 5
    policy = personalization.product_to_policy(product, max_negotiation_rounds=5, transaction_approval_threshold=20000)
    orders = []  # new buyer -- the sole risk factor here (qty=1 stays below the large-request threshold)

    buyer = BuyerAgent(qty=1, opening_discount_pct=5, max_acceptable_price=20.0, list_price=policy["list_price"])
    approval_calls = []

    def _approve(message):
        approval_calls.append(message)
        return True

    outcome = run_full_transaction(
        policy, buyer, audit_path=str(audit_path), payment_client=SpyClient(),
        product=product, catalog_path=None, approval_confirm=_approve,
        buyer_id="BUYER-BRAND-NEW-2", orders=orders,
    )

    assert outcome["state"] == "COMPLETED"
    assert approval_calls == []  # the gate was NEVER triggered -- zero added friction
    # Normal discount still fully available -- NOT forced to list_price
    # (that's HIGH-only behavior). max_discount_pct=5% -> floor 19.00,
    # below list_price (20.00).
    expected_floor = round(policy["list_price"] * (1 - policy["max_discount_pct"] / 100), 2)
    assert expected_floor < policy["list_price"]
    assert outcome["offer"]["price"] == expected_floor

    entries = _read_log(audit_path)
    assert not any(e["action"] == "approval_requested" for e in entries)  # gate never fired at all
    risk_entry = next(e for e in entries if e["action"] == "risk_review")
    assert "new buyer" in risk_entry["rationale"]
    assert "large request" not in risk_entry["rationale"]  # only the one factor is present
    assert risk_entry["evidence_paths"] == ["buyer.order_history"]


def test_qty_exceeding_inventory_routes_to_rollback_without_calling_payment_service(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    catalog_path = tmp_path / "catalog.json"
    product = _demo_product("SKU-TEST-001", current_inventory=2)  # buyer will ask for qty=3
    catalog_path.write_text(json.dumps([product]), encoding="utf-8")

    policy = personalization.product_to_policy(product, max_negotiation_rounds=5, transaction_approval_threshold=20000)
    buyer = BuyerAgent(qty=3, opening_discount_pct=5, max_acceptable_price=1000.0, list_price=policy["list_price"])
    client = SpyClient()

    outcome = run_full_transaction(
        policy, buyer, audit_path=str(audit_path), payment_client=client,
        product=product, catalog_path=str(catalog_path),
    )

    assert outcome["state"] == "ROLLBACK"
    assert outcome["reason"] == "insufficient_inventory"
    assert outcome["payment"] is None
    assert client.order.calls == []  # payment service never invoked

    entries = _read_log(audit_path)
    assert any(e["action"] == "insufficient_inventory" for e in entries)
    assert not any(e["action"].startswith("payment_") for e in entries)

    # catalog is untouched -- nothing was fulfilled
    unchanged_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert unchanged_catalog[0]["current_inventory"] == 2


def test_force_insufficient_inventory_reports_a_believable_simulated_stock_not_the_real_value(tmp_path):
    """Console-consistency follow-up (2026-09-02): FORCE_INSUFFICIENT_INVENTORY
    stages the ROLLBACK correctly, but before this fix the console box
    displayed the REAL current_inventory (almost always well above qty --
    that's the whole point of forcing this rather than depleting real
    catalog data), producing a nonsensical "Requested: 3, In stock: 138".
    outcome["simulated_stock"] (qty - 1 -- always a believable near-miss,
    even at qty=1) is what negotiation_loop._print_insufficient_inventory_summary()
    now displays instead; real current_inventory must stay completely
    untouched and truthfully reported in the audit log regardless."""
    audit_path = tmp_path / "negotiation.log"
    catalog_path = tmp_path / "catalog.json"
    product = _demo_product("SKU-TEST-STAGED", current_inventory=138)  # plenty of real stock
    catalog_path.write_text(json.dumps([product]), encoding="utf-8")

    policy = personalization.product_to_policy(product, max_negotiation_rounds=5, transaction_approval_threshold=20000)
    buyer = BuyerAgent(qty=3, opening_discount_pct=5, max_acceptable_price=1000.0, list_price=policy["list_price"])
    client = SpyClient()

    outcome = run_full_transaction(
        policy, buyer, audit_path=str(audit_path), payment_client=client,
        product=product, catalog_path=str(catalog_path), force_insufficient_inventory=True,
    )

    assert outcome["state"] == "ROLLBACK"
    assert outcome["reason"] == "insufficient_inventory"
    assert outcome["simulated_stock"] == 2  # qty(3) - 1 -- visibly below the requested qty
    assert client.order.calls == []  # payment service never invoked

    # Audit trail stays honest: names the real value even while staging.
    entries = _read_log(audit_path)
    insufficient_entry = next(e for e in entries if e["action"] == "insufficient_inventory")
    assert "simulated stock 2" in insufficient_entry["rationale"]
    assert "real current_inventory is actually 138" in insufficient_entry["rationale"]

    # Real current_inventory is completely untouched.
    unchanged_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert unchanged_catalog[0]["current_inventory"] == 138


def test_real_shortfall_still_reports_actual_current_inventory_not_simulated(tmp_path):
    """Companion to the test above: a REAL shortfall (force_insufficient_inventory
    not set) must NOT carry a simulated_stock key -- the real,
    already-believable current_inventory is what
    _print_insufficient_inventory_summary() falls back to displaying."""
    audit_path = tmp_path / "negotiation.log"
    catalog_path = tmp_path / "catalog.json"
    product = _demo_product("SKU-TEST-REAL-SHORTFALL", current_inventory=2)  # buyer will ask for qty=3
    catalog_path.write_text(json.dumps([product]), encoding="utf-8")

    policy = personalization.product_to_policy(product, max_negotiation_rounds=5, transaction_approval_threshold=20000)
    buyer = BuyerAgent(qty=3, opening_discount_pct=5, max_acceptable_price=1000.0, list_price=policy["list_price"])

    outcome = run_full_transaction(
        policy, buyer, audit_path=str(audit_path), payment_client=SpyClient(),
        product=product, catalog_path=str(catalog_path),
    )

    assert outcome["state"] == "ROLLBACK"
    assert "simulated_stock" not in outcome


def test_qty_within_inventory_proceeds_to_payment_as_normal(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    catalog_path = tmp_path / "catalog.json"
    product = _demo_product("SKU-TEST-002", current_inventory=50)
    catalog_path.write_text(json.dumps([product]), encoding="utf-8")

    policy = personalization.product_to_policy(product, max_negotiation_rounds=5, transaction_approval_threshold=20000)
    buyer = BuyerAgent(qty=3, opening_discount_pct=5, max_acceptable_price=1000.0, list_price=policy["list_price"])
    client = SpyClient()

    outcome = run_full_transaction(
        policy, buyer, audit_path=str(audit_path), payment_client=client,
        product=product, catalog_path=str(catalog_path),
    )

    assert outcome["state"] == "COMPLETED"
    assert len(client.order.calls) == 1


def test_completed_order_decrements_current_inventory_by_ordered_qty(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    catalog_path = tmp_path / "catalog.json"
    product = _demo_product("SKU-TEST-003", current_inventory=50)
    catalog_path.write_text(json.dumps([product]), encoding="utf-8")

    policy = personalization.product_to_policy(product, max_negotiation_rounds=5, transaction_approval_threshold=20000)
    buyer = BuyerAgent(qty=3, opening_discount_pct=5, max_acceptable_price=1000.0, list_price=policy["list_price"])

    outcome = run_full_transaction(
        policy, buyer, audit_path=str(audit_path), payment_client=SpyClient(),
        product=product, catalog_path=str(catalog_path),
    )

    assert outcome["state"] == "COMPLETED"
    updated_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert updated_catalog[0]["current_inventory"] == 47  # 50 - 3

    entries = _read_log(audit_path)
    assert any(e["action"] == "inventory_decremented" for e in entries)


def test_no_catalog_wired_in_behaves_exactly_as_before(tmp_path):
    """When product/catalog_path are omitted (Milestone 1/2/3a/3b default),
    no inventory check runs at all -- confirms the current single
    hardcoded POLICY path keeps working unchanged."""
    audit_path = tmp_path / "negotiation.log"
    policy = {
        "sku_id": "SKU-DEMO-001", "product_name": "Wireless Mechanical Keyboard", "currency": "INR",
        "list_price": 4999.00, "min_price": 3799.00, "max_discount_pct": 12,
        "qty_breaks": [{"min_qty": 10, "discount_pct": 18}, {"min_qty": 25, "discount_pct": 24}],
        "max_negotiation_rounds": 5, "transaction_approval_threshold": 20000, "inventory_floor": 1,
    }
    buyer = BuyerAgent(qty=3, opening_discount_pct=15, max_acceptable_price=4450.0, list_price=policy["list_price"])

    outcome = run_full_transaction(policy, buyer, audit_path=str(audit_path), payment_client=SpyClient())

    assert outcome["state"] == "COMPLETED"
    entries = _read_log(audit_path)
    assert not any(e["action"] in ("insufficient_inventory", "inventory_decremented") for e in entries)
