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


def log_entry(agent, action, offer, rationale, evidence_paths, path=DEFAULT_AUDIT_PATH, payment=None, guardrail_clamped=None):
    """Appends one JSON object per line to the audit log per
    NEGOTIATION_SPEC.md Section 4 / 4A / 4C. Returns the entry written.
    `payment` (Section 4A) is never the raw Razorpay API response -- only
    the curated payment_service field subset, which never includes
    RAZORPAY_KEY_ID/SECRET. `guardrail_clamped` (Section 4C) is set only
    on AI-merchant decision entries; absent everywhere else."""
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
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry
