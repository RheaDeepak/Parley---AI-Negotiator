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
DEFAULT_MERCHANTS_PATH = "data/merchants.json"

# Multi-merchant support (Milestone 6, 2026-09-03): which merchant sells
# each category -- single source of truth, imported by
# scripts/generate_synthetic_data.py so the generator and the runtime
# can never drift apart (same discipline as CATEGORY_LIQUIDATION_THRESHOLDS
# below). Whole-category split, confirmed with the user before generating
# anything: MERCH-001 (Voltstream Electronics, risk_approval_tier=strict)
# gets the tech/gear-adjacent categories; MERCH-002 (Hearth & Home Living,
# risk_approval_tier=standard) gets the home/lifestyle categories.
CATEGORY_TO_MERCHANT = {
    "Electronics": "MERCH-001",
    "Office & Stationery": "MERCH-001",
    "Toys & Games": "MERCH-001",
    "Sporting Goods & Outdoors": "MERCH-001",
    "Apparel & Fashion": "MERCH-002",
    "Home & Kitchen": "MERCH-002",
    "Books & Media": "MERCH-002",
    "Beauty & Personal Care": "MERCH-002",
}

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

# Risk Agent (Milestone 5, 2026-09-02): deterministic, code-only -- no
# LLM call anywhere in this module, same "bounded input feeding into
# existing guardrails" pattern already used for LTV and liquidation
# above. Confirmed with the user before implementing (same discipline as
# the liquidation thresholds):
#
# "New buyer" -- fewer than this many prior orders in orders.json. The
# seed=42 dataset has exactly 1 buyer with 0 prior orders and none with
# exactly 1, so this threshold (< 2, i.e. 0 or 1) currently catches only
# that one true zero-history buyer.
RISK_NEW_BUYER_MAX_PRIOR_ORDERS = 2

# "Large request" -- negotiated qty at or above the product's own
# lowest qty_breaks tier (currently a flat 10 across every generated
# catalog product and merchant_policy.json's fixture) -- the merchant's
# own existing definition of a meaningfully bulk order, already baked
# into the policy schema. No historical qty data exists in orders.json
# to compute a "typical size per category" from instead (the generator
# uses qty only to derive `amount`, then discards it) -- confirmed with
# the user to reuse qty_breaks rather than add a new data field.
RISK_LARGE_QTY_FALLBACK_THRESHOLD = 10  # only used if policy.qty_breaks is empty

# "Aggressive lowball" (offer vs. floor) was considered and explicitly
# dropped, confirmed with the user: it can't be evaluated before the
# buyer's opening offer exists, which conflicts with the requirement that
# this check run before any offer is generated. check_guardrails() already
# handles a below-floor offer via its own reject/counter mechanism, so a
# separate risk-based lowball check would be redundant with that anyway.

# Reframing (2026-09-02, confirmed with the user): HIGH risk (both
# factors) is a PRICING-ABUSE signal, not a trust/fraud signal -- it no
# longer blocks the negotiation before any offer exists (the original
# NEGOTIATION_DECLINED design). Instead it tightens the discount ceiling
# for that one negotiation, same "bounded input feeding into the existing
# guardrail" pattern as the LTV bonus and liquidation ramp --
# merchant_agent.py needed zero changes, since check_guardrails()/
# _floor_price() already treat max_discount_pct/qty_breaks as just more
# policy fields.
#
# Three-level gradient (2026-09-03 follow-up, confirmed with the user --
# same discipline as every other threshold in this module): both
# RISK_MODERATE_DISCOUNT_FACTOR and RISK_HIGH_DISCOUNT_FACTOR are
# multiplicative factors (0.0-1.0) applied to BOTH max_discount_pct AND
# every qty_breaks tier's discount_pct together, in the same function
# (apply_risk_discount_cap() below) -- learned from the earlier bug
# (Section 2P/2Q) where only max_discount_pct was zeroed and qty_breaks
# was left untouched, silently overriding the intended floor. 1.0 would
# mean "no change" (NONE risk skips this entirely instead, via a None
# factor -- see risk_assessment()); 0.25 keeps a quarter of the normal
# discount room; 0.0 (HIGH, unchanged value, same effect as the old
# RISK_HIGH_DISCOUNT_OVERRIDE_PCT=0) removes it completely.
RISK_MODERATE_DISCOUNT_FACTOR = 0.25
RISK_HIGH_DISCOUNT_FACTOR = 0.0


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


def find_merchant(merchants, merchant_id):
    """Milestone 6. Same shape as find_buyer()/find_product()."""
    for merchant in merchants:
        if merchant["merchant_id"] == merchant_id:
            return merchant
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


def count_prior_orders(buyer_id, orders):
    """Count of this buyer_id's historical orders. Pure function over the
    orders list -- no I/O, no randomness. Same shape as compute_ltv()."""
    return sum(1 for o in orders if o["buyer_id"] == buyer_id)


def risk_assessment(buyer_id, qty, qty_breaks, orders):
    """Milestone 5 (Risk Agent). Deterministic, no LLM. Returns
    {"level": "none"|"moderate"|"high", "factors": [...],
    "evidence_paths": [...], "rationale": str,
    "discount_factor": float | None}. "factors" is human-readable prose
    (for the rationale/console); "evidence_paths" holds schema-path-style
    strings matching every other guardrail's convention (e.g.
    "policy.max_discount_pct", "catalog.days_in_inventory") for the audit
    log's evidence_paths field -- kept separate so audit consumers see a
    consistent shape regardless of which check fired.

    Two independent factors, both knowable BEFORE any offer exists (see
    module-level comment above RISK_NEW_BUYER_MAX_PRIOR_ORDERS for why
    "aggressive lowball" isn't a third factor here):
    - "new_buyer": count_prior_orders(buyer_id, orders) < RISK_NEW_BUYER_MAX_PRIOR_ORDERS
    - "large_request": qty >= the product's lowest qty_breaks tier (or
      RISK_LARGE_QTY_FALLBACK_THRESHOLD if qty_breaks is empty)

    level is "high" (both factors), "moderate" (exactly one), or "none"
    (neither) -- confirmed with the user. Takes `qty_breaks` (not a full
    product/policy dict) so it works identically whichever policy it's
    called with, catalog-derived or the merchant_policy.json fixture.

    "high" (reframed 2026-09-02, confirmed with the user: a
    PRICING-ABUSE signal, not a trust/fraud one) no longer blocks the
    negotiation -- it tightens the discount ceiling instead.
    "discount_factor" (2026-09-03 three-level-gradient follow-up,
    confirmed with the user) carries the multiplier the caller applies
    via apply_risk_discount_cap() below: RISK_MODERATE_DISCOUNT_FACTOR
    (0.25) for "moderate", RISK_HIGH_DISCOUNT_FACTOR (0.0) for "high",
    None for "none" (no adjustment at all -- full normal discount room)."""
    prior_orders = count_prior_orders(buyer_id, orders)
    is_new_buyer = prior_orders < RISK_NEW_BUYER_MAX_PRIOR_ORDERS

    large_qty_threshold = (
        min(tier["min_qty"] for tier in qty_breaks) if qty_breaks else RISK_LARGE_QTY_FALLBACK_THRESHOLD
    )
    is_large_request = qty >= large_qty_threshold

    factors = []
    evidence_paths = []
    if is_new_buyer:
        factors.append(f"new buyer ({prior_orders} prior order{'s' if prior_orders != 1 else ''})")
        evidence_paths.append("buyer.order_history")
    if is_large_request:
        factors.append(f"large request (qty {qty} vs typical {large_qty_threshold})")
        evidence_paths.append("policy.qty_breaks")

    if is_new_buyer and is_large_request:
        level = "high"
    elif factors:
        level = "moderate"
    else:
        level = "none"

    discount_factor = {
        "high": RISK_HIGH_DISCOUNT_FACTOR, "moderate": RISK_MODERATE_DISCOUNT_FACTOR, "none": None,
    }[level]

    if level == "none":
        rationale = f"No risk factors: buyer_id={buyer_id} has {prior_orders} prior order(s), qty {qty} is below the large-request threshold ({large_qty_threshold})."
    elif level == "high":
        rationale = (
            f"Risk factors for buyer_id={buyer_id}: {'; '.join(factors)}. "
            f"max_discount_pct forced to {int(RISK_HIGH_DISCOUNT_FACTOR * 100)} for this negotiation -- "
            "full list price only, no negotiation room -- and the human-approval gate is forced "
            "regardless of policy.transaction_approval_threshold."
        )
    else:  # moderate
        rationale = (
            f"Risk factors for buyer_id={buyer_id}: {'; '.join(factors)}. "
            f"Discount ceiling reduced to {int(RISK_MODERATE_DISCOUNT_FACTOR * 100)}% of normal for this negotiation."
        )

    return {
        "level": level, "factors": factors, "evidence_paths": evidence_paths, "rationale": rationale,
        "discount_factor": discount_factor,
    }


def apply_risk_discount_cap(policy, risk):
    """Returns a NEW policy dict (does not mutate `policy`) with
    max_discount_pct AND every qty_breaks tier's discount_pct scaled by
    risk["discount_factor"] -- a no-op (returns a copy of `policy`
    unchanged) when that key is None ("none" risk -- full normal discount
    room). The SAME factor is applied to both fields, in this one
    function, for every non-None risk level ("moderate" at
    RISK_MODERATE_DISCOUNT_FACTOR, "high" at RISK_HIGH_DISCOUNT_FACTOR).

    BOTH fields need scaling together, not just max_discount_pct:
    merchant_agent._applicable_tier() always prefers a matching
    qty_breaks tier's discount_pct over max_discount_pct (Section 2 --
    qty_breaks tiers OVERRIDE the base rate, they don't stack with it).
    Since "large_request" (one of the two factors "high" requires) is
    defined as qty at or above the product's own lowest qty_breaks tier,
    any qty that triggers "high" (and often "moderate", if a request
    happens to be large without also being new-buyer-driven) will match
    at least that tier -- so scaling max_discount_pct alone would be
    silently overridden by the very qty_breaks tier the request
    qualifies for, defeating the whole point of this function (the exact
    bug found and fixed in Section 2P/2Q for the "high" case; this
    function's single-factor-for-both-fields design is what prevents it
    from recurring for "moderate" too)."""
    factor = risk.get("discount_factor")
    if factor is None:
        return dict(policy)
    effective = dict(policy)
    effective["max_discount_pct"] = policy["max_discount_pct"] * factor
    effective["qty_breaks"] = [
        {**tier, "discount_pct": tier["discount_pct"] * factor}
        for tier in policy.get("qty_breaks", [])
    ]
    return effective


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
    scenario Section 2F fixed, now reachable via a different path.

    `merchant_id` (Milestone 6, new field): passed through unchanged from
    the catalog product -- None for a product without one (e.g. an older
    fixture predating multi-merchant support). Just a passthrough; this
    function does no merchant lookup itself, same "policy carries the id,
    the caller resolves it" pattern already used for buyer_id (Section 2D)
    -- negotiation_loop.py resolves the actual merchant profile/
    risk_approval_tier separately, from data/merchants.json."""
    final_min_price = max(cost_floor_price(product), product["min_price"])
    threshold = liquidation_threshold_for(product.get("category"))
    is_liquidation_relaxed = product.get("days_in_inventory", 0) > threshold
    return {
        "sku_id": product["sku_id"],
        "product_name": product["product_name"],
        "currency": product["currency"],
        "merchant_id": product.get("merchant_id"),
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
