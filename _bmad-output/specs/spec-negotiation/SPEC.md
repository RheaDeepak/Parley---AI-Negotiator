---
id: SPEC-negotiation
companions: [policy-schema.md, state-machines.md, audit-schema.md]
sources: [../../../../AGENTS.md, ../../../../PROJECT_GUIDANCE.md]
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# Negotiation Core

## Why

Parley competes in the Razorpay AI Buildathon's Agentic Commerce track, which mandates a buyer-agent and merchant-agent that negotiate under merchant-defined constraints and transact end-to-end, explainably (a mandate to meet). It is also a vision to realize: an agentic commerce workflow where every money-adjacent decision is bounded, deterministic where it needs to be, and auditable. This spec covers Milestone 1 — the deterministic negotiation core — which every later milestone (LLM-driven negotiation, Razorpay settlement) builds on without changing the contract.

## Capabilities

- **CAP-1**
  - **intent:** A merchant-agent can evaluate a buyer's offer against merchant policy and determine accept, counter, or reject, citing the specific policy field that drove the decision.
  - **success:** Unit tests over an offer below `min_price`, an offer exceeding `max_discount_pct`, an in-bounds offer, and offers at each `qty_breaks` tier all return the correct decision and `evidence_paths`.

- **CAP-2**
  - **intent:** A negotiation loop can drive a buyer-agent and merchant-agent through rounds to a deterministic terminal state.
  - **success:** Every run terminates within `max_negotiation_rounds`, landing in exactly one of `AGREEMENT_RECORDED`, `REJECTED` (explicit violation), or `REJECTED` (round-limit exhausted).

- **CAP-3**
  - **intent:** Every negotiation action is recorded as a structured, hash-verifiable audit entry.
  - **success:** `audits/negotiation.log` holds one parseable JSON object per line per action; recomputing `decision_hash` from a logged entry's `offer` + `rationale` + `evidence_paths` matches the logged value.

## Constraints

- Merchant-agent decision logic (`evaluate()`) must be pure, deterministic, rule-based Python with no LLM or network calls in Milestone 1 — LLM-based negotiation is a later milestone.
- Negotiation rounds are hard-capped at `policy.max_negotiation_rounds` (default 5, per `AGENTS.md`); exceeding it without agreement forces terminal state `REJECTED`, never an unbounded loop.
- Every accept/counter/reject decision must cite `evidence_paths` — dotted paths into the policy object (e.g. `policy.min_price`, `policy.qty_breaks[1].discount_pct`, `policy.max_negotiation_rounds`) naming exactly which policy field drove the decision. A decision with no `evidence_paths` is invalid.
- Razorpay/payment API calls are out of scope for Milestone 1; the audit schema reserves `decision_hash` and `provenance_sha` fields for later payment-milestone use but implements no payment call.

## Non-goals

- No LLM-based negotiation logic in Milestone 1 — deterministic rule-based only.
- No real or test-mode Razorpay API integration in Milestone 1 — payment settlement is a later milestone.
- No `TRANSACTION_APPROVAL_THRESHOLD` human-approval workflow in Milestone 1 — approval gating ships alongside payment integration.
- No multi-SKU / multi-item cart negotiation in Milestone 1 — one product/SKU per `merchant_policy.json` only.

## Success signal

`pytest` over `tests/e2e/negotiation` passes covering below-min-price rejection, over-max-discount rejection, in-bounds acceptance, `qty_breaks` tier selection, and round-limit termination. A manual negotiation run produces `audits/negotiation.log` entries whose `decision_hash` independently recomputes from `offer` + `rationale` + `evidence_paths`.

## Assumptions

- Currency is INR: Razorpay is India-focused and `PROJECT_GUIDANCE.md` left currency an open question with only a numeric threshold (5000) stated.
- `qty_breaks` entries define the maximum `discount_pct` allowed at that quantity tier, **overriding** (not stacking with) the policy's base `max_discount_pct`; the highest `min_qty` tier the offer's quantity meets or exceeds applies.
- A merchant counter-offer's price is deterministic: `max(min_price, list_price * (1 - applicable_discount_pct_at_offer_qty))` — the most generous price permitted under policy at the buyer's requested quantity. The counter keeps the buyer's requested quantity unchanged.
- `offer.terms` is a free-form string reserved for future milestones, unexamined by Milestone-1 merchant logic; `offer.expiration` and audit `timestamp` are ISO 8601 UTC strings.
- `provenance_sha` in audit entries uses the literal placeholder `"UNVERIFIED"` for Milestone 1 (no git-based provenance wiring yet), matching the precedent `AGENTS.md` already sets for its own provenance line.
- Milestone 1 negotiates a single product/SKU per `merchant_policy.json`; the policy's product identifier is a plain string `sku_id`.
- `decision_hash` = sha256 hex digest of UTF-8 canonical JSON (`json.dumps` with `sort_keys=True, separators=(',', ':')`) of `{"offer": offer, "rationale": rationale, "evidence_paths": evidence_paths}` — deterministic and independently recomputable from the three logged fields.
- The buyer-agent's scripted opening offer and split-the-difference concession strategy is a Milestone-1 test-harness implementation detail, not part of the cross-milestone negotiation contract; it is not pinned in this SPEC or its companions.
