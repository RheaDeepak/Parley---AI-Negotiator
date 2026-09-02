from src.agents.merchant_agent import evaluate
from src.agents.offer_utils import new_offer

POLICY = {
    "sku_id": "SKU-DEMO-001",
    "product_name": "Wireless Mechanical Keyboard",
    "currency": "INR",
    "list_price": 4999.00,
    "min_price": 3799.00,
    "max_discount_pct": 12,
    "qty_breaks": [
        {"min_qty": 10, "discount_pct": 18},
        {"min_qty": 25, "discount_pct": 24},
    ],
    "max_negotiation_rounds": 5,
}


def test_offer_below_min_price_rejected():
    offer = new_offer(price=3500.0, qty=3)
    result = evaluate(offer, POLICY, round=1)

    assert result["decision"] == "reject"
    assert result["evidence_paths"] == ["policy.min_price"]
    assert "min_price" in result["rationale"]


def test_offer_exceeding_max_discount_pct_at_final_round_rejected():
    # Between min_price (3799) and the qty=1 floor (4999*0.88=4399.12) --
    # only violates max_discount_pct, not min_price. At the final round
    # there is no room left to counter, so it terminates in reject.
    offer = new_offer(price=4200.0, qty=1)
    result = evaluate(offer, POLICY, round=POLICY["max_negotiation_rounds"])

    assert result["decision"] == "reject"
    assert "policy.max_discount_pct" in result["evidence_paths"]
    assert "policy.max_negotiation_rounds" in result["evidence_paths"]


def test_offer_within_bounds_accepted():
    offer = new_offer(price=4399.12, qty=1)
    result = evaluate(offer, POLICY, round=1)

    assert result["decision"] == "accept"
    assert result["evidence_paths"] == ["policy.max_discount_pct"]


def test_qty_breaks_apply_correctly():
    # qty=1 -> base max_discount_pct floor
    offer_low_qty = new_offer(price=4200.0, qty=1)
    result_low_qty = evaluate(offer_low_qty, POLICY, round=1)
    assert result_low_qty["decision"] == "counter"
    assert result_low_qty["offer"]["price"] == 4399.12
    assert result_low_qty["evidence_paths"] == ["policy.max_discount_pct"]

    # qty=10 -> first qty_breaks tier (18% off -> floor 4099.18)
    offer_tier1 = new_offer(price=4000.0, qty=10)
    result_tier1 = evaluate(offer_tier1, POLICY, round=1)
    assert result_tier1["decision"] == "counter"
    assert result_tier1["offer"]["price"] == 4099.18
    assert result_tier1["evidence_paths"] == ["policy.qty_breaks[0].discount_pct"]

    # qty=25 -> second qty_breaks tier (24% off -> floor 3799.24). Priced
    # just above min_price (3799.00) but still below that floor.
    offer_tier2 = new_offer(price=3799.10, qty=25)
    result_tier2 = evaluate(offer_tier2, POLICY, round=1)
    assert result_tier2["decision"] == "counter"
    assert result_tier2["offer"]["price"] == 3799.24
    assert result_tier2["evidence_paths"] == ["policy.qty_breaks[1].discount_pct"]


def test_round_exceeding_cap_rejected():
    offer = new_offer(price=4399.12, qty=1)
    result = evaluate(offer, POLICY, round=POLICY["max_negotiation_rounds"] + 1)

    assert result["decision"] == "reject"
    assert result["evidence_paths"] == ["policy.max_negotiation_rounds"]
