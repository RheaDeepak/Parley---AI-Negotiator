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
# anything: MERCH-001 (Voltstream Marketplace, risk_approval_tier=strict)
# gets the tech/gear-adjacent categories; MERCH-002 (Hearth & Home Living,
# risk_approval_tier=standard) gets the home/lifestyle categories.
# 2026-09-05 (Section 2AA): renamed from "Voltstream Electronics" -- that
# name overpromised a single category while CATEGORY_TO_MERCHANT (below)
# always assigned it 4 (Electronics, Office & Stationery, Toys & Games,
# Sporting Goods & Outdoors), which reads as a real bug from the
# frontend (a buyer picks "Voltstream Electronics" and sees a Yoga Mat).
# Confirmed with the user: rename to accurately reflect the real
# multi-category assignment, not restructure the assignment itself.
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
#
# 2026-09-05 (Section 2Y): the top tier was originally open-ended
# (50000, inf, 8) -- confirmed with the user this made two demo
# personas indistinguishable: Premium Customer (BUYER-004, real LTV
# 66469.53) and Bulk Buyer (BUYER-003, real LTV 103672.43) both landed
# in the same tier and got the identical 8% bonus despite a ~37k real
# LTV gap. Split at 100000 (clean gap between BUYER-013's 95906.98,
# which stays at 8%, and BUYER-003's 103672.43, which now gets the new
# top tier) -- Premium Customer's own bonus is UNCHANGED (still 8%);
# only buyers with real LTV >= 100000 (BUYER-003/008/009/016) move to
# the new 12% tier. Confirmed with the user before implementing, same
# discipline as every other threshold in this module.
LTV_DISCOUNT_TIERS = (
    (0, 5000, 0),
    (5000, 20000, 2),
    (20000, 50000, 5),
    (50000, 100000, 8),
    (100000, float("inf"), 12),
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
# multiplicative factors (0.0-1.0) applied to max_discount_pct, in
# apply_risk_discount_cap() below. 1.0 would mean "no change" (NONE risk
# skips this entirely instead, via a None factor -- see
# risk_assessment()); 0.25 keeps a quarter of the normal discount room;
# 0.0 (HIGH, unchanged value, same effect as the old
# RISK_HIGH_DISCOUNT_OVERRIDE_PCT=0) removes it completely.
#
# Whether qty_breaks tiers ALSO get scaled by this same factor depends on
# the level (Section 2W, 2026-09-04): HIGH does (learned from Section
# 2P/2Q -- scaling max_discount_pct alone was silently overridden by a
# matching qty_breaks tier); MODERATE does NOT (Section 2W correction --
# scaling MODERATE's qty_breaks made an ordinary bulk order from a
# REPEAT buyer worse off than a smaller order, the opposite of what a
# bulk discount is for). See apply_risk_discount_cap()'s own docstring
# for the full live-reproduced numbers on both sides of this.
RISK_MODERATE_DISCOUNT_FACTOR = 0.25
RISK_HIGH_DISCOUNT_FACTOR = 0.0


# Milestone 9 (Section 4E) removed the separate COST_MARGIN_MULTIPLIER
# (1.02) that used to live here: it duplicated, with a DIFFERENT number,
# the margin the synthetic-data generator already bakes into every
# product's min_price (cost * 1.15 -- scripts/generate_synthetic_data.py).
# Confirmed with the user: exactly one place computes the cost-derived
# floor (the generator, at data-creation time); product_to_policy() now
# trusts product["min_price"] as-is rather than re-deriving and
# re-clamping it against a second, conflicting margin formula. This
# removes a defensive clamp against a malformed catalog entry (raw
# min_price set unsafely low relative to cost) -- an accepted tradeoff,
# not an oversight; see NEGOTIATION_SPEC.md Section 4E.

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
    max_discount_pct scaled by risk["discount_factor"] -- a no-op
    (returns a copy of `policy` unchanged) when that key is None ("none"
    risk -- full normal discount room).

    Whether qty_breaks tiers ALSO get scaled depends on the level
    (Section 2W, 2026-09-04 follow-up -- see below): only "high" does;
    "moderate" leaves qty_breaks untouched, scaling max_discount_pct
    alone.

    BOTH fields need scaling together for HIGH specifically:
    merchant_agent._applicable_tier() always prefers a matching
    qty_breaks tier's discount_pct over max_discount_pct (Section 2 --
    qty_breaks tiers OVERRIDE the base rate, they don't stack with it).
    "large_request" (one of the two factors "high" requires) is defined
    as qty at or above the product's own lowest qty_breaks tier, so any
    qty that triggers "high" will match at least that tier -- scaling
    max_discount_pct alone would be silently overridden by the very
    qty_breaks tier the request qualifies for, defeating the whole point
    of this function for HIGH (the exact bug found and fixed in Section
    2P/2Q).

    Section 2W correction (2026-09-04, same day as Section 2T/2V, live-
    reproduced): the original fix scaled qty_breaks for MODERATE too
    (same factor, same field, "for every non-None risk level"). That's
    the wrong call for MODERATE -- unlike HIGH ("new buyer" AND "large
    request" together, the actual fraud-shaped combination 2P/2Q existed
    to catch), MODERATE can fire from "large_request" ALONE, on an
    ordinary REPEAT buyer simply placing a bulk order -- not a
    circumvention attempt. Scaling an 18%-off qty_breaks tier down to
    4.5% (0.25 factor) is very often a WORSE (higher) floor than the
    unscaled max_discount_pct (e.g. 12%) that applied to a SMALLER qty
    one unit below the same tier's threshold -- so crossing into "bulk"
    territory made the price go UP, the opposite of what a bulk discount
    is for. Live-reproduced: SKU-ELEC-001, a repeat buyer (4 prior
    orders, so risk is "none" and qty_breaks is untouched below the
    threshold): qty=9 -> floor 8694.28 (12% off, unscaled); qty=10 ->
    floor 9435.27 (moderate risk from large_request alone; the 18% tier
    scaled to 4.5%) -- a ~740 INR INCREASE for ordering more. Fixed: only
    HIGH still scales qty_breaks; MODERATE now scales max_discount_pct
    only, so a qty_breaks tier -- once it applies -- is never worse than
    what an unrestricted policy would have given, preserving bulk-order
    monotonicity for the common case while HIGH's genuine new+large
    fraud signal still fully suppresses the bulk tier as before.

    `risk_level` (Section 2T, 2026-09-04; corrected same day, Section 2V
    follow-up): set to risk["level"] ("moderate" or "high" -- never for
    "none", which returns early above and never sets it).
    merchant_agent._floor_price() reads this to decide whether to treat
    the risk-scaled discount-cap floor as ITS OWN floor candidate,
    captured before liquidation relaxation ever touches `computed` --
    otherwise liquidation (which relaxes toward min_price with no
    awareness that `computed` might already be an artificially-tightened
    risk ceiling, not the normal unrestricted floor) can erode a risk
    restriction most or all of the way back to min_price on any product
    that happens to also be liquidation-eligible, silently undoing
    exactly the protection this function exists to provide.

    Section 2V correction (same day): the field was originally a bare
    `risk_discount_capped` boolean, True for BOTH "moderate" and "high" --
    _floor_price() used that boolean directly, which meant MODERATE also
    got the pre-liquidation-snapshot treatment and, exactly like the
    original HIGH-only bug this was meant to fix, ended up suppressing
    liquidation's relaxation entirely instead of stacking on top of it.
    Only HIGH risk (list_price, 0% discount) was ever meant to override
    liquidation outright; MODERATE's 25%-of-normal ceiling is a starting
    POINT for liquidation to relax from, same as an unrestricted "none"
    policy, not a separate floor candidate of its own. Storing the actual
    level (not a collapsed boolean) lets _floor_price() make that HIGH-
    vs-everything-else distinction directly instead of re-deriving it.
    See NEGOTIATION_SPEC.md Section 2T for the original live-reproduced
    bug (a HIGH-risk buyer landed at min_price instead of list_price on a
    fully-liquidation-ramped product) and Section 2V for this follow-up."""
    factor = risk.get("discount_factor")
    if factor is None:
        return dict(policy)
    level = risk.get("level")
    effective = dict(policy)
    effective["max_discount_pct"] = policy["max_discount_pct"] * factor
    # Section 2W: only HIGH scales qty_breaks tiers too -- see the
    # docstring above for why MODERATE leaving them untouched is the fix,
    # not a regression of the Section 2P/2Q anti-circumvention scaling.
    if level == "high":
        effective["qty_breaks"] = [
            {**tier, "discount_pct": tier["discount_pct"] * factor}
            for tier in policy.get("qty_breaks", [])
        ]
    effective["risk_level"] = level
    return effective


# Milestone 9 (Section 4E, multi-dimensional negotiation): the perk name a
# buyer requests -> the policy field naming what it costs the merchant.
# Single source of truth for this mapping -- merchant_agent.py imports it
# rather than re-listing the two perk names anywhere else.
PERK_COST_FIELDS = {
    "free_delivery": "shipping_cost",
    "extended_warranty": "warranty_cost",
}


# Section 2AH (2026-09-05): a SECOND, independent basis for free_delivery
# eligibility -- qty strictly greater than this threshold, regardless of
# order history. Deliberately its OWN constant, not a reuse of
# RISK_LARGE_QTY_FALLBACK_THRESHOLD above, even though both happen to be
# 10 in the current data: they answer different questions (this one is
# "is this order big enough to earn free delivery on its own merits?";
# that one is "is this qty, combined with a new buyer, a pricing-abuse
# signal?") and were confirmed with the user as intentionally separate
# thresholds that could diverge in the future -- same discipline as the
# RISK_NEW_BUYER_MAX_PRIOR_ORDERS-vs-perk-order-count distinction below.
PERK_LARGE_QTY_THRESHOLD = 10


def perk_eligibility(buyer_id, orders, persona, risk_level, qty):
    """Milestone 9 (Section 4E). Deterministic, no LLM -- same pattern as
    risk_assessment(): a pure function over already-known facts, called
    ONCE per negotiation before any offer exists. Precedence (checked in
    this order, each overriding what follows):
      1. risk_level == "high" -> no perks at all, consistent with the
         discount ceiling also being zeroed for HIGH risk.
      2. persona == "Window Shopper" -> no perks at all, regardless of
         order history or qty.
      3. 0 prior orders -> eligible for free_delivery.
      4. qty > PERK_LARGE_QTY_THRESHOLD (Section 2AH, 2026-09-05) ->
         ALSO eligible for free_delivery -- independent of rule 3, not a
         replacement for it. A returning buyer placing a large order
         (qty > 10) qualifies on this basis alone.
      5. >0 prior orders -> eligible for extended_warranty.

    Rules 3/4 grant the SAME perk (free_delivery) -- listed once even if
    both fire. Rules 4/5 are NOT mutually exclusive any more: an
    established buyer (rule 5) placing a qty>10 order (rule 4) is now
    eligible for BOTH free_delivery and extended_warranty at once. The
    original "at most one perk by construction" invariant no longer
    holds -- merchant_agent._resolve_perks() already handles N
    simultaneous candidates generically (sums their cost, one floor
    check), so this required no change there, only to its own docstring's
    now-stale claim about "at most one".

    In practice, a genuinely NEW buyer (0 prior orders) placing a qty>10
    order will almost always already be HIGH risk (rules 3+4 here mirror
    risk_assessment()'s own "new_buyer" + "large_request" factors, which
    together mean HIGH) and get nothing at all via rule 1 above, before
    rules 3/4 are ever reached -- this rule's practical effect is mainly
    for an ESTABLISHED buyer ordering qty>10, who isn't a new buyer and
    isn't automatically HIGH risk.

    Deliberately NOT the same order-count threshold as the Risk Agent's
    "new buyer" factor (RISK_NEW_BUYER_MAX_PRIOR_ORDERS, currently 2) --
    a different question (has this buyer ever ordered here at all, vs.
    risk of abusive lowballing), confirmed with the user as an
    intentional distinction, not a mismatch to align.

    Returns {"eligible": [...], "rule": str, "rationale": str} -- same
    shape/spirit as risk_assessment()'s return, for the perk_review audit
    entry to log directly. "eligible" can now hold 0, 1, or 2 perk names."""
    prior_orders = count_prior_orders(buyer_id, orders)

    if risk_level == "high":
        return {
            "eligible": [], "rule": "risk_override",
            "rationale": (
                f"High risk flagged by the Risk Agent for buyer_id={buyer_id}; no perks are "
                "offered, consistent with the discount ceiling also being zeroed at this risk level."
            ),
        }
    if persona == "Window Shopper":
        return {
            "eligible": [], "rule": "window_shopper",
            "rationale": f"buyer_id={buyer_id}'s persona is Window Shopper; no perks are ever offered to this persona, regardless of order history.",
        }

    is_new_buyer = prior_orders == 0
    is_large_qty = qty > PERK_LARGE_QTY_THRESHOLD

    if is_new_buyer and not is_large_qty:
        return {
            "eligible": ["free_delivery"], "rule": "new_buyer",
            "rationale": f"buyer_id={buyer_id} has {prior_orders} prior orders (a new buyer) -- eligible for free_delivery only.",
        }
    if not is_new_buyer and not is_large_qty:
        return {
            "eligible": ["extended_warranty"], "rule": "established_buyer",
            "rationale": f"buyer_id={buyer_id} has {prior_orders} prior order(s) and persona={persona!r} -- eligible for extended_warranty only.",
        }
    if is_new_buyer and is_large_qty:
        # Reachable in principle, but see the docstring above: this
        # combination almost always means HIGH risk instead, intercepted
        # by rule 1 before this branch is ever reached. Still eligible
        # for free_delivery only either way -- extended_warranty requires
        # prior orders regardless of qty.
        return {
            "eligible": ["free_delivery"], "rule": "new_buyer+large_qty",
            "rationale": (
                f"buyer_id={buyer_id} has {prior_orders} prior orders (a new buyer) and qty {qty} exceeds "
                f"the large-order threshold ({PERK_LARGE_QTY_THRESHOLD}) -- both independently qualify for "
                "free_delivery; still eligible for free_delivery only, since extended_warranty requires prior orders."
            ),
        }
    # not is_new_buyer and is_large_qty -- Section 2AH's new case: an
    # established buyer's large order earns free_delivery on top of the
    # extended_warranty they were already eligible for. Both at once.
    return {
        "eligible": ["free_delivery", "extended_warranty"], "rule": "established_buyer+large_qty",
        "rationale": (
            f"buyer_id={buyer_id} has {prior_orders} prior order(s) and persona={persona!r} -- eligible for "
            f"extended_warranty (established buyer). qty {qty} also exceeds the large-order threshold "
            f"({PERK_LARGE_QTY_THRESHOLD}), independently qualifying for free_delivery too -- eligible for both."
        ),
    }


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
    fulfillment check) read the original catalog product dict directly.

    `min_price` here is `product["min_price"]` passed through AS-IS
    (Milestone 9 / Section 4E: no longer re-clamped against a second,
    separately-computed cost floor -- see the removed COST_MARGIN_MULTIPLIER
    comment above). `liquidation_relaxation_fraction` carries how far into
    its ramp this product is (0.0 if fresh); merchant_agent._floor_price()
    uses it to relax the qty-dependent discount-cap floor itself, toward
    this min_price, which is where liquidation actually needs to act to
    have any real effect.

    `shipping_cost`/`warranty_cost` (Milestone 9, Section 4E): what each
    perk actually costs the merchant to provide, passed straight through
    from the catalog product. merchant_agent._floor_price()'s
    granted_perk_cost parameter adds whichever of these apply on top of
    min_price -- the same floor calculation everything else already goes
    through, not a second check.

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
    threshold = liquidation_threshold_for(product.get("category"))
    is_liquidation_relaxed = product.get("days_in_inventory", 0) > threshold
    return {
        "sku_id": product["sku_id"],
        "product_name": product["product_name"],
        "currency": product["currency"],
        "merchant_id": product.get("merchant_id"),
        "list_price": product["list_price"],
        "min_price": product["min_price"],
        "min_price_is_liquidation_relaxed": is_liquidation_relaxed,
        "liquidation_relaxation_fraction": liquidation_relaxation_fraction(product),
        "max_discount_pct": product["max_discount_pct"],
        "qty_breaks": product["qty_breaks"],
        "max_negotiation_rounds": max_negotiation_rounds,
        "transaction_approval_threshold": transaction_approval_threshold,
        "inventory_floor": product["inventory_floor"],
        "shipping_cost": product.get("shipping_cost"),
        "warranty_cost": product.get("warranty_cost"),
    }


# 2026-09-05 (Section 2AG): replaces a fixed absolute budget_range as the
# source of a buyer's spending ceiling. A stored {min, max} in rupees has
# no relationship to whichever product/qty a negotiation actually
# involves -- generate_negotiation_history.py draws both independently
# (rng.choice(catalog), rng.choice(buyers)), so a cheap-persona buyer can
# land on an expensive product and instant-reject every single time, or a
# big-spender persona can land on a cheap product and never have its
# budget bind at all. Expressing the ceiling as a discount-off-list-price
# BAND instead scales with whatever product gets drawn: max_acceptable_price
# = list_price * (1 - uniform(low, high)/100).
#
# Keyed by the buyer's `persona` label (the real, LTV-grounded field --
# see Section 2Y), not `negotiation_style` free text, because
# negotiation_style still carries stale, inconsistent wording for most
# non-curated buyers (Section 2AD: 27/30 buyers' style text was never
# reassigned when persona was relabeled) -- persona is the one field
# guaranteed consistent across all 30 buyers.json entries this table needs
# to cover. Confirmed with the user before implementing: Premium Customer
# and First Time Customer/Bargain Hunter/Stubborn Negotiator/Loyal
# Regular/Bulk Buyer/Window Shopper bands come from the user's explicit
# per-persona directional requirements (Section 2AF's negotiation_style/
# budget_range rewrite); Whale and Occasional Buyer (not part of that
# rewrite, but real labels generate_negotiation_history.py's rng.choice
# over all 30 buyers can still draw) were proposed and confirmed
# separately, same session.
PERSONA_DISCOUNT_BANDS = {
    "Premium Customer": (0, 8),
    "First Time Customer": (5, 15),
    "Whale": (5, 15),
    "Loyal Regular": (10, 18),
    "Bulk Buyer": (12, 24),
    "Stubborn Negotiator": (12, 22),
    "Occasional Buyer": (15, 25),
    "Bargain Hunter": (18, 30),
    "Window Shopper": (25, 40),
}
# Fallback for any persona label not in the table above (defensive only --
# every label currently in data/buyers.json is covered; this exists so a
# future/unrecognized persona degrades to a reasonable middle band instead
# of a KeyError).
DEFAULT_PERSONA_DISCOUNT_BAND = (15, 25)


def persona_discount_band(persona):
    """Deterministic persona label -> (low_pct, high_pct) lookup, no LLM.
    See PERSONA_DISCOUNT_BANDS above for the rationale."""
    return PERSONA_DISCOUNT_BANDS.get(persona, DEFAULT_PERSONA_DISCOUNT_BAND)


def budget_from_list_price(persona, list_price, rng):
    """A buyer's max_acceptable_price (per-unit ceiling), derived as a
    persona-appropriate discount off this specific product's list_price --
    see PERSONA_DISCOUNT_BANDS above. Replaces reading a fixed
    budget_range from data/buyers.json (Section 2AG): scales with
    whichever product is actually being negotiated over, rather than an
    absolute rupee figure with no relation to it. `rng` is an explicit
    random.Random instance so callers stay deterministic under a fixed
    seed, same discipline as every other randomized draw in
    generate_negotiation_history.py."""
    low_pct, high_pct = persona_discount_band(persona)
    discount_pct = rng.uniform(low_pct, high_pct)
    return round(list_price * (1 - discount_pct / 100), 2)


def buyer_to_persona(buyer, product_name):
    """Builds the {target_product, willingness_to_negotiate} shape
    AIBuyerAgent already expects from a data/buyers.json profile.

    2026-09-05 (Section 2AG, step 2 of 3): no longer reads budget_range,
    and "budget" is omitted from the returned dict entirely -- every real
    caller now supplies its own: src/api.py overwrites persona["budget"]
    with the human-typed Maximum Budget right after calling this function
    (interactive frontend, both AI and scripted paths), and
    src/negotiation_loop.py's BUYER_ID CLI fallback now derives it via
    budget_from_list_price() against PERSONA_DISCOUNT_BANDS above, the
    same mechanism generate_negotiation_history.py uses (step 1). A
    caller that forgets to set "budget" gets an immediate KeyError from
    AIBuyerAgent/BuyerAgent construction (loud) rather than a silent
    stale-field read -- same "loud, not silent" principle as the guard
    added in Section 2AE."""
    return {
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
