import json

import pytest

from src import personalization
from src.agents.buyer_agent import BuyerAgent
from src.negotiation_loop import run_full_transaction


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
