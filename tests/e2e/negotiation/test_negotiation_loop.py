import json

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
    buyer = BuyerAgent(qty=1, opening_discount_pct=15, max_acceptable_price=4000.0, list_price=POLICY["list_price"])

    outcome = run_negotiation(POLICY, buyer, audit_path=str(audit_path))

    assert outcome["state"] == "REJECTED"
    assert outcome["offer"] is None

    entries = _read_log(audit_path)
    merchant_entries = [e for e in entries if e["agent"] == "merchant-agent"]
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
