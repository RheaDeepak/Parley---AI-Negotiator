"""Parley HTTP API (Milestone 8) -- thin FastAPI wrapper around the
existing negotiation engine, for frontend/index.html.

No negotiation/pricing/guardrail logic lives here. Every decision (offer
evaluation, discount caps, risk assessment, approval-gate conditions,
payment attempt/rollback) is made by the same functions the CLI
(negotiation_loop.py's __main__) and the bulk seed generator
(scripts/generate_negotiation_history.py) already call:
run_negotiation() / run_full_transaction() / _attempt_payment(). This
module only does request parsing, catalog/buyer/merchant lookups, and
translating between HTTP JSON and those functions' existing shapes.

The one genuinely new mechanism is the human-approval pause: a real HTTP
request can't block on input() (the CLI's _cli_confirm) or synchronously
wait for a second request. negotiation_loop.PAUSE_FOR_APPROVAL is a
sentinel that, passed as `approval_confirm`, makes run_full_transaction()
return a new "PENDING_APPROVAL" state at the exact point it would
otherwise resolve the gate -- see its docstring in negotiation_loop.py.
This module stores just enough of that paused state (in a plain
in-memory dict -- single-user demo tool, no database, explicitly scoped
that way) to resume via _attempt_payment() -- the SAME shared function
run_full_transaction() itself calls -- when POST /api/approve arrives.

Section 2X (2026-09-04) added a SECOND, distinct pause: reaching the
negotiation round cap with no agreement no longer decides walk-away-vs-
accept upfront (the old `on_round_limit` request field) or auto-accepts
anything -- it pauses too, returning "ROUND_LIMIT_PENDING" the same way
(same PAUSE_FOR_APPROVAL sentinel), stored in its own `_PENDING_ROUND_LIMIT`
dict and resumed via POST /api/round-limit-decision, which calls
negotiation_loop.resolve_round_limit_decision() -- NOT a reimplementation
of the approval flow; accepting flows through negotiation_loop.
_process_agreement(), the exact same function a normal agreement uses,
so it can itself pause a SECOND time into the ordinary PENDING_APPROVAL
gate if the resulting price/risk tier warrants it. Two separate pending-
state dicts, two separate endpoints, deliberately -- this is a different
decision point from the human-approval gate, not a variant of it (see
frontend/index.html's separate "round-limit-card" for the same reason).

Run with:
    uvicorn src.api:app --reload --port 8000
"""
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src import personalization
from src.dashboard import compute_dashboard_data
from src.agents import merchant_agent
from src.agents.ai_buyer_agent import AIBuyerAgent
from src.agents.audit_logger import DEFAULT_AUDIT_PATH, log_entry
from src.agents.buyer_agent import BuyerAgent
from src.agents.payment_service import PaymentServiceError
from src.negotiation_loop import (
    DEFAULT_MAX_NEGOTIATION_ROUNDS,
    DEFAULT_TRANSACTION_APPROVAL_THRESHOLD,
    PAUSE_FOR_APPROVAL,
    _attempt_payment,
    resolve_round_limit_decision,
    run_full_transaction,
)

app = FastAPI(title="Parley API")

# Local demo tool: the frontend is opened separately (a plain static
# file, or a simple local HTTP server on a different port) and calls this
# API cross-origin. Wide open is fine here -- there's no auth, no real
# user data, and it never leaves localhost in the intended setup.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# negotiation_id -> everything _attempt_payment() needs to resume, once a
# human approves or declines via POST /api/approve. Plain dict, no
# database, no expiry -- a single-user demo tool, not a production
# session store (explicit scope, per the request this was built from).
_PENDING_APPROVALS = {}

# Section 2X: negotiation_id -> everything resolve_round_limit_decision()
# needs to resume, once the buyer/human decides via POST
# /api/round-limit-decision. Separate dict from _PENDING_APPROVALS above
# -- a round-limit decision and a human-approval decision are different
# pause points that can chain (accept the round-limit offer -> THAT can
# itself land in _PENDING_APPROVALS if the resulting price/risk warrants
# it), so keeping them in one dict keyed only by negotiation_id would
# make it ambiguous which decision a given entry is waiting on.
_PENDING_ROUND_LIMIT = {}


class NegotiateRequest(BaseModel):
    product_id: str
    buyer_id: str
    start_price: float
    max_price: float
    qty: int = Field(default=1, ge=1, le=20)
    requested_perks: list = Field(default_factory=list)
    buyer_mode: Literal["scripted", "ai"] = "ai"
    merchant_mode: Literal["rules", "ai"] = "rules"


class ApproveRequest(BaseModel):
    negotiation_id: str
    approved: bool


class RoundLimitDecisionRequest(BaseModel):
    negotiation_id: str
    accept: bool


def _resolve_risk_approval_tier(product):
    if not product.get("merchant_id"):
        return "standard"
    merchants = personalization.load_json(personalization.DEFAULT_MERCHANTS_PATH)
    merchant = personalization.find_merchant(merchants, product["merchant_id"])
    return merchant["risk_approval_tier"] if merchant is not None else "standard"


def _build_policy(product, buyer_id, orders):
    policy = personalization.product_to_policy(
        product, DEFAULT_MAX_NEGOTIATION_ROUNDS, DEFAULT_TRANSACTION_APPROVAL_THRESHOLD,
    )
    ltv = personalization.compute_ltv(buyer_id, orders)
    ltv_bonus_pct = personalization.ltv_discount_bonus(ltv)
    return personalization.apply_ltv_bonus(policy, ltv_bonus_pct)


def _make_collector():
    """`on_event(entry, round_num)` collector -- see run_negotiation()'s
    own docstring for the round_num convention this relies on: >=1 for
    real negotiation rounds, 0 for the risk_review entry (logged before
    round 1), None for every payment-phase entry."""
    collected = []

    def on_event(entry, round_num):
        collected.append({**entry, "round_num": round_num})

    return collected, on_event


def _extract_rounds_and_risk(collected):
    risk_review = next((e for e in collected if e.get("action") == "risk_review"), None)
    rounds = [e for e in collected if isinstance(e.get("round_num"), int) and e["round_num"] >= 1]
    return rounds, risk_review


def _enrich_risk_review(risk_review, buyer_id, qty, policy, orders, risk_approval_tier):
    """The persisted risk_review audit entry only carries free-text
    rationale/evidence_paths (NEGOTIATION_SPEC.md's audit schema was never
    meant to carry structured UI fields). The frontend's Risk Agent card
    needs the tier/factors/discount-ceiling as structured data, not prose
    to parse -- so this calls personalization.risk_assessment() again,
    the exact same PURE function (and identical inputs) run_negotiation()
    already called internally to make the real decision. This does not
    re-decide anything: it's a second read of a deterministic function,
    for display only, guaranteed to agree with what actually happened.
    Returns None (no card) when there's no risk_review entry at all --
    matches the entry's own "only logged when risk_level != none" rule."""
    if risk_review is None:
        return None
    risk = personalization.risk_assessment(buyer_id, qty, policy.get("qty_breaks", []), orders)
    approval_required = risk["level"] == "high" or (risk["level"] == "moderate" and risk_approval_tier == "strict")
    return {
        **risk_review,
        "level": risk["level"],
        "factors": risk["factors"],
        "discount_factor": risk["discount_factor"],
        "approval_required": approval_required,
    }


@app.get("/api/merchants")
def get_merchants():
    return personalization.load_json(personalization.DEFAULT_MERCHANTS_PATH)


@app.get("/api/catalog")
def get_catalog(merchant_id: str):
    catalog = personalization.load_json(personalization.DEFAULT_CATALOG_PATH)
    products = [p for p in catalog if p.get("merchant_id") == merchant_id]
    result = []
    for p in products:
        item = dict(p)
        item["mrp"] = item.pop("list_price")
        result.append(item)
    return result


# 2026-09-05 (Section 2Y): the dropdown shows exactly one buyer per
# persona -- previously "first match per persona in data/buyers.json's
# own file order," which was fragile (silently changes if the file is
# ever regenerated/reordered) and, worse, arbitrary: it happened to pick
# whichever buyer_id came first in each persona's ~4-buyer band, not the
# buyer_id whose real LTV/order history actually best exemplifies that
# persona. Replaced with this explicit, curated allowlist -- each pick
# confirmed with the user against real LTV numbers (see
# NEGOTIATION_SPEC.md Section 2Y for the full before/after table and
# reasoning per pick). data/buyers.json itself is UNCHANGED for every
# buyer_id except BUYER-001 (see below) -- every other buyer still
# carries its original persona label; this list just curates which ONE
# representative per persona the frontend ever offers, it doesn't alter
# the underlying data.
CURATED_BUYER_IDS = [
    "BUYER-001",  # First Time Customer -- LTV 0.00 (0 orders), the only true zero-history buyer.
                  # Its OWN persona field was actually changed (Window Shopper -> First Time
                  # Customer) -- not just curated here -- because perk_eligibility() specifically
                  # denies all perks to "Window Shopper" regardless of order history, which
                  # incorrectly blocked this buyer's otherwise-correct free_delivery eligibility.
    "BUYER-017",  # Window Shopper -- LTV 6182.05, lowest among currently-Window-Shopper buyers.
    "BUYER-027",  # Bargain Hunter -- LTV 15829.12, closest to its band's mean (15707.54).
    "BUYER-028",  # Stubborn Negotiator -- LTV 26915.90, closest to its band's mean (27102.37).
    "BUYER-022",  # Loyal Regular -- LTV 39091.54, highest in its band -- "solid mid-to-high".
    "BUYER-004",  # Premium Customer -- LTV 66469.53, closest to its band's mean (62682.29).
    "BUYER-003",  # Bulk Buyer -- LTV 103672.43, but chosen by ORDER COUNT (36, far above anyone
                  # else) not raw LTV -- "focused on quantity discounts" is about order volume/
                  # frequency, not total spend; the ex-Whale buyers have far higher LTV but only
                  # 7-31 orders each, a worse fit for "bulk".
]


@app.get("/api/buyers")
def get_buyers():
    """Exactly one buyer per persona -- see CURATED_BUYER_IDS above for
    which buyer_id represents each persona and why. Returned in that
    same curated order (roughly ascending real LTV), not data/buyers.json's
    file order."""
    buyers = personalization.load_json(personalization.DEFAULT_BUYERS_PATH)
    by_id = {b["buyer_id"]: b for b in buyers}
    missing = [bid for bid in CURATED_BUYER_IDS if bid not in by_id]
    if missing:
        raise RuntimeError(f"CURATED_BUYER_IDS references buyer_id(s) not found in buyers.json: {missing}")
    return [by_id[bid] for bid in CURATED_BUYER_IDS]


@app.get("/api/floor-preview")
def floor_preview(product_id: str, buyer_id: str, qty: int):
    """Read-only preview for the frontend's live floor hint. Calls the
    exact same functions (risk_assessment(), apply_risk_discount_cap(),
    merchant_agent._floor_price()) run_negotiation() itself would use for
    this product/buyer/qty -- the same pattern already used by
    _enrich_risk_review() below. Nothing here decides anything; it's a
    second, display-only read of pure functions, guaranteed to agree with
    what a real negotiation at this qty would actually enforce.

    `breakdown` (Section 2AB, 2026-09-05): every candidate
    merchant_agent._floor_price_candidates() considered and which one
    won -- the SAME candidates _floor_price() itself takes max() over
    (that function now just calls this shared helper), so the breakdown
    can never silently disagree with the real floor. Lets the frontend
    show HOW the floor was calculated, not just the final number."""
    catalog = personalization.load_json(personalization.DEFAULT_CATALOG_PATH)
    product = personalization.find_product(catalog, product_id)
    if product is None:
        raise HTTPException(404, f"product_id {product_id!r} not found in catalog")

    buyers = personalization.load_json(personalization.DEFAULT_BUYERS_PATH)
    if personalization.find_buyer(buyers, buyer_id) is None:
        raise HTTPException(404, f"buyer_id {buyer_id!r} not found")

    orders = personalization.load_json(personalization.DEFAULT_ORDERS_PATH)
    policy = _build_policy(product, buyer_id, orders)

    risk = personalization.risk_assessment(buyer_id, qty, policy.get("qty_breaks", []), orders)
    preview_policy = policy
    if risk["level"] != "none":
        preview_policy = personalization.apply_risk_discount_cap(policy, risk)
    candidates = merchant_agent._floor_price_candidates(preview_policy, qty)
    winner_idx = max(range(len(candidates)), key=lambda i: candidates[i][0])
    floor, evidence_path = candidates[winner_idx][0], candidates[winner_idx][1]

    return {
        "effective_floor": floor, "evidence_path": evidence_path,
        "currency": policy["currency"], "risk_level": risk["level"],
        "breakdown": [
            {"value": value, "evidence_path": ev_path, "label": label, "detail": detail, "is_winner": i == winner_idx}
            for i, (value, ev_path, label, detail) in enumerate(candidates)
        ],
    }


@app.get("/api/dashboard-data")
def dashboard_data():
    """Section 2Z (2026-09-05): makes dashboard.html live instead of a
    static, manually-regenerated snapshot. Read-only aggregation over the
    existing audit trail -- no negotiation/pricing/guardrail logic here
    at all. Calls the EXACT SAME src.dashboard.compute_dashboard_data()
    scripts/generate_dashboard.py itself calls (that script still works
    standalone, as a fallback for generating a one-shot static snapshot
    without the API server running) -- one computation, two consumers,
    never duplicated. Returns its result as-is; dashboard.html renders it
    client-side and re-fetches this endpoint every 30 seconds."""
    return compute_dashboard_data()


@app.get("/api/inventory")
def get_inventory():
    """Read-only merchant inventory dashboard. No negotiation/pricing
    logic here -- just a straight read of catalog.json plus a computed
    is_liquidation_eligible flag, reusing personalization.
    liquidation_threshold_for() (the same category-threshold lookup
    merchant_agent._floor_price()'s liquidation ramp is built on) rather
    than re-deriving that threshold logic here."""
    catalog = personalization.load_json(personalization.DEFAULT_CATALOG_PATH)
    return [
        {
            "sku_id": p["sku_id"],
            "product_name": p["product_name"],
            "category": p["category"],
            "current_inventory": p["current_inventory"],
            "inventory_floor": p["inventory_floor"],
            "days_in_inventory": p["days_in_inventory"],
            "is_liquidation_eligible": p["days_in_inventory"] > personalization.liquidation_threshold_for(p["category"]),
        }
        for p in catalog
    ]


@app.post("/api/negotiate")
def negotiate(req: NegotiateRequest):
    catalog = personalization.load_json(personalization.DEFAULT_CATALOG_PATH)
    product = personalization.find_product(catalog, req.product_id)
    if product is None:
        raise HTTPException(404, f"product_id {req.product_id!r} not found in catalog")

    buyers = personalization.load_json(personalization.DEFAULT_BUYERS_PATH)
    buyer_profile = personalization.find_buyer(buyers, req.buyer_id)
    if buyer_profile is None:
        raise HTTPException(404, f"buyer_id {req.buyer_id!r} not found")

    orders = personalization.load_json(personalization.DEFAULT_ORDERS_PATH)
    policy = _build_policy(product, req.buyer_id, orders)
    risk_approval_tier = _resolve_risk_approval_tier(product)

    # qty comes straight from the request (1-20, validated by
    # NegotiateRequest) -- never hardcoded here. A qty below
    # policy.inventory_floor still correctly instant-rejects via
    # check_guardrails(), same as it always has; the frontend's
    # /api/floor-preview hint is what helps a user pick a sane qty
    # before starting, not a silent clamp here.
    #
    # buyer_mode/merchant_mode (Milestone 9 frontend follow-up): the SAME
    # BuyerAgent/AIBuyerAgent and evaluate_rules/evaluate_ai the CLI
    # (negotiation_loop.py's __main__) already chooses between via
    # BUYER_MODE/MERCHANT_MODE env vars -- no new negotiation logic here,
    # just which existing implementation gets wired in. "ai" mode makes a
    # real Gemini call per round (needs GEMINI_API_KEY in the server's
    # environment); evaluate_ai() already falls back to rules-only for a
    # round, not a crash, if that backend is unavailable.
    if req.buyer_mode == "ai":
        ai_persona = personalization.buyer_to_persona(buyer_profile, policy["product_name"])
        ai_persona["budget"] = req.max_price  # what the user actually typed, not the profile's own midpoint
        # Section 2AD (2026-09-05): the buyer's opening-offer anchor is
        # list_price -- the real, advertised price, safe to reveal since
        # it's not a guardrail-derived number. The AI reasons out its own
        # opening offer as a persona-appropriate discount off it (see
        # AIBuyerAgent's prompt); the true effective floor is never
        # computed or shown here at all. Replaces the Section 2AC
        # "MRP = floor x 1.08" anchor, reverted the same day -- see
        # NEGOTIATION_SPEC.md for why.
        buyer = AIBuyerAgent(
            qty=req.qty, persona=ai_persona, list_price=policy["list_price"],
            currency=policy["currency"], max_negotiation_rounds=policy["max_negotiation_rounds"],
        )
    else:
        buyer = BuyerAgent(
            qty=req.qty, opening_discount_pct=0, max_acceptable_price=req.max_price,
            list_price=policy["list_price"], starting_price=req.start_price,
        )
    merchant_evaluate = merchant_agent.evaluate_ai if req.merchant_mode == "ai" else None

    # persona resolved here (the caller), same "policy/identity resolved by
    # the caller" split already used for buyer_id/orders/risk_approval_tier
    # -- run_negotiation() never looks buyers up itself.
    persona = buyer_profile["persona"]

    collected, on_event = _make_collector()
    try:
        result = run_full_transaction(
            policy, buyer, audit_path=DEFAULT_AUDIT_PATH, on_event=on_event,
            product=product, catalog_path=None, merchant_evaluate=merchant_evaluate,
            buyer_id=req.buyer_id, orders=orders, risk_approval_tier=risk_approval_tier,
            approval_confirm=PAUSE_FOR_APPROVAL,
            persona=persona, requested_perks=req.requested_perks,
        )
    except PaymentServiceError as exc:
        raise HTTPException(500, f"Payment service unavailable: {exc}") from exc

    rounds, risk_review = _extract_rounds_and_risk(collected)
    risk_review = _enrich_risk_review(risk_review, req.buyer_id, req.qty, policy, orders, risk_approval_tier)
    perk_review = next((e for e in collected if e.get("action") == "perk_review"), None)

    # Section 2X: a DIFFERENT pause from PENDING_APPROVAL below -- the
    # round cap was reached with no agreement, not a price/risk trigger
    # on an already-agreed price. Stored in its own dict, resumed via a
    # separate endpoint (POST /api/round-limit-decision), so the frontend
    # can render a visually distinct card ("Negotiation round limit
    # reached") instead of the human-approval one.
    if result["state"] == "ROUND_LIMIT_PENDING":
        _PENDING_ROUND_LIMIT[result["negotiation_id"]] = {
            "offer": result["offer"], "policy": result["policy"], "requested_perks": result["requested_perks"],
            "negotiation_id": result["negotiation_id"], "product_name": result["product_name"],
            "list_price": result["list_price"], "merchant_id": result["merchant_id"],
            "risk_level": result.get("risk_level"), "product": product, "risk_approval_tier": risk_approval_tier,
        }
        return {
            "status": "round_limit_reached",
            "negotiation_id": result["negotiation_id"],
            "rounds": rounds,
            "perk_review": perk_review,
            "risk_review": risk_review,
            "final_offer_price": result["offer"]["price"],
            "final_offer_qty": result["offer"]["qty"],
        }

    if result["state"] == "PENDING_APPROVAL":
        _PENDING_APPROVALS[result["negotiation_id"]] = {
            "policy": policy, "offer": result["offer"], "qty": result["qty"], "total": result["total"],
            "product": product, "product_name": result["product_name"],
            "list_price": result["list_price"], "merchant_id": result["merchant_id"],
            "granted_perks": result.get("granted_perks", []), "declined_perks": result.get("declined_perks", []),
        }
        return {
            "status": "pending_approval",
            "negotiation_id": result["negotiation_id"],
            "rounds": rounds,
            "perk_review": perk_review,
            "granted_perks": result.get("granted_perks", []),
            "declined_perks": result.get("declined_perks", []),
            "risk_review": risk_review,
            "agreed_price": result["offer"]["price"],
            "agreed_qty": result["offer"]["qty"],
            "reason": result["reason"],
        }

    return {"status": "completed", "rounds": rounds, "risk_review": risk_review, "perk_review": perk_review, **result}


@app.post("/api/approve")
def approve(req: ApproveRequest):
    pending = _PENDING_APPROVALS.pop(req.negotiation_id, None)
    if pending is None:
        raise HTTPException(404, f"No pending approval found for negotiation_id {req.negotiation_id!r}")

    offer = pending["offer"]
    common = {
        "negotiation_id": req.negotiation_id, "product_name": pending["product_name"],
        "list_price": pending["list_price"], "merchant_id": pending["merchant_id"],
        "granted_perks": pending["granted_perks"], "declined_perks": pending["declined_perks"],
    }

    if not req.approved:
        log_entry(
            "merchant-agent", "approval_declined", offer,
            "Human declined the approval gate; payment_service was not called.",
            ["merchant.approval_gate"], path=DEFAULT_AUDIT_PATH,
            negotiation_id=req.negotiation_id, product_name=pending["product_name"],
            list_price=pending["list_price"], merchant_id=pending["merchant_id"],
        )
        return {"status": "completed", "state": "APPROVAL_DECLINED", "offer": offer, "payment": None, **common}

    log_entry(
        "merchant-agent", "approval_granted", offer,
        "Human approved the transaction via the API approval endpoint.",
        ["merchant.approval_gate"], path=DEFAULT_AUDIT_PATH,
        negotiation_id=req.negotiation_id, product_name=pending["product_name"],
        list_price=pending["list_price"], merchant_id=pending["merchant_id"],
    )
    try:
        result = _attempt_payment(
            pending["policy"], offer, pending["qty"], pending["total"], DEFAULT_AUDIT_PATH,
            None, False, None, pending["product"], None,
            negotiation_id=req.negotiation_id, product_name=pending["product_name"],
            list_price=pending["list_price"], merchant_id=pending["merchant_id"],
            granted_perks=pending["granted_perks"], declined_perks=pending["declined_perks"],
        )
    except PaymentServiceError as exc:
        raise HTTPException(500, f"Payment service unavailable: {exc}") from exc

    return {"status": "completed", **result}


@app.post("/api/round-limit-decision")
def round_limit_decision(req: RoundLimitDecisionRequest):
    """Section 2X: resumes a ROUND_LIMIT_PENDING negotiation once the
    buyer/human decides whether to accept the merchant's final offer.
    A DIFFERENT decision point from POST /api/approve above -- this fires
    when the round cap was reached with NO agreement at all, not when an
    already-agreed price/risk tier needs sign-off. Deliberately not
    merged into /api/approve: the two pauses can chain (accept here ->
    land in _PENDING_APPROVALS if the resulting price/risk warrants it),
    and conflating them would make it ambiguous which decision a given
    negotiation_id is actually waiting on.

    Calls negotiation_loop.resolve_round_limit_decision() -- not a
    reimplementation -- with approval_confirm=PAUSE_FOR_APPROVAL so an
    "accept" that also needs the normal approval gate pauses a second
    time instead of silently skipping it, exactly like a fresh
    negotiation would."""
    pending = _PENDING_ROUND_LIMIT.pop(req.negotiation_id, None)
    if pending is None:
        raise HTTPException(404, f"No pending round-limit decision found for negotiation_id {req.negotiation_id!r}")

    try:
        result = resolve_round_limit_decision(
            pending, req.accept, audit_path=DEFAULT_AUDIT_PATH, approval_confirm=PAUSE_FOR_APPROVAL,
            product=pending["product"], catalog_path=None, risk_approval_tier=pending["risk_approval_tier"],
        )
    except PaymentServiceError as exc:
        raise HTTPException(500, f"Payment service unavailable: {exc}") from exc

    if result["state"] == "PENDING_APPROVAL":
        _PENDING_APPROVALS[result["negotiation_id"]] = {
            "policy": pending["policy"], "offer": result["offer"], "qty": result["qty"], "total": result["total"],
            "product": pending["product"], "product_name": result["product_name"],
            "list_price": result["list_price"], "merchant_id": result["merchant_id"],
            "granted_perks": result.get("granted_perks", []), "declined_perks": result.get("declined_perks", []),
        }
        return {
            "status": "pending_approval",
            "negotiation_id": result["negotiation_id"],
            "granted_perks": result.get("granted_perks", []),
            "declined_perks": result.get("declined_perks", []),
            "agreed_price": result["offer"]["price"],
            "agreed_qty": result["offer"]["qty"],
            "reason": result["reason"],
        }

    return {"status": "completed", **result}
