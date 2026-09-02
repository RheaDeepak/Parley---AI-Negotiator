import json
import os
from datetime import datetime, timezone

from src import personalization
from src.agents import merchant_agent, payment_service
from src.agents.ai_buyer_agent import AIBuyerAgent, BuyerUnavailableError
from src.agents.audit_logger import DEFAULT_AUDIT_PATH, log_entry
from src.agents.buyer_agent import BuyerAgent


def _log(on_event, round_num, *args, **kwargs):
    """Wraps log_entry() so callers can also observe each entry as it's
    written -- without hardwiring any I/O (printing, etc.) into the
    reusable negotiation functions themselves. `on_event`, when given, is
    called as on_event(entry, round_num) right after the entry is
    persisted; it defaults to a no-op so run_negotiation()/
    run_full_transaction() stay silent (and the existing test suite
    unaffected) unless a caller opts in."""
    entry = log_entry(*args, **kwargs)
    if on_event:
        on_event(entry, round_num)
    return entry


def _log_buyer_strategy_if_present(buyer, audit_path, on_event, round_num):
    """Logs the AI buyer's private reasoning (NEGOTIATION_SPEC.md Section
    4B) via the existing, unmodified log_entry() -- no-ops for the
    scripted BuyerAgent, which has no last_strategy attribute."""
    strategy = getattr(buyer, "last_strategy", None)
    if strategy is None:
        return
    rationale = (
        f"{strategy['strategy_note']} "
        f"(private: target_price={strategy['target_price']}, walk_away_price={strategy['walk_away_price']})"
    )
    _log(on_event, round_num, "buyer-agent", "buyer_strategy", None, rationale, [], path=audit_path)


def _log_buyer_unavailable(exc, audit_path, on_event, round_num):
    _log(on_event, round_num, "buyer-agent", "buyer_unavailable", None, str(exc), [], path=audit_path)


def run_negotiation(policy, buyer, audit_path=DEFAULT_AUDIT_PATH, merchant_evaluate=None, on_event=None):
    """Ties buyer-agent and merchant-agent together per
    NEGOTIATION_SPEC.md Section 3. Returns {"state": ..., "offer": ...}
    where state is one of AGREEMENT_RECORDED, REJECTED, or
    BUYER_UNAVAILABLE (Section 2A -- the AI buyer's LLM backend stayed
    unreachable/rate-limited through all retries). `buyer` may be the
    scripted BuyerAgent or the AIBuyerAgent -- both share the same
    initial_offer()/respond_to_counter() interface. `merchant_evaluate`
    defaults to merchant_agent.evaluate_rules (MERCHANT_MODE=rules); pass
    merchant_agent.evaluate_ai (or a partial binding its llm_call/model/
    etc.) for MERCHANT_MODE=ai (Section 2B). Either callable takes
    (offer, policy, round, negotiation_history). `on_event(entry, round_num)`,
    when given, is called after every audit entry is written -- e.g. for
    live console output; see __main__ below."""
    merchant_evaluate = merchant_evaluate or merchant_agent.evaluate_rules
    round_num = 1
    history = []
    try:
        current_offer = buyer.initial_offer()
    except BuyerUnavailableError as exc:
        _log_buyer_unavailable(exc, audit_path, on_event, round_num)
        return {"state": "BUYER_UNAVAILABLE", "offer": None}
    _log_buyer_strategy_if_present(buyer, audit_path, on_event, round_num)
    _log(on_event, round_num, "buyer-agent", "offer", current_offer, "", [], path=audit_path)
    history.append({"agent": "buyer-agent", "action": "offer", "offer": current_offer})

    while True:
        result = merchant_evaluate(current_offer, policy, round_num, history)
        _log(
            on_event, round_num, "merchant-agent", result["decision"], result["offer"],
            result["rationale"], result["evidence_paths"], path=audit_path,
            guardrail_clamped=result.get("guardrail_clamped"),
        )
        history.append({"agent": "merchant-agent", "action": result["decision"], "offer": result["offer"]})

        if result["decision"] == "accept":
            return {"state": "AGREEMENT_RECORDED", "offer": result["offer"]}
        if result["decision"] == "reject":
            return {"state": "REJECTED", "offer": None}

        try:
            buyer_response = buyer.respond_to_counter(result["offer"])
        except BuyerUnavailableError as exc:
            _log_buyer_unavailable(exc, audit_path, on_event, round_num)
            return {"state": "BUYER_UNAVAILABLE", "offer": None}
        _log_buyer_strategy_if_present(buyer, audit_path, on_event, round_num + 1)
        if buyer_response["accept"]:
            _log(on_event, round_num + 1, "buyer-agent", "accept", result["offer"], "", [], path=audit_path)
            return {"state": "AGREEMENT_RECORDED", "offer": result["offer"]}

        current_offer = buyer_response["offer"]
        round_num += 1
        _log(on_event, round_num, "buyer-agent", "offer", current_offer, "", [], path=audit_path)
        history.append({"agent": "buyer-agent", "action": "offer", "offer": current_offer})


def _cli_confirm(message):
    answer = input(f"{message} [y/n]: ").strip().lower()
    return answer == "y"


def _attempt_payment(
    policy, offer, qty, total, audit_path, payment_client, force_payment_failure, on_event,
    product, catalog_path, is_retry=False,
):
    """Shared by run_full_transaction() (the first, automatic attempt) and
    retry_payment() (an explicit, separate re-authorization -- Section
    3C). Exactly one payment_service.create_order() call per invocation,
    no internal loop or self-call -- "a failed payment is never
    automatically retried" is true by construction here, not by
    convention; see retry_payment() and NEGOTIATION_SPEC.md Section 3C."""
    label = " (retry)" if is_retry else ""
    amount_paise = round(total * 100)
    payment = payment_service.create_order(amount_paise, policy["currency"], client=payment_client)
    _log(
        on_event, None, "merchant-agent", "payment_initiated", offer,
        f"Razorpay test-mode order {payment['order_id']} created for {total:.2f} {policy['currency']}{label}.",
        [], path=audit_path, payment=payment,
    )

    result = payment_service.simulate_payment(payment, force_failure=force_payment_failure)

    if result["status"] == "completed":
        _log(
            on_event, None, "merchant-agent", "payment_completed", offer,
            f"Payment for order {result['order_id']} completed{label}.",
            [], path=audit_path, payment=result,
        )
        if product is not None and catalog_path is not None:
            remaining = personalization.decrement_inventory(catalog_path, product["sku_id"], qty)
            _log(
                on_event, None, "merchant-agent", "inventory_decremented", offer,
                f"Decremented current_inventory for {product['sku_id']} by {qty}; {remaining} remaining.",
                ["catalog.current_inventory"], path=audit_path,
            )
        return {"state": "COMPLETED", "offer": offer, "payment": result}

    _log(
        on_event, None, "merchant-agent", "payment_rollback", offer,
        f"Payment for order {result['order_id']} failed: {result['error_code']} - "
        f"{result['error_description']}. Rolling back{label}.",
        [], path=audit_path, payment=result,
    )
    _log(
        on_event, None, "merchant-agent", "inventory_release", offer,
        f"Releasing simulated inventory hold for qty {qty} of {policy['sku_id']} after payment rollback{label}.",
        [], path=audit_path,
    )
    _log(
        on_event, None, "buyer-agent", "buyer_notification", offer,
        f"Your payment for {qty}x {policy['product_name']} could not be completed "
        f"({result['error_code']}). You were not charged; a retry may be offered.",
        [], path=audit_path, payment=result,
    )
    _log(
        on_event, None, "merchant-agent", "human_notification", offer,
        f"ALERT: payment for order {result['order_id']} failed ({result['error_code']}). Manual review required.",
        [], path=audit_path, payment=result,
    )
    return {"state": "ROLLBACK", "offer": offer, "payment": result, "reason": "payment_failure"}


def retry_payment(
    offer, policy, audit_path=DEFAULT_AUDIT_PATH, payment_client=None,
    force_payment_failure=False, on_event=None, product=None, catalog_path=None,
):
    """The ONLY way a previously-failed payment is ever retried --
    NEVER called automatically by run_full_transaction() or
    _attempt_payment(). A caller (a human operator, or a future explicit
    CLI flag) must invoke this separately and deliberately per negotiation
    attempt. Per NEGOTIATION_SPEC.md Section 3C: reuses the ORIGINAL
    offer's own `expiration` timestamp as the retry window -- no second,
    unrelated timer. Raises ValueError if the offer has already expired;
    retrying against an expired offer is refused, a fresh negotiation is
    required instead."""
    expiration = datetime.strptime(offer["expiration"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expiration:
        raise ValueError(
            f"Offer {offer['offer_id']} expired at {offer['expiration']}; cannot retry payment against "
            "an expired offer -- a fresh negotiation is required."
        )

    qty = offer["qty"]
    total = offer["price"] * qty

    _log(
        on_event, None, "merchant-agent", "payment_retry_approved", offer,
        f"Explicit re-authorization: retrying payment for offer {offer['offer_id']} "
        f"(still valid until {offer['expiration']}).",
        [], path=audit_path,
    )
    _log(
        on_event, None, "merchant-agent", "inventory_hold", offer,
        f"Re-placing simulated inventory hold for qty {qty} of {policy['sku_id']} for payment retry.",
        [], path=audit_path,
    )

    return _attempt_payment(
        policy, offer, qty, total, audit_path, payment_client, force_payment_failure, on_event,
        product, catalog_path, is_retry=True,
    )


def run_full_transaction(
    policy, buyer, audit_path=DEFAULT_AUDIT_PATH,
    force_payment_failure=False, force_insufficient_inventory=False,
    approval_confirm=None, payment_client=None, merchant_evaluate=None, on_event=None,
    product=None, catalog_path=None,
):
    """Extends run_negotiation() with the payment phase per
    NEGOTIATION_SPEC.md Section 3A: AGREEMENT_RECORDED -> [inventory
    fulfillment check] -> [approval gate] -> PAYMENT_INITIATED ->
    COMPLETED / ROLLBACK. Returns {"state": ..., "offer": ...,
    "payment": ...} (ROLLBACK outcomes also carry a "reason": either
    "insufficient_inventory" or "payment_failure" -- Milestone 3c).
    `on_event(entry, round_num)` -- see run_negotiation() -- fires with
    round_num=None for every payment-phase entry, since those aren't
    negotiation rounds. `product`/`catalog_path` (Milestone 3c, both
    optional) wire in the synthetic catalog's inventory-aware fulfillment
    check and post-COMPLETED decrement; when product is None (the
    Milestone 1/2/3a/3b default), this behaves exactly as before -- no
    inventory check, no catalog write-back. `force_insufficient_inventory`
    (Milestone 4, default False) forces the same rollback the real
    inventory check would produce, without touching real catalog data --
    demo staging only; a no-op whenever product is None, same as the real
    check it stands in for."""
    negotiation_outcome = run_negotiation(
        policy, buyer, audit_path=audit_path, merchant_evaluate=merchant_evaluate, on_event=on_event,
    )
    if negotiation_outcome["state"] != "AGREEMENT_RECORDED":
        return {"state": negotiation_outcome["state"], "offer": None, "payment": None}

    offer = negotiation_outcome["offer"]
    qty = offer["qty"]
    total = offer["price"] * qty
    threshold = policy["transaction_approval_threshold"]

    real_inventory_shortfall = product is not None and not personalization.check_inventory_sufficient(product, qty)
    if product is not None and (force_insufficient_inventory or real_inventory_shortfall):
        # Simulated stock for the STAGED case only (2026-09-02 follow-up):
        # real current_inventory is almost always well above qty (that's
        # the whole point of forcing this scenario instead of depleting
        # real catalog data), so displaying it alongside "Requested: qty"
        # reads as nonsensical ("Requested: 3, In stock: 138"). Always
        # exactly one below qty -- a believable near-miss for any qty,
        # including qty=1 (simulated_stock=0) -- and used ONLY for
        # display; real current_inventory is never touched or misreported
        # as fact anywhere in the audit log (see reason_desc below, which
        # still states the real value truthfully).
        simulated_stock = max(0, qty - 1)
        if real_inventory_shortfall:
            reason_desc = f"Negotiated qty {qty} exceeds current_inventory {product['current_inventory']} for {product['sku_id']}"
        else:
            reason_desc = (
                f"Insufficient inventory staged via FORCE_INSUFFICIENT_INVENTORY for {product['sku_id']} "
                f"(simulated stock {simulated_stock}; real current_inventory is actually "
                f"{product['current_inventory']}, qty {qty})"
            )
        _log(
            on_event, None, "merchant-agent", "insufficient_inventory", offer,
            f"{reason_desc}; rolling back before any payment call.",
            ["catalog.current_inventory"], path=audit_path,
        )
        _log(
            on_event, None, "merchant-agent", "human_notification", offer,
            f"ALERT: agreed deal for {qty}x {product['sku_id']} cannot be fulfilled "
            f"({'only ' + str(product['current_inventory']) + ' in stock' if real_inventory_shortfall else f'only {simulated_stock} in stock (simulated for demo)'}). "
            "Manual review required.",
            [], path=audit_path,
        )
        result = {"state": "ROLLBACK", "offer": offer, "payment": None, "reason": "insufficient_inventory"}
        if not real_inventory_shortfall:
            result["simulated_stock"] = simulated_stock
        return result

    _log(
        on_event, None, "merchant-agent", "inventory_hold", offer,
        f"Placing simulated inventory hold for qty {qty} of {policy['sku_id']} pending payment.",
        [], path=audit_path,
    )

    if total > threshold:
        _log(
            on_event, None, "merchant-agent", "approval_requested", offer,
            f"Transaction total {total:.2f} exceeds policy.transaction_approval_threshold "
            f"({threshold}); pausing for human approval.",
            ["policy.transaction_approval_threshold"], path=audit_path,
        )
        confirm = approval_confirm or _cli_confirm
        approved = confirm(
            f"Approve payment of {total:.2f} {policy['currency']} for {qty}x {policy['product_name']}?"
        )
        if not approved:
            _log(
                on_event, None, "merchant-agent", "approval_declined", offer,
                "Human declined the approval gate; payment_service was not called.",
                ["policy.transaction_approval_threshold"], path=audit_path,
            )
            return {"state": "APPROVAL_DECLINED", "offer": offer, "payment": None}
        _log(
            on_event, None, "merchant-agent", "approval_granted", offer,
            "Human approved the transaction via the CLI gate.",
            ["policy.transaction_approval_threshold"], path=audit_path,
        )

    return _attempt_payment(
        policy, offer, qty, total, audit_path, payment_client, force_payment_failure, on_event,
        product, catalog_path,
    )


def _print_event(entry, round_num):
    """Console on_event callback: one line per audit entry as it happens,
    plus its rationale, so the CLI shows the negotiation live instead of
    only the final state."""
    round_label = f"Round {round_num}" if round_num is not None else "Payment"
    offer = entry.get("offer")
    offer_bit = f" -- price={offer['price']} qty={offer['qty']}" if offer else ""
    clamp_bit = " [GUARDRAIL CLAMPED]" if entry.get("guardrail_clamped") else ""
    print(f"[{round_label}] {entry['agent']}: {entry['action']}{offer_bit}{clamp_bit}")
    if entry.get("rationale"):
        print(f"    {entry['rationale']}")


def _print_box(title, lines):
    """Shared ASCII-box helper (Milestone 4) -- same visual language as
    the pre-existing PAYMENT FAILED block (Section 3C) so every terminal
    outcome reads consistently on a screen recording. ASCII-only, same
    reason as Section 3C: Windows consoles mangle non-ASCII without a
    UTF-8 codepage set."""
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)
    for line in lines:
        print(f"  {line}" if line else "")
    print("=" * 60)


def _print_agreement_summary(offer, policy):
    """Printed once negotiation reaches AGREEMENT_RECORDED, before the
    payment phase (if any) runs -- so a screen recording shows the agreed
    terms clearly, separate from whatever payment outcome follows."""
    total = offer["price"] * offer["qty"]
    _print_box("NEGOTIATION AGREED", [
        f"Product: {policy['product_name']} x{offer['qty']}",
        f"Price:   {offer['price']:.2f} {policy['currency']}/unit ({total:.2f} {policy['currency']} total)",
    ])


def _print_payment_completed_summary(outcome):
    """Printed on a successful COMPLETED outcome -- the happy-path
    counterpart to _print_payment_failure_summary() below."""
    payment = outcome["payment"]
    offer = outcome["offer"]
    amount = offer["price"] * offer["qty"]
    _print_box("PAYMENT COMPLETED", [
        f"Order:   {payment['order_id']}",
        f"Amount:  {amount:.2f} {payment['currency']}",
    ])


def _print_insufficient_inventory_summary(outcome, product):
    """Printed on an insufficient_inventory ROLLBACK (Section 3B) --
    distinct from the payment-failure ROLLBACK below, since no Razorpay
    call was ever made here. Every checklist line is true by construction:
    run_full_transaction()'s insufficient-inventory branch always logs
    insufficient_inventory then human_notification together, and never
    reaches payment_service at all.

    2026-09-02 follow-up: for a REAL shortfall, product['current_inventory']
    IS the meaningful "in stock" number and is already < qty by
    definition. For a FORCE_INSUFFICIENT_INVENTORY-staged one,
    outcome["simulated_stock"] (always exactly qty - 1, set by
    run_full_transaction()) is shown instead -- real current_inventory is
    almost always well above qty in that case, so displaying it here
    would read as nonsensical ("Requested: 3, In stock: 138")."""
    offer = outcome["offer"]
    displayed_stock = outcome.get("simulated_stock", product["current_inventory"])
    _print_box("INSUFFICIENT INVENTORY - ROLLBACK", [
        f"Product:   {product['sku_id']} ({product['product_name']})",
        f"Requested: {offer['qty']}",
        f"In stock:  {displayed_stock}",
        "",
        "Checklist:",
        "  [x] No payment attempt was made",
        "  [x] Human/operator was notified (human_notification logged)",
    ])


def _print_terminal_summary(outcome, policy, product):
    """Milestone 4: one call at the very end of __main__ that makes every
    possible outcome readable without parsing the raw JSON dump that
    follows it -- extends the existing per-round [Round N] trail and the
    Liquidation/floor lines already printed earlier in the run."""
    state = outcome.get("state")

    if state in ("REJECTED", "BUYER_UNAVAILABLE"):
        print()
        print(f"Outcome: {state} -- no agreement reached; no payment phase.")
        return

    # Every other state reached AGREEMENT_RECORDED at least -- show it.
    _print_agreement_summary(outcome["offer"], policy)

    if state == "AGREEMENT_RECORDED":
        return  # RAZORPAY_KEY_ID/SECRET not set -- no payment phase ran at all
    if state == "APPROVAL_DECLINED":
        print()
        print("Outcome: APPROVAL_DECLINED -- human declined the approval gate; no payment call was made.")
    elif state == "COMPLETED":
        _print_payment_completed_summary(outcome)
    elif state == "ROLLBACK":
        if outcome.get("reason") == "insufficient_inventory":
            _print_insufficient_inventory_summary(outcome, product)
        else:
            _print_payment_failure_summary(outcome)


def _print_payment_failure_summary(outcome):
    """Formatted console block for a payment_failure ROLLBACK, distinct
    from the one-line-per-entry _print_event() trail above. Every
    checklist line is true by construction whenever this is called --
    _attempt_payment()'s rollback branch always logs exactly these
    entries together in the same code path that produced this outcome."""
    payment = outcome["payment"]
    offer = outcome["offer"]
    amount = offer["price"] * offer["qty"]
    print()
    print("=" * 60)
    print("  PAYMENT FAILED - ROLLBACK")
    print("=" * 60)
    print(f"  Order:   {payment['order_id']}")
    print(f"  Reason:  {payment['error_code']} - {payment['error_description']}")
    print(f"  Amount:  {amount:.2f} {payment['currency']}")
    print()
    print("  Checklist:")
    print("    [x] Order remains unpaid (no capture occurred)")
    print("    [x] No duplicate payment attempt was made")
    print("    [x] Buyer-agent was notified (buyer_notification logged)")
    print("    [x] Human/operator was notified (human_notification logged)")
    print("    [x] Simulated inventory hold released")
    print("=" * 60)


def _read_buyer_budget():
    """BUYER_BUDGET env var, parsed to float -- None if unset, so callers
    fall back to their own hardcoded default. Raises a clear error on
    invalid (non-numeric) input rather than silently ignoring it."""
    raw = os.environ.get("BUYER_BUDGET")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"BUYER_BUDGET={raw!r} is not a valid number.")


DEFAULT_MAX_NEGOTIATION_ROUNDS = 5
DEFAULT_TRANSACTION_APPROVAL_THRESHOLD = 20000
DEFAULT_DEMO_QTY = 3


if __name__ == "__main__":
    product_id = os.environ.get("PRODUCT_ID")
    buyer_id = os.environ.get("BUYER_ID")

    # Product/policy: PRODUCT_ID looks it up in the synthetic catalog;
    # unset falls back to the original hardcoded merchant_policy.json,
    # unchanged from Milestone 1/2/3a/3b.
    if product_id:
        catalog = personalization.load_json(personalization.DEFAULT_CATALOG_PATH)
        product = personalization.find_product(catalog, product_id)
        if product is None:
            raise SystemExit(f"PRODUCT_ID={product_id!r} not found in {personalization.DEFAULT_CATALOG_PATH}")
        catalog_path = personalization.DEFAULT_CATALOG_PATH
        base_policy = personalization.product_to_policy(
            product, DEFAULT_MAX_NEGOTIATION_ROUNDS, DEFAULT_TRANSACTION_APPROVAL_THRESHOLD,
        )
        print(f"Product (PRODUCT_ID={product_id}): {product['product_name']} ({product['category']}), "
              f"current_inventory={product['current_inventory']}")
        liquidation_note = personalization.liquidation_rationale(product)
        if liquidation_note:
            _log(None, None, "merchant-agent", "liquidation_applied", None, liquidation_note,
                 ["catalog.days_in_inventory"], path=DEFAULT_AUDIT_PATH)
            print(f"Liquidation: {liquidation_note}")
    else:
        product = None
        catalog_path = None
        with open("merchant_policy.json", encoding="utf-8") as f:
            base_policy = json.load(f)

    # Buyer profile + LTV discount bonus: BUYER_ID looks it up and sums
    # data/orders.json for a deterministic tier lookup (personalization.py
    # -- pure functions, no LLM). Unset -> bonus 0%, base_policy unchanged.
    buyer_profile = None
    ltv_bonus_pct = 0
    if buyer_id:
        buyers = personalization.load_json(personalization.DEFAULT_BUYERS_PATH)
        buyer_profile = personalization.find_buyer(buyers, buyer_id)
        if buyer_profile is None:
            raise SystemExit(f"BUYER_ID={buyer_id!r} not found in {personalization.DEFAULT_BUYERS_PATH}")
        orders = personalization.load_json(personalization.DEFAULT_ORDERS_PATH)
        ltv = personalization.compute_ltv(buyer_id, orders)
        ltv_bonus_pct = personalization.ltv_discount_bonus(ltv)
        print(f"Buyer (BUYER_ID={buyer_id}): persona={buyer_profile['persona']!r}, LTV={ltv} "
              f"{base_policy['currency']} -> discount bonus +{ltv_bonus_pct}% "
              f"(hard ceiling {personalization.HARD_DISCOUNT_CEILING_PCT}%)")

    policy = personalization.apply_ltv_bonus(base_policy, ltv_bonus_pct)

    if product is not None:
        # Section 2J (2026-09-02): liquidation now relaxes the
        # discount-cap floor itself (toward min_price), not min_price --
        # merchant_agent._floor_price() already folds that in via
        # policy.liquidation_relaxation_fraction. Printed here, after the
        # LTV bonus is applied and at the actual demo qty, so the console
        # shows the real, already-liquidation-adjusted number the
        # negotiation will enforce, not a component of it.
        effective_floor, floor_evidence = merchant_agent._floor_price(policy, DEFAULT_DEMO_QTY)
        print(
            f"Effective negotiation floor at qty={DEFAULT_DEMO_QTY}: {effective_floor:.2f} "
            f"{policy['currency']} (driven by {floor_evidence})"
        )

    buyer_budget = _read_buyer_budget()
    buyer_mode = os.environ.get("BUYER_MODE", "scripted")

    persona_from_profile = (
        personalization.buyer_to_persona(buyer_profile, policy["product_name"]) if buyer_profile else None
    )
    if persona_from_profile is not None and buyer_budget is not None:
        persona_from_profile["budget"] = buyer_budget

    if buyer_mode == "ai":
        persona = persona_from_profile or {
            "budget": buyer_budget if buyer_budget is not None else policy["list_price"] * 0.95,
            "target_product": policy["product_name"],
            "willingness_to_negotiate": "moderate -- open to a fair discount but not desperate",
        }
        buyer = AIBuyerAgent(
            qty=DEFAULT_DEMO_QTY, persona=persona, list_price=policy["list_price"], currency=policy["currency"],
            max_negotiation_rounds=policy["max_negotiation_rounds"],
        )
        budget_source = (
            "from BUYER_BUDGET" if buyer_budget is not None
            else "from BUYER_ID profile" if persona_from_profile is not None
            else "hardcoded default (BUYER_BUDGET/BUYER_ID not set)"
        )
        print(f"Effective buyer persona ({budget_source}): {json.dumps(persona, indent=2)}")
    else:
        if persona_from_profile is not None:
            max_acceptable_price = persona_from_profile["budget"]
            budget_source = "from BUYER_BUDGET" if buyer_budget is not None else "from BUYER_ID profile"
        else:
            max_acceptable_price = buyer_budget if buyer_budget is not None else 4450.0
            budget_source = "from BUYER_BUDGET" if buyer_budget is not None else "hardcoded default (BUYER_BUDGET/BUYER_ID not set)"
        buyer = BuyerAgent(
            qty=DEFAULT_DEMO_QTY, opening_discount_pct=15, max_acceptable_price=max_acceptable_price,
            list_price=policy["list_price"],
        )
        print(f"Buyer budget (max acceptable price, {budget_source}): {max_acceptable_price} {policy['currency']}")

    merchant_mode = os.environ.get("MERCHANT_MODE", "rules")
    merchant_evaluate = merchant_agent.evaluate_ai if merchant_mode == "ai" else None

    force_payment_failure = os.environ.get("FORCE_PAYMENT_FAILURE", "").lower() in ("1", "true", "yes")
    force_insufficient_inventory = os.environ.get("FORCE_INSUFFICIENT_INVENTORY", "").lower() in ("1", "true", "yes")
    if force_insufficient_inventory and product is None:
        print(
            "WARNING: FORCE_INSUFFICIENT_INVENTORY is set but PRODUCT_ID is not -- the inventory "
            "check only runs on the PRODUCT_ID path, so this flag will have no effect."
        )

    if os.environ.get("RAZORPAY_KEY_ID") and os.environ.get("RAZORPAY_KEY_SECRET"):
        outcome = run_full_transaction(
            policy, buyer, merchant_evaluate=merchant_evaluate, on_event=_print_event,
            product=product, catalog_path=catalog_path, force_payment_failure=force_payment_failure,
            force_insufficient_inventory=force_insufficient_inventory,
        )
    else:
        print("RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET not set -- running negotiation only, no payment phase.")
        outcome = run_negotiation(policy, buyer, merchant_evaluate=merchant_evaluate, on_event=_print_event)

    _print_terminal_summary(outcome, policy, product)
    print()
    print(json.dumps(outcome, indent=2))
