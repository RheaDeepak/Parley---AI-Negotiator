"""Milestone 3c: synthetic-data-backed personalization layer.

Pure, deterministic, no LLM involved anywhere in this module -- same
philosophy as merchant_agent.check_guardrails(). The LTV-to-discount-bonus
function and the inventory fulfillment check are both plain functions over
JSON data; the only thing that changes is what values feed into the
existing (unmodified) guardrail logic in merchant_agent.py.
"""
import json
from pathlib import Path

DEFAULT_CATALOG_PATH = "data/catalog.json"
DEFAULT_BUYERS_PATH = "data/buyers.json"
DEFAULT_ORDERS_PATH = "data/orders.json"

# Absolute hard ceiling: no LTV tier, however high, can push the effective
# discount past this -- confirmed with the user (Milestone 3c).
HARD_DISCOUNT_CEILING_PCT = 30

# (ltv_min_inclusive, ltv_max_exclusive, bonus_pct) -- confirmed with the
# user as the "finer-grained, 4 tiers" option.
LTV_DISCOUNT_TIERS = (
    (0, 5000, 0),
    (5000, 20000, 2),
    (20000, 50000, 5),
    (50000, float("inf"), 8),
)

# The ultimate, non-negotiable price floor: cost + a 2% minimum margin --
# confirmed with the user (their own example). Takes priority over
# min_price, max_discount_pct, and the LTV bonus combined; nothing --
# loyalty, liquidation relaxation, or discount stacking -- can push the
# effective price below this.
COST_MARGIN_MULTIPLIER = 1.02

# Liquidation: the discount-cap floor (the price max_discount_pct/
# qty_breaks would otherwise permit) starts relaxing toward min_price --
# the true, unrelaxed margin floor -- once a product has sat for more than
# its CATEGORY's threshold. Category-specific, not a single flat number,
# because "aged" means abnormal relative to that category's normal
# turnover, not a fixed depreciation clock. Fast-turnover categories
# (Electronics) get a shorter threshold; slow/long-tail categories
# (Books & Media) get a longer one, since it's normal for them to sit a
# while. The user's explicit calibration, anchored at Electronics=180 and
# Books & Media=380 (past their "365+" anchor) -- confirmed as a full list
# before any code changed.
#
# Structural fix (2026-09-02, live-data investigation): liquidation
# originally relaxed min_price itself (see git history / NEGOTIATION_SPEC.md
# Section 2E's first draft), on the theory that min_price was "the" floor.
# In practice min_price = cost * 1.15 sits well below the discount-cap
# floor (list_price * (1 - max_discount_pct/100)) for the huge majority of
# generated products (79/80 in the seed=42 catalog) -- so relaxing
# min_price never actually lowered the OPERATIVE floor
# (max(min_price, discount-cap floor)) for real negotiations; confirmed
# empirically across all 8 aged outliers in the live catalog, every one
# unaffected. min_price is already close to the absolute floor and rarely
# the binding constraint day to day -- the thing that actually needs to
# relax for liquidation to have any real effect is the discount-cap floor
# itself. See liquidation_relaxation_fraction() and
# merchant_agent._floor_price() below.
CATEGORY_LIQUIDATION_THRESHOLDS = {
    "Electronics": 180,
    "Toys & Games": 200,
    "Beauty & Personal Care": 210,
    "Apparel & Fashion": 230,
    "Office & Stationery": 250,
    "Sporting Goods & Outdoors": 280,
    "Home & Kitchen": 320,
    "Books & Media": 380,
}
# Fallback for a product whose category isn't in the table above (e.g. a
# test fixture using a made-up category) -- roughly the middle of the range.
DEFAULT_LIQUIDATION_THRESHOLD = 250

# Relaxation ramp length stays a single shared constant (not asked to be
# made category-specific) -- reaches the cost floor exactly at
# category_threshold + LIQUIDATION_RAMP_DAYS.
LIQUIDATION_RAMP_DAYS = 100


def liquidation_threshold_for(category):
    """Deterministic category -> threshold lookup, no LLM."""
    return CATEGORY_LIQUIDATION_THRESHOLDS.get(category, DEFAULT_LIQUIDATION_THRESHOLD)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def find_product(catalog, product_id):
    for product in catalog:
        if product["sku_id"] == product_id:
            return product
    return None


def find_buyer(buyers, buyer_id):
    for buyer in buyers:
        if buyer["buyer_id"] == buyer_id:
            return buyer
    return None


def compute_ltv(buyer_id, orders):
    """Sum of all historical order amounts for this buyer_id. Pure
    function over the orders list -- no I/O, no randomness."""
    return round(sum(o["amount"] for o in orders if o["buyer_id"] == buyer_id), 2)


def ltv_discount_bonus(ltv):
    """Deterministic tier lookup: same ltv always yields the same bonus.
    Pure function, no LLM."""
    for lo, hi, bonus in LTV_DISCOUNT_TIERS:
        if lo <= ltv < hi:
            return bonus
    return 0  # unreachable given the tiers span [0, inf), but a safe default


def apply_ltv_bonus(policy, ltv_bonus_pct, hard_ceiling_pct=HARD_DISCOUNT_CEILING_PCT):
    """Returns a NEW policy dict (does not mutate `policy`) with
    max_discount_pct and every qty_breaks tier's discount_pct raised by
    ltv_bonus_pct, each individually capped at hard_ceiling_pct. This is
    the only integration point with merchant_agent.check_guardrails() --
    that function is completely unmodified; it just receives a policy
    dict whose discount fields already reflect the buyer's LTV bonus. If
    ltv_bonus_pct is 0, the returned policy's discount fields are
    numerically identical to the input (still a fresh dict copy)."""
    effective = dict(policy)
    effective["max_discount_pct"] = min(policy["max_discount_pct"] + ltv_bonus_pct, hard_ceiling_pct)
    effective["qty_breaks"] = [
        {**tier, "discount_pct": min(tier["discount_pct"] + ltv_bonus_pct, hard_ceiling_pct)}
        for tier in policy.get("qty_breaks", [])
    ]
    return effective


def cost_floor_price(product):
    """The ultimate, non-negotiable price floor for this product --
    cost + minimum margin. Pure function, no LLM. Nothing computed from
    min_price, max_discount_pct, the LTV bonus, or liquidation relaxation
    may ever go below this."""
    return round(product["cost"] * COST_MARGIN_MULTIPLIER, 2)


def liquidation_relaxation_fraction(product):
    """Deterministic, no LLM. 0.0 if days_in_inventory is at or below this
    product's CATEGORY-specific threshold (liquidation_threshold_for);
    otherwise ramps linearly from 0.0 to 1.0 as days_in_inventory grows
    past that threshold, reaching 1.0 (fully ramped) exactly at
    threshold + LIQUIDATION_RAMP_DAYS and staying there for any longer
    days_in_inventory. This fraction is how far merchant_agent._floor_price()
    relaxes the discount-cap floor toward min_price for a given offer's
    quantity -- see that function for the price-space interpolation
    itself; this function only computes the ramp position, since it has
    no access to a specific offer's qty-dependent discount floor."""
    days = product.get("days_in_inventory", 0)
    threshold = liquidation_threshold_for(product.get("category"))
    if days <= threshold:
        return 0.0
    return min(1.0, (days - threshold) / LIQUIDATION_RAMP_DAYS)


def liquidation_rationale(product):
    """Returns a human-readable rationale string naming the category-
    specific threshold applied and how far into the ramp this product is,
    if liquidation is active for it -- None if days_in_inventory doesn't
    exceed this product's category threshold. Deterministic, no LLM. Used
    to log a dedicated `liquidation_applied` audit entry -- see
    NEGOTIATION_SPEC.md Section 2E.

    Describes the discount-cap floor relaxing toward min_price (2026-09-02
    structural fix, Section 2J) -- not "min_price relaxed", since min_price
    itself is never altered by liquidation. No single "from X to Y" price
    can be quoted here (the actual floor is qty-dependent, computed by
    merchant_agent._floor_price() at negotiation time) -- this message
    names the ramp fraction and min_price as the ramp's ultimate target
    instead."""
    days = product.get("days_in_inventory", 0)
    category = product.get("category", "Unknown")
    threshold = liquidation_threshold_for(category)
    if days <= threshold:
        return None
    fraction = liquidation_relaxation_fraction(product)
    return (
        f"{days} days in inventory, past the {threshold}-day threshold for {category}; "
        f"the discount-cap floor is relaxed {fraction * 100:.0f}% of the way toward the "
        f"margin floor (min_price {product['min_price']:.2f})."
    )


def product_to_policy(product, max_negotiation_rounds, transaction_approval_threshold):
    """Builds a NEGOTIATION_SPEC.md Section 1-shaped policy dict from a
    data/catalog.json product entry. `cost`, `current_inventory`, and
    `days_in_inventory` are catalog-only fields, deliberately not part of
    the negotiation policy schema -- callers needing them (the inventory
    fulfillment check; this function itself, for min_price) read the
    original catalog product dict directly.

    `min_price` here is the FINAL, already-protected value -- clamped to
    never go below cost_floor_price(), but (2026-09-02 structural fix,
    Section 2J) otherwise UNTOUCHED by liquidation: min_price is already
    close to the absolute floor and rarely the operative constraint, so
    relaxing it directly had no real effect for the vast majority of
    generated products (see the module-level comment above
    CATEGORY_LIQUIDATION_THRESHOLDS). `liquidation_relaxation_fraction`
    (new field, same fix) carries how far into its ramp this product is
    (0.0 if fresh); merchant_agent._floor_price() uses it to relax the
    qty-dependent discount-cap floor itself, toward this min_price, which
    is where liquidation actually needs to act to have any real effect.

    `min_price_is_liquidation_relaxed` (Section 2F/2I, kept unchanged by
    this fix): True iff liquidation is active for this product
    (days_in_inventory past its category threshold) -- check_guardrails()
    uses this to decide whether min_price is still an absolute,
    non-negotiable floor or counter-able. Still needed under the new
    mechanism: once the discount-cap floor fully ramps down to min_price
    (see liquidation_relaxation_fraction reaching 1.0), min_price itself
    becomes the binding floor for a heavily-aged product, and it must stay
    counter-able rather than reverting to an instant reject -- the exact
    scenario Section 2F fixed, now reachable via a different path."""
    final_min_price = max(cost_floor_price(product), product["min_price"])
    threshold = liquidation_threshold_for(product.get("category"))
    is_liquidation_relaxed = product.get("days_in_inventory", 0) > threshold
    return {
        "sku_id": product["sku_id"],
        "product_name": product["product_name"],
        "currency": product["currency"],
        "list_price": product["list_price"],
        "min_price": final_min_price,
        "min_price_is_liquidation_relaxed": is_liquidation_relaxed,
        "liquidation_relaxation_fraction": liquidation_relaxation_fraction(product),
        "max_discount_pct": product["max_discount_pct"],
        "qty_breaks": product["qty_breaks"],
        "max_negotiation_rounds": max_negotiation_rounds,
        "transaction_approval_threshold": transaction_approval_threshold,
        "inventory_floor": product["inventory_floor"],
    }


def buyer_to_persona(buyer, product_name):
    """Builds the {budget, target_product, willingness_to_negotiate}
    shape AIBuyerAgent already expects from a data/buyers.json profile.
    budget is the midpoint of the buyer's budget_range, for a concrete,
    deterministic single value."""
    budget_range = buyer["budget_range"]
    return {
        "budget": round((budget_range["min"] + budget_range["max"]) / 2, 2),
        "target_product": product_name,
        "willingness_to_negotiate": buyer["negotiation_style"],
    }


def check_inventory_sufficient(product, qty):
    """Deterministic, no I/O side effects. True if the product's
    current_inventory can fulfill qty."""
    return product["current_inventory"] >= qty


def decrement_inventory(catalog_path, sku_id, qty):
    """Reads catalog_path, decrements the matching product's
    current_inventory by qty, writes the file back. Called only after a
    negotiation reaches COMPLETED (negotiation_loop.run_full_transaction).
    Raises ValueError if the sku isn't found or would go negative --
    callers are expected to have already checked
    check_inventory_sufficient() before payment, so this should never
    actually fire in the normal flow."""
    catalog = load_json(catalog_path)
    product = find_product(catalog, sku_id)
    if product is None:
        raise ValueError(f"decrement_inventory: sku_id {sku_id!r} not found in {catalog_path}")
    if product["current_inventory"] < qty:
        raise ValueError(
            f"decrement_inventory: {sku_id} has only {product['current_inventory']} units, cannot decrement by {qty}"
        )
    product["current_inventory"] -= qty
    Path(catalog_path).write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    return product["current_inventory"]
