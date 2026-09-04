from src.agents.offer_utils import new_offer


class BuyerAgent:
    """Scripted, non-AI buyer-agent for driving/testing the merchant-agent.
    Opens with an aggressive discount ask and splits the difference toward
    the merchant's counter each round, never exceeding its own price
    ceiling (max_acceptable_price)."""

    def __init__(self, qty, opening_discount_pct, max_acceptable_price, list_price, starting_price=None):
        """`starting_price` (Milestone 8, optional): a literal opening
        offer price, used as-is instead of the computed
        list_price*(1-discount%) value -- for a caller (src/api.py) whose
        UI collects the buyer's actual opening number directly rather
        than a discount percentage. None (every pre-Milestone-8 caller)
        preserves the original computed-from-discount behavior exactly."""
        self.qty = qty
        self.max_acceptable_price = max_acceptable_price
        if starting_price is not None:
            self._current_price = round(starting_price, 2)
        else:
            self._current_price = round(list_price * (1 - opening_discount_pct / 100), 2)

    def initial_offer(self):
        return new_offer(self._current_price, self.qty)

    def respond_to_counter(self, counter_offer):
        """Returns {"accept": bool, "offer": dict | None}."""
        counter_price = counter_offer["price"]
        if counter_price <= self.max_acceptable_price:
            return {"accept": True, "offer": None}

        next_price = min(round((self._current_price + counter_price) / 2, 2), self.max_acceptable_price)
        self._current_price = next_price
        return {"accept": False, "offer": new_offer(next_price, self.qty)}
