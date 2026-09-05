import json

import pytest

from src.agents.audit_logger import decision_hash
from src.agents.buyer_agent import BuyerAgent
from src.negotiation_loop import run_negotiation

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

REQUIRED_AUDIT_FIELDS = {
    "timestamp", "decision_id", "agent", "action", "offer",
    "rationale", "evidence_paths", "decision_hash", "provenance_sha",
}


def _read_log(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_buyer_id_without_orders_raises_value_error(tmp_path):
    """2026-09-05 guard: passing buyer_id alone accomplishes nothing --
    the risk-assessment block only ever runs when BOTH are given -- so it
    silently skips the Risk Agent entirely while looking like a normal,
    personalized call. This is exactly the near-miss a verification
    trace hit: a script that has a real buyer_id in scope but forgets to
    also pass orders gets risk_level=None ("never assessed"), easily
    mistaken for "none" ("assessed, found no risk"). Must raise loudly,
    not skip quietly."""
    audit_path = tmp_path / "negotiation.log"
    buyer = BuyerAgent(qty=1, opening_discount_pct=15, max_acceptable_price=4450.0, list_price=POLICY["list_price"])

    with pytest.raises(ValueError, match="requires buyer_id and orders TOGETHER, or NEITHER"):
        run_negotiation(POLICY, buyer, audit_path=str(audit_path), buyer_id="BUYER-001", orders=None)


def test_orders_without_buyer_id_raises_value_error(tmp_path):
    """The other half of the same mistake -- orders alone is just as
    useless and just as silent without this guard."""
    audit_path = tmp_path / "negotiation.log"
    buyer = BuyerAgent(qty=1, opening_discount_pct=15, max_acceptable_price=4450.0, list_price=POLICY["list_price"])

    with pytest.raises(ValueError, match="requires buyer_id and orders TOGETHER, or NEITHER"):
        run_negotiation(POLICY, buyer, audit_path=str(audit_path), buyer_id=None, orders=[])


def test_neither_buyer_id_nor_orders_is_still_a_valid_no_personalization_call(tmp_path):
    """Zero-regression guard: omitting BOTH remains completely valid and
    unchanged -- every pre-Milestone-5 caller (and every test that
    deliberately wants no personalization) relies on exactly this.
    risk_level stays Python None (never assessed), not the string "none"
    (assessed, found no risk) -- the guard protects that distinction, it
    doesn't erase the legitimate way to reach the first state."""
    audit_path = tmp_path / "negotiation.log"
    buyer = BuyerAgent(qty=1, opening_discount_pct=15, max_acceptable_price=4450.0, list_price=POLICY["list_price"])

    outcome = run_negotiation(POLICY, buyer, audit_path=str(audit_path))  # buyer_id/orders both default to None

    assert outcome["state"] == "AGREEMENT_RECORDED"
    assert outcome.get("risk_level") is None  # not the string "none" -- genuinely never assessed


def test_negotiation_converges_to_agreement(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    # Opening ask (4999*0.85=4249.15) sits between min_price and the qty=1
    # floor (4399.12), so it draws a counter rather than an instant reject;
    # ceiling (4450) clears that floor, so the counter is accepted.
    buyer = BuyerAgent(qty=1, opening_discount_pct=15, max_acceptable_price=4450.0, list_price=POLICY["list_price"])

    outcome = run_negotiation(POLICY, buyer, audit_path=str(audit_path))

    assert outcome["state"] == "AGREEMENT_RECORDED"
    assert outcome["offer"]["price"] >= POLICY["min_price"]

    entries = _read_log(audit_path)
    assert entries[-1]["action"] == "accept"


def test_negotiation_never_converges_terminates_at_round_cap(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    # Opening ask (4249.15) and ceiling (4000) both sit between min_price
    # (3799) and the qty=1 floor (4399.12): the buyer never breaches
    # min_price (so no instant reject) but can never afford the floor
    # either, so the negotiation must exhaust max_negotiation_rounds.
    # Section 2X: this pauses as ROUND_LIMIT_REACHED, carrying the
    # merchant's last real counter-offer -- not REJECTED outright, and
    # not a None offer -- for the human/buyer to decide on.
    buyer = BuyerAgent(qty=1, opening_discount_pct=15, max_acceptable_price=4000.0, list_price=POLICY["list_price"])

    outcome = run_negotiation(POLICY, buyer, audit_path=str(audit_path))

    assert outcome["state"] == "ROUND_LIMIT_REACHED"
    assert outcome["offer"] is not None

    entries = _read_log(audit_path)
    # Per-round guardrail decisions only -- excludes the "round_limit_reached"
    # pause marker itself (also agent="merchant-agent", but not a round
    # decision; see run_negotiation()'s Section 2X handling).
    merchant_entries = [e for e in entries if e["agent"] == "merchant-agent" and e["action"] in ("accept", "reject", "counter")]
    assert len(merchant_entries) == POLICY["max_negotiation_rounds"]
    assert merchant_entries[-1]["action"] == "reject"
    assert "policy.max_negotiation_rounds" in merchant_entries[-1]["evidence_paths"]


def test_negotiation_produces_valid_parseable_audit_log(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    buyer = BuyerAgent(qty=1, opening_discount_pct=15, max_acceptable_price=4450.0, list_price=POLICY["list_price"])

    run_negotiation(POLICY, buyer, audit_path=str(audit_path))

    entries = _read_log(audit_path)
    assert len(entries) > 0
    for entry in entries:
        assert REQUIRED_AUDIT_FIELDS.issubset(entry.keys())
        recomputed = decision_hash(entry["offer"], entry["rationale"], entry["evidence_paths"])
        assert entry["decision_hash"] == recomputed
