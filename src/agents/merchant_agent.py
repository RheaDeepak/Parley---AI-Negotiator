import json
import os
import time
from typing import Literal, Optional

from pydantic import BaseModel

from src.agents.llm_utils import LLMUnavailableError, TransientLLMError, call_llm_with_retry
from src.agents.offer_utils import new_offer

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


def _floor_price(policy, qty):
    """Cheapest price policy allows for this qty. Returns (price, evidence_path).

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
    only the raw discount-tier ones."""
    discount_pct, discount_evidence_path = _applicable_tier(policy, qty)
    computed = policy["list_price"] * (1 - discount_pct / 100)
    liquidation_fraction = policy.get("liquidation_relaxation_fraction", 0.0)
    if liquidation_fraction > 0:
        computed = computed - liquidation_fraction * (computed - policy["min_price"])
    if policy["min_price"] >= computed:
        return policy["min_price"], "policy.min_price"
    return computed, discount_evidence_path


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
    text, not a percentage that can silently be zero."""
    if evidence_path == "policy.max_discount_pct":
        return f"{evidence_path} ({policy['max_discount_pct']}%)"
    if evidence_path.startswith("policy.qty_breaks["):
        idx = int(evidence_path.split("[", 1)[1].split("]", 1)[0])
        return f"{evidence_path} ({policy['qty_breaks'][idx]['discount_pct']}%)"
    return evidence_path


def _accept(offer, rationale, evidence_paths):
    return {"decision": "accept", "offer": offer, "rationale": rationale, "evidence_paths": evidence_paths}


def _counter(counter_offer, rationale, evidence_paths):
    return {"decision": "counter", "offer": counter_offer, "rationale": rationale, "evidence_paths": evidence_paths}


def _reject(offer, rationale, evidence_paths):
    return {"decision": "reject", "offer": offer, "rationale": rationale, "evidence_paths": evidence_paths}


def check_guardrails(offer, policy, round):
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
    genuinely is the counter-able floor, not an instant dealbreaker."""
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

    return _accept(
        offer,
        f"Offer price {offer['price']} meets or exceeds the allowed floor {floor:.2f} for qty "
        f"{offer['qty']}, per {_evidence_label(policy, evidence_path)}.",
        [evidence_path],
    )


def evaluate(offer, policy, round):
    """Backward-compatible alias for check_guardrails() -- the Milestone-1
    signature every earlier test and MERCHANT_MODE=rules caller uses."""
    return check_guardrails(offer, policy, round)


def evaluate_rules(offer, policy, round, negotiation_history=None):
    """MERCHANT_MODE=rules adapter: same 4-arg shape as evaluate_ai() (the
    trailing negotiation_history is accepted and ignored) so
    negotiation_loop.run_negotiation() can call either uniformly. Does NOT
    set guardrail_clamped -- that field only ever appears on AI-merchant
    entries (Section 4C), so rules-only audit entries stay byte-identical
    to Milestone 1/2/3a."""
    return check_guardrails(offer, policy, round)


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
            return _accept(offer, strategy.concession_reasoning, guardrail_verdict["evidence_paths"]), False
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


def evaluate_ai(offer, policy, round, negotiation_history, llm_call=None, model=DEFAULT_MODEL, max_retries=3, sleep_fn=time.sleep):
    """MERCHANT_MODE=ai top-level entry point: Layer 2 proposes, then Layer
    1 (check_guardrails) always re-validates before anything is returned.
    Falls back to rules-only for this round (not a crash, not a negotiation-
    ending error) if the Gemini backend stays unavailable."""
    guardrail_verdict = check_guardrails(offer, policy, round)

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
