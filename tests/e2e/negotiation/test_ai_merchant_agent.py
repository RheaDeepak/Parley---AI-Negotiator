import json
from functools import partial

import pytest

from src.agents.ai_buyer_agent import AIBuyerAgent
from src.agents.buyer_agent import BuyerAgent
from src.agents.merchant_agent import TransientLLMError, check_guardrails, decide_strategy, evaluate_ai
from src.personalization import liquidation_relaxation_fraction, product_to_policy
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
    "transaction_approval_threshold": 20000,
    "inventory_floor": 1,
}
# qty=1 floor (no qty_breaks tier applies) = max(3799, 4999*0.88) = 4399.12

PERSONA = {
    "budget": 4600.0,
    "target_product": "Wireless Mechanical Keyboard",
    "willingness_to_negotiate": "moderate -- open to a fair discount but not desperate",
}


class ScriptedMerchantLLM:
    """Injectable llm_call stub: returns pre-programmed raw dicts in order."""

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.calls = []

    def __call__(self, system, user_content, model):
        self.calls.append({"system": system, "user_content": user_content, "model": model})
        return self._decisions.pop(0)


class RepeatingMerchantLLM:
    """Injectable llm_call stub: returns the same decision every call."""

    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    def __call__(self, system, user_content, model):
        self.calls += 1
        return self.decision


class AlwaysFailingLLM:
    def __init__(self, error_cls=TransientLLMError):
        self.error_cls = error_cls
        self.calls = 0

    def __call__(self, system, user_content, model):
        self.calls += 1
        raise self.error_cls("simulated Gemini outage")


def _merchant_decision(action, price=None, qty=None, terms="", reasoning="Strategic reasoning for this round."):
    counter_offer = {"price": price, "qty": qty, "terms": terms} if action == "counter" else None
    return {"action": action, "counter_offer": counter_offer, "concession_reasoning": reasoning}


def _read_log(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _offer(price, qty=1):
    return {"offer_id": "test-offer", "price": price, "qty": qty, "terms": "", "expiration": "", "timestamp": ""}


# ---------------------------------------------------------------------------
# GUARDRAIL TESTS -- the most important tests in this file.
# ---------------------------------------------------------------------------


def test_guardrail_clamp_when_proposal_violates_min_price(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    # Below min_price (3799) entirely -- the most severe violation.
    llm = ScriptedMerchantLLM([_merchant_decision("counter", price=3000.0, qty=1)])

    offer = _offer(4000.0, qty=1)  # between min_price and the qty=1 floor
    result = evaluate_ai(offer, POLICY, round=1, negotiation_history=[], llm_call=llm)

    assert result["decision"] == "counter"
    assert result["offer"]["price"] == 4399.12  # clamped to the deterministic floor
    assert result["guardrail_clamped"] is True
    assert "3000" not in json.dumps(result)

    from src.agents.audit_logger import log_entry
    log_entry(
        "merchant-agent", result["decision"], result["offer"], result["rationale"],
        result["evidence_paths"], path=str(audit_path), guardrail_clamped=result["guardrail_clamped"],
    )
    raw_log = audit_path.read_text(encoding="utf-8")
    assert "3000" not in raw_log
    assert "3000.0" not in raw_log


def test_guardrail_clamp_when_proposal_violates_max_discount_pct(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    # Above min_price (3799) but below the max_discount_pct-derived floor
    # (4399.12) -- violates the discount cap specifically, not the
    # absolute floor.
    llm = ScriptedMerchantLLM([_merchant_decision("counter", price=4000.0, qty=1)])

    offer = _offer(3900.0, qty=1)
    result = evaluate_ai(offer, POLICY, round=1, negotiation_history=[], llm_call=llm)

    assert result["decision"] == "counter"
    assert result["offer"]["price"] == 4399.12
    assert result["guardrail_clamped"] is True
    assert "policy.max_discount_pct" in result["evidence_paths"]
    assert "4000" not in json.dumps(result)

    from src.agents.audit_logger import log_entry
    log_entry(
        "merchant-agent", result["decision"], result["offer"], result["rationale"],
        result["evidence_paths"], path=str(audit_path), guardrail_clamped=result["guardrail_clamped"],
    )
    raw_log = audit_path.read_text(encoding="utf-8")
    assert "4000" not in raw_log
    assert "4000.0" not in raw_log


def test_guardrail_counters_at_liquidation_relaxed_min_price_instead_of_rejecting(tmp_path):
    """Bug-fix regression test (Section 2E follow-up, 2026-09-01): a
    liquidation-relaxed min_price is a system-relaxed floor, not a
    merchant-set absolute one -- an offer below it should draw a COUNTER
    at the allowed floor, same as test_guardrail_clamp_when_proposal_
    violates_max_discount_pct, not an instant round-1 REJECT. Before the
    fix, check_guardrails() rejected unconditionally on offer.price <
    policy.min_price regardless of how min_price was derived.

    The allowed floor is still max(min_price, discount-computed floor)
    exactly as always -- here the discount-cap floor (7040.00) is HIGHER
    than the liquidation-relaxed min_price (4517.74), so the counter lands
    at 7040.00 and cites policy.max_discount_pct, not policy.min_price.
    Confirmed with the user against the real SKU-ELEC-003 numbers before
    implementing (see NEGOTIATION_SPEC.md Section 2F)."""
    audit_path = tmp_path / "negotiation.log"
    liquidation_policy = {
        **POLICY,
        "sku_id": "SKU-ELEC-003",
        "list_price": 8000.00,
        "min_price": 4517.74,  # liquidation-relaxed, well below the discount-cap floor
        "min_price_is_liquidation_relaxed": True,
        "max_discount_pct": 12,
        "qty_breaks": [],
    }
    # qty=1 floor (no qty_breaks tier applies) = max(4517.74, 8000*0.88) = 7040.00

    llm = ScriptedMerchantLLM([_merchant_decision("counter", price=5000.0, qty=1)])
    offer = _offer(3800.0, qty=1)  # below min_price entirely, same as the user's repro
    result = evaluate_ai(offer, liquidation_policy, round=1, negotiation_history=[], llm_call=llm)

    assert result["decision"] == "counter"
    assert result["offer"]["price"] == 7040.00
    assert result["guardrail_clamped"] is True
    assert "policy.max_discount_pct" in result["evidence_paths"]
    assert "policy.min_price" not in result["evidence_paths"]

    from src.agents.audit_logger import log_entry
    log_entry(
        "merchant-agent", result["decision"], result["offer"], result["rationale"],
        result["evidence_paths"], path=str(audit_path), guardrail_clamped=result["guardrail_clamped"],
    )
    raw_log = audit_path.read_text(encoding="utf-8")
    assert "5000" not in raw_log


def test_guardrail_evidence_path_names_min_price_when_it_dominates_after_liquidation_relaxation(tmp_path):
    """Bug-fix follow-up (flagged, not fixed, at the end of Section 2H;
    fixed 2026-09-01): _floor_price()'s evidence_path always cited the
    discount-tier evidence regardless of which term of
    max(min_price, discount-computed) actually won. Before Section 2F this
    was unreachable (a below-min_price offer was always caught by
    check_guardrails()'s earlier absolute-reject when min_price was
    binding); Section 2F's liquidation fall-through made it reachable for a
    liquidation-relaxed product whose relaxed min_price is STILL the
    binding term -- unlike SKU-ELEC-003 above, where the discount-cap floor
    dominates. The clamped PRICE was always correct; only the
    evidence_paths/rationale attribution was wrong, misnaming
    policy.max_discount_pct when policy.min_price actually drove the
    floor."""
    liquidation_policy = {
        **POLICY,
        "sku_id": "SKU-LIQ-MINPRICE-DOMINANT",
        "list_price": 2000.0,
        "min_price": 1950.0,  # relaxed, but still ABOVE the discount-derived floor (1900)
        "max_discount_pct": 5,
        "min_price_is_liquidation_relaxed": True,
        "qty_breaks": [],
    }
    # discount-derived floor = 2000 * (1 - 5/100) = 1900.0; min_price (1950) wins the max()

    offer = _offer(1000.0, qty=1)  # below min_price entirely, same shape as the user's repro
    result = check_guardrails(offer, liquidation_policy, round=1)

    assert result["decision"] == "counter"
    assert result["offer"]["price"] == 1950.0
    assert result["evidence_paths"] == ["policy.min_price"]


def test_guardrail_evidence_path_still_names_max_discount_pct_when_it_dominates(tmp_path):
    """Companion regression test to
    test_guardrail_evidence_path_names_min_price_when_it_dominates_after_liquidation_relaxation
    above (Section 2I fix, 2026-09-01): proves the fix didn't regress the
    ORIGINAL, already-correct case. When the discount-derived floor is the
    actual binding term (as in every Milestone 3a/3b test), evidence_path
    must still correctly cite policy.max_discount_pct, not policy.min_price.

    Reuses POLICY -- this file's module-level fixture, used throughout --
    as the existing "max_discount_pct is binding" case; no new product
    needed. min_price=3799, discount floor at qty=1 = 4999*0.88=4399.12,
    which is HIGHER than min_price, so max_discount_pct wins the max()
    here. test_guardrail_clamp_when_proposal_violates_max_discount_pct
    above already exercises this via evaluate_ai() (Layer 2's clamp
    branch); this test checks check_guardrails() directly instead,
    mirroring the min_price-dominant test's structure exactly for a clean
    side-by-side pair, and asserts evidence_paths by exact equality
    rather than membership."""
    offer = _offer(3900.0, qty=1)  # above min_price (3799), below the discount floor (4399.12)
    result = check_guardrails(offer, POLICY, round=1)

    assert result["decision"] == "counter"
    assert result["offer"]["price"] == 4399.12
    assert result["evidence_paths"] == ["policy.max_discount_pct"]


def test_guardrail_still_rejects_below_ordinary_non_liquidation_min_price(tmp_path):
    """Companion to the test above: an ordinary, merchant-set min_price
    (min_price_is_liquidation_relaxed absent/False) must still be an
    absolute, non-negotiable floor -- unchanged by the fix. This is the
    exact scenario test_guardrail_clamp_when_proposal_violates_min_price
    already covers for the counter-side; this test proves the reject-side
    default (False) still fires when the flag is simply absent, matching
    every Milestone 1-3c(pre-fix) policy dict in this test file."""
    assert "min_price_is_liquidation_relaxed" not in POLICY

    offer = _offer(3000.0, qty=1)  # below POLICY's ordinary min_price (3799)
    result = check_guardrails(offer, POLICY, round=1)

    assert result["decision"] == "reject"
    assert result["evidence_paths"] == ["policy.min_price"]


def test_liquidation_lowers_the_effective_floor_measurably_even_when_discount_cap_was_binding(tmp_path):
    """Structural-fix regression test (2026-09-02, Section 2J): live-data
    investigation found that liquidation_adjusted_min_price() (the old
    mechanism, relaxing min_price directly) had ZERO real effect on any
    of the 8 aged products in the actual seed=42 catalog -- min_price sits
    well below the discount-cap floor (max_discount_pct-derived) for 79/80
    generated products, so relaxing min_price never changed the OPERATIVE
    floor (max(min_price, discount-cap floor)). This is the "nearly every
    product in the catalog" case the fix targets: max_discount_pct is
    genuinely the pre-liquidation-binding constraint here (discount floor
    4680.00 >> min_price 1600.00), exactly like the vast majority of real
    products.

    Proves the fix using REAL per-round evaluate_ai() output within
    run_negotiation() -- not just liquidation_relaxation_fraction() called
    in isolation -- for two negotiations over IDENTICAL product economics,
    differing only in days_in_inventory (fresh vs. fully aged): the aged
    negotiation's guardrail-clamped counter must be measurably lower than
    the fresh one's, proving liquidation now has a real, non-zero effect
    even in the discount-cap-dominant case."""
    base_product = {
        "sku_id": "SKU-LIQ-DISCOUNT-DOMINANT", "product_name": "Test Cabinet", "currency": "INR",
        "category": "Home & Kitchen", "list_price": 5200.0, "cost": 1000.0, "min_price": 1600.0,
        "max_discount_pct": 10, "qty_breaks": [], "current_inventory": 50, "inventory_floor": 1,
    }
    # Home & Kitchen threshold = 320, ramp = 100 days -- fresh is nowhere
    # near it; aged is exactly threshold + ramp (fully ramped, per
    # liquidation_relaxation_fraction()'s own contract).
    fresh_product = {**base_product, "days_in_inventory": 50}
    aged_product = {**base_product, "days_in_inventory": 420}

    assert liquidation_relaxation_fraction(fresh_product) == 0.0
    assert liquidation_relaxation_fraction(aged_product) == 1.0

    fresh_policy = product_to_policy(fresh_product, max_negotiation_rounds=5, transaction_approval_threshold=20000)
    aged_policy = product_to_policy(aged_product, max_negotiation_rounds=5, transaction_approval_threshold=20000)
    # min_price itself is IDENTICAL and untouched by aging -- confirms this
    # fix no longer relaxes min_price at all, only the discount-cap floor.
    assert fresh_policy["min_price"] == aged_policy["min_price"] == 1600.0

    def _run(policy, audit_path):
        merchant_evaluate = partial(
            evaluate_ai, llm_call=RepeatingMerchantLLM(_merchant_decision("counter", price=1000.0, qty=1)),
        )
        buyer = BuyerAgent(qty=1, opening_discount_pct=50, max_acceptable_price=1500.0, list_price=policy["list_price"])
        run_negotiation(policy, buyer, audit_path=str(audit_path), merchant_evaluate=merchant_evaluate)
        entries = _read_log(audit_path)
        countered = [e for e in entries if e["agent"] == "merchant-agent" and e["action"] == "counter"]
        assert countered, "expected at least one guardrail-clamped counter to inspect"
        prices = {e["offer"]["price"] for e in countered}
        assert len(prices) == 1, f"every clamped counter should cite the same computed floor, got {prices}"
        return countered[0]

    fresh_audit = tmp_path / "fresh.log"
    aged_audit = tmp_path / "aged.log"
    fresh_entry = _run(fresh_policy, fresh_audit)
    aged_entry = _run(aged_policy, aged_audit)

    assert fresh_entry["offer"]["price"] == 4680.0  # discount-cap floor, untouched -- max_discount_pct genuinely binds
    assert fresh_entry["evidence_paths"] == ["policy.max_discount_pct"]

    assert aged_entry["offer"]["price"] == 1600.0  # fully ramped down to min_price -- a real, measurable drop
    assert aged_entry["offer"]["price"] < fresh_entry["offer"]["price"]
    assert aged_entry["evidence_paths"] == ["policy.min_price"]  # fully ramped -- min_price now the binding term

    raw_log = aged_audit.read_text(encoding="utf-8")
    assert "1000" not in raw_log  # Layer 2's invalid proposal never leaked through


def test_liquidation_partial_ramp_still_cites_max_discount_pct_while_measurably_lowering_the_floor():
    """Companion to the test above: PARTIAL aging (ramp fraction strictly
    between 0 and 1) must still lower the floor measurably below the fresh
    value, while the discount-cap floor remains the binding term (and
    correctly labeled as such) until the ramp fully reaches min_price --
    confirms the Section 2I evidence_path distinction still tracks which
    term actually wins now that liquidation can move the discount-cap
    term, not just leave it fixed."""
    product = {
        "sku_id": "SKU-LIQ-PARTIAL", "product_name": "Test Cabinet", "currency": "INR",
        "category": "Home & Kitchen", "list_price": 5200.0, "cost": 1000.0, "min_price": 1600.0,
        "max_discount_pct": 10, "qty_breaks": [], "current_inventory": 50, "inventory_floor": 1,
        "days_in_inventory": 370,  # threshold(320) + 50 -- halfway through the 100-day ramp
    }
    assert liquidation_relaxation_fraction(product) == 0.5

    policy = product_to_policy(product, max_negotiation_rounds=5, transaction_approval_threshold=20000)
    from src.agents.merchant_agent import _floor_price
    floor, evidence_path = _floor_price(policy, qty=1)

    assert floor == 3140.0  # 4680 - 0.5*(4680-1600) -- halfway from the discount floor toward min_price
    assert 1600.0 < floor < 4680.0  # measurably below fresh (4680), still above min_price (1600)
    assert evidence_path == "policy.max_discount_pct"  # discount-cap term still binds mid-ramp


def test_guardrail_clamp_via_full_negotiation_loop_never_leaks_invalid_value(tmp_path):
    """End-to-end version of the guardrail test: runs the invalid proposal
    through run_negotiation() (real audit log + real negotiation_history,
    not just a direct evaluate_ai() call) and checks both."""
    audit_path = tmp_path / "negotiation.log"
    # Merchant AI always proposes an invalid, below-min_price counter.
    merchant_evaluate = partial(evaluate_ai, llm_call=RepeatingMerchantLLM(_merchant_decision("counter", price=1234.56, qty=1)))
    buyer = BuyerAgent(qty=1, opening_discount_pct=15, max_acceptable_price=4600.0, list_price=POLICY["list_price"])

    outcome = run_negotiation(POLICY, buyer, audit_path=str(audit_path), merchant_evaluate=merchant_evaluate)

    assert outcome["state"] == "AGREEMENT_RECORDED"  # clamped counters are still valid, negotiable offers

    entries = _read_log(audit_path)
    merchant_entries = [e for e in entries if e["agent"] == "merchant-agent"]
    assert all(e["guardrail_clamped"] is True for e in merchant_entries)
    assert all(e["offer"]["price"] == 4399.12 for e in merchant_entries)  # always the clamped floor, never 1234.56

    raw_log = audit_path.read_text(encoding="utf-8")
    assert "1234.56" not in raw_log


def test_layer_2_accept_on_sub_floor_offer_is_overridden_not_passed_through():
    """Accepting a below-floor offer is exactly the class of violation this
    milestone exists to prevent, even though it's not literally a
    counter_offer price violation."""
    llm = ScriptedMerchantLLM([_merchant_decision("accept")])

    offer = _offer(3900.0, qty=1)  # below the qty=1 floor (4399.12)
    result = evaluate_ai(offer, POLICY, round=1, negotiation_history=[], llm_call=llm)

    assert result["decision"] != "accept"
    assert result["decision"] == "counter"
    assert result["guardrail_clamped"] is True


def test_decide_strategy_prompt_includes_negotiation_history_with_buyer_prior_offers():
    """Regression test for the 'merchant seems to ignore the buyer's
    concessions' bug report (2026-09-01): live reproduction (SKU-ELEC-003,
    BUYER_MODE=ai, MERCHANT_MODE=ai, BUYER_BUDGET=4800) confirmed Layer 2
    WAS being called every round (5 real Gemini calls, no silent
    rules-only fallback), and negotiation_history WAS already being
    embedded in decide_strategy()'s prompt every round, correctly growing
    to include the buyer's actual conceding offers (4200 -> 4350 -> 4500
    -> 4500 -> 4800). The flat 7098.68 counter and generic clamp rationale
    the bug report described were check_guardrails() correctly clamping
    to the same floor every round because that floor was above the
    buyer's entire budget range -- not a plumbing defect. This test locks
    down the actual contract so it can't silently regress: the prompt
    handed to the LLM must contain the buyer's PRIOR round offers, not
    just the current one in isolation."""
    negotiation_history = [
        {"agent": "buyer-agent", "action": "offer", "offer": _offer(4200.0, qty=3)},
        {"agent": "merchant-agent", "action": "counter", "offer": _offer(7098.68, qty=3)},
        {"agent": "buyer-agent", "action": "offer", "offer": _offer(4350.0, qty=3)},
        {"agent": "merchant-agent", "action": "counter", "offer": _offer(7098.68, qty=3)},
    ]
    llm = ScriptedMerchantLLM([_merchant_decision("counter", price=7098.68, qty=3)])
    current_offer = _offer(4500.0, qty=3)  # the current round's offer -- distinct from every history price

    decide_strategy(current_offer, POLICY, round=3, negotiation_history=negotiation_history, llm_call=llm)

    assert len(llm.calls) == 1
    prompt = llm.calls[0]["user_content"]
    # The buyer's two PRIOR conceding offers must reach the LLM, not just
    # the current-round offer -- this is what "has nothing to react to"
    # would look like if it broke.
    assert "4200" in prompt
    assert "4350" in prompt
    assert "4500" in prompt  # the current offer is still present too


def test_decide_strategy_prompt_omits_history_section_content_on_first_round():
    """Companion sanity check: round 1 has no prior rounds yet, so the
    prompt should say so explicitly rather than embedding an empty list
    silently -- confirms the "No prior rounds." fallback text
    (_build_merchant_prompt) is reachable and distinguishable from a
    history that failed to populate."""
    llm = ScriptedMerchantLLM([_merchant_decision("counter", price=4450.0, qty=1)])
    offer = _offer(4000.0, qty=1)

    decide_strategy(offer, POLICY, round=1, negotiation_history=[], llm_call=llm)

    prompt = llm.calls[0]["user_content"]
    assert "No prior rounds." in prompt


# ---------------------------------------------------------------------------
# Valid proposal passes through unclamped.
# ---------------------------------------------------------------------------


def test_valid_in_bounds_proposal_passes_through_unclamped():
    llm = ScriptedMerchantLLM([_merchant_decision("counter", price=4450.0, qty=1, reasoning="Holding slightly above the floor.")])

    offer = _offer(4000.0, qty=1)
    result = evaluate_ai(offer, POLICY, round=1, negotiation_history=[], llm_call=llm)

    assert result["decision"] == "counter"
    assert result["offer"]["price"] == 4450.0  # exactly what Layer 2 decided, unmodified
    assert result["guardrail_clamped"] is False
    assert result["rationale"] == "Holding slightly above the floor."


# ---------------------------------------------------------------------------
# Round-cap enforcement still works with the AI merchant.
# ---------------------------------------------------------------------------


def test_round_cap_enforcement_still_works_with_ai_merchant(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    # Merchant AI always proposes a valid, unclamped counter at the floor;
    # buyer's ceiling never reaches it, so round-limit must force REJECT
    # regardless of what Layer 2 keeps proposing.
    merchant_evaluate = partial(evaluate_ai, llm_call=RepeatingMerchantLLM(_merchant_decision("counter", price=4399.12, qty=1)))
    buyer = BuyerAgent(qty=1, opening_discount_pct=15, max_acceptable_price=4000.0, list_price=POLICY["list_price"])

    outcome = run_negotiation(POLICY, buyer, audit_path=str(audit_path), merchant_evaluate=merchant_evaluate)

    assert outcome["state"] == "REJECTED"
    entries = _read_log(audit_path)
    merchant_entries = [e for e in entries if e["agent"] == "merchant-agent"]
    assert len(merchant_entries) == POLICY["max_negotiation_rounds"]
    assert merchant_entries[-1]["action"] == "reject"
    # The final round overrides Layer 2's "counter" -> that's a clamp;
    # every earlier round's valid, unclamped counter is not.
    assert merchant_entries[-1]["guardrail_clamped"] is True
    assert all(e["guardrail_clamped"] is False for e in merchant_entries[:-1])


# ---------------------------------------------------------------------------
# Gemini failure falls back to rules-only, does not crash the negotiation.
# ---------------------------------------------------------------------------


def test_gemini_failure_falls_back_to_rules_only_without_crashing():
    llm = AlwaysFailingLLM()
    offer = _offer(4000.0, qty=1)

    result = evaluate_ai(offer, POLICY, round=1, negotiation_history=[], llm_call=llm, max_retries=1, sleep_fn=lambda s: None)

    assert result["decision"] == "counter"
    assert result["offer"]["price"] == 4399.12  # identical to what check_guardrails() alone would produce
    assert result["guardrail_clamped"] is False  # nothing was clamped -- Layer 2 simply didn't run


def test_gemini_failure_end_to_end_negotiation_still_completes(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    merchant_evaluate = partial(evaluate_ai, llm_call=AlwaysFailingLLM(), max_retries=1, sleep_fn=lambda s: None)
    buyer = BuyerAgent(qty=1, opening_discount_pct=15, max_acceptable_price=4450.0, list_price=POLICY["list_price"])

    outcome = run_negotiation(POLICY, buyer, audit_path=str(audit_path), merchant_evaluate=merchant_evaluate)

    assert outcome["state"] == "AGREEMENT_RECORDED"  # negotiation completed normally, rules-only throughout
    entries = _read_log(audit_path)
    merchant_entries = [e for e in entries if e["agent"] == "merchant-agent"]
    assert all("fell back to rules-only guardrails" in e["rationale"] for e in merchant_entries)


# ---------------------------------------------------------------------------
# AI buyer AND AI merchant both active.
# ---------------------------------------------------------------------------


def test_ai_buyer_and_ai_merchant_together_converges_cleanly(tmp_path):
    audit_path = tmp_path / "negotiation.log"

    def buyer_decision(price):
        return {
            "target_price": 4200.0,
            "walk_away_price": 4600.0,
            "strategy_note": "Conceding toward the merchant's counter.",
            "offer": {"price": price, "qty": 1, "terms": ""},
        }

    buyer_llm_calls = [buyer_decision(4000.0), buyer_decision(4150.0), buyer_decision(4399.12)]

    class BuyerLLM:
        def __init__(self, decisions):
            self._decisions = list(decisions)

        def __call__(self, system, user_content, model):
            return self._decisions.pop(0)

    buyer = AIBuyerAgent(qty=1, persona=PERSONA, list_price=POLICY["list_price"], llm_call=BuyerLLM(buyer_llm_calls))
    merchant_evaluate = partial(
        evaluate_ai, llm_call=RepeatingMerchantLLM(_merchant_decision("counter", price=4399.12, qty=1)),
    )

    outcome = run_negotiation(POLICY, buyer, audit_path=str(audit_path), merchant_evaluate=merchant_evaluate)

    assert outcome["state"] in ("AGREEMENT_RECORDED", "REJECTED", "BUYER_UNAVAILABLE")
    assert outcome["state"] == "AGREEMENT_RECORDED"

    entries = _read_log(audit_path)
    assert any(e["action"] == "buyer_strategy" for e in entries)  # buyer's private reasoning logged
    assert any("guardrail_clamped" in e for e in entries if e["agent"] == "merchant-agent")  # merchant's clamp field logged


# ---------------------------------------------------------------------------
# Live integration test, same marker as the buyer-agent.
# ---------------------------------------------------------------------------


@pytest.mark.live_llm
def test_live_ai_merchant_returns_valid_guardrail_checked_decision():
    """Sanity check against the real Gemini API. Skipped by default -- run
    with `pytest --run-live-llm`. Needs GEMINI_API_KEY in the environment."""
    offer = _offer(3900.0, qty=1)  # deliberately below the qty=1 floor (4399.12)

    result = evaluate_ai(offer, POLICY, round=1, negotiation_history=[])

    assert result["decision"] in ("accept", "counter", "reject")
    assert result["offer"]["price"] >= POLICY["min_price"]
    if result["decision"] == "counter":
        floor = 4399.12
        assert result["offer"]["price"] >= floor - 0.01  # never below the real floor, real model or not
    assert isinstance(result["guardrail_clamped"], bool)


@pytest.mark.live_llm
def test_live_ai_merchant_mostly_avoids_clamps_in_a_straightforward_negotiation(tmp_path):
    """Regression check for the prompt fix: _build_merchant_prompt() now
    hands the LLM the exact, already-computed valid price range for this
    offer's qty instead of making it derive the floor itself from
    min_price/max_discount_pct/qty_breaks. Before that fix, Layer 2's
    proposals were being guardrail-clamped almost every round. Skipped by
    default -- run with `pytest --run-live-llm`. Needs GEMINI_API_KEY."""
    audit_path = tmp_path / "negotiation.log"
    # Buyer never reaches the floor (ceiling 4000 < floor 4399.12), so this
    # runs the full max_negotiation_rounds (5) -- giving Layer 2 several
    # real rounds to propose in. The final round is always a hard
    # guardrail override by design (round-limit), independent of prompt
    # quality, so majority-unbounds only requires most of the OTHER rounds
    # to land in-bounds.
    buyer = BuyerAgent(qty=1, opening_discount_pct=15, max_acceptable_price=4000.0, list_price=POLICY["list_price"])
    merchant_evaluate = partial(evaluate_ai, sleep_fn=lambda seconds: None)

    outcome = run_negotiation(POLICY, buyer, audit_path=str(audit_path), merchant_evaluate=merchant_evaluate)

    assert outcome["state"] == "REJECTED"  # round-limit, per the setup above
    entries = _read_log(audit_path)
    merchant_entries = [e for e in entries if e["agent"] == "merchant-agent"]
    assert len(merchant_entries) == POLICY["max_negotiation_rounds"]

    unclamped = sum(1 for e in merchant_entries if e["guardrail_clamped"] is False)
    assert unclamped > len(merchant_entries) / 2, (
        f"only {unclamped}/{len(merchant_entries)} of Layer 2's proposals landed in-bounds -- "
        "the merchant prompt may not be giving the LLM the computed floor clearly enough"
    )
