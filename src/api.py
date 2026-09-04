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

Run with:
    uvicorn src.api:app --reload --port 8000
"""
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src import personalization
from src.agents import merchant_agent
from src.agents.audit_logger import DEFAULT_AUDIT_PATH, log_entry
from src.agents.buyer_agent import BuyerAgent
from src.agents.payment_service import PaymentServiceError
from src.negotiation_loop import (
    DEFAULT_MAX_NEGOTIATION_ROUNDS,
    DEFAULT_TRANSACTION_APPROVAL_THRESHOLD,
    PAUSE_FOR_APPROVAL,
    _attempt_payment,
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



class NegotiateRequest(BaseModel):
    product_id: str
    buyer_id: str
    start_price: float
    max_price: float
    qty: int = Field(default=1, ge=1, le=20)
    on_round_limit: Literal["accept_final", "walk_away"] = "walk_away"


class ApproveRequest(BaseModel):
    negotiation_id: str
    approved: bool


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


@app.get("/api/buyers")
def get_buyers():
    """One representative buyer per distinct `persona` label, not all 30
    -- many buyers share a persona (Bargain Hunter, Loyal Regular, etc.),
    and the dropdown only needs to demonstrate each persona once. First
    match wins (data/buyers.json's own order), so this is deterministic
    across calls."""
    buyers = personalization.load_json(personalization.DEFAULT_BUYERS_PATH)
    seen_personas = set()
    unique = []
    for b in buyers:
        if b["persona"] not in seen_personas:
            seen_personas.add(b["persona"])
            unique.append(b)
    return unique


@app.get("/api/floor-preview")
def floor_preview(product_id: str, buyer_id: str, qty: int):
    """Read-only preview for the frontend's live floor hint. Calls the
    exact same functions (risk_assessment(), apply_risk_discount_cap(),
    merchant_agent._floor_price()) run_negotiation() itself would use for
    this product/buyer/qty -- the same pattern already used by
    _enrich_risk_review() below. Nothing here decides anything; it's a
    second, display-only read of pure functions, guaranteed to agree with
    what a real negotiation at this qty would actually enforce."""
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
    floor, evidence_path = merchant_agent._floor_price(preview_policy, qty)

    return {
        "effective_floor": floor, "evidence_path": evidence_path,
        "currency": policy["currency"], "risk_level": risk["level"],
    }


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
    buyer = BuyerAgent(
        qty=req.qty, opening_discount_pct=0, max_acceptable_price=req.max_price,
        list_price=policy["list_price"], starting_price=req.start_price,
    )

    collected, on_event = _make_collector()
    try:
        result = run_full_transaction(
            policy, buyer, audit_path=DEFAULT_AUDIT_PATH, on_event=on_event,
            product=product, catalog_path=None,
            buyer_id=req.buyer_id, orders=orders, risk_approval_tier=risk_approval_tier,
            approval_confirm=PAUSE_FOR_APPROVAL, on_round_limit=req.on_round_limit,
        )
    except PaymentServiceError as exc:
        raise HTTPException(500, f"Payment service unavailable: {exc}") from exc

    rounds, risk_review = _extract_rounds_and_risk(collected)
    risk_review = _enrich_risk_review(risk_review, req.buyer_id, req.qty, policy, orders, risk_approval_tier)

    if result["state"] == "PENDING_APPROVAL":
        _PENDING_APPROVALS[result["negotiation_id"]] = {
            "policy": policy, "offer": result["offer"], "qty": result["qty"], "total": result["total"],
            "product": product, "product_name": result["product_name"],
            "list_price": result["list_price"], "merchant_id": result["merchant_id"],
        }
        return {
            "status": "pending_approval",
            "negotiation_id": result["negotiation_id"],
            "rounds": rounds,
            "risk_review": risk_review,
            "agreed_price": result["offer"]["price"],
            "agreed_qty": result["offer"]["qty"],
            "reason": result["reason"],
        }

    return {"status": "completed", "rounds": rounds, "risk_review": risk_review, **result}


@app.post("/api/approve")
def approve(req: ApproveRequest):
    pending = _PENDING_APPROVALS.pop(req.negotiation_id, None)
    if pending is None:
        raise HTTPException(404, f"No pending approval found for negotiation_id {req.negotiation_id!r}")

    offer = pending["offer"]
    common = {
        "negotiation_id": req.negotiation_id, "product_name": pending["product_name"],
        "list_price": pending["list_price"], "merchant_id": pending["merchant_id"],
    }

    if not req.approved:
        log_entry(
            "merchant-agent", "approval_declined", offer,
            "Human declined the approval gate; payment_service was not called.",
            ["merchant.approval_gate"], path=DEFAULT_AUDIT_PATH, **common,
        )
        return {"status": "completed", "state": "APPROVAL_DECLINED", "offer": offer, "payment": None, **common}

    log_entry(
        "merchant-agent", "approval_granted", offer,
        "Human approved the transaction via the API approval endpoint.",
        ["merchant.approval_gate"], path=DEFAULT_AUDIT_PATH, **common,
    )
    try:
        result = _attempt_payment(
            pending["policy"], offer, pending["qty"], pending["total"], DEFAULT_AUDIT_PATH,
            None, False, None, pending["product"], None,
            negotiation_id=req.negotiation_id, product_name=pending["product_name"],
            list_price=pending["list_price"], merchant_id=pending["merchant_id"],
        )
    except PaymentServiceError as exc:
        raise HTTPException(500, f"Payment service unavailable: {exc}") from exc

    return {"status": "completed", **result}
