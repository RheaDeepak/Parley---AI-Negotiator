import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_AUDIT_PATH = "audits/negotiation.log"


def decision_hash(offer, rationale, evidence_paths, payment=None, guardrail_clamped=None):
    """Per NEGOTIATION_SPEC.md Section 4 / 4A / 4C. Entries with no
    payment/guardrail_clamped hash identically to Milestone 1 -- a strict
    extension each time."""
    obj = {"offer": offer, "rationale": rationale, "evidence_paths": evidence_paths}
    if payment is not None:
        obj["payment"] = payment
    if guardrail_clamped is not None:
        obj["guardrail_clamped"] = guardrail_clamped
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def log_entry(
    agent, action, offer, rationale, evidence_paths, path=DEFAULT_AUDIT_PATH,
    payment=None, guardrail_clamped=None, negotiation_id=None,
    product_name=None, list_price=None, merchant_id=None,
):
    """Appends one JSON object per line to the audit log per
    NEGOTIATION_SPEC.md Section 4 / 4A / 4C. Returns the entry written.
    `payment` (Section 4A) is never the raw Razorpay API response -- only
    the curated payment_service field subset, which never includes
    RAZORPAY_KEY_ID/SECRET. `guardrail_clamped` (Section 4C) is set only
    on AI-merchant decision entries; absent everywhere else.

    `negotiation_id` (Section 4D, Milestone 7): a plain top-level field,
    sibling to `decision_id`, generated ONCE per negotiation (by
    negotiation_loop.run_negotiation()) and threaded through every entry
    that negotiation produces -- including its later payment-phase
    entries -- so a dashboard/report can group entries with a single
    groupby on this one key, no nested lookups. Identity/session
    metadata, same treatment as `decision_id`/`timestamp`: NOT part of
    decision_hash()'s input, since it doesn't describe what was decided.
    Omitted from the entry entirely when None (every pre-Milestone-7
    caller/test) -- same "only present when meaningful" convention
    already used for `payment`/`guardrail_clamped`.

    `product_name`/`list_price` (Section 4D follow-up, Milestone 7): same
    treatment as `negotiation_id` -- captured ONCE from `policy` at the
    top of run_negotiation() and threaded through every entry for that
    negotiation (including its payment phase), so a dashboard can show
    which product a negotiation was for and compute discount % vs.
    list_price without parsing rationale prose. Context, not decision
    content -- excluded from decision_hash() and omitted from the entry
    when None, same as negotiation_id.

    `merchant_id` (Section 4D second follow-up, Milestone 7): same
    treatment again -- `policy["merchant_id"]` (Milestone 6's
    multi-merchant field, already on every catalog-derived policy) is
    captured once and threaded through every entry, so a dashboard can
    tell which merchant a negotiation belongs to instead of aggregating
    everyone together. Just the id, not the human-readable merchant name
    -- resolving MERCH-001 -> "Voltstream Electronics" is a presentation
    concern for whatever reads the log (data/merchants.json is a small,
    static lookup), not something worth repeating on every entry."""
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "decision_id": uuid.uuid4().hex,
        "agent": agent,
        "action": action,
        "offer": offer,
        "rationale": rationale,
        "evidence_paths": evidence_paths,
        "decision_hash": decision_hash(offer, rationale, evidence_paths, payment=payment, guardrail_clamped=guardrail_clamped),
        "provenance_sha": "UNVERIFIED",
    }
    if payment is not None:
        entry["payment"] = payment
    if guardrail_clamped is not None:
        entry["guardrail_clamped"] = guardrail_clamped
    if negotiation_id is not None:
        entry["negotiation_id"] = negotiation_id
    if product_name is not None:
        entry["product_name"] = product_name
    if list_price is not None:
        entry["list_price"] = list_price
    if merchant_id is not None:
        entry["merchant_id"] = merchant_id
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry
