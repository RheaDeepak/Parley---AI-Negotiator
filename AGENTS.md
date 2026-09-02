<!-- BMAD: AGENTS BLOCK START -->
Project: AI Merchant Negotiator
Provenance:
- date: 2026-08-27
- sha: UNVERIFIED (no git available)

Competition Track: AI Growth & Agentic Commerce

Purpose
- Small repo-level agent instructions so AI agents behave safely and predictably while building the competition-ready agentic commerce system: an AI buyer negotiates with a merchant AI under merchant-defined constraints, then completes the transaction via Razorpay test-mode APIs. These lines are authoritative, minimal, and verifiable.

Communication
- language: English
- contact: repo maintainers (add in `CONTRIBUTORS.md`)

Agents & Roles
- `buyer-agent`: Represents the purchaser; optimizes for buyer utility subject to ethical constraints and merchant rules.
- `merchant-agent`: Represents the merchant; enforces product constraints, pricing rules, inventory, and acceptable concessions.
- `auditor-agent`: Records and verifies negotiation steps and transaction evidence for the audit trail.
- `dev-agent`: Developer helper for CI checks, tests, and local environment actions.

Negotiation protocol (required)
- Use structured turn-based offers: {offer_id, price, qty, terms, expiration, timestamp}.
- Max negotiation cycles: 5 rounds by default (override only by explicit human approval).
- Each offer must include a concise, explicit rationale (1–2 sentences) that references evidence or rule lines.
- Merchant constraints are authoritative: if a proposed offer violates a merchant constraint, the `merchant-agent` must reject and explain which rule was violated (include file/line evidence).
- If agreement reached, record `agreement` record with timestamp, decision_id, rationales from both agents, and trigger payment flow (test-mode only).

Payment & Razorpay (test-mode only)
- Use Razorpay test-mode APIs only. Do not call live payment endpoints in demos.
- Required env vars: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`.
- The human-approval threshold lives in `merchant_policy.json` as
  `transaction_approval_threshold` (per NEGOTIATION_SPEC.md Section 1),
  not as an env var — updated 2026-08-29, Milestone 2.
- Payment actions must be initiated by `merchant-agent` or `auditor-agent` only after an `agreement` record exists.
- Do NOT log raw secrets. Log payment request/response metadata with sensitive fields redacted.

Audit trail & logging (mandatory)
- All negotiation actions emit structured JSONL audit entries to `audits/negotiation.log` with fields:
  - timestamp, decision_id, agent, action, offer, rationale, evidence_paths, decision_hash
- Save full transcripts and decision metadata in `audits/` with access controls; store secret-free evidence only.
- Each audit entry must include provenance SHA for the code/docs used to form the decision.

Safety & constraints
- Enforce merchant-defined constraints (inventory, min price, allowed discounts).
- Require human approval for transactions above `TRANSACTION_APPROVAL_THRESHOLD`.
- Redact PII, do not persist payment instrument numbers or raw personal data.
- Disallow arbitrary outbound access from agents; restrict to approved endpoints (product catalog, Razorpay test API).

Tooling & allowed actions
- Agents may read repository files and call approved HTTP APIs (product catalog endpoints, Razorpay test endpoints).
- Agents may not write code without an explicit `bmad-build` or human approval.
- Recommended CI hooks:
  - secret-scan on push
  - auditable test that simulates negotiation with fixed seeds
  - linting for negotiation decision format

Where things are (initial)
- Specs & PRD: to be created by `bmad-prd` and `bmad-spec`
- Audit folder: `audits/`
- Scripts/harness: `_bmad/scripts/`
- Negotiation tests: `tests/e2e/negotiation/` (create via QA step)

Pitfalls (observed / anticipated)
- Agents ignoring merchant constraints — recommend unit tests that assert constraint enforcement.
- Secrets accidentally logged in debug output — require secret-scan CI and runtime redaction checks.
- Malformed Razorpay requests — add contract tests against Razorpay test fixtures.

Maintenance & refresh
- Re-run `bmad-project-context` with `refresh` after changes to constraints, payment flow, or CI.
- To verify provenance SHAs, run the skill's verification step (fills `sha` lines) before an audit demo.

Approval
- Inserted by `bmad-project-context` on 2026-08-27. Verify and update `sha` after initializing git.

<!-- BMAD: AGENTS BLOCK END -->
