import json
import os
import time
from typing import Literal, Optional

from pydantic import BaseModel

from src.agents.llm_utils import LLMUnavailableError, TransientLLMError, call_llm_with_retry
from src.agents.offer_utils import new_offer
from src.personalization import PERK_COST_FIELDS

DEFAULT_MODEL = "gemini-3.5-flash-lite"


def _applicable_tier(policy, qty):
    """Largest qty_breaks entry whose min_qty <= qty; falls back to
    max_discount_pct if none applies. Returns (discount_pct, evidence_path)."""
    qty_breaks = policy.get("qty_breaks", [])
    best_idx, best = None, None
    for idx, tier in enumerate(qty_breaks):
        if qty >= tier["min_qty"] and (best is None or tier["min_qty"] > best["min_qty"]):
            best_idx, best = idx, tier
    if best is None:
        return policy["max_discount_pct"], "policy.max_discount_pct"
    return best["discount_pct"], f"policy.qty_breaks[{best_idx}].discount_pct"


def _floor_price(policy, qty, granted_perk_cost=0):
    """Cheapest price policy allows for this qty. Returns (price, evidence_path).

    `granted_perk_cost` (Milestone 9, Section 4E, default 0 -- every
    pre-existing caller unaffected): the combined cost of whatever perks
    are being considered for grant. Folded into the SAME max()-based
    floor everything else already goes through -- not a second, separate
    check -- so a granted perk's cost reduces the effective margin
    available for price discount exactly like liquidation/min_price/
    max_discount_pct already do. See _resolve_perks() below for how this
    gets computed and checked at acceptance time.

    Liquidation (2026-09-02 structural fix, Section 2J): `computed` (the
    discount-cap floor) is relaxed toward policy["min_price"] by whatever
    fraction policy["liquidation_relaxation_fraction"] carries (0.0 for a
    fresh/non-catalog policy, via .get() -- byte-identical to before this
    fix when the field is absent). This is where liquidation actually
    takes effect now; personalization.py no longer touches min_price
    itself, since min_price was already close to the floor and rarely the
    binding constraint -- relaxing it directly had no measurable effect
    for ~99% of the generated catalog. The interpolation happens directly
    in price-space here, working toward min_price as an absolute price
    target, not as a discount percentage -- so it's independent of (and
    doesn't need to respect) HARD_DISCOUNT_CEILING_PCT, which governs a
    different mechanism (LTV-bonus stacking on the nominal discount rate,
    Section 2C). min_price remains the one true, non-negotiable cap on how
    far ANY relaxation -- LTV, liquidation, or both -- can ever go.

    evidence_path names whichever term of max(min_price, computed) actually
    won -- not always the discount-tier evidence. Before Section 2F this
    distinction was unreachable (a below-min_price offer was always caught
    by check_guardrails()'s earlier absolute-reject when min_price was
    binding); Section 2F's liquidation fall-through made it reachable, so a
    dominant min_price now needs its own evidence_path (Section 2H
    follow-up). Under the liquidation ramp, a fully-aged product's
    computed floor converges exactly to min_price, so evidence_path
    naturally flips from the discount-tier evidence to "policy.min_price"
    right at that point -- this logic is unchanged by the 2026-09-02 fix;
    it just now also applies to liquidation-relaxed computed values, not
    only the raw discount-tier ones.

    Risk ceiling (Section 2T, 2026-09-04 bug fix, confirmed live-
    reproduced; corrected same day, Section 2V follow-up -- also
    confirmed live-reproduced): `policy["risk_level"] == "high"` means
    `computed` above was derived from a RISK-TIGHTENED max_discount_pct/
    qty_breaks with NO room at all (0% allowed, exactly list_price).
    Liquidation relaxation has no awareness of that; left alone, it
    interpolates this risk-tightened `computed` toward min_price exactly
    as if it were the normal floor, silently eroding the risk restriction
    entirely on any product that also happens to be liquidation-eligible
    (live-reproduced: SKU-ELEC-003, HIGH risk, fully liquidation-ramped --
    floor collapsed to min_price 5093.53 instead of list_price 7976.05, a
    ~2882 INR discount the Risk Agent's own card said was 0%). The fix:
    capture `computed` as a THIRD floor candidate BEFORE liquidation
    touches it, ONLY when risk_level is specifically "high", and take the
    max() of all three (min_price(+perk_cost), the liquidation-relaxed
    discount-cap floor, and the pre-liquidation risk ceiling) -- the same
    "combine via max(), never a second separate check" principle every
    other guardrail here already follows, just with a third term added.

    Section 2V correction (same day): the original fix checked
    `policy.get("risk_discount_capped", False)`, a boolean personalization.
    apply_risk_discount_cap() set True for BOTH "moderate" and "high" --
    so MODERATE also got the pre-liquidation-snapshot candidate, and
    since that candidate (MODERATE's 25%-of-normal ceiling, computed
    BEFORE liquidation) is always >= the liquidation-relaxed value,
    max() picked it every time, suppressing liquidation for MODERATE
    exactly like the original bug did for HIGH -- live-reproduced on the
    same SKU-ELEC-003: MODERATE risk (new-buyer only, no large_request)
    computed an effective floor of ~7756.71, essentially list_price, with
    liquidation's relaxation invisible despite days_in_inventory=379.
    Only HIGH was ever meant to override liquidation outright; MODERATE's
    ceiling is meant to be a tighter STARTING POINT for liquidation to
    relax from, same mechanism as an unrestricted policy, not a floor
    candidate of its own. Checking risk_level == "high" specifically
    (rather than "is risk active at all") fixes this: MODERATE and "none"
    both skip the risk_ceiling candidate entirely and fall through to the
    normal min_price/computed max(), so liquidation relaxes the
    (already risk-tightened, for MODERATE) `computed` exactly as it
    always has for an unrestricted policy -- MODERATE's 25% ceiling still
    shows up as a tighter interpolation STARTING point, it just no longer
    blocks liquidation from relaxing past it. When risk isn't "high" at
    all, this candidate is never added, so liquidation relaxes exactly as
    before Section 2T -- existing liquidation-only and NONE-risk behavior
    is unchanged."""
    candidates = _floor_price_candidates(policy, qty, granted_perk_cost)
    # max() with a list of (value, label) tuples compares tuples
    # lexicographically on ties, which would pick by label text rather
    # than insertion order -- so compare on the value alone via key=,
    # keeping candidates ordered [min_price, computed, risk_ceiling] so a
    # tie resolves to the same term Section 2H's evidence_path convention
    # already established (min_price wins ties over computed).
    price, evidence_path, _label, _detail = max(candidates, key=lambda c: c[0])
    return price, evidence_path


def _floor_price_candidates(policy, qty, granted_perk_cost=0):
    """Section 2AB (2026-09-05): the exact candidate computation
    _floor_price() takes max() over, extracted so a display/explainability
    consumer (GET /api/floor-preview, for the frontend's "how is this
    calculated?" breakdown) can show EVERY candidate and which one won --
    not just the final number. Pure extraction, zero behavior change:
    _floor_price() itself now just calls this and takes the max, so
    there is exactly ONE place this formula is ever computed -- the same
    "never duplicate, never let two implementations drift apart"
    discipline this file follows everywhere else (the risk-vs-liquidation
    and MODERATE-vs-qty_breaks bugs earlier were both exactly two
    almost-identical computations silently disagreeing).

    Returns a list of (value, evidence_path, label, detail) 4-tuples, in
    the same fixed order _floor_price() itself relies on for tie-breaking
    (min_price first, then the discount-tier floor, then the risk
    ceiling if present) -- `label`/`detail` are human-readable, for
    display only, and never touch the audit-log evidence_paths schema."""
    discount_pct, discount_evidence_path = _applicable_tier(policy, qty)
    computed_before_liquidation = policy["list_price"] * (1 - discount_pct / 100)
    # Captured BEFORE liquidation relaxation below can touch `computed` --
    # see _floor_price()'s own docstring for why this must be a snapshot,
    # not a live re-read. ONLY "high" -- see Section 2V correction there
    # for why checking a broader "is risk active" signal was the bug.
    risk_ceiling = computed_before_liquidation if policy.get("risk_level") == "high" else None

    liquidation_fraction = policy.get("liquidation_relaxation_fraction", 0.0)
    computed = computed_before_liquidation
    liquidation_applied = liquidation_fraction > 0
    if liquidation_applied:
        computed = computed - liquidation_fraction * (computed - policy["min_price"])

    min_price_floor = policy["min_price"] + granted_perk_cost
    min_price_evidence = "policy.min_price" if granted_perk_cost == 0 else "policy.min_price+perk_cost"
    min_price_label = "Absolute margin floor (min_price)" + (" + granted perk cost" if granted_perk_cost else "")
    min_price_detail = (
        f"min_price {policy['min_price']:.2f}" + (f" + perk cost {granted_perk_cost:.2f}" if granted_perk_cost else "")
    )

    discount_label = f"Discount-tier floor ({discount_pct:.2f}% off list price)"
    discount_detail = f"list_price {policy['list_price']:.2f} x (1 - {discount_pct:.2f}%)"
    if liquidation_applied:
        discount_label += f", liquidation-relaxed {liquidation_fraction * 100:.0f}% toward min_price"
        discount_detail += f", then relaxed {liquidation_fraction * 100:.0f}% of the way toward min_price {policy['min_price']:.2f}"

    candidates = [
        (min_price_floor, min_price_evidence, min_price_label, min_price_detail),
        (computed, discount_evidence_path, discount_label, discount_detail),
    ]
    if risk_ceiling is not None:
        candidates.append((
            risk_ceiling, "risk_agent.discount_ceiling",
            "Risk ceiling (HIGH risk -- full list price, liquidation suppressed)",
            f"list_price {policy['list_price']:.2f}, captured before liquidation so a HIGH-risk buyer never gets an aged-inventory discount",
        ))
    return candidates


def _evidence_label(policy, evidence_path):
    """Percentage-annotated version of an evidence_path string, for
    CONSOLE/RATIONALE display only -- never used for the evidence_paths
    audit-schema field, which stays pure dotted-path strings
    (NEGOTIATION_SPEC.md Section 4; changing that would break every
    exact-match test on it). Confirmed with the user (2026-09-02): a bare
    "policy.qty_breaks[0].discount_pct" label reads as if a real discount
    is being applied even when the Risk Agent (Section 2N) has zeroed it
    to 0% -- e.g. a HIGH-risk negotiation settling at exactly list_price
    still cited that tier as "driving" the floor with no indication its
    rate was 0, costing two rounds of back-and-forth to rule out an
    actual computation bug. Stating the live value inline removes that
    ambiguity at the source, everywhere the label is shown, so it can't
    recur. "policy.min_price" passes through unchanged -- it's already an
    absolute price, always stated directly in the surrounding rationale
    text, not a percentage that can silently be zero.

    2026-09-03 follow-up (Risk Agent three-level gradient, Section 2S):
    discount_pct values coming out of apply_risk_discount_cap() are now
    genuinely fractional (e.g. 13 * 0.25 = 3.25), not just whole numbers
    or a hardcoded 0 -- :g formatting shows "0" and "3.25" cleanly
    instead of "0.0" or an unpredictable number of decimal places."""
    if evidence_path == "policy.max_discount_pct":
        return f"{evidence_path} ({policy['max_discount_pct']:g}%)"
    if evidence_path.startswith("policy.qty_breaks["):
        idx = int(evidence_path.split("[", 1)[1].split("]", 1)[0])
        return f"{evidence_path} ({policy['qty_breaks'][idx]['discount_pct']:g}%)"
    return evidence_path


def _resolve_perks(offer, policy, requested_perks):
    """Milestone 9, Section 4E. Called ONLY once an offer's price already
    clears the non-perk floor (i.e. would be accepted) -- perks are never
    considered during counter-price computation, only at the moment of
    acceptance, using whatever price the negotiation actually landed on.

    Checks whether that price can ALSO afford whichever requested perks
    are eligible (policy["eligible_perks"], resolved once upfront by
    personalization.perk_eligibility() -- ineligible requests are already
    conclusively declined before this ever runs, logged separately as a
    perk_review entry; this function only ever sees candidates that
    passed eligibility). Uses the SAME _floor_price() calculation as
    every other floor check, just with the candidates' combined cost
    folded in via granted_perk_cost -- not a second, separate check, per
    the explicit requirement this guards against ("only one pathway
    touched" bug class from liquidation/risk).

    All-or-nothing: every eligible, requested perk is granted or declined
    TOGETHER, based on whether the offer clears the floor with ALL of
    their combined cost folded in -- never "grant some, decline others"
    picked one at a time. Originally justified by a buyer being eligible
    for at most one perk by construction; Section 2AH (2026-09-05) added
    a second, independent eligibility path (qty > PERK_LARGE_QTY_THRESHOLD)
    that can now make a buyer eligible for BOTH free_delivery and
    extended_warranty simultaneously (an established buyer placing a
    large order) -- the all-or-nothing behavior itself didn't need to
    change: `candidates` and `total_cost` above already generalize to N
    perks with no special-casing, so two simultaneous candidates are
    still granted or declined together by the same single floor check.
    Returns (granted, declined_for_margin)."""
    eligible_perks = policy.get("eligible_perks", [])
    candidates = [p for p in requested_perks if p in eligible_perks]
    if not candidates:
        return [], []

    total_cost = sum(policy.get(PERK_COST_FIELDS[p]) or 0 for p in candidates)
    floor_with_perks, _ = _floor_price(policy, offer["qty"], granted_perk_cost=total_cost)
    if offer["price"] >= floor_with_perks:
        return candidates, []
    return [], candidates


def _accept(offer, rationale, evidence_paths, granted_perks=None, declined_perks=None):
    return {
        "decision": "accept", "offer": offer, "rationale": rationale, "evidence_paths": evidence_paths,
        "granted_perks": granted_perks or [], "declined_perks": declined_perks or [],
    }


def _counter(counter_offer, rationale, evidence_paths):
    return {"decision": "counter", "offer": counter_offer, "rationale": rationale, "evidence_paths": evidence_paths}


def _reject(offer, rationale, evidence_paths):
    return {"decision": "reject", "offer": offer, "rationale": rationale, "evidence_paths": evidence_paths}


def check_guardrails(offer, policy, round, requested_perks=None):
    """Layer 1 per NEGOTIATION_SPEC.md Section 3 / 3B -- pure, deterministic,
    non-negotiable. No LLM or network calls. This is the renamed/extended
    Milestone-1 evaluate() logic (added: policy.inventory_floor, the
    minimum-order-quantity guardrail) and is the sole source of truth for
    what's a valid price/quantity at a given round; evaluate_ai() re-checks
    every AI proposal against exactly this function before anything reaches
    the buyer or the audit log.

    policy.min_price_is_liquidation_relaxed (Section 2F, name kept by the
    2026-09-02 Section 2J fix even though min_price's own VALUE is no
    longer touched by liquidation -- see _floor_price()) -- absent/False
    for every Milestone 1-3c(pre-fix) policy, so behavior there is
    byte-identical to before: an offer below min_price is an absolute,
    non-negotiable reject. True whenever liquidation is active for this
    product (days_in_inventory past its category threshold), regardless of
    how far the ramp has progressed -- an offer below min_price then falls
    through to the normal floor/counter logic below instead of an instant
    reject, because a heavily-aged product's _floor_price() can itself
    converge to exactly min_price (full ramp), at which point min_price
    genuinely is the counter-able floor, not an instant dealbreaker.

    `requested_perks` (Milestone 9, Section 4E, default None -- every
    pre-existing caller unaffected): perk names the buyer asked for, if
    any. Only ever consulted once the offer would already be accepted on
    price alone -- see _resolve_perks()."""
    max_rounds = policy["max_negotiation_rounds"]
    inventory_floor = policy.get("inventory_floor", 1)
    floor, evidence_path = _floor_price(policy, offer["qty"])
    min_price_is_liquidation_relaxed = policy.get("min_price_is_liquidation_relaxed", False)

    if round > max_rounds:
        return _reject(
            offer,
            f"Round {round} exceeds policy.max_negotiation_rounds ({max_rounds}); negotiation forcibly ends.",
            ["policy.max_negotiation_rounds"],
        )

    if offer["qty"] < inventory_floor:
        return _reject(
            offer,
            f"Offer qty {offer['qty']} is below policy.inventory_floor ({inventory_floor}), "
            "the minimum order quantity.",
            ["policy.inventory_floor"],
        )

    if offer["price"] < policy["min_price"] and not min_price_is_liquidation_relaxed:
        return _reject(
            offer,
            f"Offer price {offer['price']} is below policy.min_price ({policy['min_price']}), the absolute floor.",
            ["policy.min_price"],
        )

    if offer["price"] < floor:
        if round >= max_rounds:
            return _reject(
                offer,
                f"Offer price {offer['price']} is below the allowed floor {floor:.2f} for qty "
                f"{offer['qty']} ({_evidence_label(policy, evidence_path)}), and "
                f"policy.max_negotiation_rounds ({max_rounds}) has been reached with no agreement.",
                [evidence_path, "policy.max_negotiation_rounds"],
            )
        counter_offer = new_offer(floor, offer["qty"])
        return _counter(
            counter_offer,
            f"Offer price {offer['price']} is below the allowed floor {floor:.2f} for qty "
            f"{offer['qty']}, per {_evidence_label(policy, evidence_path)}. Countering at {floor:.2f}.",
            [evidence_path],
        )

    granted_perks, declined_perks = _resolve_perks(offer, policy, requested_perks or [])
    perk_note = ""
    if granted_perks:
        perk_note = f" Perk(s) granted: {', '.join(granted_perks)}."
    if declined_perks:
        perk_note += (
            f" Perk(s) declined: {', '.join(declined_perks)} -- the combined price concession and "
            "perk cost would breach the minimum margin floor."
        )
    return _accept(
        offer,
        f"Offer price {offer['price']} meets or exceeds the allowed floor {floor:.2f} for qty "
        f"{offer['qty']}, per {_evidence_label(policy, evidence_path)}.{perk_note}",
        [evidence_path],
        granted_perks=granted_perks, declined_perks=declined_perks,
    )


def evaluate(offer, policy, round):
    """Backward-compatible alias for check_guardrails() -- the Milestone-1
    signature every earlier test and MERCHANT_MODE=rules caller uses."""
    return check_guardrails(offer, policy, round)


def evaluate_rules(offer, policy, round, negotiation_history=None, requested_perks=None):
    """MERCHANT_MODE=rules adapter: same shape as evaluate_ai() (the
    trailing negotiation_history is accepted and ignored) so
    negotiation_loop.run_negotiation() can call either uniformly. Does NOT
    set guardrail_clamped -- that field only ever appears on AI-merchant
    entries (Section 4C), so rules-only audit entries stay byte-identical
    to Milestone 1/2/3a. `requested_perks` (Milestone 9): an eligible,
    affordable perk is auto-granted here -- there's no LLM in rules mode
    to exercise "whether/when" discretion, so the deterministic default
    is to grant whatever Layer 1 itself already validated as eligible and
    affordable (confirmed with the user)."""
    return check_guardrails(offer, policy, round, requested_perks=requested_perks)


# ---------------------------------------------------------------------------
# Layer 2: strategic judgment (Gemini). Genuinely autonomous within whatever
# room Layer 1 leaves -- but every proposal is re-validated against
# check_guardrails() below before it can reach the buyer or the audit log.
# ---------------------------------------------------------------------------


class MerchantOfferFields(BaseModel):
    price: float
    qty: int
    terms: str


class MerchantDecision(BaseModel):
    action: Literal["accept", "counter", "reject"]
    counter_offer: Optional[MerchantOfferFields] = None
    concession_reasoning: str


MERCHANT_SYSTEM_PROMPT = (
    "You are the strategic-judgment layer of an AI merchant-agent. A separate, "
    "deterministic guardrail system will independently re-check and, if "
    "necessary, clamp anything you propose before it ever reaches the buyer -- "
    "so your job is not to avoid catastrophic outcomes, it's to negotiate well "
    "within the policy limits you're given. Treat those limits as your genuine "
    "target, not a suggestion to test. Decide HOW to negotiate this round: how "
    "much to concede (if at all), whether to hold firm given the trend so far, "
    "and how to frame your counter-offer. You may mention quantity-break "
    "incentives in your offer's terms/reasoning if it could help close the "
    "deal, but the qty you counter with must stay the same as the buyer's "
    "current offer. This is a real strategic decision, not a narration of a "
    "decision already made. The prompt tells you the exact valid price range "
    "for this offer's quantity, already computed from the policy -- use that "
    "range directly and never counter below its floor; do not re-derive the "
    "floor yourself from the raw min_price/max_discount_pct/qty_breaks "
    "numbers, since that combination is easy to get wrong."
)


def _real_llm_call(system, user_content, model):
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise LLMUnavailableError("GEMINI_API_KEY is not set in the environment.")

    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=model,
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=MerchantDecision,
            ),
        )
    except genai_errors.ServerError as exc:
        raise TransientLLMError(f"Gemini server error: {exc}") from exc
    except genai_errors.ClientError as exc:
        if getattr(exc, "code", None) == 429:
            raise TransientLLMError(f"Gemini rate limit / quota exceeded: {exc}") from exc
        raise
    except (ConnectionError, TimeoutError) as exc:
        raise TransientLLMError(f"Gemini API unreachable: {exc}") from exc

    if response.parsed is not None:
        return response.parsed.model_dump()
    return json.loads(response.text)


def _build_merchant_prompt(offer, policy, round, negotiation_history):
    floor, evidence_path = _floor_price(policy, offer["qty"])
    policy_desc = (
        f"Policy (background context): min_price={policy['min_price']}, "
        f"max_discount_pct={policy['max_discount_pct']}, list_price={policy['list_price']}, "
        f"currency={policy['currency']}, qty_breaks={policy.get('qty_breaks', [])}, "
        f"max_negotiation_rounds={policy['max_negotiation_rounds']}, current round={round}."
    )
    valid_range_desc = (
        f"VALID COUNTER-OFFER PRICE RANGE for this offer's quantity ({offer['qty']}): "
        f"{floor:.2f} to {policy['list_price']:.2f} {policy['currency']}, inclusive. "
        f"{floor:.2f} is the exact, already-computed floor for qty {offer['qty']} (it already "
        f"combines min_price, max_discount_pct, and any applicable qty_breaks tier -- driven by "
        f"{_evidence_label(policy, evidence_path)}). Any counter_offer.price you propose must be >= {floor:.2f}; do not "
        "recompute this floor yourself from the raw policy numbers above."
    )
    history_desc = json.dumps(negotiation_history, indent=2) if negotiation_history else "No prior rounds."
    offer_desc = f"The buyer's current offer: {json.dumps(offer)}."
    return f"{policy_desc}\n\n{valid_range_desc}\n\n{offer_desc}\n\nNegotiation history so far:\n{history_desc}"


def decide_strategy(offer, policy, round, negotiation_history, llm_call=None, model=DEFAULT_MODEL, max_retries=3, sleep_fn=time.sleep):
    """Layer 2. One Gemini call. Raises LLMUnavailableError if the backend
    stays unreachable/rate-limited through all retries -- callers (i.e.
    evaluate_ai()) decide how to fall back."""
    llm_call = llm_call or _real_llm_call
    user_content = _build_merchant_prompt(offer, policy, round, negotiation_history)
    raw = call_llm_with_retry(
        llm_call, MERCHANT_SYSTEM_PROMPT, user_content, model,
        max_retries=max_retries, sleep_fn=sleep_fn,
    )
    return MerchantDecision.model_validate(raw)


def _validate_against_guardrails(strategy, guardrail_verdict, offer, policy, round):
    """The critical re-validation: no field of `strategy` (the untrusted
    LLM proposal) is ever written into the returned result unless it has
    passed a check_guardrails()-equivalent test first. Returns
    (result_dict, guardrail_clamped: bool). On any clamp, the rejected raw
    proposal (price, or concession_reasoning, which could echo it in prose)
    is deliberately NOT included anywhere in the result -- only which
    policy field it violated."""

    # Guardrails already say this round is a hard reject (min_price,
    # inventory_floor, or round-limit) -- nothing Layer 2 proposes can
    # override that, no matter what it is.
    if guardrail_verdict["decision"] == "reject":
        clamped = strategy.action != "reject"
        result = dict(guardrail_verdict)
        if clamped:
            result["rationale"] = (
                result["rationale"] + " Layer 2 proposed a different action; overridden by hard guardrails."
            )
        return result, clamped

    if strategy.action == "reject":
        # A strategic reject is always guardrail-safe -- it's the most
        # conservative action available.
        return _reject(offer, strategy.concession_reasoning, []), False

    if strategy.action == "accept":
        if guardrail_verdict["decision"] == "accept":
            # granted_perks/declined_perks are Layer 1's own, already-computed
            # verdict -- carried through unchanged; Layer 2 only supplies the
            # narrative (concession_reasoning), never the perk decision itself.
            return _accept(
                offer, strategy.concession_reasoning, guardrail_verdict["evidence_paths"],
                granted_perks=guardrail_verdict.get("granted_perks"),
                declined_perks=guardrail_verdict.get("declined_perks"),
            ), False
        # The current offer does NOT clear the guardrail floor -- accepting
        # it would be exactly the violation this milestone exists to
        # prevent. Override with the guardrail's own verdict.
        result = dict(guardrail_verdict)
        annotated_paths = [_evidence_label(policy, p) for p in guardrail_verdict["evidence_paths"]]
        result["rationale"] = (
            f"Layer 2 proposed accepting an offer that violates {annotated_paths}; "
            "overridden -- countering at the policy floor instead."
        )
        return result, True

    # strategy.action == "counter"
    if guardrail_verdict["decision"] == "accept" and round >= policy["max_negotiation_rounds"]:
        # Bug fix (2026-09-02): the offer already clears the floor
        # (guardrail_verdict says "accept") and this is the LAST allowed
        # round -- Layer 2 has no discretion to hold out for a better
        # price here, since any counter necessarily requires a further
        # round to resolve, which would exceed
        # policy.max_negotiation_rounds. Before this fix, a counter here
        # was validated only against the floor (never against the round
        # cap), so it passed straight through -- pushing the buyer into
        # an illegal round beyond the cap, where check_guardrails()'s
        # `round > max_rounds` check would only THEN catch it and reject.
        # Live-reproduced: SKU-ELEC-003, round 5 offer 5500 (above floor
        # 5093.53) drew a counter at 5550 instead of an accept, forcing a
        # round 6 that should never have existed. Force the accept Layer
        # 1 already computed instead.
        result = dict(guardrail_verdict)
        result["rationale"] = (
            result["rationale"] + " Layer 2 proposed a counter instead of accepting on the final round; "
            "overridden -- a further round would exceed policy.max_negotiation_rounds."
        )
        return result, True

    if strategy.counter_offer is None:
        result = dict(guardrail_verdict)
        result["rationale"] = (
            result["rationale"] + " Layer 2 proposed to counter but returned no counter_offer; "
            "fell back to the guardrail-computed action."
        )
        return result, True

    qty = offer["qty"]  # pinned to the buyer's current qty -- see NEGOTIATION_SPEC.md Section 2B
    floor, evidence_path = _floor_price(policy, qty)
    if strategy.counter_offer.price < floor:
        clamped_offer = new_offer(floor, qty, terms=strategy.counter_offer.terms)
        rationale = (
            f"Layer 2's proposed counter violated {_evidence_label(policy, evidence_path)}; clamped to "
            "the policy floor before being sent to the buyer."
        )
        return _counter(clamped_offer, rationale, [evidence_path]), True

    valid_offer = new_offer(strategy.counter_offer.price, qty, terms=strategy.counter_offer.terms)
    return _counter(valid_offer, strategy.concession_reasoning, [evidence_path]), False


def evaluate_ai(offer, policy, round, negotiation_history, requested_perks=None, llm_call=None, model=DEFAULT_MODEL, max_retries=3, sleep_fn=time.sleep):
    """MERCHANT_MODE=ai top-level entry point: Layer 2 proposes, then Layer
    1 (check_guardrails) always re-validates before anything is returned.
    Falls back to rules-only for this round (not a crash, not a negotiation-
    ending error) if the Gemini backend stays unavailable.

    `requested_perks` (Milestone 9, Section 4E): passed straight to
    check_guardrails() -- Layer 1 always makes the definitive
    eligibility+affordability decision, exactly as in rules mode. Layer 2
    (the LLM) is never given a "grant this perk" field to propose at all;
    it only ever decides accept/counter/reject on price as before, and
    whichever grant/decline Layer 1 already computed rides along
    unchanged whenever an accept is validated (see
    _validate_against_guardrails()'s "accept" branch). This keeps perk
    grants on the same non-negotiable footing as eligibility -- no new
    LLM-trust surface for a money-relevant decision."""
    guardrail_verdict = check_guardrails(offer, policy, round, requested_perks=requested_perks)

    try:
        strategy = decide_strategy(
            offer, policy, round, negotiation_history,
            llm_call=llm_call, model=model, max_retries=max_retries, sleep_fn=sleep_fn,
        )
    except LLMUnavailableError:
        result = dict(guardrail_verdict)
        result["rationale"] = result["rationale"] + " (AI strategy layer unavailable this round; fell back to rules-only guardrails.)"
        result["guardrail_clamped"] = False
        return result

    result, clamped = _validate_against_guardrails(strategy, guardrail_verdict, offer, policy, round)
    result = dict(result)
    result["guardrail_clamped"] = clamped
    return result
