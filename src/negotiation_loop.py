import json
import os
import random
import uuid
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


def _perk_outcome_note(granted_perks, declined_perks):
    """Milestone 9 (Section 4E). Same wording merchant_agent._accept()
    uses when the merchant itself accepts the buyer's offer -- reused
    here for the other two acceptance paths (buyer accepting a merchant
    counter; _accept_round_limit_offer() below), both of which call
    merchant_agent._resolve_perks() directly since check_guardrails()
    never runs again for a price it didn't itself propose."""
    note = ""
    if granted_perks:
        note += f" Perk(s) granted: {', '.join(granted_perks)}."
    if declined_perks:
        note += (
            f" Perk(s) declined: {', '.join(declined_perks)} -- the combined price concession and "
            "perk cost would breach the minimum margin floor."
        )
    return note


def _log_buyer_strategy_if_present(
    buyer, audit_path, on_event, round_num, negotiation_id=None, product_name=None, list_price=None,
    merchant_id=None,
):
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
    _log(
        on_event, round_num, "buyer-agent", "buyer_strategy", None, rationale, [],
        path=audit_path, negotiation_id=negotiation_id, product_name=product_name, list_price=list_price,
        merchant_id=merchant_id,
    )


def _log_buyer_unavailable(
    exc, audit_path, on_event, round_num, negotiation_id=None, product_name=None, list_price=None,
    merchant_id=None,
):
    _log(
        on_event, round_num, "buyer-agent", "buyer_unavailable", None, str(exc), [],
        path=audit_path, negotiation_id=negotiation_id, product_name=product_name, list_price=list_price,
        merchant_id=merchant_id,
    )


def run_negotiation(
    policy, buyer, audit_path=DEFAULT_AUDIT_PATH, merchant_evaluate=None, on_event=None,
    buyer_id=None, orders=None, risk_approval_tier="standard",
    persona=None, requested_perks=None,
):
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
    live console output; see __main__ below.

    `buyer_id`/`orders` (Milestone 5, both optional, default None): when
    both are given, personalization.risk_assessment() runs FIRST, before
    buyer.initial_offer() is ever called. Reframed 2026-09-02 (Section 2N,
    confirmed with the user): "high" risk is a PRICING-ABUSE signal, not a
    trust/fraud one -- it no longer blocks the negotiation. Instead
    apply_risk_discount_cap() tightens `policy` (a local reassignment,
    the original dict passed in is never mutated) so the rest of THIS
    negotiation runs with the discount ceiling scaled down, via the same
    check_guardrails()/_floor_price() guardrail every other policy field
    already flows through, no separate code path. A THREE-level gradient
    (2026-09-03 follow-up, confirmed with the user): "high" scales
    max_discount_pct/qty_breaks to RISK_HIGH_DISCOUNT_FACTOR (0.0 -- full
    list price, no room at all); "moderate" scales them to
    RISK_MODERATE_DISCOUNT_FACTOR (0.25 -- a quarter of normal room,
    not zero); "none" leaves policy completely untouched.
    "moderate"/"high" are both threaded onto the AGREEMENT_RECORDED
    outcome's "risk_level" key; run_full_transaction() reads it, but
    (Section 2O, 2026-09-02 follow-up, unchanged by the gradient above)
    only forces the human-approval gate for "high" by default --
    "moderate" still proceeds with zero added APPROVAL friction (though,
    as of the gradient above, it is no longer pricing-neutral), visible
    only via its risk_review audit entry. When buyer_id/orders are None
    (every pre-Milestone-5 caller), this whole block is skipped --
    behavior is byte-identical to before.

    `risk_approval_tier` (Milestone 6, default "standard" -- every
    pre-Milestone-6 caller behaves byte-identically): the SELLING
    merchant's own tolerance, resolved by the caller from
    data/merchants.json and passed straight through here and to
    run_full_transaction() (same "policy carries data, caller resolves
    identity" split as buyer_id/orders above). Does not change
    risk_assessment()'s none/moderate/high computation at all -- that
    stays purely about the buyer/qty factors, unaware of which merchant
    is involved. Only changes what a "moderate" verdict means for THIS
    merchant: "strict" appends a note to the risk_review rationale here
    (the actual gating decision is made later, in run_full_transaction(),
    which also receives risk_approval_tier directly for that).

    Round-limit handling (Section 2X, 2026-09-04 -- replaces the
    Milestone 8 `on_round_limit` parameter and Section 2U's auto-accept-
    then-forced-approval logic entirely, per the user's explicit request
    to redesign this rather than layer another fix on it): hitting
    policy.max_negotiation_rounds with no agreement no longer decides
    walk-away-vs-accept upfront, and no longer auto-accepts anything.
    Instead this function ALWAYS returns a new "ROUND_LIMIT_REACHED"
    state -- a genuine pause, carrying the merchant's own last real
    counter-offer (the last agent="merchant-agent" "counter" entry in
    `history` -- a price that WAS already validated against every
    guardrail when it was proposed, never a fabricated or re-derived
    number) plus the exact `policy` this negotiation was running under
    (already risk-tightened/perk-eligibility-merged, if applicable) and
    `requested_perks`, so whoever resolves the pause (see
    _accept_round_limit_offer()/_decline_round_limit_offer()/
    resolve_round_limit_decision() below) can finish the decision using
    the SAME state, never re-derived. Only fires for a round-cap
    rejection specifically -- identified by "policy.max_negotiation_rounds"
    appearing in evidence_paths, which check_guardrails() sometimes
    returns alongside a floor-evidence path (e.g.
    ["policy.max_discount_pct", "policy.max_negotiation_rounds"] when the
    final round's offer is still below the floor) and sometimes alone --
    membership, not exact-list equality, so both forms match. An instant
    reject for an unrelated reason (e.g. below min_price on the very
    first offer) never carries this evidence path and returns REJECTED
    exactly as before. If no merchant counter-offer exists at all (the
    round cap was hit on the buyer's very first, instantly-rejected
    offer -- no round ever produced a counter to fall back to), this also
    returns REJECTED -- there is nothing to offer the human a choice
    about.

    `persona`/`requested_perks` (Milestone 9, Section 4E -- multi-
    dimensional negotiation, both optional, default None): `persona` is
    the buyer's data/buyers.json persona label, resolved by the CALLER
    (same "policy/identity resolved by the caller" split as buyer_id/
    orders/risk_approval_tier above). `requested_perks` is a list of
    perk names ("free_delivery", "extended_warranty") the buyer is
    asking for, decided ONCE before the negotiation starts -- never
    re-requested or changed mid-negotiation. When buyer_id/orders/persona
    are all given and requested_perks is non-empty,
    personalization.perk_eligibility() runs once (reusing the SAME
    risk_level already computed above -- HIGH risk overrides perk
    eligibility, consistent with it also zeroing the discount ceiling),
    logged as a perk_review entry (same pattern as risk_review), and the
    result is merged into the local `policy` copy as
    policy["eligible_perks"] for merchant_agent.check_guardrails() to
    consult at acceptance time. An ineligible request is conclusively
    declined right here, with a rule-specific rationale -- no LLM
    discretion on that boundary, ever.

    `buyer_id`/`orders` guard (2026-09-05, added after a real near-miss:
    a verification script called this function with neither argument,
    which is valid -- see below -- but the risk-vs-floor claim it then
    made would have been silently wrong had that buyer/qty pair actually
    carried risk): passing exactly ONE of buyer_id/orders is ALWAYS a
    caller mistake -- the risk-assessment block below only ever runs
    when BOTH are given, so one alone accomplishes nothing except
    silently skipping the Risk Agent entirely while looking like a
    normal call. Raises ValueError immediately rather than let that
    happen quietly. Passing NEITHER remains completely valid and
    unchanged -- every pre-Milestone-5 caller, and every test that
    deliberately wants "no personalization," relies on exactly that.
    The resulting distinction already existed and is preserved: `risk_level`
    stays Python `None` ("never assessed") when both are omitted, vs. the
    string `"none"` ("assessed, found no risk") when both are given and
    risk_assessment() runs -- this guard just stops a caller from landing
    in the first state by accident while believing they're in the second."""
    if (buyer_id is None) != (orders is None):
        raise ValueError(
            "run_negotiation() requires buyer_id and orders TOGETHER, or NEITHER -- "
            f"got buyer_id={buyer_id!r}, orders={'<list of len ' + str(len(orders)) + '>' if orders is not None else None!r}. "
            "Passing exactly one silently skips the Risk Agent check entirely (risk_level "
            "stays None -- 'never assessed' -- not the string \"none\", which means 'assessed, "
            "found no risk'). That's easy to mistake for a genuine no-risk result. Pass both "
            "to run the real risk assessment for this buyer, or neither to explicitly skip it."
        )
    merchant_evaluate = merchant_evaluate or merchant_agent.evaluate_rules
    round_num = 1
    history = []
    risk_level = None
    # Milestone 7 (Section 4D): generated ONCE, first thing, before any
    # entry this negotiation produces is logged -- threaded through every
    # _log()/_log_buyer_strategy_if_present()/_log_buyer_unavailable()
    # call below, and returned on every terminal outcome so
    # run_full_transaction() can carry it into the payment-phase entries
    # too. A plain top-level field on every entry (audit_logger.log_entry()),
    # not nested -- a single groupby("negotiation_id") is enough to
    # reconstruct one negotiation's full entry set from a combined log.
    negotiation_id = uuid.uuid4().hex
    # Section 4D follow-up: captured once, same place/reasoning as
    # negotiation_id above -- both already sit on `policy` as passed in,
    # no new parameter needed. Omitted (None) for any policy dict lacking
    # these keys (pre-Milestone-7 test fixtures), so log_entry() drops
    # them from the entry exactly like negotiation_id.
    product_name = policy.get("product_name")
    list_price = policy.get("list_price")
    # Section 4D third follow-up: same capture-once-and-thread treatment,
    # for Milestone 6's multi-merchant `merchant_id` field -- lets a
    # dashboard/report distinguish negotiations by merchant instead of
    # aggregating everyone together.
    merchant_id = policy.get("merchant_id")

    if buyer_id is not None and orders is not None:
        risk = personalization.risk_assessment(buyer_id, buyer.qty, policy.get("qty_breaks", []), orders)
        risk_level = risk["level"]
        # 2026-09-03 follow-up: MODERATE now also tightens the discount
        # ceiling (RISK_MODERATE_DISCOUNT_FACTOR, not just HIGH's full
        # zeroing) -- both go through this SAME call, which scales
        # max_discount_pct AND every qty_breaks tier together (see
        # apply_risk_discount_cap()'s own docstring for why both fields
        # must move together, not just one).
        if risk_level != "none":
            policy = personalization.apply_risk_discount_cap(policy, risk)
        if risk_level != "none":
            rationale = risk["rationale"]
            if risk_level == "moderate":
                if risk_approval_tier == "strict":
                    rationale = (
                        f"{rationale} This merchant (risk_approval_tier=strict) requires human approval "
                        "on MODERATE risk too."
                    )
                else:
                    rationale = f"{rationale} No approval required at this risk level for this merchant."
            if risk_level in ("moderate", "high"):
                # 2026-09-02 follow-up (extended 2026-09-03 to MODERATE):
                # state the ACTUAL, freshly-recomputed effective floor in
                # the SAME line as the discount-ceiling claim, via the
                # exact _floor_price() call the negotiation itself is
                # about to use (against the already-tightened `policy`
                # above) -- not a separate, independently-asserted number
                # that could silently drift from what check_guardrails()
                # actually enforces if a future change broke the
                # qty_breaks-scaling in apply_risk_discount_cap(). If
                # those two ever disagree, this line itself becomes
                # visibly wrong, not silently so.
                effective_floor, _ = merchant_agent._floor_price(policy, buyer.qty)
                rationale = f"{rationale} Effective floor for this negotiation: {effective_floor:.2f} {policy['currency']}."
            # round_num=0, not None -- _print_event() reads round_num=None
            # as "Payment phase" (Milestone 2/3c convention); this check
            # runs BEFORE round 1, not during payment, so 0 reads
            # correctly as "before round 1" on the console.
            _log(
                on_event, 0, "risk-agent", "risk_review", None, rationale, risk["evidence_paths"],
                path=audit_path, negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
            )

    if buyer_id is not None and orders is not None and persona is not None and requested_perks:
        perk_result = personalization.perk_eligibility(buyer_id, orders, persona, risk_level)
        # Local reassignment only -- never mutates the caller's original
        # policy dict, same discipline as apply_risk_discount_cap() above.
        policy = {**policy, "eligible_perks": perk_result["eligible"]}
        ineligible = [p for p in requested_perks if p not in perk_result["eligible"]]
        perk_rationale = perk_result["rationale"]
        if ineligible:
            perk_rationale = (
                f"{perk_rationale} Requested but declined as ineligible: {', '.join(ineligible)}."
            )
        else:
            perk_rationale = f"{perk_rationale} Requested and eligible: {', '.join(perk_result['eligible'])}."
        _log(
            on_event, 0, "risk-agent", "perk_review", None, perk_rationale, [f"perk_eligibility.{perk_result['rule']}"],
            path=audit_path, negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
        )

    try:
        current_offer = buyer.initial_offer()
    except BuyerUnavailableError as exc:
        _log_buyer_unavailable(
            exc, audit_path, on_event, round_num,
            negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
        )
        return {
            "state": "BUYER_UNAVAILABLE", "offer": None, "negotiation_id": negotiation_id,
            "product_name": product_name, "list_price": list_price, "merchant_id": merchant_id,
        }
    _log_buyer_strategy_if_present(
        buyer, audit_path, on_event, round_num,
        negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
    )
    _log(
        on_event, round_num, "buyer-agent", "offer", current_offer, "", [],
        path=audit_path, negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
    )
    history.append({"agent": "buyer-agent", "action": "offer", "offer": current_offer})

    while True:
        result = merchant_evaluate(current_offer, policy, round_num, history, requested_perks=requested_perks)
        _log(
            on_event, round_num, "merchant-agent", result["decision"], result["offer"],
            result["rationale"], result["evidence_paths"], path=audit_path,
            guardrail_clamped=result.get("guardrail_clamped"), negotiation_id=negotiation_id,
            product_name=product_name, list_price=list_price, merchant_id=merchant_id,
        )
        history.append({"agent": "merchant-agent", "action": result["decision"], "offer": result["offer"]})

        if result["decision"] == "accept":
            return {
                "state": "AGREEMENT_RECORDED", "offer": result["offer"], "risk_level": risk_level,
                "negotiation_id": negotiation_id, "product_name": product_name, "list_price": list_price, "merchant_id": merchant_id,
                "granted_perks": result.get("granted_perks", []), "declined_perks": result.get("declined_perks", []),
            }
        if result["decision"] == "reject":
            if "policy.max_negotiation_rounds" in result["evidence_paths"]:
                last_merchant_counter = next(
                    (h["offer"] for h in reversed(history) if h["agent"] == "merchant-agent" and h["action"] == "counter"),
                    None,
                )
                if last_merchant_counter is not None:
                    _log(
                        on_event, round_num, "merchant-agent", "round_limit_reached", last_merchant_counter,
                        f"Round limit ({policy['max_negotiation_rounds']}) reached without agreement; "
                        "pausing for the buyer/human to decide whether to accept the merchant's final offer.",
                        ["policy.max_negotiation_rounds"], path=audit_path, negotiation_id=negotiation_id,
                        product_name=product_name, list_price=list_price, merchant_id=merchant_id,
                    )
                    return {
                        "state": "ROUND_LIMIT_REACHED", "offer": last_merchant_counter, "risk_level": risk_level,
                        "negotiation_id": negotiation_id, "product_name": product_name,
                        "list_price": list_price, "merchant_id": merchant_id,
                        "policy": policy, "requested_perks": requested_perks,
                    }
            return {
                "state": "REJECTED", "offer": None, "negotiation_id": negotiation_id,
                "product_name": product_name, "list_price": list_price, "merchant_id": merchant_id,
            }

        try:
            buyer_response = buyer.respond_to_counter(result["offer"])
        except BuyerUnavailableError as exc:
            _log_buyer_unavailable(
                exc, audit_path, on_event, round_num,
                negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
            )
            return {
                "state": "BUYER_UNAVAILABLE", "offer": None, "negotiation_id": negotiation_id,
                "product_name": product_name, "list_price": list_price, "merchant_id": merchant_id,
            }
        _log_buyer_strategy_if_present(
            buyer, audit_path, on_event, round_num + 1,
            negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
        )
        if buyer_response["accept"]:
            granted_perks, declined_perks = merchant_agent._resolve_perks(result["offer"], policy, requested_perks or [])
            _log(
                on_event, round_num + 1, "buyer-agent", "accept", result["offer"],
                _perk_outcome_note(granted_perks, declined_perks).strip(), [],
                path=audit_path, negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
            )
            return {
                "state": "AGREEMENT_RECORDED", "offer": result["offer"], "risk_level": risk_level,
                "negotiation_id": negotiation_id, "product_name": product_name, "list_price": list_price, "merchant_id": merchant_id,
                "granted_perks": granted_perks, "declined_perks": declined_perks,
            }

        current_offer = buyer_response["offer"]
        round_num += 1
        _log(
            on_event, round_num, "buyer-agent", "offer", current_offer, "", [],
            path=audit_path, negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
        )
        history.append({"agent": "buyer-agent", "action": "offer", "offer": current_offer})


def _accept_round_limit_offer(round_limit_outcome, audit_path, on_event):
    """Section 2X. Called once the human/buyer decides to accept the
    merchant's final offer after a ROUND_LIMIT_REACHED pause -- whether
    that decision was made synchronously (run_full_transaction()'s own
    confirm() call) or much later, via resolve_round_limit_decision()
    after a real pause/resume round-trip. `round_limit_outcome` is the
    exact dict run_negotiation() returned for "ROUND_LIMIT_REACHED" (or
    whatever subset of it the caller persisted across the pause) --
    critically, its "policy" field is the SAME (possibly risk-tightened/
    perk-eligibility-merged) policy the negotiation actually paused
    under, never re-derived here.

    Resolves perks against that policy (same mechanism
    check_guardrails()'s own accept branch and the buyer-accepts-counter
    path both use -- _resolve_perks() is the one place this decision is
    ever made), logs the acceptance, and returns an AGREEMENT_RECORDED-
    shaped dict ready to flow into _process_agreement() -- the exact same
    function a normal negotiated agreement flows into, so a round-limit
    acceptance is never exempt from any guardrail (inventory check,
    approval gate) a genuine agreement would also have to clear."""
    offer = round_limit_outcome["offer"]
    policy = round_limit_outcome["policy"]
    negotiation_id = round_limit_outcome.get("negotiation_id")
    product_name = round_limit_outcome.get("product_name")
    list_price = round_limit_outcome.get("list_price")
    merchant_id = round_limit_outcome.get("merchant_id")
    granted_perks, declined_perks = merchant_agent._resolve_perks(
        offer, policy, round_limit_outcome.get("requested_perks") or [],
    )
    _log(
        on_event, None, "buyer-agent", "accept", offer,
        "Round limit reached; buyer accepted the merchant's last counter-offer." +
        _perk_outcome_note(granted_perks, declined_perks),
        ["policy.max_negotiation_rounds"], path=audit_path, negotiation_id=negotiation_id,
        product_name=product_name, list_price=list_price, merchant_id=merchant_id,
    )
    return {
        "state": "AGREEMENT_RECORDED", "offer": offer, "risk_level": round_limit_outcome.get("risk_level"),
        "negotiation_id": negotiation_id, "product_name": product_name, "list_price": list_price,
        "merchant_id": merchant_id, "granted_perks": granted_perks, "declined_perks": declined_perks,
    }


def _decline_round_limit_offer(round_limit_outcome, audit_path, on_event):
    """Section 2X. The other resolution of a ROUND_LIMIT_REACHED pause --
    the human/buyer declines the merchant's final offer. Produces the
    same REJECTED shape a normal round-cap walk-away always has."""
    negotiation_id = round_limit_outcome.get("negotiation_id")
    product_name = round_limit_outcome.get("product_name")
    list_price = round_limit_outcome.get("list_price")
    merchant_id = round_limit_outcome.get("merchant_id")
    _log(
        on_event, None, "buyer-agent", "reject", None,
        "Round limit reached; buyer declined the merchant's last counter-offer.",
        ["policy.max_negotiation_rounds"], path=audit_path, negotiation_id=negotiation_id,
        product_name=product_name, list_price=list_price, merchant_id=merchant_id,
    )
    return {
        "state": "REJECTED", "offer": None, "negotiation_id": negotiation_id,
        "product_name": product_name, "list_price": list_price, "merchant_id": merchant_id,
    }


def resolve_round_limit_decision(
    round_limit_outcome, accept, audit_path=DEFAULT_AUDIT_PATH, on_event=None, approval_confirm=None,
    payment_client=None, force_payment_failure=False, force_insufficient_inventory=False,
    product=None, catalog_path=None, risk_approval_tier="standard",
):
    """Section 2X. Resumes a paused ROUND_LIMIT_REACHED negotiation once
    the human/buyer decides. This is the ONE function both the CLI's
    synchronous confirm() path (inside run_full_transaction()) and the
    API's asynchronous POST /api/round-limit-decision resume path call --
    not two implementations of the same decision.

    Declining returns REJECTED immediately (_decline_round_limit_offer()).

    Accepting resolves perks and logs the acceptance
    (_accept_round_limit_offer()), then proceeds through _process_agreement()
    -- the EXACT SAME post-agreement logic (inventory check, approval
    gate, payment) a normal AGREEMENT_RECORDED goes through. No shortcut:
    if the resulting price/risk tier would normally trigger the human-
    approval gate, it still does here -- `approval_confirm` (including
    PAUSE_FOR_APPROVAL, for a second pause chained right after this one)
    is passed straight through, exactly as run_full_transaction() itself
    would use it."""
    if not accept:
        return _decline_round_limit_offer(round_limit_outcome, audit_path, on_event)
    negotiation_outcome = _accept_round_limit_offer(round_limit_outcome, audit_path, on_event)
    return _process_agreement(
        negotiation_outcome, round_limit_outcome["policy"], audit_path, force_payment_failure,
        force_insufficient_inventory, approval_confirm, payment_client, on_event, product, catalog_path,
        risk_approval_tier,
    )


def _cli_confirm(message):
    answer = input(f"{message} [y/n]: ").strip().lower()
    return answer == "y"


# Milestone 8 (frontend/API): a sentinel, not a callable. A caller that
# wants to PAUSE at the approval gate instead of resolving it synchronously
# (e.g. src/api.py's POST /api/negotiate, which cannot block on input() or
# an HTTP round-trip mid-request) passes this object as `approval_confirm`.
# run_full_transaction() checks for it by identity (`is`) at the exact
# point it would otherwise call confirm(message) -- every existing caller
# passes either None or a real bool-returning callable, never this object,
# so this is a zero-behavior-change addition for all of them. The resume
# path (approved or declined, arriving later via POST /api/approve) is
# handled by the caller directly logging the decision and, if approved,
# calling _attempt_payment() -- the same shared function
# run_full_transaction() itself calls, not a reimplementation.
PAUSE_FOR_APPROVAL = object()


def _attempt_payment(
    policy, offer, qty, total, audit_path, payment_client, force_payment_failure, on_event,
    product, catalog_path, is_retry=False, negotiation_id=None, product_name=None, list_price=None,
    merchant_id=None, granted_perks=None, declined_perks=None,
):
    """Shared by run_full_transaction() (the first, automatic attempt) and
    retry_payment() (an explicit, separate re-authorization -- Section
    3C). Exactly one payment_service.create_order() call per invocation,
    no internal loop or self-call -- "a failed payment is never
    automatically retried" is true by construction here, not by
    convention; see retry_payment() and NEGOTIATION_SPEC.md Section 3C.

    `negotiation_id` (Milestone 7, Section 4D): threaded through every
    entry logged here, so a payment attempt's entries -- retry or not --
    stay grouped with the negotiation that produced the offer being paid
    for. `product_name`/`list_price` (same section, follow-up): same
    treatment."""
    label = " (retry)" if is_retry else ""
    amount_paise = round(total * 100)
    payment = payment_service.create_order(amount_paise, policy["currency"], client=payment_client)
    _log(
        on_event, None, "merchant-agent", "payment_initiated", offer,
        f"Razorpay test-mode order {payment['order_id']} created for {total:.2f} {policy['currency']}{label}.",
        [], path=audit_path, payment=payment, negotiation_id=negotiation_id,
        product_name=product_name, list_price=list_price, merchant_id=merchant_id,
    )

    result = payment_service.simulate_payment(payment, force_failure=force_payment_failure)

    if result["status"] == "completed":
        _log(
            on_event, None, "merchant-agent", "payment_completed", offer,
            f"Payment for order {result['order_id']} completed{label}.",
            [], path=audit_path, payment=result, negotiation_id=negotiation_id,
            product_name=product_name, list_price=list_price, merchant_id=merchant_id,
        )
        if product is not None and catalog_path is not None:
            remaining = personalization.decrement_inventory(catalog_path, product["sku_id"], qty)
            _log(
                on_event, None, "merchant-agent", "inventory_decremented", offer,
                f"Decremented current_inventory for {product['sku_id']} by {qty}; {remaining} remaining.",
                ["catalog.current_inventory"], path=audit_path, negotiation_id=negotiation_id,
                product_name=product_name, list_price=list_price, merchant_id=merchant_id,
            )
        return {
            "state": "COMPLETED", "offer": offer, "payment": result, "negotiation_id": negotiation_id,
            "product_name": product_name, "list_price": list_price, "merchant_id": merchant_id,
            "granted_perks": granted_perks or [], "declined_perks": declined_perks or [],
        }

    _log(
        on_event, None, "merchant-agent", "payment_rollback", offer,
        f"Payment for order {result['order_id']} failed: {result['error_code']} - "
        f"{result['error_description']}. Rolling back{label}.",
        [], path=audit_path, payment=result, negotiation_id=negotiation_id,
        product_name=product_name, list_price=list_price, merchant_id=merchant_id,
    )
    _log(
        on_event, None, "merchant-agent", "inventory_release", offer,
        f"Releasing simulated inventory hold for qty {qty} of {policy['sku_id']} after payment rollback{label}.",
        [], path=audit_path, negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
    )
    _log(
        on_event, None, "buyer-agent", "buyer_notification", offer,
        f"Your payment for {qty}x {policy['product_name']} could not be completed "
        f"({result['error_code']}). You were not charged; a retry may be offered.",
        [], path=audit_path, payment=result, negotiation_id=negotiation_id,
        product_name=product_name, list_price=list_price, merchant_id=merchant_id,
    )
    _log(
        on_event, None, "merchant-agent", "human_notification", offer,
        f"ALERT: payment for order {result['order_id']} failed ({result['error_code']}). Manual review required.",
        [], path=audit_path, payment=result, negotiation_id=negotiation_id,
        product_name=product_name, list_price=list_price, merchant_id=merchant_id,
    )
    return {
        "state": "ROLLBACK", "offer": offer, "payment": result, "reason": "payment_failure",
        "negotiation_id": negotiation_id, "product_name": product_name, "list_price": list_price, "merchant_id": merchant_id,
        "granted_perks": granted_perks or [], "declined_perks": declined_perks or [],
    }


def retry_payment(
    offer, policy, audit_path=DEFAULT_AUDIT_PATH, payment_client=None,
    force_payment_failure=False, on_event=None, product=None, catalog_path=None, negotiation_id=None,
    product_name=None, list_price=None, merchant_id=None,
):
    """The ONLY way a previously-failed payment is ever retried --
    NEVER called automatically by run_full_transaction() or
    _attempt_payment(). A caller (a human operator, or a future explicit
    CLI flag) must invoke this separately and deliberately per negotiation
    attempt. Per NEGOTIATION_SPEC.md Section 3C: reuses the ORIGINAL
    offer's own `expiration` timestamp as the retry window -- no second,
    unrelated timer. Raises ValueError if the offer has already expired;
    retrying against an expired offer is refused, a fresh negotiation is
    required instead.

    `negotiation_id` (Milestone 7, Section 4D, optional -- default None
    for backward compatibility with any pre-Milestone-7 caller): the
    ORIGINAL negotiation's id, from that negotiation's own outcome dict.
    A retry is meaningless without knowing which negotiation it belongs
    to, but this stays optional rather than required so existing callers
    that never captured it don't break -- entries just won't carry the
    grouping key in that case, same as any other None-negotiation_id
    entry. `product_name`/`list_price` (same section, follow-up): same
    optional-passthrough treatment."""
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
        [], path=audit_path, negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
    )
    _log(
        on_event, None, "merchant-agent", "inventory_hold", offer,
        f"Re-placing simulated inventory hold for qty {qty} of {policy['sku_id']} for payment retry.",
        [], path=audit_path, negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
    )

    return _attempt_payment(
        policy, offer, qty, total, audit_path, payment_client, force_payment_failure, on_event,
        product, catalog_path, is_retry=True, negotiation_id=negotiation_id,
        product_name=product_name, list_price=list_price, merchant_id=merchant_id,
    )


def run_full_transaction(
    policy, buyer, audit_path=DEFAULT_AUDIT_PATH,
    force_payment_failure=False, force_insufficient_inventory=False,
    approval_confirm=None, payment_client=None, merchant_evaluate=None, on_event=None,
    product=None, catalog_path=None, buyer_id=None, orders=None, risk_approval_tier="standard",
    persona=None, requested_perks=None,
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
    check it stands in for. `buyer_id`/`orders` (Milestone 5, both
    optional) are passed straight through to run_negotiation() for the
    Risk Agent check (see there); when the negotiation's risk_level comes
    back "high", the approval gate below is forced regardless of
    transaction_approval_threshold (that negotiation also already ran
    with max_discount_pct forced to 0, inside run_negotiation() --
    nothing further to do with that here). "moderate" (Section 2O,
    2026-09-02 follow-up) is NOT gated by default -- it proceeds with
    zero added friction, visible only via its risk_review audit entry.
    `risk_approval_tier` (Milestone 6, default "standard") changes that
    default per-merchant: "strict" also forces the gate on "moderate" --
    see run_negotiation() for how the flag is resolved/threaded.

    `approval_confirm=PAUSE_FOR_APPROVAL` (Milestone 8): returns a new
    terminal state, "PENDING_APPROVAL", at the exact point the gate would
    otherwise call confirm() -- for a caller (src/api.py) that cannot
    resolve the gate synchronously. See PAUSE_FOR_APPROVAL's own comment
    above _cli_confirm().

    Round-limit handling (Section 2X, 2026-09-04 -- replaces the
    Milestone 8 `on_round_limit` parameter and Section 2U's forced-
    approval logic entirely): if run_negotiation() returns
    "ROUND_LIMIT_REACHED" instead of a normal terminal state, this
    function pauses (if `approval_confirm is PAUSE_FOR_APPROVAL`,
    returning a new "ROUND_LIMIT_PENDING" state -- src/api.py's own
    resume mechanism, mirroring PENDING_APPROVAL exactly, see
    resolve_round_limit_decision() below) or resolves it synchronously
    via `confirm()` (the CLI path). Either way, accepting flows into
    _process_agreement() -- the SAME function a normal AGREEMENT_RECORDED
    uses -- so a round-limit acceptance is never exempt from the normal
    approval gate: if the resulting price/risk tier would have triggered
    it for a genuine agreement, it triggers it here too, no special-
    cased forced trigger (unlike the Section 2U design this replaces)."""
    negotiation_outcome = run_negotiation(
        policy, buyer, audit_path=audit_path, merchant_evaluate=merchant_evaluate, on_event=on_event,
        buyer_id=buyer_id, orders=orders, risk_approval_tier=risk_approval_tier,
        persona=persona, requested_perks=requested_perks,
    )

    if negotiation_outcome["state"] == "ROUND_LIMIT_REACHED":
        if approval_confirm is PAUSE_FOR_APPROVAL:
            return {
                "state": "ROUND_LIMIT_PENDING", "offer": negotiation_outcome["offer"], "payment": None,
                "negotiation_id": negotiation_outcome.get("negotiation_id"),
                "product_name": negotiation_outcome.get("product_name"),
                "list_price": negotiation_outcome.get("list_price"),
                "merchant_id": negotiation_outcome.get("merchant_id"),
                "risk_level": negotiation_outcome.get("risk_level"),
                # Carried verbatim so the resume path (POST /api/round-limit-decision
                # -> resolve_round_limit_decision()) never has to re-derive
                # the exact policy/requested_perks this negotiation paused
                # under -- same discipline as PENDING_APPROVAL's reason/
                # evidence_paths above.
                "policy": negotiation_outcome["policy"], "requested_perks": negotiation_outcome.get("requested_perks") or [],
            }
        confirm = approval_confirm or _cli_confirm
        offer = negotiation_outcome["offer"]
        accepted = confirm(
            f"Round limit reached with no agreement. Accept the merchant's final offer of "
            f"{offer['price']:.2f} {policy['currency']} for {offer['qty']}x {policy['product_name']}?"
        )
        if not accepted:
            return _decline_round_limit_offer(negotiation_outcome, audit_path, on_event)
        negotiation_outcome = _accept_round_limit_offer(negotiation_outcome, audit_path, on_event)

    # Milestone 7 (Section 4D): every payment-phase entry this function
    # logs below carries the SAME negotiation_id run_negotiation() just
    # generated, so a negotiation's negotiation + payment entries group
    # together as one unit.
    negotiation_id = negotiation_outcome.get("negotiation_id")
    # Section 4D follow-up: carried forward the same way as negotiation_id
    # above, from run_negotiation()'s own outcome dict -- not re-read from
    # `policy` here, since risk-driven repricing may have replaced the
    # local `policy` reference inside run_negotiation() without mutating
    # the caller's original dict.
    product_name = negotiation_outcome.get("product_name")
    list_price = negotiation_outcome.get("list_price")
    merchant_id = negotiation_outcome.get("merchant_id")
    # Milestone 9 (Section 4E): resolved once, inside run_negotiation(),
    # at whichever of its three acceptance points actually fired -- carried
    # through unchanged from here on, same treatment as negotiation_id/
    # product_name/list_price/merchant_id above.
    granted_perks = negotiation_outcome.get("granted_perks", [])
    declined_perks = negotiation_outcome.get("declined_perks", [])
    if negotiation_outcome["state"] != "AGREEMENT_RECORDED":
        return {
            "state": negotiation_outcome["state"], "offer": None, "payment": None,
            "negotiation_id": negotiation_id, "product_name": product_name, "list_price": list_price, "merchant_id": merchant_id,
            "granted_perks": granted_perks, "declined_perks": declined_perks,
        }

    return _process_agreement(
        negotiation_outcome, policy, audit_path, force_payment_failure, force_insufficient_inventory,
        approval_confirm, payment_client, on_event, product, catalog_path, risk_approval_tier,
    )


def _process_agreement(
    negotiation_outcome, policy, audit_path, force_payment_failure, force_insufficient_inventory,
    approval_confirm, payment_client, on_event, product, catalog_path, risk_approval_tier,
):
    """Section 2X extraction: everything run_full_transaction() does once
    it has a genuine AGREEMENT_RECORDED in hand -- inventory fulfillment
    check, the approval gate, and payment. Previously inline in
    run_full_transaction() itself; factored out so
    resolve_round_limit_decision() (a round-limit acceptance arriving via
    a LATER, separate API call) can reuse the EXACT SAME logic a normal
    agreement flows through immediately -- one function, not two
    implementations of "what happens after an agreement," so a round-
    limit acceptance can never silently skip a guardrail a genuine
    agreement would also have to clear."""
    negotiation_id = negotiation_outcome.get("negotiation_id")
    product_name = negotiation_outcome.get("product_name")
    list_price = negotiation_outcome.get("list_price")
    merchant_id = negotiation_outcome.get("merchant_id")
    granted_perks = negotiation_outcome.get("granted_perks", [])
    declined_perks = negotiation_outcome.get("declined_perks", [])

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
            ["catalog.current_inventory"], path=audit_path, negotiation_id=negotiation_id,
            product_name=product_name, list_price=list_price, merchant_id=merchant_id,
        )
        _log(
            on_event, None, "merchant-agent", "human_notification", offer,
            f"ALERT: agreed deal for {qty}x {product['sku_id']} cannot be fulfilled "
            f"({'only ' + str(product['current_inventory']) + ' in stock' if real_inventory_shortfall else f'only {simulated_stock} in stock (simulated for demo)'}). "
            "Manual review required.",
            [], path=audit_path, negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
        )
        result = {
            "state": "ROLLBACK", "offer": offer, "payment": None, "reason": "insufficient_inventory",
            "negotiation_id": negotiation_id, "product_name": product_name, "list_price": list_price, "merchant_id": merchant_id,
            "granted_perks": granted_perks, "declined_perks": declined_perks,
        }
        if not real_inventory_shortfall:
            result["simulated_stock"] = simulated_stock
        return result

    _log(
        on_event, None, "merchant-agent", "inventory_hold", offer,
        f"Placing simulated inventory hold for qty {qty} of {policy['sku_id']} pending payment.",
        [], path=audit_path, negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
    )

    risk_level = negotiation_outcome.get("risk_level")
    # 2026-09-02 follow-up (Section 2O): MODERATE no longer forces this
    # gate by default -- it proceeds with zero friction (still logged as
    # a risk_review entry for visibility, just not gated). HIGH always
    # does, alongside its own max_discount_pct=0 tightening (applied
    # earlier, inside run_negotiation()).
    #
    # Multi-merchant follow-up (Milestone 6, 2026-09-03): a "strict"
    # merchant (risk_approval_tier) also gates on MODERATE -- the exact
    # same gate mechanism, just a lower trigger threshold for that one
    # merchant. Every pre-Milestone-6 caller passes the default
    # "standard", so gate_moderate is always False there -- behavior is
    # byte-identical to Section 2O.
    gate_moderate = risk_level == "moderate" and risk_approval_tier == "strict"
    # Section 2U (2026-09-04): every independent reason approval could be
    # required -- collected as (evidence_paths, reason_fragment) pairs
    # rather than an if/elif/else that only ever cites the FIRST one that
    # matched. Two or more can genuinely be true at once (e.g. a HIGH-risk
    # negotiation that also exceeds the threshold) -- silently citing only
    # one would misreport the audit trail. One gate, one prompt, every
    # applicable reason named.
    #
    # Section 2X follow-up (2026-09-04, same day): a round-limit
    # acceptance used to ALWAYS add itself as a forced trigger here
    # (Section 2U). That's gone -- a round-limit acceptance is no longer
    # special-cased at all; it's just another way an AGREEMENT_RECORDED
    # arrived, and gets the SAME threshold/risk-only evaluation below as
    # any negotiated agreement. See run_negotiation()'s "Round-limit
    # handling" docstring section for why (the user's own framing: no
    # longer an auto-accept that needs a compensating forced check, but a
    # genuine human decision made BEFORE this point).
    approval_triggers = []
    if total > threshold:
        approval_triggers.append((
            ["policy.transaction_approval_threshold"],
            f"transaction total {total:.2f} exceeds policy.transaction_approval_threshold ({threshold})",
        ))
    if risk_level == "high":
        approval_triggers.append((
            ["risk_agent.risk_level"],
            "high risk flagged by the Risk Agent for this buyer/request",
        ))
    elif gate_moderate:
        approval_triggers.append((
            ["risk_agent.risk_level", "merchant.risk_approval_tier"],
            "moderate risk flagged by the Risk Agent, and this merchant (risk_approval_tier=strict) "
            "requires approval on moderate risk too",
        ))

    if approval_triggers:
        evidence = []
        for paths, _ in approval_triggers:
            for p in paths:
                if p not in evidence:
                    evidence.append(p)
        reason = (
            "Approval required: " + " AND ".join(fragment for _, fragment in approval_triggers) +
            "; pausing for human approval regardless of policy.transaction_approval_threshold."
        )
        _log(
            on_event, None, "merchant-agent", "approval_requested", offer, reason, evidence,
            path=audit_path, negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
        )
        if approval_confirm is PAUSE_FOR_APPROVAL:
            # The approval_requested entry above is already real and
            # written -- this just stops short of resolving it. Nothing
            # about WHY approval is needed gets recomputed by the caller;
            # `reason`/`evidence` (already derived above from every
            # applicable trigger -- threshold and/or risk) are handed
            # back verbatim so the resume path never has to re-derive
            # them.
            return {
                "state": "PENDING_APPROVAL", "offer": offer, "payment": None,
                "negotiation_id": negotiation_id, "product_name": product_name,
                "list_price": list_price, "merchant_id": merchant_id, "risk_level": risk_level,
                "reason": reason, "evidence_paths": evidence, "total": total, "qty": qty,
                "granted_perks": granted_perks, "declined_perks": declined_perks,
            }
        confirm = approval_confirm or _cli_confirm
        approved = confirm(
            f"Approve payment of {total:.2f} {policy['currency']} for {qty}x {policy['product_name']}?"
        )
        if not approved:
            _log(
                on_event, None, "merchant-agent", "approval_declined", offer,
                "Human declined the approval gate; payment_service was not called.",
                evidence, path=audit_path, negotiation_id=negotiation_id,
                product_name=product_name, list_price=list_price, merchant_id=merchant_id,
            )
            return {
                "state": "APPROVAL_DECLINED", "offer": offer, "payment": None, "negotiation_id": negotiation_id,
                "product_name": product_name, "list_price": list_price, "merchant_id": merchant_id,
                "granted_perks": granted_perks, "declined_perks": declined_perks,
            }
        _log(
            on_event, None, "merchant-agent", "approval_granted", offer,
            "Human approved the transaction via the CLI gate.",
            evidence, path=audit_path, negotiation_id=negotiation_id,
            product_name=product_name, list_price=list_price, merchant_id=merchant_id,
        )

    return _attempt_payment(
        policy, offer, qty, total, audit_path, payment_client, force_payment_failure, on_event,
        product, catalog_path, negotiation_id=negotiation_id, product_name=product_name, list_price=list_price, merchant_id=merchant_id,
        granted_perks=granted_perks, declined_perks=declined_perks,
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


def _read_demo_qty():
    """DEMO_QTY env var (Milestone 5 follow-up), parsed to int --
    DEFAULT_DEMO_QTY if unset. Lets a demo trigger the Risk Agent's
    "large request" factor (qty >= the product's own qty_breaks
    threshold, 10 in the generated catalog) from the CLI directly,
    without a Python snippet -- e.g. DEMO_QTY=10 alongside a new
    BUYER_ID reaches "high" risk end to end. Same invalid-input handling
    as _read_buyer_budget() -- a clear error, not a silent fallback."""
    raw = os.environ.get("DEMO_QTY")
    if raw is None:
        return DEFAULT_DEMO_QTY
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"DEMO_QTY={raw!r} is not a valid integer.")


if __name__ == "__main__":
    product_id = os.environ.get("PRODUCT_ID")
    buyer_id = os.environ.get("BUYER_ID")
    demo_qty = _read_demo_qty()

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

        # Multi-merchant support (Milestone 6, 2026-09-03): every catalog
        # product carries a merchant_id (CATEGORY_TO_MERCHANT, generator
        # + personalization.py single source of truth); resolve the real
        # merchant profile here so risk_approval_tier can vary the Risk
        # Agent's approval-gate behavior per merchant, not one global
        # rule. A product with no merchant_id (an older fixture) falls
        # back to "standard" -- byte-identical to pre-Milestone-6.
        risk_approval_tier = "standard"
        if product.get("merchant_id"):
            merchants = personalization.load_json(personalization.DEFAULT_MERCHANTS_PATH)
            merchant = personalization.find_merchant(merchants, product["merchant_id"])
            if merchant is None:
                raise SystemExit(
                    f"merchant_id={product['merchant_id']!r} (from {product_id}) not found in "
                    f"{personalization.DEFAULT_MERCHANTS_PATH}"
                )
            risk_approval_tier = merchant["risk_approval_tier"]
            print(
                f"Merchant: {merchant['merchant_name']} "
                f"(risk_approval_tier={risk_approval_tier})"
            )
    else:
        product = None
        catalog_path = None
        risk_approval_tier = "standard"
        with open("merchant_policy.json", encoding="utf-8") as f:
            base_policy = json.load(f)

    # Buyer profile + LTV discount bonus: BUYER_ID looks it up and sums
    # data/orders.json for a deterministic tier lookup (personalization.py
    # -- pure functions, no LLM). Unset -> bonus 0%, base_policy unchanged.
    buyer_profile = None
    ltv_bonus_pct = 0
    orders = None  # Milestone 5: stays None unless BUYER_ID is set -- gates the Risk Agent check below
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
        #
        # Section 2N follow-up (widened 2026-09-03 to cover MODERATE's new
        # discount reduction too, not just HIGH's): this preview must
        # ALSO reflect any risk-driven discount-cap adjustment, or it
        # shows a stale floor for exactly the scenario this feature
        # exists to demo -- the real risk check runs later, inside
        # run_negotiation(), so without this the printed floor and the
        # negotiation's actual settled price visibly disagree.
        # preview_policy is throwaway (read-only, risk_assessment() is a
        # pure function with no logging side effects) -- the real
        # `policy` passed to run_negotiation() below is untouched; that
        # function still does its own official risk check, logging, and
        # cap application independently.
        preview_policy = policy
        if buyer_id is not None and orders is not None:
            preview_risk = personalization.risk_assessment(buyer_id, demo_qty, policy.get("qty_breaks", []), orders)
            if preview_risk["level"] != "none":
                preview_policy = personalization.apply_risk_discount_cap(policy, preview_risk)
        effective_floor, floor_evidence = merchant_agent._floor_price(preview_policy, demo_qty)
        print(
            f"Effective negotiation floor at qty={demo_qty}: {effective_floor:.2f} "
            f"{policy['currency']} (driven by {merchant_agent._evidence_label(preview_policy, floor_evidence)})"
        )

    buyer_budget = _read_buyer_budget()
    buyer_mode = os.environ.get("BUYER_MODE", "scripted")

    persona_from_profile = (
        personalization.buyer_to_persona(buyer_profile, policy["product_name"]) if buyer_profile else None
    )
    if persona_from_profile is not None:
        # Section 2AG (2026-09-05): BUYER_ID alone (no BUYER_BUDGET) no
        # longer reads a stored budget_range -- it derives the ceiling as
        # a persona-appropriate discount off THIS policy's real
        # list_price, same mechanism (and same PERSONA_DISCOUNT_BANDS
        # table) as generate_negotiation_history.py. Not seeded: this is
        # an interactive CLI demo invocation, not the deterministic batch
        # generator, so a fresh persona-band draw each run is fine.
        persona_from_profile["budget"] = (
            buyer_budget if buyer_budget is not None
            else personalization.budget_from_list_price(buyer_profile["persona"], policy["list_price"], random)
        )

    if buyer_mode == "ai":
        persona = persona_from_profile or {
            "budget": buyer_budget if buyer_budget is not None else policy["list_price"] * 0.95,
            "target_product": policy["product_name"],
            "willingness_to_negotiate": "moderate -- open to a fair discount but not desperate",
        }
        buyer = AIBuyerAgent(
            qty=demo_qty, persona=persona, list_price=policy["list_price"], currency=policy["currency"],
            max_negotiation_rounds=policy["max_negotiation_rounds"],
        )
        budget_source = (
            "from BUYER_BUDGET" if buyer_budget is not None
            else "from BUYER_ID profile's persona band" if persona_from_profile is not None
            else "hardcoded default (BUYER_BUDGET/BUYER_ID not set)"
        )
        print(f"Effective buyer persona ({budget_source}): {json.dumps(persona, indent=2)}")
    else:
        if persona_from_profile is not None:
            max_acceptable_price = persona_from_profile["budget"]
            budget_source = "from BUYER_BUDGET" if buyer_budget is not None else "from BUYER_ID profile's persona band"
        else:
            max_acceptable_price = buyer_budget if buyer_budget is not None else 4450.0
            budget_source = "from BUYER_BUDGET" if buyer_budget is not None else "hardcoded default (BUYER_BUDGET/BUYER_ID not set)"
        buyer = BuyerAgent(
            qty=demo_qty, opening_discount_pct=15, max_acceptable_price=max_acceptable_price,
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
            buyer_id=buyer_id, orders=orders, risk_approval_tier=risk_approval_tier,
        )
    else:
        print("RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET not set -- running negotiation only, no payment phase.")
        outcome = run_negotiation(
            policy, buyer, merchant_evaluate=merchant_evaluate, on_event=_print_event,
            buyer_id=buyer_id, orders=orders, risk_approval_tier=risk_approval_tier,
        )

    _print_terminal_summary(outcome, policy, product)
    print()
    print(json.dumps(outcome, indent=2))
