# Audit Log Schema

Per [SPEC.md](SPEC.md) CAP-3. One JSON object per line, appended to `audits/negotiation.log` for every negotiation action (initial offer, each counter, and the terminal decision).

## Fields

| Field | Type | Description |
|---|---|---|
| `timestamp` | string | ISO 8601 UTC, when this action was recorded. |
| `decision_id` | string | Unique id for this audit entry (e.g. `uuid4` hex). |
| `agent` | string | Which agent produced this action: `"buyer-agent"` or `"merchant-agent"`. |
| `action` | string | One of `"offer"`, `"counter"`, `"accept"`, `"reject"`. |
| `offer` | object | The offer this entry concerns, in the shape defined in `state-machines.md` (`offer_id, price, qty, terms, expiration, timestamp`). |
| `rationale` | string | 1–2 sentence explanation of the decision, naming the specific policy field that drove it (e.g. `"Offer price 3500 is below policy.min_price 3799.0"`). Empty string `""` for plain buyer `"offer"` actions that carry no merchant rationale. |
| `evidence_paths` | array of strings | Dotted paths into the policy object identifying which field(s) drove the decision (e.g. `["policy.min_price"]`, `["policy.qty_breaks[1].discount_pct"]`, `["policy.max_negotiation_rounds"]`). Empty array `[]` for plain buyer `"offer"` actions. |
| `decision_hash` | string | sha256 hex digest — see **decision_hash formula** below. |
| `provenance_sha` | string | SHA of the code/docs version that informed this decision. Milestone 1 uses the literal placeholder `"UNVERIFIED"` (see SPEC.md Assumptions). |

## `decision_hash` formula

```python
import hashlib
import json

def decision_hash(offer: dict, rationale: str, evidence_paths: list[str]) -> str:
    canonical = json.dumps(
        {"offer": offer, "rationale": rationale, "evidence_paths": evidence_paths},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Independently recomputable from any logged entry's `offer`, `rationale`, and `evidence_paths` fields — this is exactly what CAP-3's success criterion tests.

## Example entry (merchant rejects below `min_price`)

```json
{"timestamp": "2026-08-29T10:15:03Z", "decision_id": "8f14e45f-ceea-467e-9998-1234567890ab", "agent": "merchant-agent", "action": "reject", "offer": {"offer_id": "a1b2c3", "price": 3500.0, "qty": 3, "terms": "", "expiration": "2026-08-29T10:20:03Z", "timestamp": "2026-08-29T10:15:00Z"}, "rationale": "Offer price 3500.0 is below policy.min_price 3799.0 for qty 3.", "evidence_paths": ["policy.min_price"], "decision_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b85", "provenance_sha": "UNVERIFIED"}
```

(The `decision_hash` value above is illustrative — the real value is whatever the formula produces for this exact `offer`/`rationale`/`evidence_paths` triple.)
