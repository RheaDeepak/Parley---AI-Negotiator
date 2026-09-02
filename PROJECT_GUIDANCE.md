# Project Guidance — AI Merchant Negotiator

Purpose
- Context and next-steps guidance for contributors and BMad workflows. This file complements the `AGENTS.md` block by documenting competition requirements, immediate non-technical constraints, and suggested next BMad workflows.

Competition context
- Track: AI Growth & Agentic Commerce
- Objective: build an agent-to-agent commerce workflow where an AI buyer and merchant negotiate and transact end-to-end (Razorpay test-mode). Money actions must be explainable, bounded, and gated. Demonstrate an audit trail and at least one graceful failure handling.

Key non-implementation constraints
- Do not call live payment endpoints — Razorpay test-mode only.
- Every money-related decision must be recorded in `audits/` as structured JSONL entries with provenance.
- Human approval gating required above `TRANSACTION_APPROVAL_THRESHOLD`.

Default choices recorded here (can be changed after discussion)
- Max negotiation rounds: 5
- Default transaction approval threshold: 5000 (currency to be confirmed)
- Default failure scenario to demonstrate: Razorpay payment failure / network error handled gracefully with rollback and human notification.

Environment variables (runtime)
- RAZORPAY_KEY_ID — test key id (do not commit to repo)
- RAZORPAY_KEY_SECRET — test key secret (do not commit to repo)

Human-approval threshold (updated 2026-08-29, Milestone 2)
- Lives in `merchant_policy.json` as `transaction_approval_threshold`, not
  as an env var — confirmed with the user; see NEGOTIATION_SPEC.md
  Section 1.

Audit trail format (implementation guidance)
- Write each audit event as a single JSON object per line to `audits/negotiation.log`.
- Required fields: `timestamp`, `decision_id`, `agent`, `action`, `offer`, `rationale`, `evidence_paths`, `decision_hash`, `provenance_sha`.

Minimal demo scenarios (MVP)
1. Successful negotiation and test-mode payment (happy path).
2. Payment failure: simulate Razorpay test-mode error; system rolls back any provisional inventory hold and records a failure event with remediation steps.
3. Constraint rejection: buyer proposes below-min-price; merchant-agent rejects and explains the rule.

Recommended immediate BMad workflow
1. `bmad-project-context` (this run) — finished.
2. `bmad-prd` — create a short PRD capturing goals above and success criteria for the demo.
3. `bmad-spec` — author machine-readable SPEC for negotiation protocol and audit events.
4. `bmad-architecture` — produce a minimal architecture spine (data flows and invariants), not implementation choices.
5. `bmad-create-epics-and-stories` + `bmad-sprint-planning` — break into developer tasks and plan an MVP sprint.

CI & testing recommendations
- Add secret-scan step on PRs.
- Add a reproducible negotiation simulation test that runs in CI with fixed RNG seeds.
- Add a contract-level test for Razorpay request shapes using recorded test fixtures.

Questions for the product owner (these materially affect behavior)
1. Confirm `TRANSACTION_APPROVAL_THRESHOLD` numeric value and currency (default 5000). Is currency INR? or USD?
2. Confirm desired default for `Max negotiation cycles` (default 5 acceptable?).
3. Do you have Razorpay test account credentials to place into CI secrets, or should we provide instructions for testers to supply their own keys?

How I'll proceed after your answers
- Update `AGENTS.md` provenance `sha` if git is available or leave as verified after you initialize a repo.
- Run `bmad-prd` to draft the PRD, then `bmad-spec` to produce the machine spec.
