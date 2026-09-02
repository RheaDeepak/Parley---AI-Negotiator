import json
from datetime import datetime, timedelta, timezone

import pytest

from src.agents.buyer_agent import BuyerAgent
from src.negotiation_loop import retry_payment, run_full_transaction

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
    "transaction_approval_threshold": 20000,
}

FAKE_SECRET = "test_secret_should_never_appear_9f8e7d6c"


class FakeOrderAPI:
    def __init__(self, order_id="order_test123"):
        self.order_id = order_id
        self.calls = []

    def create(self, data):
        self.calls.append(data)
        return {"id": self.order_id, "amount": data["amount"], "currency": data["currency"], "status": "created"}


class FakeRazorpayClient:
    def __init__(self, order_id="order_test123"):
        self.order = FakeOrderAPI(order_id)


def _low_value_buyer():
    # qty=1: total stays well under transaction_approval_threshold (20000).
    return BuyerAgent(qty=1, opening_discount_pct=15, max_acceptable_price=4450.0, list_price=POLICY["list_price"])


def _high_value_buyer():
    # qty=10: floor 4099.18 * 10 = 40991.80, comfortably over the threshold.
    return BuyerAgent(qty=10, opening_discount_pct=20, max_acceptable_price=4150.0, list_price=POLICY["list_price"])


def _read_log(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_successful_payment_reaches_completed(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    client = FakeRazorpayClient()

    outcome = run_full_transaction(
        POLICY, _low_value_buyer(), audit_path=str(audit_path),
        force_payment_failure=False, payment_client=client,
    )

    assert outcome["state"] == "COMPLETED"
    assert outcome["payment"]["status"] == "completed"
    assert len(client.order.calls) == 1

    entries = _read_log(audit_path)
    actions = [e["action"] for e in entries]
    assert "payment_initiated" in actions
    assert "payment_completed" in actions
    assert entries[-1]["payment"]["status"] == "completed"


def test_forced_failure_reaches_rollback_with_release_and_notification(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    client = FakeRazorpayClient()

    outcome = run_full_transaction(
        POLICY, _low_value_buyer(), audit_path=str(audit_path),
        force_payment_failure=True, payment_client=client,
    )

    assert outcome["state"] == "ROLLBACK"
    assert outcome["payment"]["status"] == "failed"
    assert outcome["payment"]["error_code"]

    entries = _read_log(audit_path)
    actions = [e["action"] for e in entries]
    assert "payment_rollback" in actions
    assert "inventory_release" in actions
    assert "human_notification" in actions

    rollback_entry = next(e for e in entries if e["action"] == "payment_rollback")
    assert rollback_entry["payment"]["error_code"] == outcome["payment"]["error_code"]


def test_above_threshold_pauses_for_approval_and_declines_without_payment_call(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    client = FakeRazorpayClient()

    outcome = run_full_transaction(
        POLICY, _high_value_buyer(), audit_path=str(audit_path),
        payment_client=client, approval_confirm=lambda message: False,
    )

    assert outcome["state"] == "APPROVAL_DECLINED"
    assert len(client.order.calls) == 0  # payment_service never called

    entries = _read_log(audit_path)
    actions = [e["action"] for e in entries]
    assert "approval_requested" in actions
    assert "approval_declined" in actions
    assert "payment_initiated" not in actions


def test_above_threshold_pauses_then_proceeds_once_approved(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    client = FakeRazorpayClient()

    outcome = run_full_transaction(
        POLICY, _high_value_buyer(), audit_path=str(audit_path),
        payment_client=client, approval_confirm=lambda message: True,
    )

    assert outcome["state"] == "COMPLETED"
    assert len(client.order.calls) == 1  # called only after approval

    entries = _read_log(audit_path)
    actions = [e["action"] for e in entries]
    assert actions.index("approval_requested") < actions.index("approval_granted") < actions.index("payment_initiated")


def test_no_secret_ever_appears_in_audit_log(tmp_path, monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fakekeyid")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", FAKE_SECRET)
    audit_path = tmp_path / "negotiation.log"
    client = FakeRazorpayClient()

    run_full_transaction(
        POLICY, _low_value_buyer(), audit_path=str(audit_path),
        force_payment_failure=False, payment_client=client,
    )
    run_full_transaction(
        POLICY, _low_value_buyer(), audit_path=str(audit_path),
        force_payment_failure=True, payment_client=client,
    )

    raw_log = audit_path.read_text(encoding="utf-8")
    assert FAKE_SECRET not in raw_log
    assert "rzp_test_fakekeyid" not in raw_log


def test_buyer_notification_logged_distinct_from_human_notification_on_failure(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    client = FakeRazorpayClient()

    run_full_transaction(
        POLICY, _low_value_buyer(), audit_path=str(audit_path),
        force_payment_failure=True, payment_client=client,
    )

    entries = _read_log(audit_path)
    buyer_notifs = [e for e in entries if e["action"] == "buyer_notification"]
    human_notifs = [e for e in entries if e["action"] == "human_notification"]

    assert len(buyer_notifs) == 1
    assert len(human_notifs) == 1
    assert buyer_notifs[0]["agent"] == "buyer-agent"
    assert human_notifs[0]["agent"] == "merchant-agent"
    assert buyer_notifs[0]["rationale"] != human_notifs[0]["rationale"]


def test_failed_payment_is_never_automatically_retried(tmp_path):
    """The core guarantee (point 2): one run_full_transaction() call that
    hits a forced failure makes exactly one payment_service call. No
    automatic second attempt happens without a separate, explicit
    retry_payment() call -- see the next test for that explicit path."""
    audit_path = tmp_path / "negotiation.log"
    client = FakeRazorpayClient()

    outcome = run_full_transaction(
        POLICY, _low_value_buyer(), audit_path=str(audit_path),
        force_payment_failure=True, payment_client=client,
    )

    assert outcome["state"] == "ROLLBACK"
    assert len(client.order.calls) == 1  # exactly one attempt -- never auto-retried

    entries = _read_log(audit_path)
    actions = [e["action"] for e in entries]
    assert actions.count("payment_initiated") == 1
    assert "payment_retry_approved" not in actions  # no retry happened at all


def test_retry_payment_is_a_distinct_explicit_call_that_can_then_succeed(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    client = FakeRazorpayClient()

    first_outcome = run_full_transaction(
        POLICY, _low_value_buyer(), audit_path=str(audit_path),
        force_payment_failure=True, payment_client=client,
    )
    assert first_outcome["state"] == "ROLLBACK"
    assert len(client.order.calls) == 1

    # A distinct, separate call is required -- nothing retries on its own.
    second_outcome = retry_payment(
        first_outcome["offer"], POLICY, audit_path=str(audit_path),
        payment_client=client, force_payment_failure=False,
    )

    assert second_outcome["state"] == "COMPLETED"
    assert len(client.order.calls) == 2  # exactly one more call, from the explicit retry

    entries = _read_log(audit_path)
    actions = [e["action"] for e in entries]
    assert "payment_retry_approved" in actions
    assert actions.count("payment_initiated") == 2


def test_retry_payment_refuses_an_expired_offer(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    client = FakeRazorpayClient()
    expired_offer = {
        "offer_id": "expired-offer-1", "price": 4399.12, "qty": 1, "terms": "",
        "expiration": (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timestamp": (datetime.now(timezone.utc) - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    with pytest.raises(ValueError):
        retry_payment(expired_offer, POLICY, audit_path=str(audit_path), payment_client=client)

    assert client.order.calls == []  # never even attempted against an expired offer
