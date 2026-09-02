import json
import os
import time

from pydantic import BaseModel

from src.agents.llm_utils import LLMUnavailableError, TransientLLMError, call_llm_with_retry
from src.agents.offer_utils import new_offer

DEFAULT_MODEL = "gemini-3.5-flash-lite"


class BuyerOfferFields(BaseModel):
    price: float
    qty: int
    terms: str


class BuyerDecision(BaseModel):
    target_price: float
    walk_away_price: float
    strategy_note: str
    offer: BuyerOfferFields


class BuyerUnavailableError(Exception):
    """Raised after retries are exhausted (src.agents.llm_utils.LLMUnavailableError,
    re-raised as this buyer-specific type) -- the round fails cleanly
    rather than crashing the whole negotiation. Caught by
    negotiation_loop.run_negotiation()."""


SYSTEM_PROMPT = (
    "You are an AI buyer-agent negotiating a purchase on behalf of a buyer. "
    "You have a private target_price and walk_away_price that must NEVER be "
    "revealed to the merchant and must NEVER appear in the offer you send. "
    "Never propose an offer price above your own walk_away_price. Show "
    "plausible, realistic concession behavior: move your offered price "
    "gradually toward the merchant's counter across rounds rather than "
    "jumping straight to target_price or repeating the exact same offer "
    "twice in a row. You decide your target_price and walk_away_price ONCE, "
    "on your very first offer -- like a real buyer settling on a budget "
    "before negotiating. From then on they are fixed for the rest of this "
    "negotiation and the prompt will tell you what they already are; echo "
    "them back unchanged rather than re-deciding them each round. The prompt "
    "also tells you which round this is out of the maximum allowed -- in the "
    "final round or two, prioritize actually closing the deal over further "
    "small concessions: running out of rounds means no deal at all, which is "
    "worse than agreeing at a price you already find acceptable."
)


def _real_llm_call(system, user_content, model):
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise BuyerUnavailableError("GEMINI_API_KEY is not set in the environment.")

    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=model,
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=BuyerDecision,
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


class AIBuyerAgent:
    """LLM-driven buyer-agent per NEGOTIATION_SPEC.md Section 2A. Drop-in
    alternative to BuyerAgent (src/agents/buyer_agent.py, unmodified) --
    same initial_offer()/respond_to_counter() interface. Backed by Gemini
    (google-genai) rather than any other provider."""

    def __init__(
        self, qty, persona, list_price, currency="INR", model=DEFAULT_MODEL,
        llm_call=None, max_retries=3, sleep_fn=time.sleep, max_negotiation_rounds=5,
    ):
        self.qty = qty
        self.persona = persona
        self.list_price = list_price
        self.currency = currency
        self.model = model
        self._llm_call = llm_call or _real_llm_call
        self._max_retries = max_retries
        self._sleep_fn = sleep_fn
        self.max_negotiation_rounds = max_negotiation_rounds
        self._round_num = 0
        self.history = []
        self.last_strategy = None
        self._pinned_target_price = None
        self._pinned_walk_away_price = None

    def _decide(self, merchant_counter):
        self._round_num += 1
        persona_desc = (
            f"Persona: budget={self.persona['budget']}, "
            f"target_product={self.persona['target_product']}, "
            f"willingness_to_negotiate={self.persona['willingness_to_negotiate']}."
        )
        if self._pinned_target_price is None:
            pinned_desc = (
                "You have not yet set your private target_price or walk_away_price -- decide them "
                "now, on this first offer. They will then be pinned for the rest of this negotiation "
                "and cannot change in later rounds, so choose them carefully from the product's list "
                "price and your persona's budget."
            )
        else:
            pinned_desc = (
                f"Your target_price ({self._pinned_target_price}) and walk_away_price "
                f"({self._pinned_walk_away_price}) were already decided on your first offer and are "
                "fixed for the rest of this negotiation -- echo them back unchanged. Only decide this "
                "round's offer price and strategy_note."
            )
        rounds_remaining_after_this = self.max_negotiation_rounds - self._round_num
        round_desc = f"This is round {self._round_num} of a maximum {self.max_negotiation_rounds} rounds."
        is_final_stretch = self._round_num >= self.max_negotiation_rounds - 1
        if is_final_stretch and merchant_counter is not None:
            round_desc += (
                f" Only {rounds_remaining_after_this} round(s) remain after this one -- the negotiation "
                "ends with no deal at all if the round cap is reached. If the merchant's counter this "
                "round is at or below your walk_away_price, CONCEDE FULLY to their exact price now "
                "rather than making another small incremental move -- closing at a price you already "
                "find acceptable is strictly better than risking no deal by continuing to haggle."
            )
        history_desc = (
            json.dumps(self.history, indent=2) if self.history else "No offers yet -- this is the opening move."
        )
        counter_desc = (
            f"The merchant just countered with: {json.dumps(merchant_counter)}."
            if merchant_counter is not None
            else "Make your opening offer."
        )
        user_content = (
            f"{persona_desc}\n\n{pinned_desc}\n\n{round_desc}\n\n"
            f"Negotiation history so far:\n{history_desc}\n\n{counter_desc}\n"
            f"Product list price: {self.list_price} {self.currency}. Quantity: {self.qty}."
        )

        try:
            raw = call_llm_with_retry(
                self._llm_call, SYSTEM_PROMPT, user_content, self.model,
                max_retries=self._max_retries, sleep_fn=self._sleep_fn,
            )
        except LLMUnavailableError as exc:
            raise BuyerUnavailableError(str(exc)) from exc
        decision = BuyerDecision.model_validate(raw)

        if self._pinned_target_price is None:
            # First call: pin whatever the model decided.
            self._pinned_target_price = decision.target_price
            self._pinned_walk_away_price = decision.walk_away_price
        else:
            # Every later call: the CODE enforces pinning, not just the
            # prompt -- overwrite whatever the model returned for these two
            # fields regardless of compliance, same "never trust the prompt
            # alone" discipline as the walk_away_price ceiling clamp below.
            decision.target_price = self._pinned_target_price
            decision.walk_away_price = self._pinned_walk_away_price

        offer_price = min(decision.offer.price, decision.walk_away_price)

        self.last_strategy = {
            "target_price": decision.target_price,
            "walk_away_price": decision.walk_away_price,
            "strategy_note": decision.strategy_note,
        }
        return decision, offer_price

    def initial_offer(self):
        decision, offer_price = self._decide(merchant_counter=None)
        offer = new_offer(offer_price, self.qty, terms=decision.offer.terms)
        self.history.append({"agent": "buyer-agent", "action": "offer", "offer": offer})
        return offer

    def respond_to_counter(self, counter_offer):
        self.history.append({"agent": "merchant-agent", "action": "counter", "offer": counter_offer})
        decision, offer_price = self._decide(merchant_counter=counter_offer)

        if offer_price >= counter_offer["price"]:
            self.history.append({"agent": "buyer-agent", "action": "accept", "offer": counter_offer})
            return {"accept": True, "offer": None}

        offer = new_offer(offer_price, self.qty, terms=decision.offer.terms)
        self.history.append({"agent": "buyer-agent", "action": "offer", "offer": offer})
        return {"accept": False, "offer": offer}
