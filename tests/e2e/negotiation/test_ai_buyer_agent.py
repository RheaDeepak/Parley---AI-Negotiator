import json

import pytest
from pydantic import ValidationError

from src.agents.ai_buyer_agent import AIBuyerAgent, BuyerUnavailableError, TransientLLMError
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
}

PERSONA = {
    "budget": 4600.0,
    "target_product": "Wireless Mechanical Keyboard",
    "willingness_to_negotiate": "moderate -- open to a fair discount but not desperate",
}


class ScriptedLLM:
    """Injectable llm_call stub: returns pre-programmed raw dicts in order."""

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.calls = []

    def __call__(self, system, user_content, model):
        self.calls.append({"system": system, "user_content": user_content, "model": model})
        return self._decisions.pop(0)


def _decision(price, target_price=4200.0, walk_away_price=4600.0, note="Conceding toward the merchant's counter."):
    return {
        "target_price": target_price,
        "walk_away_price": walk_away_price,
        "strategy_note": note,
        "offer": {"price": price, "qty": 1, "terms": ""},
    }


class FlakyLLM:
    """Injectable llm_call stub that raises `error_cls` for the first
    `fail_times` calls, then returns `decision_after`."""

    def __init__(self, fail_times, decision_after=None, error_cls=TransientLLMError):
        self.fail_times = fail_times
        self.decision_after = decision_after
        self.error_cls = error_cls
        self.calls = 0

    def __call__(self, system, user_content, model):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error_cls("simulated Gemini rate limit / 429")
        return self.decision_after


def _read_log(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_opening_offer_prompt_shows_list_price_and_persona_appropriate_discount_guidance():
    """Section 2AD (2026-09-05, replacing Section 2AC's MRP anchor,
    reverted the same day): the buyer's prompt shows the product's real
    `list_price` -- safe to reveal, it's the advertised price, not a
    guardrail-derived number -- and instructs the model to reason out its
    OWN opening offer as a persona-appropriate discount off it, rather
    than requiring any externally-supplied starting price. The effective
    floor is never computed or mentioned here at all."""
    llm = ScriptedLLM([_decision(4200.0)])
    buyer = AIBuyerAgent(qty=1, persona=PERSONA, list_price=POLICY["list_price"], llm_call=llm)

    buyer.initial_offer()

    prompt = llm.calls[0]["user_content"]
    assert f"Product list price: {POLICY['list_price']}" in prompt
    assert "choose them carefully from the product's list price" in prompt
    assert "reasoned discount off list_price" in prompt
    assert "persona's own negotiating style" in prompt
    assert "MRP" not in prompt


def test_structured_output_parses_into_valid_offer_schema():
    llm = ScriptedLLM([_decision(4000.0)])
    buyer = AIBuyerAgent(qty=1, persona=PERSONA, list_price=POLICY["list_price"], llm_call=llm)

    offer = buyer.initial_offer()

    assert set(offer.keys()) == {"offer_id", "price", "qty", "terms", "expiration", "timestamp"}
    assert offer["price"] == 4000.0
    assert offer["qty"] == 1
    assert isinstance(offer["offer_id"], str) and offer["offer_id"]


def test_malformed_llm_output_raises_instead_of_silently_producing_bad_offer():
    bad_llm = ScriptedLLM([{"target_price": 4200.0, "strategy_note": "missing fields"}])
    buyer = AIBuyerAgent(qty=1, persona=PERSONA, list_price=POLICY["list_price"], llm_call=bad_llm)

    with pytest.raises(ValidationError):
        buyer.initial_offer()


def test_offer_never_exceeds_walk_away_price_even_if_model_returns_higher():
    llm = ScriptedLLM([_decision(price=5000.0, walk_away_price=4600.0)])
    buyer = AIBuyerAgent(qty=1, persona=PERSONA, list_price=POLICY["list_price"], llm_call=llm)

    offer = buyer.initial_offer()

    assert offer["price"] <= 4600.0


def test_target_price_and_walk_away_price_stay_pinned_across_rounds(tmp_path):
    """Each mocked call returns DIFFERENT target_price/walk_away_price --
    if pinning weren't enforced in code, these would leak through round to
    round. Confirms the CODE (not the model) is what keeps them fixed."""
    audit_path = tmp_path / "negotiation.log"
    llm = ScriptedLLM([
        _decision(4000.0, target_price=4200.0, walk_away_price=4600.0),
        _decision(4150.0, target_price=4300.0, walk_away_price=4650.0),  # different on purpose
        _decision(4399.12, target_price=4100.0, walk_away_price=4700.0),  # different again
    ])
    buyer = AIBuyerAgent(qty=1, persona=PERSONA, list_price=POLICY["list_price"], llm_call=llm)

    outcome = run_negotiation(POLICY, buyer, audit_path=str(audit_path))

    assert outcome["state"] == "AGREEMENT_RECORDED"
    assert len(llm.calls) == 3

    entries = _read_log(audit_path)
    strategy_entries = [e for e in entries if e["action"] == "buyer_strategy"]
    assert len(strategy_entries) == 3

    # Every round's logged private reasoning must show the FIRST call's
    # values, never round 2's or round 3's divergent ones.
    for entry in strategy_entries:
        assert "target_price=4200.0" in entry["rationale"]
        assert "walk_away_price=4600.0" in entry["rationale"]
    assert not any("target_price=4300.0" in e["rationale"] for e in strategy_entries)
    assert not any("target_price=4100.0" in e["rationale"] for e in strategy_entries)
    assert not any("walk_away_price=4650.0" in e["rationale"] for e in strategy_entries)
    assert not any("walk_away_price=4700.0" in e["rationale"] for e in strategy_entries)


def test_buyer_concedes_fully_in_final_rounds_when_counter_within_walk_away_price():
    """Round 4 of 5 (the final stretch) -- the merchant's counter
    (4399.12) is within the buyer's walk_away_price (4600.0). The prompt
    must tell the buyer it's in the final stretch and to concede fully
    rather than under-conceding again; given a mocked model that complies,
    the buyer must actually converge (accept) instead of proposing yet
    another small incremental move."""
    llm = ScriptedLLM([
        _decision(4000.0, target_price=4200.0, walk_away_price=4600.0),  # round 1
        _decision(4100.0, target_price=4200.0, walk_away_price=4600.0),  # round 2: small concession
        _decision(4150.0, target_price=4200.0, walk_away_price=4600.0),  # round 3: small concession
        _decision(4399.12, target_price=4200.0, walk_away_price=4600.0),  # round 4: full concession
    ])
    buyer = AIBuyerAgent(qty=1, persona=PERSONA, list_price=POLICY["list_price"], llm_call=llm, max_negotiation_rounds=5)

    buyer.initial_offer()  # round 1
    buyer.respond_to_counter({"price": 4399.12, "qty": 1})  # round 2
    buyer.respond_to_counter({"price": 4399.12, "qty": 1})  # round 3
    response = buyer.respond_to_counter({"price": 4399.12, "qty": 1})  # round 4 -- final stretch

    # The round-4 prompt must actually tell the model it's in the final
    # stretch and to concede fully -- not just hope the behavior emerges.
    round4_prompt = llm.calls[3]["user_content"]
    assert "round 4 of a maximum 5" in round4_prompt
    assert "CONCEDE FULLY" in round4_prompt

    # Given the mocked model complied (offered exactly the merchant's
    # price), the buyer must treat this as convergence -- not propose
    # another incremental counter and risk running out of rounds.
    assert response["accept"] is True
    assert response["offer"] is None


def test_concession_behavior_moves_monotonically_toward_merchant(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    # qty=1 floor is 4399.12 (see NEGOTIATION_SPEC.md Section 1 example).
    llm = ScriptedLLM([
        _decision(4000.0),    # opening offer, below floor
        _decision(4150.0),    # concedes toward the merchant's counter
        _decision(4399.12),   # meets the merchant's counter -> accepted
    ])
    buyer = AIBuyerAgent(qty=1, persona=PERSONA, list_price=POLICY["list_price"], llm_call=llm)

    outcome = run_negotiation(POLICY, buyer, audit_path=str(audit_path))

    assert outcome["state"] == "AGREEMENT_RECORDED"
    assert len(llm.calls) == 3

    entries = _read_log(audit_path)
    buyer_prices = [e["offer"]["price"] for e in entries if e["agent"] == "buyer-agent" and e["action"] in ("offer", "accept")]

    assert buyer_prices == sorted(buyer_prices)
    assert len(set(buyer_prices)) == len(buyer_prices)  # no repeated price


def test_ai_buyer_negotiation_terminates_at_round_cap_when_never_converging(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    # Every offer stays below the qty=1 floor (4399.12) and below its own
    # walk_away_price (4200) -- convergence is impossible, so the loop
    # must exhaust max_negotiation_rounds. Section 2X: this no longer
    # means REJECTED outright -- run_negotiation() ALWAYS pauses as
    # ROUND_LIMIT_REACHED when a merchant counter exists (one does here,
    # every round before the final reject), carrying that counter as the
    # offer for the human/buyer to decide on.
    llm = ScriptedLLM([
        _decision(4000.0, walk_away_price=4200.0),
        _decision(4050.0, walk_away_price=4200.0),
        _decision(4100.0, walk_away_price=4200.0),
        _decision(4150.0, walk_away_price=4200.0),
        _decision(4200.0, walk_away_price=4200.0),
    ])
    buyer = AIBuyerAgent(qty=1, persona=PERSONA, list_price=POLICY["list_price"], llm_call=llm)

    outcome = run_negotiation(POLICY, buyer, audit_path=str(audit_path))

    assert outcome["state"] == "ROUND_LIMIT_REACHED"
    assert outcome["offer"] is not None  # the merchant's last real counter-offer, not fabricated
    entries = _read_log(audit_path)
    # Per-round guardrail decisions only -- excludes the "round_limit_reached"
    # pause marker itself (also agent="merchant-agent", but not a round
    # decision; see run_negotiation()'s Section 2X handling).
    merchant_entries = [e for e in entries if e["agent"] == "merchant-agent" and e["action"] in ("accept", "reject", "counter")]
    assert len(merchant_entries) == POLICY["max_negotiation_rounds"]
    assert merchant_entries[-1]["action"] == "reject"
    assert "policy.max_negotiation_rounds" in merchant_entries[-1]["evidence_paths"]


def test_strategy_note_and_target_price_never_appear_in_offer_entries(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    llm = ScriptedLLM([
        _decision(4000.0),
        _decision(4150.0),
        _decision(4399.12),
    ])
    buyer = AIBuyerAgent(qty=1, persona=PERSONA, list_price=POLICY["list_price"], llm_call=llm)

    run_negotiation(POLICY, buyer, audit_path=str(audit_path))

    entries = _read_log(audit_path)
    offer_bearing_entries = [e for e in entries if e["action"] in ("offer", "counter", "accept")]
    assert len(offer_bearing_entries) > 0
    for e in offer_bearing_entries:
        if e["offer"] is None:
            continue
        assert "target_price" not in e["offer"]
        assert "walk_away_price" not in e["offer"]
        assert "strategy_note" not in e["offer"]

    strategy_entries = [e for e in entries if e["action"] == "buyer_strategy"]
    assert len(strategy_entries) == 3  # one per AI-buyer decision (initial + 2 responses)
    for e in strategy_entries:
        assert e["offer"] is None
        assert "target_price" in e["rationale"]
        assert "walk_away_price" in e["rationale"]


def test_scripted_buyer_never_logs_buyer_strategy_entries(tmp_path):
    from src.agents.buyer_agent import BuyerAgent

    audit_path = tmp_path / "negotiation.log"
    buyer = BuyerAgent(qty=1, opening_discount_pct=15, max_acceptable_price=4450.0, list_price=POLICY["list_price"])

    run_negotiation(POLICY, buyer, audit_path=str(audit_path))

    entries = _read_log(audit_path)
    assert not any(e["action"] == "buyer_strategy" for e in entries)


def test_transient_error_is_retried_then_succeeds():
    sleeps = []
    llm = FlakyLLM(fail_times=2, decision_after=_decision(4000.0))
    buyer = AIBuyerAgent(
        qty=1, persona=PERSONA, list_price=POLICY["list_price"],
        llm_call=llm, max_retries=3, sleep_fn=sleeps.append,
    )

    offer = buyer.initial_offer()

    assert offer["price"] == 4000.0
    assert llm.calls == 3  # 2 simulated rate-limit failures + 1 success
    assert len(sleeps) == 2  # backed off before each retry, not before the final success


def test_transient_error_exhausting_retries_raises_buyer_unavailable():
    llm = FlakyLLM(fail_times=99, error_cls=TransientLLMError)  # never succeeds
    buyer = AIBuyerAgent(
        qty=1, persona=PERSONA, list_price=POLICY["list_price"],
        llm_call=llm, max_retries=2, sleep_fn=lambda seconds: None,
    )

    with pytest.raises(BuyerUnavailableError):
        buyer.initial_offer()

    assert llm.calls == 3  # 1 initial attempt + 2 retries, then gives up


def test_negotiation_handles_gemini_unavailability_gracefully_not_a_crash(tmp_path):
    audit_path = tmp_path / "negotiation.log"
    llm = FlakyLLM(fail_times=99, error_cls=TransientLLMError)
    buyer = AIBuyerAgent(
        qty=1, persona=PERSONA, list_price=POLICY["list_price"],
        llm_call=llm, max_retries=1, sleep_fn=lambda seconds: None,
    )

    outcome = run_negotiation(POLICY, buyer, audit_path=str(audit_path))

    assert outcome["state"] == "BUYER_UNAVAILABLE"
    assert outcome["offer"] is None

    entries = _read_log(audit_path)
    assert len(entries) == 1
    assert entries[0]["agent"] == "buyer-agent"
    assert entries[0]["action"] == "buyer_unavailable"


def test_non_transient_error_is_not_retried_or_swallowed():
    class ConfigurationBug(Exception):
        pass

    def raiser(system, user_content, model):
        raise ConfigurationBug("400 bad request -- not a rate limit, should not be retried")

    buyer = AIBuyerAgent(
        qty=1, persona=PERSONA, list_price=POLICY["list_price"],
        llm_call=raiser, max_retries=3, sleep_fn=lambda seconds: None,
    )

    with pytest.raises(ConfigurationBug):
        buyer.initial_offer()


@pytest.mark.live_llm
def test_live_ai_buyer_returns_valid_structured_offer():
    """Sanity check against the real Gemini API. Skipped by default --
    run with `pytest --run-live-llm`. Needs GEMINI_API_KEY in the
    environment."""
    buyer = AIBuyerAgent(qty=1, persona=PERSONA, list_price=POLICY["list_price"], currency=POLICY["currency"])

    offer = buyer.initial_offer()

    assert set(offer.keys()) == {"offer_id", "price", "qty", "terms", "expiration", "timestamp"}
    assert isinstance(offer["price"], float)
    assert buyer.last_strategy is not None
    assert isinstance(buyer.last_strategy["target_price"], float)
    assert isinstance(buyer.last_strategy["walk_away_price"], float)
    assert isinstance(buyer.last_strategy["strategy_note"], str) and buyer.last_strategy["strategy_note"]
    assert offer["price"] <= buyer.last_strategy["walk_away_price"]
