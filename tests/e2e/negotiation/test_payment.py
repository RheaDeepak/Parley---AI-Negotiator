import json
from datetime import datetime, timedelta, timezone

import pytest

from src.agents.buyer_agent import BuyerAgent
from src.negotiation_loop import PAUSE_FOR_APPROVAL, resolve_round_limit_decision, retry_payment, run_full_transaction

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


# ---------------------------------------------------------------------------
# Section 2U (2026-09-04): a round-limit accept_final auto-accept forces the
# approval gate too -- same footing as HIGH risk / strict-merchant MODERATE
# risk, reusing the exact same approval_requested/granted/declined
# mechanism, never a second flow.
# ---------------------------------------------------------------------------


def _stalemate_buyer(max_acceptable_price=4200.0):
    """Opens ABOVE min_price (3799) but below the discount-cap floor
    (4399.12 at qty=1) -- so the merchant COUNTERS, not instant-rejects --
    and never raises its ceiling to meet that counter, so the negotiation
    genuinely exhausts all 5 rounds with no agreement on price alone."""
    return BuyerAgent(
        qty=1, opening_discount_pct=20, max_acceptable_price=max_acceptable_price,
        list_price=POLICY["list_price"],
    )


def test_round_limit_reached_pauses_correctly(tmp_path):
    """Section 2X (2026-09-04): replaces the removed `on_round_limit`
    parameter and Section 2U's auto-accept-then-forced-approval logic
    entirely. Exhausting all 5 rounds with no agreement no longer decides
    walk-away-vs-accept upfront -- it ALWAYS pauses, returning
    "ROUND_LIMIT_PENDING" (via the SAME PAUSE_FOR_APPROVAL sentinel the
    human-approval gate already uses) with the merchant's own last real
    counter-offer (4399.12, the discount-cap floor -- never a fabricated
    number), and the exact policy/requested_perks the resume path needs.
    No payment call, no approval prompt yet -- this is a DIFFERENT,
    earlier decision point than the approval gate (see
    NEGOTIATION_SPEC.md Section 2X)."""
    audit_path = tmp_path / "negotiation.log"
    client = FakeRazorpayClient()

    outcome = run_full_transaction(
        POLICY, _stalemate_buyer(), audit_path=str(audit_path),
        payment_client=client, approval_confirm=PAUSE_FOR_APPROVAL,
    )

    assert outcome["state"] == "ROUND_LIMIT_PENDING"
    assert outcome["offer"]["price"] == 4399.12  # the merchant's floor -- well under the 20000 threshold
    assert "policy" in outcome and "requested_perks" in outcome  # resolve_round_limit_decision() needs both
    assert len(client.order.calls) == 0  # payment_service never called

    entries = _read_log(audit_path)
    actions = [e["action"] for e in entries]
    assert "round_limit_reached" in actions
    assert "approval_requested" not in actions  # no threshold/risk trigger here -- nothing to gate yet
    assert "payment_initiated" not in actions


def test_round_limit_accept_proceeds_to_normal_payment_flow(tmp_path):
    """Accepting a round-limit pause flows through the EXACT SAME
    post-agreement logic (_process_agreement()) a genuine negotiated
    agreement would -- inventory check, approval gate (not triggered
    here -- NONE risk, price well under threshold), payment. No
    shortcut: this reuses run_full_transaction()'s own function, not a
    reimplementation, so there's no separate code path that could drift
    or silently skip a guardrail."""
    audit_path = tmp_path / "negotiation.log"
    client = FakeRazorpayClient()

    pending = run_full_transaction(
        POLICY, _stalemate_buyer(), audit_path=str(audit_path),
        payment_client=client, approval_confirm=PAUSE_FOR_APPROVAL,
    )
    assert pending["state"] == "ROUND_LIMIT_PENDING"

    outcome = resolve_round_limit_decision(
        pending, accept=True, audit_path=str(audit_path), payment_client=client,
    )

    assert outcome["state"] == "COMPLETED"
    assert outcome["offer"]["price"] == 4399.12
    assert len(client.order.calls) == 1

    entries = _read_log(audit_path)
    actions = [e["action"] for e in entries]
    assert actions.index("round_limit_reached") < actions.index("accept") < actions.index("payment_initiated") < actions.index("payment_completed")


def test_round_limit_decline_produces_rejected(tmp_path):
    """The other resolution: declining the merchant's final offer
    produces the same REJECTED shape a normal round-cap walk-away always
    has -- no payment call, ever."""
    audit_path = tmp_path / "negotiation.log"
    client = FakeRazorpayClient()

    pending = run_full_transaction(
        POLICY, _stalemate_buyer(), audit_path=str(audit_path),
        payment_client=client, approval_confirm=PAUSE_FOR_APPROVAL,
    )
    assert pending["state"] == "ROUND_LIMIT_PENDING"

    outcome = resolve_round_limit_decision(pending, accept=False, audit_path=str(audit_path))

    assert outcome["state"] == "REJECTED"
    assert outcome["offer"] is None
    assert len(client.order.calls) == 0

    entries = _read_log(audit_path)
    actions = [e["action"] for e in entries]
    assert "round_limit_reached" in actions
    assert "reject" in actions
    assert "payment_initiated" not in actions


def test_round_limit_accept_combined_with_high_risk_pauses_again_not_twice(tmp_path):
    """The specific "don't double-prompt" regression this replaces
    (Section 2U's combined-rationale approach) needs re-confirming under
    the new design: a round-limit acceptance that ALSO turns out to need
    the normal human-approval gate (here: HIGH risk, computed only AFTER
    accepting, since the risk-tightened floor -- list_price, 4999.00 --
    IS the merchant's final counter in this scenario) must pause a
    SECOND, separate time -- not skip the gate, and not merge the two
    into one prompt the way Section 2U did. Exactly one
    "round_limit_reached" entry and exactly one SEPARATE
    "approval_requested" entry, the latter naming ONLY the risk reason
    (round-limit is no longer a forced trigger at all -- see
    run_full_transaction()'s Section 2X docstring). buyer_id has 0 prior
    orders (new buyer) and qty=10 hits POLICY's qty_breaks[0] tier, so
    both risk factors are present -> HIGH. A generous
    transaction_approval_threshold override keeps the threshold trigger
    OUT of this test, isolating exactly the risk-only trigger."""
    audit_path = tmp_path / "negotiation.log"
    client = FakeRazorpayClient()
    high_threshold_policy = {**POLICY, "transaction_approval_threshold": 100000}

    # Opens above min_price (3799) but below what a HIGH-risk floor will
    # become (list_price, 4999.00 exactly, once the Risk Agent zeroes the
    # discount ceiling) -- and never raises its ceiling to meet it, so
    # this also stalemates for all 5 rounds.
    buyer = BuyerAgent(qty=10, opening_discount_pct=10, max_acceptable_price=4500.0, list_price=POLICY["list_price"])

    pending = run_full_transaction(
        high_threshold_policy, buyer, audit_path=str(audit_path),
        payment_client=client, approval_confirm=PAUSE_FOR_APPROVAL,
        buyer_id="BUYER-COMBO-TEST", orders=[],
    )
    assert pending["state"] == "ROUND_LIMIT_PENDING"
    assert pending["offer"]["price"] == 4999.00  # HIGH risk's floor -- full list price, no discount at all

    outcome = resolve_round_limit_decision(
        pending, accept=True, audit_path=str(audit_path), payment_client=client,
        approval_confirm=PAUSE_FOR_APPROVAL,
    )

    assert outcome["state"] == "PENDING_APPROVAL"  # a SECOND, separate pause -- not skipped, not merged
    assert len(client.order.calls) == 0  # payment never attempted -- still gated

    entries = _read_log(audit_path)
    round_limit_entries = [e for e in entries if e["action"] == "round_limit_reached"]
    approval_requests = [e for e in entries if e["action"] == "approval_requested"]
    assert len(round_limit_entries) == 1  # not duplicated
    assert len(approval_requests) == 1  # not duplicated, and not skipped either

    rationale = approval_requests[0]["rationale"]
    assert "high risk" in rationale.lower()
    assert "round limit" not in rationale.lower()  # a clean, single-reason trigger -- not a Section 2U-style merge
    assert approval_requests[0]["evidence_paths"] == ["risk_agent.risk_level"]


def test_existing_threshold_and_moderate_risk_approval_triggers_unchanged(tmp_path):
    """Zero-regression guard: the pre-Section-2X threshold-triggered
    approval path (the exact scenario
    test_above_threshold_pauses_for_approval_and_declines_without_payment_call
    already covers) still fires exactly as before -- a genuine agreement
    reached well before the round cap never touches the round-limit
    pause at all, so the rationale names only the threshold."""
    audit_path = tmp_path / "negotiation.log"
    client = FakeRazorpayClient()

    outcome = run_full_transaction(
        POLICY, _high_value_buyer(), audit_path=str(audit_path),
        payment_client=client, approval_confirm=lambda message: False,
    )

    assert outcome["state"] == "APPROVAL_DECLINED"
    entries = _read_log(audit_path)
    approval_requests = [e for e in entries if e["action"] == "approval_requested"]
    assert len(approval_requests) == 1
    rationale = approval_requests[0]["rationale"]
    assert "transaction total" in rationale
    assert "round limit" not in rationale
    assert " AND " not in rationale
    assert approval_requests[0]["evidence_paths"] == ["policy.transaction_approval_threshold"]
