"""Milestone 4: one clean integration test proving the WHOLE pipeline
chains correctly end to end -- negotiation (BUYER_MODE=ai,
MERCHANT_MODE=ai) -> AGREEMENT_RECORDED -> payment -> COMPLETED -- not
just each layer in isolation (every other test file in this suite tests
one piece at a time). Mocked LLM (both agents) and a mocked Razorpay
client keep this fast/free in the regular suite; no live API calls.
"""
import json
from functools import partial

from src import personalization
from src.agents.ai_buyer_agent import AIBuyerAgent
from src.agents.merchant_agent import evaluate_ai
from src.negotiation_loop import run_full_transaction

# Real catalog product, confirmed with the user 2026-09-01 as the "known
# good" scenario for this test: a plain, non-liquidation-eligible product
# (18 days_in_inventory) so the test isn't entangled with Section 2E-2I's
# liquidation math -- this test is about pipeline plumbing, not pricing.
PRODUCT_ID = "SKU-ELEC-001"


class _ScriptedBuyerLLM:
    """Injectable llm_call stub: pre-programmed BuyerDecision-shaped dicts."""

    def __init__(self, decisions):
        self._decisions = list(decisions)

    def __call__(self, system, user_content, model):
        return self._decisions.pop(0)


class _RepeatingMerchantLLM:
    """Injectable llm_call stub: returns the same MerchantDecision-shaped
    dict every call."""

    def __init__(self, decision):
        self.decision = decision

    def __call__(self, system, user_content, model):
        return self.decision


class _SpyOrderAPI:
    def __init__(self):
        self.calls = []

    def create(self, data):
        self.calls.append(data)
        return {
            "id": "order_test_full_pipeline", "amount": data["amount"],
            "currency": data["currency"], "status": "created",
        }


class _SpyClient:
    """Stands in for a real razorpay.Client -- payment_service.create_order()
    only ever touches client.order.create(), so this is a complete mock."""

    def __init__(self):
        self.order = _SpyOrderAPI()


def _read_log(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _merchant_decision(action, price=None, qty=None, terms="", reasoning="Countering at the computed floor."):
    counter_offer = {"price": price, "qty": qty, "terms": terms} if action == "counter" else None
    return {"action": action, "counter_offer": counter_offer, "concession_reasoning": reasoning}


def test_full_pipeline_ai_buyer_ai_merchant_reaches_completed_payment(tmp_path):
    """Economics (data/catalog.json, seed=42): list_price=9879.86,
    min_price=6305.84, max_discount_pct=12% -> floor at qty=3 =
    max(6305.84, 9879.86*0.88) = 8694.28 (discount-cap driven, not
    min_price). Buyer persona budget (9200) sits comfortably above that
    floor, so a scripted two-round negotiation (opening low, then meeting
    the merchant's floor) reaches AGREEMENT_RECORDED deterministically.

    Runs through negotiation_loop.run_full_transaction() -- the exact
    orchestration __main__ uses on the PRODUCT_ID path -- with an
    isolated tmp_path copy of the product so real data/catalog.json is
    never touched, and a mocked Razorpay client so no live API call is
    made."""
    audit_path = tmp_path / "negotiation.log"
    catalog_path = tmp_path / "catalog.json"

    catalog = personalization.load_json(personalization.DEFAULT_CATALOG_PATH)
    product = personalization.find_product(catalog, PRODUCT_ID)
    assert product is not None, f"{PRODUCT_ID} must exist in {personalization.DEFAULT_CATALOG_PATH}"
    original_inventory = product["current_inventory"]
    catalog_path.write_text(json.dumps([product]), encoding="utf-8")  # isolated copy

    policy = personalization.product_to_policy(product, max_negotiation_rounds=5, transaction_approval_threshold=20000)
    assert policy["min_price_is_liquidation_relaxed"] is False  # a plain product -- confirms this is the right fixture

    expected_floor = 8694.28
    assert round(policy["list_price"] * (1 - policy["max_discount_pct"] / 100), 2) == expected_floor
    assert policy["min_price"] < expected_floor  # the discount cap is what actually binds here, not min_price

    persona = {
        "budget": 9200.0, "target_product": policy["product_name"],
        "willingness_to_negotiate": "moderate -- open to a fair discount but not desperate",
    }
    buyer_llm = _ScriptedBuyerLLM([
        {
            "target_price": 8700.0, "walk_away_price": 9200.0,
            "strategy_note": "Opening below target to leave room to negotiate.",
            "offer": {"price": 8000.0, "qty": 3, "terms": ""},
        },
        {
            "target_price": 8700.0, "walk_away_price": 9200.0,
            "strategy_note": "Meeting the merchant's floor to close the deal.",
            "offer": {"price": expected_floor, "qty": 3, "terms": ""},
        },
    ])
    buyer = AIBuyerAgent(
        qty=3, persona=persona, list_price=policy["list_price"], currency=policy["currency"],
        max_negotiation_rounds=policy["max_negotiation_rounds"], llm_call=buyer_llm,
    )
    merchant_evaluate = partial(
        evaluate_ai, llm_call=_RepeatingMerchantLLM(_merchant_decision("counter", price=expected_floor, qty=3)),
    )
    payment_client = _SpyClient()

    outcome = run_full_transaction(
        policy, buyer, audit_path=str(audit_path), merchant_evaluate=merchant_evaluate,
        payment_client=payment_client, product=product, catalog_path=str(catalog_path),
        approval_confirm=lambda message: True,  # total (26082.84) exceeds the 20000 threshold -- auto-approve
    )

    assert outcome["state"] == "COMPLETED"
    assert outcome["offer"]["price"] == expected_floor
    assert outcome["offer"]["qty"] == 3
    assert outcome["payment"]["status"] == "completed"
    assert outcome["payment"]["order_id"] == "order_test_full_pipeline"
    assert len(payment_client.order.calls) == 1  # exactly one order call -- no auto-retry, no duplicate

    # Inventory decremented on the isolated copy; real catalog untouched.
    updated_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert updated_catalog[0]["current_inventory"] == original_inventory - 3
    real_catalog_still = personalization.load_json(personalization.DEFAULT_CATALOG_PATH)
    real_product_still = personalization.find_product(real_catalog_still, PRODUCT_ID)
    assert real_product_still["current_inventory"] == original_inventory

    # The audit trail actually chains through every phase, not just the
    # final outcome dict -- negotiation, then payment, then fulfillment.
    entries = _read_log(audit_path)
    actions = [e["action"] for e in entries]
    assert actions.count("offer") == 1  # round 1's opening offer; round 2 accepts the merchant's counter directly
    assert "counter" in actions
    assert "accept" in actions
    assert "inventory_hold" in actions
    assert "approval_requested" in actions and "approval_granted" in actions
    assert "payment_initiated" in actions
    assert "payment_completed" in actions
    assert "inventory_decremented" in actions
