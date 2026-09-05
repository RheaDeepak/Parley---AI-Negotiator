# Negotiation Spec

> Milestone 1 was generated via the `bmad-spec` skill, derived from
> [`_bmad-output/specs/spec-negotiation/`](_bmad-output/specs/spec-negotiation/).
> Per `CLAUDE.md`, the BMAD pipeline is not re-run unless explicitly asked,
> so from Milestone 2 onward this file is the hand-maintained canonical
> spec — the `_bmad-output/` folder is left as a historical snapshot of
> Milestone 1's spec-genesis and is no longer kept in sync.

## Why

Parley competes in the Razorpay AI Buildathon's Agentic Commerce track,
which mandates a buyer-agent and merchant-agent that negotiate under
merchant-defined constraints and transact end-to-end, explainably. Milestone
1 built the deterministic negotiation core; Milestone 2 wired that agreement
into a test-mode Razorpay payment, with a human-approval gate and rollback
handling. Milestone 3a replaced the scripted buyer with an LLM-driven one.
Milestone 3b gives the merchant-agent a genuinely autonomous strategic
layer on top of its existing hard guardrails — the guardrails stay exactly
as strict as before; the LLM decides HOW to negotiate within whatever room
they leave, and every proposal is re-validated in code before it can reach
the buyer or the audit log. Milestone 3c adds synthetic scale (a product
catalog, buyer profiles, historical orders) and two deterministic,
non-LLM personalization behaviors on top: a loyalty (LTV) discount bonus
and an inventory-aware fulfillment check — same "rules, not judgment"
philosophy as the guardrails, just fed different bounded inputs.

## Capabilities

- **CAP-1** — A merchant-agent can evaluate a buyer's offer against
  merchant policy and determine accept, counter, or reject, citing the
  specific policy field that drove the decision.
- **CAP-2** — A negotiation loop can drive a buyer-agent and merchant-agent
  through rounds to a deterministic terminal state.
- **CAP-3** — Every negotiation action is recorded as a structured,
  hash-verifiable audit entry.
- **CAP-4** — A recorded agreement can be settled through Razorpay
  test-mode: a real order is created, and the payment outcome (success or
  forced failure) carries the transaction to `COMPLETED` or `ROLLBACK`.
- **CAP-5** — A transaction whose value (`agreed_price * qty`) exceeds
  `policy.transaction_approval_threshold` pauses for explicit human
  confirmation before any payment call is made.
- **CAP-6** — A forced payment failure releases the simulated inventory
  hold, records a `ROLLBACK` audit entry carrying the Razorpay error code,
  and emits a `human_notification` event.
- **CAP-7** — An LLM-driven buyer-agent can propose an opening offer and
  react to merchant counters with plausible, monotonic concession
  behavior, driven by a private (never-disclosed) target price and walk-
  away price, as a drop-in alternative to the scripted buyer.
- **CAP-8** — The AI buyer's private reasoning (target price, walk-away
  price, strategy note) is recorded as its own audit entry, separate from
  and never present inside the offer entry the merchant-agent evaluates.
- **CAP-9** — An LLM-driven merchant strategy layer can decide how much to
  concede, whether to hold firm, and how to frame a counter-offer within
  the room the deterministic guardrails leave — genuine judgment, not
  narration of an already-made decision.
- **CAP-10** — Every Layer-2 (strategic) proposal is re-validated against
  Layer 1 (`check_guardrails`) before it can reach the buyer or the audit
  log; a violating proposal is clamped to the nearest guardrail-valid
  value and the clamp is recorded, never passed through.
- **CAP-11** — A buyer's historical order total (LTV) deterministically
  raises the discount ceiling available to them in a specific negotiation,
  never past an absolute hard ceiling regardless of how high LTV is — a
  pure function, no LLM, feeding the existing guardrails as a bounded
  input rather than changing how they're enforced.
- **CAP-12** — Before payment is initiated, a negotiated quantity that
  exceeds the product's current stock routes directly to `ROLLBACK` with
  a distinct `insufficient_inventory` reason, without ever calling the
  payment service; a `COMPLETED` order decrements stock by the ordered
  quantity.
- **CAP-13** — A payment failure is never automatically retried; retrying
  requires a separate, explicit `retry_payment()` call bounded by the
  original offer's own expiration, and the buyer-agent's side of the
  failure is recorded in the audit trail as its own event, distinct from
  the operator-facing notification.
- **CAP-14** — A cost-derived minimum-margin floor takes priority over
  `min_price`, `max_discount_pct`, and the LTV bonus combined — no buyer,
  however loyal, can ever negotiate a price below it. Inventory that has
  aged past a threshold relaxes `min_price` toward that same floor
  (never below it), giving the merchant more room the longer stock sits.

## Constraints

- `check_guardrails()` (Layer 1) must be pure, deterministic, rule-based
  Python with no LLM or network calls — this is the Milestone-1 `evaluate()`
  logic, renamed and extended (Section 1's `inventory_floor`), unchanged in
  strictness. `evaluate()` remains a backward-compatible alias for it.
- Negotiation rounds are hard-capped at `policy.max_negotiation_rounds`
  (default 5); exceeding it without agreement forces terminal state
  `REJECTED`, never an unbounded loop.
- Every accept/counter/reject decision must cite `evidence_paths` — dotted
  paths into the policy object naming exactly which field drove it. A
  decision with no `evidence_paths` is invalid.
- `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` are read only from environment
  variables, never hardcoded, and never appear in any log or audit entry —
  including the key ID, which is withheld too even though it isn't secret.
- Only real Razorpay test-mode API calls are used for order creation
  (`orders.create`); no live/production payment endpoint is ever called.
  Payment *capture* normally requires a browser checkout step Parley's CLI
  flow has no way to drive, so the payment outcome (success or forced
  failure) is simulated in-process rather than captured against a real
  Razorpay payment — see Section 3A. This is a documented demo limitation,
  not a hidden shortcut.
- Audit entries for payment actions never carry the raw Razorpay API
  request/response body — only the curated `payment` field subset in
  Section 4A.
- The AI buyer's `target_price` and `walk_away_price` are never included
  in the `offer` object sent to (or evaluated by) the merchant-agent —
  only in its own separate `buyer_strategy` audit entry (Section 4B).
- The AI buyer never offers above its own `walk_away_price`, enforced in
  code (not just prompted), regardless of what the model returns.
- The Milestone-1 scripted `BuyerAgent` (`src/agents/buyer_agent.py`) is
  unmodified and remains the default for fast, deterministic,
  non-network tests.
- No field of Layer 2's raw proposal (`MerchantDecision`) is ever written
  to the returned result, the buyer, or the audit log unless it has
  independently passed a `check_guardrails()`-equivalent check first —
  the validation happens in code, never by trusting the prompt.
  Architecturally, `evaluate_ai()` cannot skip this: it always calls
  `check_guardrails()` itself and only ever returns a value built from
  that verdict or from an explicitly-validated field of the proposal.
- When a guardrail clamp occurs, the rejected raw value (and the LLM's
  free-text `concession_reasoning`, which could echo it in prose) is never
  written to the audit log — only which policy field it violated
  (Section 4C). `concession_reasoning` is logged verbatim only when no
  clamp was needed.
- `check_guardrails()` remains the pure rules-only path and is the
  MERCHANT_MODE=rules default/fallback — both for a mode-selection
  failure and for a Gemini outage mid-negotiation (Section 2B).
- The LTV-to-discount-bonus function (`src/personalization.py`) and the
  inventory fulfillment check are both pure, deterministic, no-LLM code —
  same philosophy as `check_guardrails()`. Nothing in Milestone 3c lets an
  LLM decide a discount amount, an inventory limit, or an LTV tier.
- The LTV bonus integrates with `check_guardrails()` **without modifying
  it at all**: `apply_ltv_bonus()` returns a new policy dict with
  `max_discount_pct` and each `qty_breaks` tier's `discount_pct` already
  raised and capped; `check_guardrails()` just receives that dict like any
  other policy — see Section 2C.
- The hard discount ceiling (`personalization.HARD_DISCOUNT_CEILING_PCT`,
  30%) applies independently to `max_discount_pct` and to every
  `qty_breaks` tier — no combination of LTV tier and quantity tier can
  ever produce an effective discount above it.

## Non-goals

- No live/production Razorpay payment calls, ever — test-mode only.
- No real browser-based Razorpay Checkout flow — payment outcome is
  simulated in-process (Section 3A), not captured against a real checkout
  payment.
- No real inventory/stock tracking for the default hardcoded
  `merchant_policy.json` path — still just `inventory_floor` (a minimum
  order quantity check, Section 1), no stock counter. The synthetic
  catalog path (Milestone 3c, `PRODUCT_ID` set) is the one exception: it
  *does* track real `current_inventory` for the fulfillment check and
  decrements it on `COMPLETED` — see Section 3B. This narrows, not
  reverses, the Milestone 2/3b non-goal.
- No multi-SKU / multi-item cart negotiation.
- No multi-agent buyer or merchant architecture — one LLM call per round
  per side, not a pipeline of sub-agents.
- No mid-negotiation quantity changes proposed by the AI merchant — a
  Layer-2 counter always keeps the buyer's current `qty`; qty_break
  incentives may only be *mentioned* in `terms`/reasoning, never enacted
  as an actual qty change this round (see Section 2B).
- No "suspicious buyer" or risk-based blocking logic — every profile in
  `buyers.json` is treated as legitimate. The user's explicit exclusion,
  deferred to a future milestone.

---

## Section 1 — Merchant policy schema

`merchant_policy.json` at the repo root. One file, one product/SKU.

| Field | Type | Required | Description |
|---|---|---|---|
| `sku_id` | string | yes | Plain string identifier for the product being negotiated. |
| `product_name` | string | yes | Human-readable name, for rationale/audit text. |
| `currency` | string | yes | ISO 4217 code. Milestone 1 assumes `"INR"`. |
| `list_price` | number | yes | Reference price the offer is discounted against. |
| `min_price` | number | yes | Absolute floor. Any offer price below this is rejected regardless of quantity. Must be `<= list_price`. |
| `max_discount_pct` | number | yes | Base maximum discount off `list_price` (percentage), applied when no `qty_breaks` tier matches. |
| `qty_breaks` | array of objects | yes (may be `[]`) | Quantity-tiered discount overrides — see below. |
| `max_negotiation_rounds` | integer | yes | Hard cap on negotiation rounds (default demo value: `5`). |
| `transaction_approval_threshold` | number | yes | If `agreed_price * qty` exceeds this (same currency/decimal units as `list_price`), the transaction pauses for human approval before payment — see Section 3A. |
| `inventory_floor` | integer | no (default `1`) | Minimum order quantity the merchant will accept. Offers below it are rejected (`policy.inventory_floor`), independent of price. Not a stock counter — see Non-goals. Optional for backward compatibility with Milestone 1/2/3a policy fixtures. |

### `qty_breaks` shape

```json
"qty_breaks": [
  { "min_qty": 10, "discount_pct": 20 },
  { "min_qty": 25, "discount_pct": 28 }
]
```

- The merchant-agent selects the entry with the **largest `min_qty` that
  is `<= offer.qty`**.
- If no tier's `min_qty` is met, the base `max_discount_pct` applies.
- The selected tier's `discount_pct` **overrides** (does not stack with)
  `max_discount_pct`.
- Effective floor price at a given quantity:
  `max(min_price, list_price * (1 - applicable_discount_pct / 100))`.

### Example (`merchant_policy.json` demo values)

```json
{
  "sku_id": "SKU-DEMO-001",
  "product_name": "Wireless Mechanical Keyboard",
  "currency": "INR",
  "list_price": 4999.00,
  "min_price": 3799.00,
  "max_discount_pct": 12,
  "qty_breaks": [
    { "min_qty": 10, "discount_pct": 18 },
    { "min_qty": 25, "discount_pct": 24 }
  ],
  "max_negotiation_rounds": 5,
  "transaction_approval_threshold": 20000,
  "inventory_floor": 1
}
```

At qty 1–9: floor = `max(3799, 4999*0.88)` = `4399.12`.
At qty 10–24: floor = `max(3799, 4999*0.82)` = `4099.18`.
At qty 25+: floor = `max(3799, 4999*0.76)` = `3799.24`.

---

## Section 1B — Synthetic data schemas (Milestone 3c)

Generated by `scripts/generate_synthetic_data.py` (seeded, reproducible —
`python scripts/generate_synthetic_data.py [--seed 42] [--output-dir data]`)
into `data/catalog.json`, `data/buyers.json`, `data/orders.json`. An
alternative to the single hardcoded `merchant_policy.json`/persona, wired
into `negotiation_loop.py` via `PRODUCT_ID`/`BUYER_ID` — see Section 2D.
80 products / 30 buyers / 200 orders, across 6 categories: Electronics,
Apparel & Fashion, Home & Kitchen, Sporting Goods & Outdoors, Books &
Media, Beauty & Personal Care, Toys & Games, Office & Stationery (the
user's "broader general retail" choice).

### `catalog.json` — one entry per product

| Field | Type | Description |
|---|---|---|
| `sku_id` | string | Unique, e.g. `"SKU-ELEC-003"`. |
| `product_name` | string | e.g. `"27-inch 4K Monitor"`. |
| `category` | string | One of the 8 categories above. |
| `currency` | string | `"INR"`. |
| `list_price` | number | Reference price. |
| `cost` | number | Merchant's cost — catalog-only, **not** part of the Section 1 negotiation policy schema. |
| `min_price` | number | `cost * 1.15` — cost plus a 15% minimum acceptable margin. |
| `max_discount_pct` | integer | Base discount cap, 8-15%. |
| `qty_breaks` | array | Same shape as Section 1, 2 tiers, each ≤ the hard ceiling (Section 2C). |
| `current_inventory` | integer | Real stock count — see Section 3B. Decremented on `COMPLETED`. |
| `inventory_floor` | integer | Minimum order qty, 1-3 (Section 1's field, reused). |
| `days_in_inventory` | integer | How long this product has sat unsold — see Section 2E. 1-90 for 72 products (recent, comfortably under every category's threshold). 8 designated outliers (one per category) get `category_threshold + 20..220` days — proportionate to that category's own threshold, not one shared flat range. |

`min_price > cost` is a real, checked invariant (a merchant never sells
below cost) — distinct from `min_price <= list_price` (Section 1).

### `buyers.json` — one entry per buyer

| Field | Type | Description |
|---|---|---|
| `buyer_id` | string | Unique, e.g. `"BUYER-016"`. |
| `persona` | string | Originally one of 8 labels (Bargain Hunter, Loyal Regular, Bulk Buyer, Window Shopper, Premium Customer, Occasional Buyer, Whale, Stubborn Negotiator), assigned by buyer index at data-creation time. Every buyer's own field here is untouched history except `BUYER-001` (relabeled to `"First Time Customer"`, a real perk-eligibility fix — see Section 2Y). `GET /api/buyers` no longer surfaces every buyer or every original label — see Section 2Y's `CURATED_BUYER_IDS`, which exposes exactly 7 buyer_ids, one hand-picked representative per persona (First Time Customer, Window Shopper, Bargain Hunter, Stubborn Negotiator, Loyal Regular, Premium Customer, Bulk Buyer — "Occasional Buyer"/"Whale" retired). |
| `category_affinity` | string | One of the 8 categories — weights which category `orders.json` mostly buys from for this buyer. |
| `negotiation_style` | string | Free text, e.g. `"aggressive -- pushes hard for the lowest possible price"` — same free-text shape as `AIBuyerAgent`'s existing `willingness_to_negotiate` (Section 2A). |

`budget_range` (formerly `{min, max}`) was **removed** (Section 2AG,
2026-09-05): a buyer's spending ceiling is now derived at negotiation
time from `personalization.PERSONA_DISCOUNT_BANDS` against whichever
product's real `list_price` is actually in play, rather than a fixed
absolute range with no relation to it. See Section 2AG for the full
rationale and every affected call site.

### `orders.json` — ~200 historical orders, past 12 months

| Field | Type | Description |
|---|---|---|
| `order_id` | string | Unique. |
| `buyer_id` | string | References `buyers.json`. |
| `product_id` | string | References `catalog.json`'s `sku_id`. |
| `category` | string | Denormalized from the product, for convenience. |
| `amount` | number | `unit_price * qty` for that historical order (`unit_price` is a random 85-100% of the product's *current* `list_price` — a past-deal discount, not today's negotiation). |
| `currency` | string | `"INR"`. |
| `timestamp` | string | ISO 8601 UTC, drawn from a **fixed** reference window (`2025-08-31` to `2026-08-31`), not wall-clock "now" — reproducibility must not depend on when the generator is actually run. |

Order volume per buyer is deliberately skewed (a handful of "whale"
buyers get ~8x the order weight of everyone else) — the user's explicit
choice, so the LTV tiers (Section 2C) are actually exercised across the
population in the demo rather than everyone landing in the same tier.

---

## Section 2 — Negotiation protocol / offer shape

Every offer/counter is a structured object:

```
{offer_id, price, qty, terms, expiration, timestamp}
```

- `offer_id`: string, unique per offer (e.g. `uuid4` hex).
- `price`: number, proposed unit price.
- `qty`: integer, requested quantity.
- `terms`: string, free-form, unexamined by Milestone-1 logic.
- `expiration`: ISO 8601 UTC timestamp string.
- `timestamp`: ISO 8601 UTC timestamp string, when the offer was created.

Every decision (accept/counter/reject) carries a concise rationale string
and an `evidence_paths` array of dotted paths into the policy object, e.g.
`["policy.min_price"]`, `["policy.qty_breaks[1].discount_pct"]`,
`["policy.max_negotiation_rounds"]`.

Merchant counter-offer price is deterministic:
`max(min_price, list_price * (1 - applicable_discount_pct_at_offer_qty))`
— the most generous price permitted under policy at the buyer's requested
quantity. The counter keeps the buyer's requested quantity unchanged.

---

## Section 2A — AI buyer-agent (Milestone 3a)

An alternate, LLM-driven buyer implementation (`src/agents/ai_buyer_agent.py`,
`AIBuyerAgent`), selectable via `BUYER_MODE=scripted|ai` (default
`scripted`) instead of the Milestone-1 `BuyerAgent`. Same external
interface (`initial_offer()`, `respond_to_counter(counter_offer)`) so the
negotiation loop treats either as a drop-in.

One Gemini API call per round (`gemini-3.5-flash-lite`, via the
`google-genai` SDK's `client.models.generate_content` with
`response_mime_type="application/json"` and `response_schema=BuyerDecision`
— model and provider both confirmed with the user 2026-08-29, superseding
the Claude-based build from the same milestone's first attempt), never a
multi-agent pipeline. Structured output:

```json
{
  "target_price": 4300.0,
  "walk_away_price": 4550.0,
  "strategy_note": "Opening low since list price leaves room to negotiate; will concede toward the merchant's counter.",
  "offer": { "price": 4100.0, "qty": 3, "terms": "" }
}
```

- `target_price` / `walk_away_price`: the buyer's private goal and ceiling
  — never sent to the merchant, never present in the `offer` object.
- `strategy_note`: 1–2 sentence private reasoning — audit only (Section 4B).
- `offer`: `{price, qty, terms}` — combined with a generated `offer_id`,
  `expiration`, and `timestamp` (via the existing `offer_utils.new_offer`
  helper) to produce a Section 2-shaped offer, identical in structure to
  the scripted buyer's.

The model is prompted with the persona config (Milestone-3a-only, not a
merchant-policy-schema field — see Assumptions), the full prior negotiation
history (offers and counters so far), and the merchant's latest counter
(absent on the opening move), with instructions to move price gradually
toward the merchant's counter across rounds rather than jumping to
`target_price` or repeating a prior offer — see the AI-buyer test
requirements in the Milestone-3a build for the concrete monotonic-
concession check. Code enforces `offer.price <= walk_away_price`
regardless of what the model returns (Constraints).

`respond_to_counter`'s accept/counter decision is derived programmatically,
mirroring the scripted buyer: if the model's decided price this round is
`>=` the merchant's counter price, that counts as accepting the counter
(at the counter's price); otherwise it's a new counter-offer.

### Error handling and retries

`GEMINI_API_KEY` is read from the environment only, never hardcoded or
logged. Two internal exception types (`src/agents/ai_buyer_agent.py`)
distinguish failure modes:

- **`TransientLLMError`** — a Gemini rate limit (`ClientError` code 429), a
  Gemini server error (`ServerError`, 5xx), or a network/connection
  failure. Retried with exponential backoff (default: 3 retries, 1s base
  delay, 20s cap) before giving up.
- **`BuyerUnavailableError`** — raised once retries are exhausted (or
  immediately if `GEMINI_API_KEY` is unset). Caught by
  `run_negotiation()`, which logs a `buyer_unavailable` audit entry and
  returns the new terminal state `BUYER_UNAVAILABLE` (Section 3) instead
  of letting the exception crash the whole negotiation.

A non-429 4xx error (bad request, auth failure, permission denied) is
**not** treated as transient — it propagates immediately, uncaught,
rather than being silently retried or swallowed. Those indicate a
configuration or code bug, not a free-tier quota/availability issue, and
should surface loudly rather than presenting as a graceful "buyer
unavailable" outcome.

The retry primitives (`TransientLLMError`, `LLMUnavailableError`,
`call_llm_with_retry`) live in `src/agents/llm_utils.py`, shared by both
the buyer (this section) and the merchant (Section 2B) — `ai_buyer_agent.py`
re-raises the shared `LLMUnavailableError` as its own `BuyerUnavailableError`
for backward compatibility with existing imports/tests.

---

## Section 2B — AI merchant strategy layer (Milestone 3b)

The merchant-agent gets a second, optional layer on top of the
Milestone-1 guardrails, selectable via `MERCHANT_MODE=rules|ai` (default
`rules`) — same pattern as `BUYER_MODE`. `negotiation_loop.run_negotiation()`
stays implementation-agnostic: it calls whatever `merchant_evaluate(offer,
policy, round, negotiation_history)` callable it's given, defaulting to
`merchant_agent.evaluate_rules`.

### Layer 1 — `check_guardrails(offer, policy, round)`

Pure, deterministic, no LLM or network calls. This **is** the Milestone-1
`evaluate()` logic (Section 3), renamed, with one addition:
`policy.inventory_floor` (Section 1) — `offer.qty < inventory_floor` is a
hard reject, independent of price. `evaluate()` is now a one-line
backward-compatible alias for `check_guardrails()`; every existing caller
and test that imports `evaluate` keeps working unchanged.
`evaluate_rules(offer, policy, round, negotiation_history=None)` wraps it
with the 4-arg shape `run_negotiation()` expects (the trailing history
argument is accepted and ignored) — this is `MERCHANT_MODE=rules`.

### Layer 2 — `decide_strategy(offer, policy, round, negotiation_history)`

One Gemini call (`gemini-3.5-flash-lite`, same model/provider as the
buyer-agent), structured output:

```json
{
  "action": "counter",
  "counter_offer": { "price": 4300.0, "qty": 3, "terms": "" },
  "concession_reasoning": "The buyer has moved up twice already; holding just above the midpoint should close this without giving up more margin than needed."
}
```

- `action`: `"accept" | "counter" | "reject"`.
- `counter_offer`: `{price, qty, terms}` when `action == "counter"`, else
  `null`.
- `concession_reasoning`: private strategic reasoning — audit only
  (Section 4C), and only ever logged when the proposal needed no clamp
  (see Constraints).

The model is prompted with the full policy (including `qty_breaks`, so it
can *mention* a quantity-break incentive in `terms`/reasoning), the
current round and cap, the buyer's current offer, and the negotiation
history so far. It is explicitly told a separate, deterministic system
will re-check everything it proposes — its job is genuine strategy within
the policy limits, not caution against catastrophe.

### `evaluate_ai(offer, policy, round, negotiation_history, ...)` — the guardrail re-validation

This is `MERCHANT_MODE=ai`'s top-level entry point and the architectural
core of this milestone:

1. Always calls `check_guardrails(offer, policy, round)` first — this is
   the ground truth, computed independently of anything Layer 2 says.
2. Calls `decide_strategy(...)`. On `LLMUnavailableError` (retries
   exhausted, or `GEMINI_API_KEY` unset), falls back to step 1's verdict
   directly for this round only — `guardrail_clamped: false`, a note
   appended to `rationale`, negotiation continues normally. This is a
   **per-round** fallback, not a terminal state like the buyer's
   `BUYER_UNAVAILABLE` — the merchant just negotiates as if
   `MERCHANT_MODE=rules` for that one round.
3. Otherwise, validates the Layer-2 proposal against the Layer-1 verdict:

| Layer 1 verdict | Layer 2 proposes | Result | Clamped? |
|---|---|---|---|
| `reject` (min_price, inventory_floor, or round-limit) | anything other than `reject` | Layer 1's own reject, verbatim | yes |
| `reject` | `reject` | Layer 1's own reject | no |
| `accept` (offer clears the floor) | `accept` | accept, `concession_reasoning` as rationale | no |
| `accept` | `reject` | strategic reject — always guardrail-safe | no |
| `accept` | `counter`, price `>= floor` at this qty | that counter, `concession_reasoning` as rationale | no |
| `accept` | `counter`, price `< floor` at this qty | counter clamped to the floor | yes |
| `counter` (offer below floor) | `accept` | Layer 1's own counter (floor), not the accept | yes |
| `counter` | `reject` | strategic reject — always guardrail-safe | no |
| `counter` | `counter`, price `>= floor` | that counter, `concession_reasoning` as rationale | no |
| `counter` | `counter`, price `< floor` | counter clamped to the floor | yes |
| any | `counter` with no `counter_offer` (malformed) | Layer 1's own verdict | yes |

"The floor" is always `check_guardrails`'s own qty-tier-aware value —
`max(min_price, list_price * (1 - applicable_discount_pct / 100))` at the
buyer's current `qty` — never a flatter `min_price`-only clamp (the
user's explicit choice, since `min_price` alone would under-clamp at
higher qty tiers and hand away more discount than the policy's tier
structure intends).

`qty` in any Layer-2 counter is always pinned to the buyer's current
offer `qty`, regardless of what the proposal contains — see Non-goals.

---

## Section 2C — LTV-to-discount-bonus (Milestone 3c)

Pure, deterministic, `src/personalization.py`. No LLM anywhere in this
path — same philosophy as `check_guardrails()`.

```python
HARD_DISCOUNT_CEILING_PCT = 30  # absolute; no tier can ever cross this

LTV_DISCOUNT_TIERS = (
    (0,      5000,     0),   # ltv < 5000        -> +0%
    (5000,   20000,    2),   # 5000 <= ltv < 20000  -> +2%
    (20000,  50000,    5),   # 20000 <= ltv < 50000 -> +5%
    (50000,  inf,      8),   # ltv >= 50000      -> +8%
)

def compute_ltv(buyer_id, orders):
    return sum(o["amount"] for o in orders if o["buyer_id"] == buyer_id)

def ltv_discount_bonus(ltv):
    for lo, hi, bonus in LTV_DISCOUNT_TIERS:
        if lo <= ltv < hi:
            return bonus

def apply_ltv_bonus(policy, ltv_bonus_pct, hard_ceiling_pct=HARD_DISCOUNT_CEILING_PCT):
    effective = dict(policy)
    effective["max_discount_pct"] = min(policy["max_discount_pct"] + ltv_bonus_pct, hard_ceiling_pct)
    effective["qty_breaks"] = [
        {**t, "discount_pct": min(t["discount_pct"] + ltv_bonus_pct, hard_ceiling_pct)}
        for t in policy.get("qty_breaks", [])
    ]
    return effective
```

The 4-tier table is the user's explicit choice (over the 3-tier example
in their own request), and `HARD_DISCOUNT_CEILING_PCT = 30` is their
explicit choice too, from three grounded options.

**The integration point is the policy dict, not `check_guardrails()`
itself.** `apply_ltv_bonus()` returns a new policy whose `max_discount_pct`
and every `qty_breaks` tier's `discount_pct` are already bonused and
capped; `check_guardrails()` (and `evaluate_ai()`, `evaluate_rules()`)
receive that dict exactly as they'd receive any other policy — zero
changes to `merchant_agent.py` for this milestone. This is also why the
ceiling binds **per-field**: the base rate and every quantity tier are
each independently capped, so no combination of a generous qty tier and a
high LTV tier can stack past 30%.

Applying a `ltv_bonus_pct` of `0` (no `BUYER_ID`, or a buyer whose LTV
falls in the bottom tier) still produces a fresh policy dict, numerically
identical to the input — `apply_ltv_bonus()` is called unconditionally in
`negotiation_loop.py`'s `__main__`, not gated behind an `if bonus > 0`.

---

## Section 2D — PRODUCT_ID / BUYER_ID wiring (Milestone 3c)

`negotiation_loop.py`'s `__main__` reads two more optional env vars,
same pattern as `BUYER_MODE`/`MERCHANT_MODE`/`BUYER_BUDGET`:

| Env var | Effect when set | Default when unset |
|---|---|---|
| `PRODUCT_ID` | Looks up that `sku_id` in `data/catalog.json`; builds the negotiation policy from it (`personalization.product_to_policy`) plus `apply_ltv_bonus()`. `max_negotiation_rounds`/`transaction_approval_threshold` aren't catalog fields (Section 1B) — they default to `5`/`20000`, matching `merchant_policy.json`'s existing demo values. | Loads `merchant_policy.json` — Milestone 1/2/3a/3b behavior, byte-for-byte unchanged. |
| `BUYER_ID` | Looks up that buyer in `data/buyers.json`, sums `data/orders.json` for LTV, and builds `persona`/`max_acceptable_price` from the profile (`personalization.buyer_to_persona`) — budget is derived via `personalization.budget_from_list_price()` (Section 2AG, 2026-09-05: a persona-appropriate discount off the resolved policy's real `list_price`, replacing the old `budget_range` midpoint). | `ltv_bonus_pct = 0`; persona/budget built the same hardcoded way as before. |

The two are independent: either, both, or neither may be set.
`BUYER_BUDGET`, when also set, still overrides whatever budget the
`BUYER_ID` profile would have given — it was built as a manual demo
override and keeps that role for every buyer-construction path, not just
the hardcoded one. An unrecognized `PRODUCT_ID`/`BUYER_ID` exits with a
clear error rather than silently falling back, on the same "don't hide
demo-config mistakes" logic as `BUYER_BUDGET`'s invalid-number handling.

---

## Section 2E — Margin-aware cost floor & inventory liquidation (Milestone 3c follow-up)

Two more deterministic, no-LLM inputs feeding the existing guardrails —
same integration pattern as the LTV bonus (Section 2C): both are
resolved entirely inside `product_to_policy()`, so `check_guardrails()`
in `merchant_agent.py` needed zero changes. Both only apply on the
`PRODUCT_ID` path — `merchant_policy.json` has no `cost`/
`days_in_inventory` fields, so the hardcoded fallback is unaffected.

### Cost floor — the ultimate backstop

```python
COST_MARGIN_MULTIPLIER = 1.02  # cost + 2% minimum margin -- the user's own example

def cost_floor_price(product):
    return round(product["cost"] * COST_MARGIN_MULTIPLIER, 2)
```

Takes priority over `min_price`, `max_discount_pct`, and the LTV bonus
combined (CAP-14) — nothing, including buyer loyalty, can push the
effective price below it. Enforced by construction: whatever `min_price`
`product_to_policy()` computes (raw, or already liquidation-relaxed
below), the function's last step is
`final_min_price = max(cost_floor_price(product), candidate_min_price)`.
Since `check_guardrails()`'s own floor formula is already
`max(min_price, list_price * (1 - discount_pct/100))`, and `min_price`
itself can now never be below the cost floor, the cost floor propagates
through unconditionally — no changes needed to that formula.

### Inventory liquidation — min_price relaxes for aged stock, per category

> **Superseded 2026-09-02 (Section 2J):** this subsection describes the
> ORIGINAL mechanism (`liquidation_adjusted_min_price()`, relaxing
> `min_price` itself). Live-data analysis found it had no real effect on
> ~99% of the generated catalog — kept here as historical record per this
> file's layered-correction discipline; see Section 2J for the current
> mechanism (`liquidation_relaxation_fraction()`, relaxing the
> discount-cap floor toward min_price instead).

**Category-specific thresholds, not a single flat number** (replaced a
flat 100-day threshold from this same follow-up's first draft, before
the user asked for this refinement). "Aged" means abnormal shelf time
*relative to that category's normal turnover* — not a depreciation
clock. Fast-turnover categories (Electronics) need a shorter threshold
to count as aged; slow, long-tail categories (Books & Media) need a much
longer one, since it's normal for them to sit a while. The user's
explicit calibration, anchored at Electronics=180 and Books & Media=380
(past their "365+" anchor), confirmed as a full list before any code
changed:

| Category | Threshold (days) |
|---|---|
| Electronics | 180 |
| Toys & Games | 200 |
| Beauty & Personal Care | 210 |
| Apparel & Fashion | 230 |
| Office & Stationery | 250 |
| Sporting Goods & Outdoors | 280 |
| Home & Kitchen | 320 |
| Books & Media | 380 |

```python
CATEGORY_LIQUIDATION_THRESHOLDS = { ... the table above ... }
DEFAULT_LIQUIDATION_THRESHOLD = 250  # fallback for an unlisted category
LIQUIDATION_RAMP_DAYS = 100          # still one shared constant -- not asked to vary by category

def liquidation_threshold_for(category):
    return CATEGORY_LIQUIDATION_THRESHOLDS.get(category, DEFAULT_LIQUIDATION_THRESHOLD)

def liquidation_adjusted_min_price(product):
    days = product.get("days_in_inventory", 0)
    threshold = liquidation_threshold_for(product.get("category"))
    if days <= threshold:
        return product["min_price"]
    floor = cost_floor_price(product)
    relaxation_pct = min(1.0, (days - threshold) / LIQUIDATION_RAMP_DAYS)
    relaxed = product["min_price"] - relaxation_pct * (product["min_price"] - floor)
    return round(max(floor, relaxed), 2)
```

At or below its category's threshold, `min_price` is untouched. Past it,
`min_price` relaxes **linearly** toward the cost floor, reaching it
exactly at `category_threshold + LIQUIDATION_RAMP_DAYS` and staying
there for any longer `days_in_inventory` — never overshooting below.
`product_to_policy()` applies this first, then clamps the result with
`cost_floor_price()` as described above (a second, defensive clamp —
redundant given the ramp's own `max(floor, relaxed)`, but cheap and
keeps the "cost floor always wins" invariant obviously true from
`product_to_policy()`'s code alone, without having to trust the ramp
math is bug-free).

`CATEGORY_LIQUIDATION_THRESHOLDS` lives in `src/personalization.py` as
the single source of truth — `scripts/generate_synthetic_data.py`
imports it directly (rather than duplicating the numbers) so the
generator's aged-outlier days and the runtime threshold check can never
drift apart.

### Audit trail — `liquidation_applied`

When a `PRODUCT_ID`'s `days_in_inventory` exceeds its category's
threshold, `__main__` logs one `liquidation_applied` entry (via the
existing, unmodified `log_entry()` — no schema change) naming the
category-specific threshold applied, e.g.:

> `"379 days in inventory, past the 180-day threshold for Electronics; floor relaxed from 5093.53 to 4517.74."`

`personalization.liquidation_rationale(product)` returns this string (or
`None` if not aged) — a separate, standalone audit entry rather than
threading the explanation into `check_guardrails()`'s own per-round
rationale text, since that logic lives in `merchant_agent.py` and stays
untouched (same "zero changes to merchant_agent.py" discipline as the
LTV bonus and cost floor above — despite the user's message assuming
this lookup lived there, corrected before writing any code). `offer` is
`null` on this entry, same pattern as `buyer_strategy` (Section 4B).

### Worked example (seed-42 catalog)

`SKU-ELEC-003` (27-inch 4K Monitor), the Electronics category's aged
outlier: `cost=4429.16`, raw `min_price=5093.53`, `days_in_inventory=379`
(past its 180-day threshold + comfortably into the 100-day ramp).
`cost_floor_price` = `4517.74`. `min_price` is fully relaxed:
`product_to_policy()` returns `min_price: 4517.74` — a real ~11%
reduction from the raw catalog value, capped exactly at the cost floor.
`SKU-BOOK-003` (Personal Finance Guide), the Books & Media outlier at
`days_in_inventory=541` — past its own, much longer 380-day threshold —
relaxes from `1013.20` to `898.66`. A fresh product
(`days_in_inventory=18`) in the same run keeps its raw `min_price`
unchanged.

---

## Section 2F — Liquidation-relaxed min_price is counter-able, not an instant reject (bug-fix follow-up, 2026-09-01)

**Bug report:** on `SKU-ELEC-003` (Section 2E's worked example,
`min_price` relaxed to `4517.74`), a buyer offer of `3800` on round 1 of
5 produced an immediate `REJECT` with no counter — instead of the
expected `COUNTER` at the clamped floor, mirroring the behavior
`test_guardrail_clamp_when_proposal_violates_max_discount_pct` already
proves for a non-liquidation floor.

**Investigation:** traced `evaluate_ai()`, `round` handling, and
`check_guardrails()` directly (not by inspection alone) and confirmed
this was **not** a defect in Layer 2 validation, round-passing, or a
separate personalization code path — `check_guardrails()` has had, since
Milestone 1, an unconditional rule that any offer below `policy.min_price`
is an absolute, non-negotiable instant reject (Section 1). That rule
fired identically whether `min_price` came from a raw hardcoded policy or
from Section 2E's liquidation relaxation — the two features had never
been reconciled, since neither is individually wrong; their interaction
was simply never decided.

**Decision 1 (confirmed with the user):** a merchant-set `min_price`
stays an absolute floor everywhere else, unchanged. Only a `min_price`
that liquidation itself lowered becomes counter-able, like the
discount-cap floor — the system already gave ground on this floor to
close an aged-stock deal, so crossing it shouldn't also be treated as an
instant dealbreaker.

**Decision 2 (surfaced and confirmed before implementing):** the counter
still lands at `check_guardrails()`'s existing floor formula,
`max(min_price, list_price * (1 - discount_pct/100))` — unchanged. For
`SKU-ELEC-003` at qty=3, the discount-cap floor (`7098.68`) is *higher*
than the liquidation-relaxed `min_price` (`4517.74`), so the actual fixed
behavior counters at `7098.68`, citing `policy.max_discount_pct` — not at
`4517.74` as the original bug report assumed. Confirmed as correct
before implementing, rather than silently building toward the originally
assumed number.

**Implementation** — the one deliberate exception to Section 2E's "zero
changes to `merchant_agent.py`" discipline in this milestone, since this
required `check_guardrails()` itself to distinguish a merchant-set floor
from a system-relaxed one:

```python
# personalization.py -- product_to_policy() gains one new field
is_liquidation_relaxed = product.get("days_in_inventory", 0) > threshold
policy["min_price_is_liquidation_relaxed"] = is_liquidation_relaxed

# merchant_agent.py -- check_guardrails()
min_price_is_liquidation_relaxed = policy.get("min_price_is_liquidation_relaxed", False)
if offer["price"] < policy["min_price"] and not min_price_is_liquidation_relaxed:
    return _reject(offer, ..., ["policy.min_price"])
# else falls through to the existing offer["price"] < floor counter/reject logic, unchanged
```

Defaults to `False` when absent, so every Milestone 1–3c(pre-fix) policy
dict (including `merchant_policy.json`'s hardcoded fallback, which has no
`days_in_inventory`) is byte-identical in behavior to before this fix.
Verified via the full test suite (73 passed, 3 skipped — up from 71
passed pre-fix, zero regressions) plus a direct reproduction against the
live seed-42 catalog's `SKU-ELEC-003` confirming the exact expected
transition: `REJECT` (`policy.min_price`) → `COUNTER` at `7098.68`
(`policy.max_discount_pct`).

---

## Section 2G — Investigation: identical counter price across rounds is not a plumbing bug (2026-09-01)

**Bug report:** in a live negotiation (`SKU-ELEC-003`, `BUYER_MODE=ai`,
`MERCHANT_MODE=ai`, `BUYER_BUDGET=4800`), the merchant's counter was
`7098.68` in all 5 rounds despite the buyer conceding round over round,
with generic clamp rationale instead of rationale referencing the
buyer's movement — suspected as Layer 2 either silently falling back to
rules-only, or not receiving `negotiation_history` in its prompt.

**Investigation:** reproduced the exact scenario live against the real
Gemini API with a wrapping `llm_call` that logged every prompt actually
sent. Confirmed:
- Layer 2 was called all 5 rounds (5 logged real API calls — no silent
  rules-only fallback).
- `negotiation_history` was present and correctly accumulating in every
  round's prompt, including the buyer's real, genuinely conceding offers
  (`4200` → `4350` → `4500` → `4500` → `4800` across the 5 rounds).

**Root cause:** at qty=3, `SKU-ELEC-003`'s guardrail floor is `7098.68`
(`policy.max_discount_pct`-derived) — above the buyer's entire budget
range (max `4800`). `check_guardrails()` clamps any counter below the
floor to exactly the floor (Section 2), every round, independent of
whatever price Layer 2 actually proposed underneath — so an identical
counter price across all rounds is the correct, by-design output for an
unreachable floor, not evidence Layer 2 saw the same thing every round.
Likewise, `_validate_against_guardrails()`'s clamp branch has always used
a fixed, generic rationale string, never `strategy.concession_reasoning`
(Section 2B/4C) — deliberately, since a clamped proposal's raw text could
echo the invalid price. Earlier "responsive" live runs (e.g.
`test_live_ai_merchant_mostly_avoids_clamps_in_a_straightforward_negotiation`)
looked different only because their floor *was* reachable, so most
rounds passed through unclamped and surfaced Layer 2's real
`concession_reasoning` — not because this run's plumbing regressed.

**Outcome:** no code change — `decide_strategy()`/`_build_merchant_prompt()`
already had the correct contract. Added
`test_decide_strategy_prompt_includes_negotiation_history_with_buyer_prior_offers`
and a first-round companion test
(`test_ai_merchant_agent.py`) asserting the actual prompt string sent to
the LLM contains prior rounds' buyer offer values, not just the
current-round offer — locking down the contract this investigation
verified, so a future regression here would fail a test instead of
requiring a fresh live investigation.

---

## Section 2H — Investigation: "floor relaxed to Y" console message misread as the negotiation floor (2026-09-01)

**Bug report:** on `SKU-ELEC-003` with `BUYER_BUDGET=5000`, the console
prints "floor relaxed from 5093.53 to 4517.74" at negotiation start, but
every counter across all 5 rounds is clamped to `7098.68`; the buyer's
walk-away price (`5000`, above `4517.74`) still gets rejected at round 5.
Suspected as `check_guardrails()` using a stale/raw floor instead of
`personalization.liquidation_adjusted_min_price()`.

**Investigation:** direct trace confirmed `policy["min_price"]` is
`4517.74` — the correctly liquidation-adjusted value — everywhere
`check_guardrails()`/`_floor_price()` reads it; there is exactly one
source of truth (`product_to_policy()`), no second/stale number anywhere
in the pipeline. `7098.68` comes entirely from `max_discount_pct`
(`list_price × (1 − 11%)`), a guardrail dimension liquidation was never
designed to touch (Section 2E: liquidation only ever relaxes `min_price`).
This is the exact same product/numbers already investigated in Section
2F, where the two-part floor formula `max(min_price, discount-cap floor)`
and the resulting `7098.68` counter were explicitly confirmed correct.
Also re-ran `test_completed_order_decrements_current_inventory_by_ordered_qty`
and the other personalization tests the bug report named — none of them
exercise a liquidation-eligible product where the discount-cap floor
dominates over the relaxed `min_price`, so nothing was masking this.

**Decision (confirmed with the user):** no behavior change — Section 2F's
floor formula stands. Liquidation relaxes `min_price` only; it was never
meant to bypass `max_discount_pct`/`qty_breaks`.

**Fix (communication only, not logic):**
- `personalization.liquidation_rationale()` now says "min_price relaxed
  from X to Y" instead of "floor relaxed from X to Y" — it describes one
  input to the floor formula, not necessarily the resulting negotiation
  floor.
- `negotiation_loop.py`'s `__main__` now also prints "Effective
  negotiation floor at qty=N: F (driven by evidence_path)" right after
  the policy (incl. LTV bonus) is finalized, using the same
  `_floor_price()` the negotiation itself will enforce — so the console
  shows the real operative number before round 1, not just the
  `min_price` component, and this exact class of misreading can't recur.
- Added `test_liquidation_adjusted_min_price_is_the_single_source_of_truth_in_real_negotiation_rounds`
  (`test_ai_merchant_agent.py`) proving the single-source-of-truth claim
  in a scenario where `min_price` genuinely *is* the binding floor after
  relaxation (unlike `SKU-ELEC-003` at qty=3) — verified against real
  per-round `evaluate_ai()` output inside `run_negotiation()`, not
  `liquidation_adjusted_min_price()` called in isolation.

**Follow-up flagged, not fixed here:** constructing that test scenario
surfaced a real, narrower latent issue — `_floor_price()`'s
`evidence_path` always cites the discount-tier evidence regardless of
which term of `max(min_price, discount-computed)` actually won. Before
Section 2F this was unreachable (the absolute min_price-reject always
caught a below-min_price offer first when min_price was binding); Section
2F's fall-through makes it reachable for a liquidation-relaxed product
whose relaxed `min_price` is still the binding term — the clamped PRICE
stays correct, but `evidence_paths`/`rationale` can misname
`policy.max_discount_pct` when `policy.min_price` was the actual driver.
Spawned as a separate, scoped follow-up rather than fixed inline here.

---

## Section 2I — `_floor_price()` evidence_path fix: correctly attribute a dominant min_price (bug-fix follow-up, 2026-09-01)

Fixes the latent issue flagged at the end of Section 2H. `_floor_price()`
computed the right clamped price (`max(min_price, discount-computed)`) but
always returned the discount-tier `evidence_path` (`policy.max_discount_pct`
or `policy.qty_breaks[N].discount_pct`) regardless of which operand of that
`max()` actually won. Harmless before Section 2F — an offer below
`min_price` was always caught by `check_guardrails()`'s earlier,
unconditional absolute-reject first, so the mislabeled branch was
unreachable whenever `min_price` was the binding term. Section 2F's
liquidation fall-through made it reachable: for a liquidation-relaxed
product whose relaxed `min_price` is still higher than the discount-derived
floor, execution now falls through to the counter branch with `min_price`
as the true driver, but the audit trail cited the discount tier instead.

**Fix:** `_floor_price()` now compares `policy["min_price"]` against the
discount-computed value directly and returns `"policy.min_price"` as the
`evidence_path` whenever `min_price` is `>=` that computed value (i.e.
whenever it's the term that wins the `max()`); the discount-tier evidence
path from `_applicable_tier()` is returned only when the discount-computed
value genuinely wins. The clamped price itself is unchanged — this is an
audit-trail-attribution fix only, not a pricing change. Both call sites
(`check_guardrails()` and `_validate_against_guardrails()`'s clamp branch,
via `_build_merchant_prompt()`) already propagate whatever `_floor_price()`
returns, so no other code needed to change.

Added
`test_guardrail_evidence_path_names_min_price_when_it_dominates_after_liquidation_relaxation`
(`test_ai_merchant_agent.py`) — a liquidation-relaxed product (`min_price`
relaxed to 1950, discount-derived floor 1900) whose relaxed `min_price` is
still the binding term, asserting `evidence_paths == ["policy.min_price"]`
on the resulting counter (previously `["policy.max_discount_pct"]`).

Added a direct companion,
`test_guardrail_evidence_path_still_names_max_discount_pct_when_it_dominates`
(2026-09-02 follow-up — flagged as missing after the first pass only
covered the min_price-dominant side) — proves the fix didn't regress the
original, already-correct case. Reuses `POLICY` (`test_ai_merchant_agent.py`'s
module-level fixture, already the "discount floor is binding" case —
`min_price=3799` < discount floor `4399.12`) rather than a new product,
calling `check_guardrails()` directly and asserting
`evidence_paths == ["policy.max_discount_pct"]` by exact equality. Pairs
directly with the min_price-dominant test above for a clean side-by-side
regression check. Verified via the full test suite: 79 passed, 3 skipped
— up from 76 passed pre-fix, zero regressions.

---

## Section 2J — Structural fix: liquidation relaxes the discount-cap floor, not min_price (2026-09-02)

**Investigation:** live-data analysis found that `liquidation_adjusted_min_price()`
(Section 2E's original mechanism, relaxing `min_price` toward
`cost_floor_price()`) had **zero real effect** on any negotiation outcome
in the actual generated catalog. `min_price = cost * 1.15` sits well
below the discount-cap floor (`list_price * (1 - max_discount_pct/100)`)
for 79/80 products (seed=42) -- confirmed empirically across all 8 aged
outliers: every one's effective floor (`max(min_price, discount-cap
floor)`) was identical whether or not liquidation had engaged, because
the discount-cap term always dominated regardless. The mechanism
computed and logged a value correctly (Section 2E/2F/2G/2H/2I all
verified *that* faithfully) -- but the value it computed was never the
one that mattered for 99% of products.

**Fix:** liquidation now relaxes the **discount-cap floor** itself,
ramping it toward `min_price` (the true, unrelaxed margin floor) as
`days_in_inventory` grows past the category threshold -- the opposite
direction from the original mechanism. `min_price` is no longer touched
by liquidation at all; it stays exactly the raw catalog value (still
defensively clamped up to `cost_floor_price()`, Section 2E, unchanged).

```python
# personalization.py
def liquidation_relaxation_fraction(product):
    """0.0 (untouched) to 1.0 (fully ramped) -- how far past this
    product's category threshold days_in_inventory has progressed."""
    days = product.get("days_in_inventory", 0)
    threshold = liquidation_threshold_for(product.get("category"))
    if days <= threshold:
        return 0.0
    return min(1.0, (days - threshold) / LIQUIDATION_RAMP_DAYS)

# product_to_policy() no longer calls anything to adjust min_price;
# it just clamps the raw value up to cost_floor_price() (Section 2E,
# unchanged) and adds the new field:
policy["liquidation_relaxation_fraction"] = liquidation_relaxation_fraction(product)

# merchant_agent.py -- _floor_price(), the one place that knows the
# qty-dependent discount-computed price
discount_pct, discount_evidence_path = _applicable_tier(policy, qty)
computed = policy["list_price"] * (1 - discount_pct / 100)
liquidation_fraction = policy.get("liquidation_relaxation_fraction", 0.0)
if liquidation_fraction > 0:
    computed = computed - liquidation_fraction * (computed - policy["min_price"])
if policy["min_price"] >= computed:
    return policy["min_price"], "policy.min_price"
return computed, discount_evidence_path
```

Defaults to `0.0` when the field is absent (`merchant_policy.json`'s
hardcoded fallback, every pre-fix test fixture), so behavior there is
byte-identical to before. The interpolation happens directly in
price-space, targeting `min_price` as an absolute price -- not as a
discount percentage -- so it's independent of `HARD_DISCOUNT_CEILING_PCT`
(Section 2C), which governs a different mechanism (LTV-bonus stacking on
the nominal discount rate) and was never meant to constrain how far
liquidation can approach the merchant's own already-established margin
floor.

**Section 2F/2I interactions, confirmed still correct:**
- `min_price_is_liquidation_relaxed` (Section 2F) keeps its exact
  assignment logic (`days_in_inventory > threshold`) and role in
  `check_guardrails()` -- unchanged. Still needed: once a product's ramp
  fully reaches `min_price` (fraction `1.0`), `min_price` itself becomes
  the binding floor, and it must stay counter-able rather than reverting
  to an instant reject -- the exact scenario Section 2F fixed, now
  reachable via a different path (see live verification below).
- Section 2I's evidence_path fix required no changes at all:
  `_floor_price()`'s `if policy["min_price"] >= computed` check now simply
  receives an already-liquidation-adjusted `computed`, and naturally
  flips `evidence_path` to `"policy.min_price"` exactly when the ramp
  reaches full (or a defensively-clamped `min_price` is reached early),
  and stays `"policy.max_discount_pct"`/`qty_breaks[N]` for partial ramps
  where the discount-cap term still wins. Verified with two new tests
  (`test_liquidation_lowers_the_effective_floor_measurably_even_when_discount_cap_was_binding`,
  `test_liquidation_partial_ramp_still_cites_max_discount_pct_while_measurably_lowering_the_floor`)
  and confirmed against the real seed=42 catalog.

**Live verification (real seed=42 catalog, all 8 aged outliers):**

| SKU | ramp fraction | fresh floor | aged floor | drop | evidence |
|---|---|---|---|---|---|
| SKU-ELEC-003 | 1.00 | 7098.68 | 5093.53 | 28.2% | policy.min_price |
| SKU-APRL-003 | 0.55 | 1825.09 | 1808.53 | 0.9% | policy.max_discount_pct |
| SKU-HOME-003 | 1.00 | 2601.84 | 2243.96 | 13.8% | policy.min_price |
| SKU-SPRT-003 | 1.00 | 6689.73 | 6152.32 | 8.0% | policy.min_price |
| SKU-BOOK-003 | 1.00 | 1173.52 | 1013.20 | 13.7% | policy.min_price |
| SKU-BEAU-003 | 0.71 | 2202.50 | 1756.08 | 20.3% | policy.max_discount_pct |
| SKU-TOYS-003 | 1.00 | 777.43 | 726.70 | 6.5% | policy.min_price |
| SKU-OFFC-003 | 0.79 | 824.49 | 772.88 | 6.3% | policy.max_discount_pct |

Every aged product now shows a real, non-zero drop -- versus 0.0% for
all 8 before this fix. Also confirmed live end-to-end
(`PRODUCT_ID=SKU-ELEC-003`): a round-1 offer of `3800` (below `min_price`)
still draws a `COUNTER` at `5093.53` citing `policy.min_price`, not an
instant reject -- Section 2F's fix still holds under the new mechanism.

**`liquidation_rationale()` message updated** (personalization.py) --
was "min_price relaxed from X to Y" (Section 2G's wording, itself already
correcting an earlier "floor relaxed" phrasing that Section 2H flagged as
misleading); now: "the discount-cap floor is relaxed N% of the way toward
the margin floor (min_price P)" -- no longer implies min_price's own
value changed, and no longer implies a single quotable "from X to Y"
floor price exists independent of qty (the actual floor is qty-dependent,
computed by `_floor_price()` at negotiation time). The `negotiation_loop.py`
"Effective negotiation floor at qty=N" console line (Section 2H) needed no
changes -- it already calls `_floor_price()` directly, so it automatically
reflects the corrected, now-measurably-lower value.

Verified via the full test suite: 80 passed, 3 skipped, zero regressions
(up from 79 passed pre-fix). `liquidation_adjusted_min_price()` is
removed (no longer meaningful under the new mechanism); every test that
called it directly was rewritten against `liquidation_relaxation_fraction()`
and/or the real effective floor via `merchant_agent._floor_price()`.

---

## Section 2K — Bug fix: Layer 2 could counter past the round cap when the final offer already cleared the floor (2026-09-02)

**Bug report:** a live run on `SKU-ELEC-003` (`BUYER_MODE=ai`,
`MERCHANT_MODE=ai`, `BUYER_BUDGET=5500`) ran 6 rounds before terminating,
not 5. Round 6's reject cited `"Round 6 exceeds
policy.max_negotiation_rounds (5)"` — the round cap fired, but one round
too late. Initially suspected as a round-counting off-by-one introduced
by Milestone 4's `negotiation_loop.py` changes.

**Investigation:** `run_negotiation()`'s loop and `check_guardrails()`'s
`round > max_rounds` / `round >= max_rounds` comparisons were confirmed
unaltered and correct — `test_round_cap_enforcement_still_works_with_ai_merchant`
already asserts exact equality (`== max_negotiation_rounds`, not `<=`)
and was passing. Four separate live reproductions (scripted+rules,
ai+ai, and two on `merchant_policy.json`) all terminated correctly at
round 5. The user then supplied the actual failing transcript, which
revealed the real mechanism: at round 5, the buyer's offer (`5500`)
already cleared the floor (`5093.53`) — `check_guardrails()` correctly
computed `"accept"` — but Layer 2 (`decide_strategy()`) proposed a
`counter` at `5550` anyway, holding out for a better price.
`_validate_against_guardrails()`'s counter-handling branch only checks a
proposed counter's *price* against the floor; it has no rule for whether
countering at all is still legal on the *final* round, where any counter
necessarily requires a round beyond the cap to resolve. The counter
passed straight through un-clamped, the buyer submitted a 6th (illegal)
offer, and only THEN did `check_guardrails()`'s `round > max_rounds`
check catch it — one round late.

This is **not** a Milestone 4 regression: `_validate_against_guardrails()`
has been unchanged since Section 2B (Milestone 3b); Milestone 4's diff
never touched `merchant_agent.py`. It's a pre-existing design gap that
simply had never been exercised before — `test_round_cap_enforcement_still_works_with_ai_merchant`'s
buyer never reaches the floor at all, so `guardrail_verdict["decision"]`
is never `"accept"` mid-negotiation in that test; the gap needed a buyer
whose offer crosses the floor at or near the final round specifically.

**Fix** (`merchant_agent._validate_against_guardrails()`):

```python
# strategy.action == "counter"
if guardrail_verdict["decision"] == "accept" and round >= policy["max_negotiation_rounds"]:
    # The offer already clears the floor and this is the LAST allowed
    # round -- Layer 2 has no discretion to hold out here, since any
    # counter would require a round beyond the cap to resolve.
    result = dict(guardrail_verdict)
    result["rationale"] = (
        result["rationale"] + " Layer 2 proposed a counter instead of accepting on the final round; "
        "overridden -- a further round would exceed policy.max_negotiation_rounds."
    )
    return result, True

if strategy.counter_offer is None:
    ...
```

Placed before the existing `strategy.counter_offer is None` check, so it
applies regardless of whether Layer 2's proposed counter was well-formed.
On earlier rounds, Layer 2 keeps its existing discretion to hold out for
more even when the current offer already clears the floor (legitimate
merchant strategy) — this only removes that discretion on the round
where holding out is no longer possible to honor.

**Verification:**
- Directly re-ran the exact live-run scenario through `evaluate_ai()`:
  round 5, offer `5500`, Layer 2 proposing counter `5550` — now correctly
  returns `"accept"` at `5500`, `guardrail_clamped: true`.
- Live-verified end-to-end with the user's exact reproduction command
  (`PRODUCT_ID=SKU-ELEC-003 BUYER_MODE=ai MERCHANT_MODE=ai BUYER_BUDGET=5500`):
  negotiation now correctly closes at round 5 with `AGREEMENT_RECORDED`,
  no round 6.
- `test_round_cap_enforcement_still_works_with_ai_merchant` re-run and
  still passes — confirmed its assertions were already exact
  (`== max_negotiation_rounds`); the gap was scenario coverage, not
  assertion looseness, so it was left as-is (it correctly covers a
  different, valid scenario: a buyer that never reaches the floor).
- Added `test_layer_2_cannot_counter_past_the_round_cap_when_the_final_offer_already_clears_the_floor`
  (`test_ai_merchant_agent.py`) — a dedicated end-to-end test for the
  scenario the existing test didn't cover: buyer offers stay below the
  floor for rounds 1-4 and land exactly on it at round 5, while Layer 2
  keeps proposing a much higher counter every round including the last.
  Confirmed this test fails (`REJECTED` instead of `AGREEMENT_RECORDED`)
  against the pre-fix code and passes against the fix, by temporarily
  reverting the fix and re-running it.
- Full suite: 81 passed, 3 skipped, zero regressions (up from 80 passed
  pre-fix).

---

## Section 2L — Fix: FORCE_INSUFFICIENT_INVENTORY console box showed the real (irrelevant) stock number (2026-09-02)

**Bug report:** the `FORCE_INSUFFICIENT_INVENTORY` demo flag (Section 4,
Milestone 4) correctly staged the `ROLLBACK`, but its console box showed
`"Requested: 3, In stock: 138"` — real `current_inventory`, which is
almost always well above `qty` (that's the entire reason to force this
scenario rather than deplete real catalog data). The state was right;
the displayed scenario wasn't believable.

**Fix:** `run_full_transaction()` now computes `simulated_stock = max(0,
qty - 1)` for the STAGED case only (never for a genuine shortfall, where
real `current_inventory` is already believably low by definition), and
returns it on the outcome dict as `"simulated_stock"`.
`_print_insufficient_inventory_summary()` displays
`outcome.get("simulated_stock", product["current_inventory"])` — the
simulated number when staged, the real one when genuine. `qty - 1` is
always a believable near-miss regardless of the negotiated quantity,
including `qty=1` (`simulated_stock=0`).

The audit log stays fully honest throughout — untouched by this fix's
console-only concern: the `insufficient_inventory` rationale already
named the real `current_inventory` truthfully alongside the staged
label, and now also names the simulated number for clarity, e.g.:

> `"Insufficient inventory staged via FORCE_INSUFFICIENT_INVENTORY for SKU-ELEC-002 (simulated stock 2; real current_inventory is actually 60, qty 3); rolling back before any payment call."`

Real `current_inventory` in `catalog.json` was never touched by this
flag before this fix and still isn't — only the *displayed* number
changed.

**Verification:**
- Live-verified: `FORCE_INSUFFICIENT_INVENTORY=1` now shows `"Requested: 3, In stock: 2"` — internally consistent.
- Added `test_force_insufficient_inventory_reports_a_believable_simulated_stock_not_the_real_value`
  and `test_real_shortfall_still_reports_actual_current_inventory_not_simulated`
  (`test_personalization.py`) — confirm `simulated_stock` is `qty - 1`
  and present only on the staged path, real `current_inventory` is
  untouched, and the audit log names both the simulated and real values.
- Full suite: 83 passed, 3 skipped, zero regressions (up from 81 passed
  pre-fix).

---

## Section 2M — Risk Agent (Milestone 5)

> **Superseded 2026-09-02 (Section 2N, then Section 2O):** this section's
> "high" and "moderate" rows and its Wiring code below describe the
> ORIGINAL design — `high` risk blocked the negotiation entirely
> (`NEGOTIATION_DECLINED`, no offer ever generated), and `moderate` forced
> the human-approval gate. Both reframed the same day, before this
> design shipped to any real demo — kept here as historical record per
> this file's layered-correction discipline. See Section 2N for `high`'s
> current behavior (a forced `max_discount_pct` of 0, not a block) and
> Section 2O for `moderate`'s (no gate, zero added friction — logged
> only). The `none` row and the threshold table below are UNCHANGED.

Deterministic, code-only — no LLM call anywhere in this feature. Reuses
the exact "bounded input feeding into the existing guardrails" pattern
already used for LTV (Section 2C) and liquidation (Section 2J):
`personalization.py` computes a signal from data already available;
`merchant_agent.py`/`negotiation_loop.py` are the only things that act on
it, and only `negotiation_loop.py` needed changes here (`merchant_agent.py`
is untouched — the risk gate runs entirely before any offer reaches it).

### Thresholds — confirmed with the user before implementing, same discipline as the liquidation category table

Two independent factors, both knowable **before any offer exists** (a
third candidate, "aggressive lowball" — offer vs. floor — was considered
and explicitly dropped: it structurally can't be evaluated pre-offer, and
`check_guardrails()`'s own reject/counter logic already covers a
below-floor offer once one exists):

| Factor | Threshold | Why |
|---|---|---|
| `new_buyer` | `count_prior_orders(buyer_id, orders) < 2` (i.e. 0 or 1 prior orders) | The seed=42 dataset has exactly 1 buyer with 0 prior orders and none with exactly 1 — this threshold currently catches only that true zero-history buyer. |
| `large_request` | `qty >= min(tier["min_qty"] for tier in policy["qty_breaks"])` (currently a flat 10 across every generated catalog product and `merchant_policy.json`'s fixture); falls back to `RISK_LARGE_QTY_FALLBACK_THRESHOLD = 10` if `qty_breaks` is empty | `orders.json` has no historical qty data to compute a "typical size per category" from (the generator uses qty only to derive `amount`, then discards it) — confirmed with the user to reuse the product's own bulk-tier threshold instead, rather than add a new data field. |

`personalization.risk_assessment(buyer_id, qty, qty_breaks, orders)` —
pure function, returns `{"level": "none"|"moderate"|"high", "factors":
[...], "evidence_paths": [...], "rationale": str}`. `factors` is
human-readable prose (for the rationale); `evidence_paths` holds
schema-path-style strings (`"buyer.order_history"`, `"policy.qty_breaks"`)
matching every other guardrail's convention, kept deliberately separate
from the prose.

| Level | Condition | Effect |
|---|---|---|
| `high` | Both factors | `NEGOTIATION_DECLINED` — negotiation never starts, no offer is ever generated. |
| `moderate` | Exactly one factor | Negotiation proceeds normally; the human-approval gate is forced later, regardless of `policy.transaction_approval_threshold`. |
| `none` | Neither factor | No effect — byte-identical to a policy with no Risk Agent involvement at all. |

The check only runs when both `buyer_id` and `orders` are supplied to
`run_negotiation()`/`run_full_transaction()` (both default `None`) — the
`BUYER_ID`-unset path (including the `merchant_policy.json` smoke-test
fixture) is completely unaffected, confirmed with the user rather than
treating an anonymous buyer as automatically "new."

### Wiring

`run_negotiation()` runs the check as literally its first action, before
`buyer.initial_offer()` is ever called:

```python
if buyer_id is not None and orders is not None:
    risk = personalization.risk_assessment(buyer_id, buyer.qty, policy.get("qty_breaks", []), orders)
    risk_level = risk["level"]
    if risk_level != "none":
        _log(on_event, 0, "risk-agent", "risk_review", None, risk["rationale"], risk["evidence_paths"], path=audit_path)
    if risk_level == "high":
        return {"state": "NEGOTIATION_DECLINED", "offer": None, "risk_level": risk_level}
```

A `risk_review` entry (new `agent: "risk-agent"` value, joining
`"buyer-agent"`/`"merchant-agent"`) is logged only when risk is
moderate or high — mirroring `liquidation_applied`'s "only log when
something notable happened" precedent (Section 2E), not logged on every
negotiation. Uses `round_num=0` (not `None`) for its `on_event` callback:
`_print_event()` reads `round_num=None` as "Payment phase" (Milestone 2
convention); this check runs before round 1, not during payment, so `0`
reads correctly on the console as "before round 1."

`risk_level` is carried on the `AGREEMENT_RECORDED` outcome dict (`None`
on every pre-Milestone-5 caller and on non-agreement outcomes) so
`run_full_transaction()` can act on it later:

```python
risk_level = negotiation_outcome.get("risk_level")
if total > threshold or risk_level == "moderate":
    ...  # same approval_requested / approval_granted / approval_declined
         # flow as the price-threshold case, with rationale/evidence_paths
         # ("risk_agent.risk_level") distinguishing which condition fired
```

`buyer_id`/`orders` are threaded through `run_full_transaction()` (both
new, optional, default `None`) straight to `run_negotiation()` — no other
change to that function. `__main__` passes its existing `buyer_id`
variable and the `orders` list already loaded for the LTV computation
(Section 2C) — no extra I/O.

### Verification

- Live-verified all three levels against the real seed=42 dataset:
  `BUYER-001` (the one true zero-order buyer) with the default demo qty
  (3, below the large-request threshold) → `moderate`, forces approval
  even on a transaction (`5008.50` INR) far below
  `policy.transaction_approval_threshold` (`20000`). Direct
  `run_negotiation()` call with `qty=10` → `high` → `NEGOTIATION_DECLINED`,
  `offer: null`, confirmed via the audit log that the risk_review entry
  is the *only* entry ever written (no offer was ever generated).
- Added `test_risk_assessment_pure_function_level_combinations`,
  `test_new_buyer_large_request_is_declined_before_any_offer_generated`,
  `test_established_buyer_normal_qty_proceeds_completely_unaffected`, and
  `test_moderate_risk_forces_human_approval_even_for_small_transaction`
  (`test_personalization.py`) — covering all four requirement #5 cases,
  including proving the established-buyer case is byte-identical (price,
  qty) to omitting the Risk Agent entirely.
- Full suite: 87 passed, 3 skipped, zero regressions (up from 83 passed
  pre-feature).

---

## Section 2N — Reframe: HIGH risk tightens pricing instead of blocking the negotiation (2026-09-02)

**Request:** reframe HIGH risk (both factors) as a PRICING-ABUSE signal,
not a trust/fraud signal — remove `NEGOTIATION_DECLINED` entirely; the
negotiation now runs normally, but with `max_discount_pct` forced to 0
for that one negotiation (full list price, no room to negotiate below
it), feeding into the SAME guardrail mechanism already used for
LTV/liquidation rather than a new separate code path. HIGH still forces
the human-approval gate, same as MODERATE — confirmed with the user
rather than assumed ("the risk hasn't gone away, just the response to it
has").

### A catch found while implementing, not just asserted

Capping `max_discount_pct` alone would NOT have achieved "full list
price only" — `merchant_agent._applicable_tier()` (Section 2) always
prefers a matching `qty_breaks` tier's `discount_pct` over
`max_discount_pct`, and `large_request` (one of the two factors HIGH
requires) is *defined* as `qty >= the product's lowest qty_breaks tier` —
meaning any qty that qualifies for HIGH risk will always also match at
least that tier, silently overriding a `max_discount_pct=0` cap and
defeating the intent entirely. Confirmed live before shipping: at
`qty=10` on `SKU-ELEC-007` (`discount_pct=21%` at that tier), capping
`max_discount_pct` alone would have settled at `1515.98` (list price
`1918.96 × 0.79`), not full list price.

**Fix:** `apply_risk_discount_cap()` caps BOTH `max_discount_pct` and
every `qty_breaks` tier's `discount_pct` (via `min()`, so it only ever
tightens, never raises a rate):

```python
def apply_risk_discount_cap(policy, risk):
    override_pct = risk.get("max_discount_pct_override")
    if override_pct is None:
        return dict(policy)
    effective = dict(policy)
    effective["max_discount_pct"] = min(policy["max_discount_pct"], override_pct)
    effective["qty_breaks"] = [
        {**tier, "discount_pct": min(tier["discount_pct"], override_pct)}
        for tier in policy.get("qty_breaks", [])
    ]
    return effective
```

`risk_assessment()` now returns `max_discount_pct_override` (`0` for
`"high"`, `None` otherwise, via new constant
`RISK_HIGH_DISCOUNT_OVERRIDE_PCT = 0`) instead of a decline signal.
`run_negotiation()` applies the cap to its own local `policy` (a
reassignment — the caller's original dict is never mutated) right after
the risk check, before `buyer.initial_offer()` — everything downstream
(the offer/counter loop, `_floor_price()`, the audit trail) automatically
runs against the tightened policy with zero changes to
`merchant_agent.py`. `NEGOTIATION_DECLINED`'s decline branch, the dead
`_print_terminal_summary()` handling for it, and the "high risk never
reaches run_full_transaction()" docstring claim are all removed.

`run_full_transaction()`'s approval-gate condition changed from
`risk_level == "moderate"` to `risk_level in ("moderate", "high")` —
HIGH now also forces the gate, per the confirmed assumption above.
`risk_assessment()`'s `"high"` rationale now names both the discount
override and the forced approval gate explicitly, so the audit trail
stays fully explainable despite no longer blocking anything.

### A second inconsistency found while live-verifying, fixed the same way as Section 2L

`negotiation_loop.py`'s "Effective negotiation floor at qty=N" console
line (Section 2H) is computed in `__main__`, BEFORE `run_negotiation()`
(and therefore before the risk check) ever runs — for a HIGH-risk
scenario this printed the stale, pre-cap floor while the negotiation
actually settled at the post-cap (full list price) value, a visible
discrepancy on exactly the scenario this feature exists to demo. Fixed
by computing a throwaway `preview_policy` in `__main__` (calling
`risk_assessment()`/`apply_risk_discount_cap()` read-only, purely for
the print — `risk_assessment()` is a pure function with no logging side
effects) so the printed floor matches what `run_negotiation()`'s own,
independent, official risk check will actually enforce.

### `DEMO_QTY` env var (Milestone 5 follow-up)

`negotiation_loop.py` hardcoded the negotiated quantity at `DEFAULT_DEMO_QTY = 3`
with no way to override it from the CLI — meaning HIGH risk (which needs
`qty >= 10`) was unreachable through `python -m src.negotiation_loop`
without editing source. `DEMO_QTY` (parsed like `BUYER_BUDGET`, a clear
`SystemExit` on invalid input rather than a silent fallback) now
overrides it, so both MODERATE and HIGH are demonstrable end to end
through the normal CLI.

### Verification

- Live-verified end to end via the CLI (`PRODUCT_ID=SKU-ELEC-007
  BUYER_ID=BUYER-001 DEMO_QTY=10`, real Razorpay test-mode payment):
  negotiation proceeds normally, settles at `1918.96` (== `list_price`,
  confirmed zero discount room), `approval_requested` correctly cites
  "High risk," and the "Effective negotiation floor" preview line
  matches the settled price exactly.
- Replaced `test_new_buyer_large_request_is_declined_before_any_offer_generated`
  (renamed `..._proceeds_with_zero_discount_room_via_run_negotiation`) to
  assert the new behavior instead of `NEGOTIATION_DECLINED`. Added
  `test_high_risk_forces_list_price_only_and_still_requires_human_approval`
  (`test_personalization.py`) — the requirement's explicit replacement
  test: a HIGH-risk negotiation reaches `COMPLETED`, the effective floor
  equals `list_price` exactly, and human-approval fires regardless of
  transaction total.
- Full suite: 88 passed, 3 skipped, zero regressions (up from 87 passed
  pre-reframe).

---

## Section 2O — Drop forced human-approval from MODERATE risk (2026-09-02)

**Request:** drop forced human-approval from MODERATE risk entirely.
Final behavior across all three tiers:

| Level | Pricing | Approval gate | Audit |
|---|---|---|---|
| `none` | untouched | not forced | no `risk_review` entry logged at all |
| `moderate` (one factor) | untouched | **not forced** (changed by this section) | `risk_review` entry logged, naming the single factor |
| `high` (both factors) | `max_discount_pct` forced to 0 (Section 2N, unchanged) | forced (Section 2N, unchanged) | `risk_review` entry logged, naming both factors and both consequences |

**Fix:** `run_full_transaction()`'s approval-gate condition changed from
`risk_level in ("moderate", "high")` back to `risk_level == "high"` —
`total > threshold` is still evaluated independently either way (a large
enough transaction still triggers the gate regardless of risk level, for
any tier). `personalization.risk_assessment()` needed no changes at all:
its `"moderate"` rationale never mentioned approval to begin with (only
`"high"`'s did, and still does) — dropping the gate for `"moderate"`
was purely a `negotiation_loop.py` change.

**Verification:**
- Live-verified: `PRODUCT_ID=SKU-ELEC-007 BUYER_ID=BUYER-001` (MODERATE,
  new buyer only) now goes straight from `inventory_hold` to
  `payment_initiated` with no `approval_requested` entry at all, and
  settles at the normal discounted price (`1669.50`, not `list_price`).
- Rewrote `test_moderate_risk_forces_human_approval_even_for_small_transaction`
  → `test_moderate_risk_logs_but_does_not_gate_or_restrict_pricing`
  (`test_personalization.py`): asserts `approval_calls == []` (the gate
  is never invoked at all, not just "not required"), the settled price
  equals the normal discount-capped floor (not `list_price`), and the
  `risk_review` entry is still present naming only the one factor.
  `test_high_risk_forces_list_price_only_and_still_requires_human_approval`
  is unchanged — HIGH's behavior is untouched by this section.
- Full suite: 88 passed, 3 skipped, zero regressions (same count as
  pre-fix — one test rewritten in place, not added).

---

## Section 2P — Investigation + console-explainability fix: HIGH-risk console rationale vs. actual floor (2026-09-02)

**Bug report:** the console states `"max_discount_pct forced to 0 --
full list price only, no negotiation room"` for a HIGH-risk negotiation,
but the effective floor still comes from
`policy.qty_breaks[0].discount_pct`, not `list_price` — implying
`apply_risk_discount_cap()` (Section 2N) wasn't actually zeroing
`qty_breaks`.

**Investigation:** could not reproduce a wrong PRICE. A direct
`risk_assessment()` → `apply_risk_discount_cap()` → `_floor_price()`
check, and a full live CLI run of the exact reproduction
(`PRODUCT_ID=SKU-ELEC-007 BUYER_ID=BUYER-001 DEMO_QTY=10`), both showed
`floor == list_price` exactly (`1918.96 == 1918.96`) — `qty_breaks` was
correctly zeroed by Section 2N's fix. The evidence label naming
`"policy.qty_breaks[0].discount_pct"` is accurate but genuinely
confusing at 0% — it reads as if a qty-break discount is active when
none is; `_floor_price()`'s evidence-attribution logic (Section 2I)
correctly names whichever term won the `max()`, it just doesn't say
*how much* that term is currently worth.

**A real gap found regardless:** the existing HIGH-risk test
(`test_high_risk_forces_list_price_only_and_still_requires_human_approval`)
used `_demo_product()`, which has EMPTY `qty_breaks` — so it never
actually exercised the `qty_breaks`-zeroing code path in
`apply_risk_discount_cap()` at all; its "large_request" factor was
triggered via `RISK_LARGE_QTY_FALLBACK_THRESHOLD` instead. The test
would have passed even if that zeroing loop were entirely broken.
Confirmed by temporarily reverting the zeroing and re-running the test:
it failed as expected (`22 == 0` assertion, then restored and re-verified
passing).

**Fixes:**
1. **Test coverage** — rewrote the test to use a product with REAL
   `qty_breaks` tiers (mirroring `SKU-ELEC-007`'s shape: `min_qty=10,
   discount_pct=22`), at a qty matching that tier, and added an explicit
   assertion that the tier's `discount_pct` was actually zeroed
   (`tightened_policy["qty_breaks"][0]["discount_pct"] == 0`) — not just
   that the final floor happened to equal `list_price`, which could
   theoretically be coincidental.
2. **Console explainability** — `run_negotiation()`'s risk_review log
   entry now states the ACTUAL, freshly-recomputed effective floor in
   the SAME rationale line as the "no negotiation room" claim, via the
   exact `_floor_price()` call the negotiation itself is about to use
   (against the already-tightened `policy`) — not a separate,
   independently-asserted claim that could silently drift from what
   `check_guardrails()` actually enforces if a future change broke the
   zeroing again:

   ```python
   if risk_level == "high":
       effective_floor, _ = merchant_agent._floor_price(policy, buyer.qty)
       rationale = f"{rationale} Effective floor for this negotiation: {effective_floor:.2f} {policy['currency']}."
   ```

   Example (live-verified): `"...max_discount_pct forced to 0 for this
   negotiation -- full list price only, no negotiation room -- and the
   human-approval gate is forced regardless of
   policy.transaction_approval_threshold. Effective floor for this
   negotiation: 1918.96 INR."` If the zeroing ever broke again, this
   number would visibly disagree with `list_price` right there, in the
   line making the "no negotiation room" claim — not two separately
   printed/logged facts that could quietly drift apart.

**Verification:**
- Full suite: 88 passed, 3 skipped, zero regressions.

---

## Section 2Q — Console label follow-up: state the live discount percentage inline (2026-09-02)

**Request:** the exact ambiguity from Section 2P (a bare
`"policy.qty_breaks[0].discount_pct"` label reading as if a real
discount were active, when the Risk Agent had zeroed it to 0%) cost two
rounds of back-and-forth before it was conclusively ruled out as a real
computation bug (proven both directions with side-by-side arithmetic and
a full live payment run in Section 2P). Rather than leave that ambiguity
for a judge to re-discover in the audit log, state the live percentage
inline in the label itself, everywhere it appears — e.g.
`"policy.qty_breaks[0].discount_pct (0%)"`.

**Fix:** new `merchant_agent._evidence_label(policy, evidence_path)` —
a display-only annotator, used exclusively in rationale/console text:

```python
def _evidence_label(policy, evidence_path):
    if evidence_path == "policy.max_discount_pct":
        return f"{evidence_path} ({policy['max_discount_pct']}%)"
    if evidence_path.startswith("policy.qty_breaks["):
        idx = int(evidence_path.split("[", 1)[1].split("]", 1)[0])
        return f"{evidence_path} ({policy['qty_breaks'][idx]['discount_pct']}%)"
    return evidence_path  # policy.min_price -- already an absolute value, stated directly elsewhere
```

Deliberately **not** used for the `evidence_paths` audit-schema field
(Section 4: `"array of strings | Dotted paths into the policy object"`)
— that field stays pure dotted-path strings, unchanged, so every
existing exact-match test on it (Section 2I's evidence-attribution
tests especially) needed zero changes. The annotation is applied only
where a human (or a judge) actually reads the label: every rationale
string in `check_guardrails()` (accept/counter/round-cap-reject),
`_validate_against_guardrails()`'s clamp rationale, `_build_merchant_prompt()`'s
LLM-facing floor description, and both "Effective negotiation floor"
console lines in `negotiation_loop.py` (the pre-negotiation preview and
the risk_review entry's inline floor statement from Section 2P).

**Live-verified**, same reproduction as Section 2P — both occurrences
now show the annotated label:

```
Effective negotiation floor at qty=10: 1918.96 INR (driven by policy.qty_breaks[0].discount_pct (0%))
...
    Offer price 1631.12 is below the allowed floor 1918.96 for qty 10, per policy.qty_breaks[0].discount_pct (0%). Countering at 1918.96.
```

**Verification:**
- Added `test_evidence_label_states_the_live_discount_percentage_inline`
  (`test_ai_merchant_agent.py`) — two cases: an ordinary, non-risk
  counter must show the real, non-zero percentage (`POLICY`'s 12%,
  proving the label isn't hardcoded to always show `0%`), and a
  HIGH-risk-zeroed counter (product with a real `qty_breaks` tier,
  mirroring Section 2P's `SKU-ELEC-007` gap-closing test) must show
  `(0%)` and must NOT contain the raw, pre-tightening catalog rate
  (`22%`) anywhere in the rationale.
- Full suite: 89 passed, 3 skipped, zero regressions (up from 88 passed
  pre-fix).

---

## Section 2R — Multi-merchant support (Milestone 6, 2026-09-03)

Two merchant profiles, `data/merchants.json`, confirmed with the user
before generating any data (same discipline as the liquidation category
table and the Risk Agent thresholds):

| Field | `MERCH-001` | `MERCH-002` |
|---|---|---|
| `merchant_name` | Voltstream Electronics | Hearth & Home Living |
| Categories (whole-category split, confirmed with the user) | Electronics, Office & Stationery, Toys & Games, Sporting Goods & Outdoors | Apparel & Fashion, Home & Kitchen, Books & Media, Beauty & Personal Care |
| Product count | 40 | 40 |
| `risk_approval_tier` | `strict` | `standard` |

`business_description`, `currency`, `supported_payment_methods`,
`shipping_rules`, `return_policy` are real profile data (displayed at
negotiation start, loaded from the file) but — confirmed scope, not an
oversight — not yet wired into any guardrail or pricing decision. Only
`risk_approval_tier` actually changes behavior in this milestone.

### Reused 100% of the existing negotiation/guardrail/risk-agent code

No new negotiation engine, no per-merchant policy duplication.
`personalization.risk_assessment()` is completely unchanged — its
none/moderate/high computation stays purely about the buyer and
quantity, unaware a merchant concept even exists. The only new logic is
in `negotiation_loop.py`, where `risk_approval_tier` (new parameter,
default `"standard"` everywhere — every pre-Milestone-6 caller behaves
byte-identically) widens the SAME approval-gate condition Section 2O
already established:

```python
# run_full_transaction()
gate_moderate = risk_level == "moderate" and risk_approval_tier == "strict"
if total > threshold or risk_level == "high" or gate_moderate:
    ...
```

`run_negotiation()` also receives `risk_approval_tier` directly (not
derived from `policy` — same "policy carries data, caller resolves
identity" split already used for `buyer_id`/`orders`), so it can append
the merchant-specific note to the `risk_review` rationale at the point
the risk level is first logged, before the gating decision is even made:
`"...This merchant (risk_approval_tier=strict) requires human approval
on MODERATE risk too."`

### Product tagging — single source of truth, same pattern as liquidation

`personalization.CATEGORY_TO_MERCHANT` (imported by
`scripts/generate_synthetic_data.py`, exactly like
`CATEGORY_LIQUIDATION_THRESHOLDS`) is the only place the category→merchant
split is defined — the generator and the runtime can never drift apart.
Regenerating the catalog with this change added only the `merchant_id`
field to every product; every other field, and `buyers.json`/`orders.json`
entirely, came back byte-identical (verified by diffing before/after).

`product_to_policy()` gained one passthrough field,
`"merchant_id": product.get("merchant_id")` — `None` for a product
predating this milestone, so nothing downstream breaks on old fixtures.
New `personalization.find_merchant(merchants, merchant_id)` mirrors
`find_buyer()`/`find_product()`.

### Console output

`__main__` resolves the real merchant profile from `policy["merchant_id"]`
right after the product/liquidation lines and prints it:

```
Product (PRODUCT_ID=SKU-ELEC-007): Noise-Cancelling Earbuds (Electronics), current_inventory=16
Merchant: Voltstream Electronics (risk_approval_tier=strict)
```

### Verification

Live-verified: the SAME buyer (`BUYER-001`, zero prior orders — the sole
MODERATE-triggering factor at the default demo qty) against a
`MERCH-001` (strict) product forces the approval gate; against a
`MERCH-002` (standard) product it does not — identical buyer, identical
risk factor, different merchant, different gating outcome:

```
# MERCH-001 (strict), SKU-ELEC-007, qty=3:
[Round 0] risk-agent: risk_review
    Risk factors for buyer_id=BUYER-001: new buyer (0 prior orders). This merchant (risk_approval_tier=strict) requires human approval on MODERATE risk too.
...
[Payment] merchant-agent: approval_requested -- price=1669.5 qty=3
    Moderate risk flagged by the Risk Agent, and this merchant (risk_approval_tier=strict) requires approval on MODERATE risk too; pausing for human approval regardless of policy.transaction_approval_threshold.
```

Added `test_strict_merchant_forces_approval_on_moderate_risk_but_standard_does_not`
(the requirement's explicit primary test — same buyer/product-economics/qty
run twice, only `risk_approval_tier` differs, settled price identical
either way, only the gate and the `risk_review`/`approval_requested`
rationale differ) and `test_high_risk_behavior_identical_regardless_of_merchant_tier`
(HIGH's zero-discount + forced-approval response is byte-identical
whichever tier — `risk_approval_tier` only ever adds friction to
MODERATE, never changes HIGH's already-maximal one). Also added
`test_find_merchant_and_real_merchants_json_shape` and extended the
synthetic-data-generator test to assert the 40/40 split and that every
product's `merchant_id` matches `CATEGORY_TO_MERCHANT[category]`.

Full suite: 92 passed, 3 skipped, zero regressions (up from 89 passed
pre-feature).

---

## Section 2S — Risk Agent discount handling becomes a true three-level gradient (2026-09-03)

**Request:** MODERATE risk (exactly one factor) applies a partial
discount reduction, not the previous full-vs-zero (untouched-vs-HIGH)
behavior. NONE and HIGH stay exactly as they are. Reduction factor
confirmed with the user the same way as every other threshold in this
project — proposed 50% with a worked example, the user chose **25%**
instead.

| Level | `discount_factor` | Effect on `max_discount_pct` and every `qty_breaks` tier |
|---|---|---|
| `none` | `None` | Untouched — full normal discount room. |
| `moderate` | `RISK_MODERATE_DISCOUNT_FACTOR = 0.25` | Scaled to 25% of normal — a real, partial reduction. |
| `high` | `RISK_HIGH_DISCOUNT_FACTOR = 0.0` | Scaled to 0% — full list price, unchanged from Section 2N. |

**Learned from the earlier bug (Section 2P/2Q):** `apply_risk_discount_cap()`
already scales BOTH `max_discount_pct` AND every `qty_breaks` tier's
`discount_pct` together, in one function — extending it to MODERATE
reuses that exact same code path rather than introducing a second,
parallel adjustment mechanism that could independently drift the way the
original HIGH-only implementation once did:

```python
def apply_risk_discount_cap(policy, risk):
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
```

`risk["max_discount_pct_override"]` (an absolute override value, HIGH-only)
is replaced by `risk["discount_factor"]` (a multiplier, all three
levels) — a genuine generalization, not just a rename.

### Worked example (confirmed with the user before implementing)

`SKU-ELEC-007` (`max_discount_pct=13%`, `list_price=1918.96`), qty=3 (no
`qty_breaks` tier applies at this qty, so `max_discount_pct` is the
binding term regardless of level):

| Level | Effective `max_discount_pct` | Effective floor |
|---|---|---|
| `none` | 13% | 1669.50 |
| `moderate` | 3.25% (13 × 0.25) | **1856.59** |
| `high` | 0% | 1918.96 (= `list_price`) |

A strict, measurable NONE < MODERATE < HIGH ordering — live-verified
exactly matching this table.

### Console/rationale — same "state the actual number inline" pattern as Section 2P

`risk_assessment()`'s own MODERATE rationale states the reduction
percentage (`"Discount ceiling reduced to 25% of normal for this
negotiation."`); `run_negotiation()` then appends the freshly-recomputed
effective floor, via the exact `_floor_price()` call the negotiation
itself is about to use — widened from HIGH-only (Section 2P) to also
cover MODERATE, so it can never silently drift for either tier. The
existing, already-tested strict-merchant approval note (Section 2R) is
left completely unchanged; a new, symmetric
`"No approval required at this risk level for this merchant."` sentence
is added on the standard-tier path so the audit trail is equally
explicit either way, without ever having the two claims read as
contradicting each other. `_evidence_label()` (Section 2Q) also needed a
small fix: `discount_pct` values are now genuinely fractional (13 × 0.25
= 3.25), so the label formatting switched from a bare `%d` to `:g` —
`"0%"`/`"3.25%"` instead of `"0.0%"`/an unpredictable number of decimals.
`__main__`'s "Effective negotiation floor" preview (Section 2N/2Q) also
widened from HIGH-only to any non-`"none"` level, for the same
stale-preview reason as before.

**Live-verified**, matching the worked example exactly:

```
Effective negotiation floor at qty=3: 1856.59 INR (driven by policy.max_discount_pct (3.25%))
...
[Round 0] risk-agent: risk_review
    Risk factors for buyer_id=BUYER-001: new buyer (0 prior orders). Discount ceiling reduced to 25% of normal for this negotiation. This merchant (risk_approval_tier=strict) requires human approval on MODERATE risk too. Effective floor for this negotiation: 1856.59 INR.
[Round 1] merchant-agent: counter -- price=1856.59 qty=3
    Offer price 1631.12 is below the allowed floor 1856.59 for qty 3, per policy.max_discount_pct (3.25%). Countering at 1856.59.
```

### Verification

Rewrote `test_moderate_risk_logs_but_does_not_gate_or_restrict_pricing`
→ `test_moderate_risk_applies_a_partial_discount_reduction_between_none_and_high`
(the requirement's explicit replacement test): computes NONE/MODERATE/HIGH
floors for the identical product economics, asserts the strict ordering
`none_floor < moderate_floor < high_floor` (800.00 < 950.00 < 1000.00),
and separately confirms end to end (via `run_full_transaction()`) that
MODERATE still forces no approval and settles at the reduced (not
zeroed, not full) ceiling, with both the reduction percentage and the
concrete recomputed floor present in the `risk_review` rationale.
Re-confirmed `test_high_risk_forces_list_price_only_and_still_requires_human_approval`
and `test_established_buyer_normal_qty_proceeds_completely_unaffected`
both still pass completely unchanged.

Full suite: 92 passed, 3 skipped, zero regressions (same count as
pre-change — one test rewritten in place, not added).

---

## Section 2T — Bug fix: liquidation silently erased the Risk Agent's discount ceiling (2026-09-04)

Live-reproduced by the user with exact numbers: SKU-ELEC-003 (list_price
7976.05, min_price 5093.53, `days_in_inventory`=379, fully past the
Electronics 180-day liquidation threshold). A HIGH-risk negotiation
(new buyer, qty=12) correctly showed the Risk Agent card stating
"Discount ceiling: 0% -- full list price only" — but the merchant's
actual counter and the buyer's final accepted price was 5093.53, not
7976.05. A ~2882 INR discount a HIGH-risk buyer explicitly should not
have received.

**Root cause**: `merchant_agent._floor_price()` computes `computed` (the
discount-cap floor) from whatever `max_discount_pct`/`qty_breaks` the
`policy` it's given carries — which, for a risk-affected negotiation, are
already the RISK-TIGHTENED values `apply_risk_discount_cap()` produced
(e.g. exactly 0% for HIGH, per Section 2S). `_floor_price()` then relaxes
THIS value toward `min_price` by `liquidation_relaxation_fraction`, with
no awareness that `computed` might already be an artificially-tightened
risk ceiling rather than the product's normal, unrestricted floor. On a
fully-ramped product (`liquidation_relaxation_fraction == 1.0`), that
interpolation lands exactly on `min_price`, regardless of what `computed`
started as — silently erasing the risk restriction entirely. Two
constraints (liquidation's relaxation, risk's tightening) were each
independently correct on their own, but composed by SEQUENTIAL
overwriting rather than `max()` — the same bug class Section 2J fixed
for liquidation-vs-min_price, and Section 2P/2Q fixed for the AI
console-display gap, now found in a third combination.

**Fix**: `personalization.apply_risk_discount_cap()` now sets
`policy["risk_discount_capped"] = True` whenever it actually tightens a
policy (both "moderate" and "high" — never "none", which returns early
and never sets it) — the same "policy carries a fact, the field is absent
by default" convention `min_price_is_liquidation_relaxed` already
established. `_floor_price()` reads this flag: when set, it snapshots
`computed` (the risk-scaled discount-cap floor) as a THIRD floor
candidate BEFORE liquidation relaxation ever touches the working
`computed` value, then returns `max(min_price(+perk_cost), the
liquidation-relaxed discount-cap floor, the pre-liquidation risk
ceiling)` — three simultaneously active constraints combined via one
`max()`, the same principle every other guardrail combination in this
system already follows. When risk isn't active at all, the third
candidate is never added, so liquidation-only negotiations are completely
unaffected — verified via the full existing suite (every pre-existing
liquidation-only and risk-only test still passes unchanged).

No new function parameter was needed: because every existing caller of
`_floor_price(policy, qty)` (both inside `check_guardrails()` and inside
`evaluate_ai()`'s `_validate_against_guardrails()`) reads the same
`policy` dict, threading the fix through a policy field (like
`liquidation_relaxation_fraction` itself already does) fixed every call
site at once, with no call-site changes required anywhere.

### Live-verified (both directions of the priority question)

| Risk level | Pre-fix (buggy) floor | Post-fix floor | Matches |
|---|---|---|---|
| HIGH | 5093.53 (= min_price) | **7976.05** | list_price exactly (0% discount) |
| MODERATE | 5093.53 (= min_price) | **7557.307375** | `list_price * (1 - 21%*0.25)` — the qty_breaks[0] tier (21%, since qty=12 ≥ min_qty=10) scaled by `RISK_MODERATE_DISCOUNT_FACTOR` (0.25) |

MODERATE was independently confirmed to suffer the identical bug before
the fix (the user explicitly asked whether it did) — not just HIGH. Both
are now correctly bounded: `min_price (5093.53) < MODERATE floor
(7557.31) < HIGH floor (7976.05) = list_price`, the expected ordering
given MODERATE keeps a quarter of normal discount room and HIGH keeps
none.

Two new tests in `test_personalization.py`
(`test_high_risk_ceiling_overrides_liquidation_relaxation_on_the_same_product`,
`test_moderate_risk_ceiling_also_overrides_liquidation_on_the_same_product`)
use this exact live-reproduced product/scenario. Full suite: 104 passed,
3 skipped (102 + 2 new), zero regressions, confirmed across 3 repeated
runs.

---

## Section 2U — Round-limit accept_final forces human approval too (2026-09-04)

`on_round_limit=accept_final` (Section 4D follow-up, Milestone 8) auto-
accepts the merchant's last counter-offer when the round cap is reached
with no genuine agreement. That price was never actually agreed to by
both sides in the normal sense — so it now forces the human-approval gate
in `run_full_transaction()`, the exact same footing HIGH risk and
strict-merchant MODERATE risk already have: a human confirms before
payment, regardless of `policy.transaction_approval_threshold`.

**Reuses the existing mechanism, not a second flow**: the same
`approval_requested`/`approval_granted`/`approval_declined` audit actions,
the same `PAUSE_FOR_APPROVAL` pause point (`src/api.py`'s existing
Approve/Decline endpoints and frontend buttons), the same `confirm()`
callback for the CLI/test path. `run_negotiation()`'s accept_final branch
(the one place that returns `AGREEMENT_RECORDED` from the round-cap
fallback, not the other two acceptance pathways) now sets
`"round_limit_auto_accepted": True` on its outcome dict — absent
(`False` via `.get()`) for a genuine buyer/merchant agreement, whichever
of the other two pathways produced it. `run_full_transaction()` reads
this alongside `total`/`risk_level` to decide the gate.

**Combining reasons, not duplicating prompts**: the gate condition and
reason-building were restructured from an if/elif/else (which could only
ever cite the FIRST matching reason, even when two were simultaneously
true) into a list of `(evidence_paths, reason_fragment)` triggers,
collected and joined with `" AND "` into one `approval_requested`
rationale — e.g. "Approval required: the round limit was reached and the
merchant's final offer was auto-accepted (on_round_limit=accept_final)
AND high risk flagged by the Risk Agent for this buyer/request; ...".
Exactly one `approval_requested` entry fires regardless of how many
triggers apply; `evidence_paths` is the deduplicated union of every
applicable trigger's own evidence. This incidentally also fixes a
pre-existing version of the same gap for the threshold+HIGH-risk
combination (previously only the threshold reason was ever cited when
both applied) — not separately requested, but the same restructuring
that fixes round-limit's combination naturally fixes this one too, since
both go through the same trigger-collection list now.

### Tests (`test_payment.py`)

- `test_round_limit_auto_accept_forces_approval_even_under_threshold` —
  the exact scenario requested: NONE risk, final price (4399.12) nowhere
  near the 20000 threshold, still correctly gated purely by the
  round-limit trigger; declining produces `APPROVAL_DECLINED`.
- `test_round_limit_auto_accept_approval_can_also_be_granted` — the
  approve path completes to `COMPLETED`, same ordering guarantee
  (`approval_requested` → `approval_granted` → `payment_initiated`) the
  pre-existing threshold test already asserts.
- `test_round_limit_auto_accept_combined_with_high_risk_fires_only_one_approval_request`
  — round-limit + HIGH risk (0-prior-order buyer, qty=10, a
  `transaction_approval_threshold` override to isolate exactly these two
  triggers) produces exactly ONE `approval_requested` entry, rationale
  containing `" AND "` and naming both reasons, `evidence_paths` the
  union of both.
- `test_existing_threshold_and_moderate_risk_approval_triggers_unchanged`
  — zero-regression guard: the pre-Section-2U threshold trigger alone
  still produces a single-reason rationale with no `" AND "`.

Full suite: 108 passed, 3 skipped (104 + 4 new), zero regressions,
confirmed across 3 repeated runs. Every pre-existing risk/threshold
approval test
(`test_above_threshold_pauses_for_approval_and_declines_without_payment_call`,
`test_above_threshold_pauses_then_proceeds_once_approved`,
`test_high_risk_forces_list_price_only_and_still_requires_human_approval`,
`test_strict_merchant_forces_approval_on_moderate_risk_but_standard_does_not`)
confirmed passing unchanged by name.

---

## Section 2V — Bug fix: Section 2T's fix over-applied to MODERATE risk, suppressing liquidation for it too (2026-09-04)

**Regression, confirmed live-reproduced:** SKU-ELEC-003 (fully
liquidation-ramped, `liquidation_relaxation_fraction == 1.0`,
`days_in_inventory=379`), buyer_id=BUYER-001, MODERATE risk (only "new
buyer" fired -- `large_request` did not), merchant `risk_approval_tier`
strict. Computed "Effective floor for this negotiation: 7756.71 INR" --
essentially list_price (7976.05), with no visible liquidation relaxation
at all.

**Root cause:** `merchant_agent._floor_price()`'s risk-ceiling candidate
(added by Section 2T, above) was gated on
`policy.get("risk_discount_capped", False)` -- a boolean
`personalization.apply_risk_discount_cap()` set `True` for **both**
"moderate" and "high" (never for "none"). That's "is risk active at
all", not "is risk HIGH specifically" -- so MODERATE also got treated as
a pre-liquidation floor candidate. Since that candidate (MODERATE's
25%-of-normal ceiling, snapshotted BEFORE liquidation) is always >= the
liquidation-relaxed value, `max()` picked it every time, suppressing
liquidation for MODERATE exactly like the original Section 2T bug did
for HIGH. Only HIGH was ever meant to override/suppress liquidation
outright (Section 2T's own docstring already said so); MODERATE's
ceiling is meant to be a tighter STARTING POINT for liquidation to relax
from, same mechanism as an unrestricted policy, not a floor candidate of
its own.

**Fix:** replaced the boolean `risk_discount_capped` flag with the
actual `policy["risk_level"]` string (set by `apply_risk_discount_cap()`
to `risk["level"]`, still only for "moderate"/"high", never "none"), and
changed `_floor_price()`'s risk-ceiling gate from "is risk active at
all" to `policy.get("risk_level") == "high"` specifically. MODERATE and
"none" now both skip the risk_ceiling candidate entirely and fall
through to the normal `min_price`/`computed` `max()` -- liquidation
relaxes the (already risk-tightened, for MODERATE) `computed` exactly as
it always has for an unrestricted policy. HIGH's behavior (Section 2T)
is unchanged -- still the one deliberate carve-out.

**All three tiers, side by side, SKU-ELEC-003 at qty=12 (list_price
7976.05, min_price 5093.53, qty_breaks[0] 21% tier, fully liquidation-
ramped):**

| Risk tier | Pre-fix floor | Post-fix floor | evidence_path |
|---|---|---|---|
| NONE | 5093.53 (unaffected by this bug -- baseline) | **5093.53** | `policy.min_price` |
| MODERATE | ~7557.31 (bug: liquidation suppressed) | **5093.53** | `policy.min_price` |
| HIGH | 7976.05 (correct, Section 2T) | **7976.05** (unchanged) | `risk_agent.discount_ceiling` |

MODERATE's post-fix floor lands exactly equal to NONE's on this
*specific* product -- not a bug, and not evidence MODERATE has no
effect. Liquidation's interpolation (`computed - fraction * (computed -
min_price)`) only fully erases the starting-point difference at
`fraction == 1.0` (this product's exact case, "fully ramped"); on a
partially-ramped product MODERATE's tighter starting point still lands
above NONE's floor. What this fix guarantees is that liquidation is no
longer *suppressed* for MODERATE -- it lands meaningfully below
list_price, not near it, on any liquidation-eligible product regardless
of ramp fraction.

**Tests** (`tests/e2e/negotiation/test_personalization.py`):
- `test_none_risk_liquidation_fully_applies_on_the_liquidation_eligible_product` (new) -- baseline, no risk applied at all.
- `test_moderate_risk_ceiling_stacks_with_liquidation_relaxation_on_the_same_product` (rewritten -- was `..._also_overrides_liquidation_on_the_same_product`, which asserted the buggy 7557.307375 behavior as *correct*) -- now asserts liquidation stacks through to 5093.53, matching NONE, and asserts the floor is meaningfully below list_price (`< list_price * 0.95`) as the explicit regression guard.
- `test_high_risk_ceiling_overrides_liquidation_relaxation_on_the_same_product` (unchanged behavior/assertions, only the `risk_discount_capped` → `risk_level` field-name update) -- confirms HIGH's carve-out still holds.

Full suite: 109 passed, 3 skipped (108 + 1 net new test), zero
regressions, confirmed across 3 repeated runs.

---

## Section 2W — Bug fix: MODERATE risk scaling qty_breaks tiers made bulk orders MORE expensive, not less (2026-09-04)

**Reported by the user, confirmed live-reproduced:** "Floor price
increases as quantity increases... Floor price should decrease if it's a
bulk order, not the opposite." SKU-ELEC-001, a REPEAT buyer (4 prior
orders -- not a new-buyer edge case): at qty=9, floor 8694.28 (12% off,
unscaled -- risk is "none" below the qty_breaks threshold); at qty=10,
floor 9435.27 (large_request fires alone -> MODERATE; the 18% qty_breaks
tier scaled by RISK_MODERATE_DISCOUNT_FACTOR=0.25 down to 4.5%) -- a
~740 INR INCREASE for ordering one more unit. Not confined to new
buyers: any buyer's order crossing a qty_breaks threshold trips
"large_request" (Section 2's own definition: qty >= the product's lowest
qty_breaks tier), which is the exact same threshold the bulk discount
itself is keyed to.

**Root cause:** `apply_risk_discount_cap()` (Section 2P/2Q) scaled BOTH
`max_discount_pct` AND every `qty_breaks` tier's `discount_pct` by the
same factor, for every non-"none" risk level. That's correct for HIGH
(new buyer AND large request together -- the actual fraud-shaped
combination 2P/2Q existed to stop from sailing through the bulk tier
unscaled) but wrong for MODERATE, which can fire from `large_request`
ALONE on an ordinary repeat buyer simply placing a bulk order. Scaling
an 18%-off qty_breaks tier down to 4.5% is very often worse than the
plain unscaled max_discount_pct (12%) that applied to a smaller qty just
below the same threshold.

**Fix:** `apply_risk_discount_cap()` now only scales `qty_breaks` tiers
for `level == "high"`. MODERATE scales `max_discount_pct` alone --
still flagged, still visible in the risk_review entry, still gates
approval per the merchant's `risk_approval_tier`, but no longer able to
make a qty_breaks tier worse than what an unrestricted policy would give
at the same qty. HIGH's behavior (Section 2P/2Q, and its list-price-only
enforcement from Section 2N) is completely unchanged -- this was a
deliberate, confirmed-with-the-user choice (a genuinely new+large
request is still treated as the fraud-shaped signal it always was; only
the ordinary-repeat-buyer MODERATE case was in scope for this fix).

**Confirmed with the user before implementing** (two options presented:
scale only for HIGH vs. guarantee strict floor monotonicity even for
HIGH) -- chose "Only HIGH risk scales bulk tiers," preserving the
anti-circumvention protection for the genuinely risky combination.

**Before/after, SKU-ELEC-001, repeat buyer (4 prior orders):**

| qty | risk | floor (pre-fix) | floor (post-fix) |
|---|---|---|---|
| 9 | none | 8694.28 | 8694.28 (unaffected) |
| 10 | moderate | 9435.27 (increase) | **8101.49** (decrease, full 18% tier) |
| 25 | moderate | ~9262 (increase vs. tier-0) | **7409.90** (decrease, full 25% tier) |

New-buyer HIGH case (qty>=10 also trips new_buyer) is unchanged by
design: floor still rises to list_price (9879.86) at the threshold --
the deliberate fraud circuit-breaker, not touched by this fix.

**Test** (`tests/e2e/negotiation/test_personalization.py`):
`test_moderate_risk_from_large_request_alone_does_not_worsen_the_floor_at_higher_qty`
-- an isolated unit fixture (repeat buyer, `large_request`-only
MODERATE), directly asserting `floor_at <= floor_below` across the
qty_breaks threshold, plus `tightened_policy["qty_breaks"][0]["discount_pct"]
== 18` (NOT scaled to 4.5) as the mechanism-level check. The existing
Section 2P/2Q HIGH-risk regression test
(`test_high_risk_forces_list_price_only_and_still_requires_human_approval`)
re-confirmed passing unchanged -- HIGH still zeroes qty_breaks tiers.

Full suite: 110 passed, 3 skipped (109 + 1 new test), zero regressions,
confirmed across 3 repeated runs.

**Separately investigated, no bug found:** the user also reported the
product dropdown "still isn't showing only the products from the
selected merchant." Live-verified via direct DOM inspection (switching
between MERCH-001 and MERCH-002 in the running frontend): the dropdown
correctly re-filters to exactly that merchant's own products every time,
zero cross-contamination. MERCH-001 ("Voltstream Electronics")
genuinely carries a 40-product, 4-category catalog (Electronics, Sports,
Toys, Office Supplies) per its own `business_description` in
`data/merchants.json` -- not a filtering defect, just a wider-than-
expected catalog for that merchant.

---

## Section 2X — Round-limit redesign: pause and let the buyer/human choose, instead of an upfront `on_round_limit` flag (2026-09-04)

**Removed entirely** (per the user's explicit request that this replace,
not extend, the earlier design): the `on_round_limit` parameter
(`run_negotiation()`/`run_full_transaction()`/`POST /api/negotiate`) and
Section 2U's auto-accept-then-forced-approval logic. The frontend's
upfront "If no agreement after 5 rounds" radio choice is gone too --
nothing is decided before the negotiation starts.

**New behavior:** hitting `policy.max_negotiation_rounds` with no
agreement now ALWAYS pauses, returning a new `"ROUND_LIMIT_REACHED"`
state from `run_negotiation()` -- the merchant's own last real counter-
offer (never fabricated), plus the exact `policy` (already risk-
tightened/perk-merged, if applicable) and `requested_perks` the pause
needs to resolve later. `run_full_transaction()` reuses the SAME
`PAUSE_FOR_APPROVAL` sentinel the human-approval gate already uses: a
caller that can't resolve synchronously gets back `"ROUND_LIMIT_PENDING"`
(mirroring `"PENDING_APPROVAL"` exactly); the CLI path resolves it via a
real `confirm()` call, same mechanism, no new flow.

Accepting flows through `_process_agreement()` -- the SAME function a
genuine negotiated agreement uses (extracted out of
`run_full_transaction()` for exactly this reuse) -- so a round-limit
acceptance is NEVER exempt from the normal approval gate: if the
resulting price/risk tier would trigger it for a real agreement, it
pauses a SECOND, separate time here too (not skipped, not merged into a
combined rationale the way Section 2U did). Declining returns the
existing `REJECTED` shape. `src/api.py` exposes this via a NEW, separate
endpoint, `POST /api/round-limit-decision` (`{negotiation_id, accept}`),
with its own `_PENDING_ROUND_LIMIT` store -- deliberately not merged
into `POST /api/approve` / `_PENDING_APPROVALS`, since the two pauses
can chain and conflating them would make it ambiguous which decision a
given `negotiation_id` is waiting on. The frontend shows a visually
distinct card ("Negotiation round limit reached", amber-bordered) from
the human-approval card (plain-bordered "Human approval required"),
reusing the `.approve`/`.decline` button styling per the user's request,
but never the same card.

**Tests** (`tests/e2e/negotiation/test_payment.py`, replacing the four
Section 2U tests):
- `test_round_limit_reached_pauses_correctly`
- `test_round_limit_accept_proceeds_to_normal_payment_flow`
- `test_round_limit_decline_produces_rejected`
- `test_round_limit_accept_combined_with_high_risk_pauses_again_not_twice`
  -- re-confirms the "don't double-prompt" property under the new design:
  exactly one `round_limit_reached` entry and exactly one SEPARATE
  `approval_requested` entry, the latter naming ONLY the risk reason
  (round-limit is no longer a forced trigger at all).

Also updated: three pre-existing round-cap tests
(`test_ai_buyer_negotiation_terminates_at_round_cap_when_never_converging`,
`test_round_cap_enforcement_still_works_with_ai_merchant`,
`test_negotiation_never_converges_terminates_at_round_cap`) now assert
`"ROUND_LIMIT_REACHED"` with a real offer instead of `"REJECTED"`/`None`,
since hitting the round cap with a merchant counter in history no longer
resolves synchronously to REJECTED by default.

---

## Section 2Y — Reduce buyer personas to exactly one representative per label; add a real 5th LTV tier (2026-09-05)

### Persona simplification

Per the user's explicit request to simplify progressively (first: fold
"Whale"/"Occasional Buyer" into adjacent bands; then: reduce further to
exactly one buyer per persona), the frontend's buyer dropdown now shows
exactly 7 buyers, one per persona -- not the ~4-buyer bands the earlier
relabeling pass (`scripts/relabel_buyer_personas.py`) produced.

**`data/buyers.json` changed for exactly one buyer_id**: `BUYER-001`'s
`persona` field, `"Window Shopper"` -> `"First Time Customer"`. This is
a genuine behavioral fix, not cosmetic: `personalization.
perk_eligibility()`'s rule 2 denies ALL perks to persona `"Window
Shopper"` regardless of order history, which incorrectly blocked
BUYER-001 (0 prior orders) from its otherwise-correct rule-3 eligibility
(`free_delivery`). Live-verified before/after:
- Before (`persona="Window Shopper"`): `{"eligible": [], "rule":
  "window_shopper", ...}`
- After (`persona="First Time Customer"`): `{"eligible":
  ["free_delivery"], "rule": "new_buyer", ...}`

Every other buyer_id's `persona` field is UNCHANGED -- including the
retired `"Whale"`/`"Occasional Buyer"` labels still sitting on their
original buyers, and every non-chosen band member's original label. Per
the user's own "leave the rest untouched, only curate what the frontend
shows" option, `GET /api/buyers` (`src/api.py`) no longer deduplicates
by "first match per persona in file order" (fragile -- silently
changes if the file is ever reordered, and arbitrary -- picks whoever
happened to come first, not whoever best fits). It now returns an
explicit `CURATED_BUYER_IDS` allowlist, each pick confirmed against
real LTV/order-history numbers before implementing:

| Persona | buyer_id | Real LTV | Orders | Why |
|---|---|---|---|---|
| First Time Customer | BUYER-001 | 0.00 | 0 | Only true zero-order buyer |
| Window Shopper | BUYER-017 | 6182.05 | 4 | Lowest LTV among currently-Window-Shopper buyers |
| Bargain Hunter | BUYER-027 | 15829.12 | 3 | Closest to its band's mean (15707.54) |
| Stubborn Negotiator | BUYER-028 | 26915.90 | 2 | Closest to its band's mean (27102.37) |
| Loyal Regular | BUYER-022 | 39091.54 | 4 | Highest in its band, tied for most orders -- "solid mid-to-high" |
| Premium Customer | BUYER-004 | 66469.53 | 3 | Closest to its band's mean (62682.29) |
| Bulk Buyer | BUYER-003 | 103672.43 | **36** | Picked from the combined Bulk Buyer + ex-Whale pool by ORDER COUNT, not raw LTV -- "focused on quantity discounts" is about volume/frequency; 36 orders dwarfs every other candidate (next-highest 31), while the ex-Whale buyers (7-31 orders each, far higher LTV) fit "occasional huge-ticket spender" better than "bulk" |

### A real 5th LTV tier

Checking the 7 picks against `LTV_DISCOUNT_TIERS` surfaced a real gap:
the old top tier was open-ended (`50000, inf, 8`), so Premium Customer
(66469.53) and Bulk Buyer (103672.43) landed in the SAME tier and got
the IDENTICAL 8% bonus despite a ~37k real LTV gap -- indistinguishable
in a live demo. Confirmed with the user before implementing (same
discipline as every other threshold in this module): split the top tier
at 100000 (a clean gap -- next-highest real buyer below is BUYER-013 at
95906.98, next above is BUYER-003 at 103672.43):

```python
LTV_DISCOUNT_TIERS = (
    (0, 5000, 0),
    (5000, 20000, 2),
    (20000, 50000, 5),
    (50000, 100000, 8),
    (100000, float("inf"), 12),   # new
)
```

Premium Customer's own bonus is UNCHANGED (still 8%); only buyers with
real LTV >= 100000 (BUYER-003/008/009/016) move to the new 12% tier.
Live-verified: `ltv_discount_bonus(66469.53) == 8`,
`ltv_discount_bonus(103672.43) == 12`. Still well under
`HARD_DISCOUNT_CEILING_PCT` (30%); the user explicitly declined coupling
this to `transaction_approval_threshold` instead, since that field has
no existing LTV relationship anywhere in the codebase and wiring one in
would be a larger, separate architectural change than a demo-
distinctness fix calls for.

**Tests** (`tests/e2e/negotiation/test_personalization.py`):
`test_ltv_tier_lookup_is_deterministic_and_exact_at_boundaries` extended
with the new 100000 boundary (`99999.99 -> 8`, `100000 -> 12`,
`10_000_000 -> 12`, was `8`); `test_ltv_bonus_never_pushes_effective_
discount_past_hard_ceiling` and the max-bonus assertion in the min-
price-untouched-by-LTV-bonus test updated from 8 to 12 (the real top
tier's new value).

Full suite: 113 passed, 3 skipped (110 + 3 net new/changed parametrize
cases), zero regressions, confirmed across 3 repeated runs.

---

## Section 2Z — Live dashboard: `GET /api/dashboard-data`, shared computation, no duplicated logic (2026-09-05)

**Motivation:** `dashboard.html` was a static file, fully regenerated by
`scripts/generate_dashboard.py` (baking every stat/table row into
literal HTML at generation time). The user ran a full session's worth of
negotiations through `frontend/index.html` and found the dashboard still
showing yesterday's numbers -- correct behavior for a static file, but
not what a "dashboard" should do. Fixed by making it live, reusing the
script's existing computation rather than rewriting it.

**Extraction, not duplication:** every pure computation function that
used to live inline in `scripts/generate_dashboard.py` (`load_entries`,
`load_merchant_names`, `group_by_negotiation`, `classify_outcome`, the
`summarize_*`/`compute_*` helpers) moved, unchanged, into a new module,
`src/dashboard.py` -- consistent with this project's existing
architecture where `src/` holds shared logic and `scripts/` are thin CLI
consumers of it (mirrors how `scripts/generate_negotiation_history.py`
already imports from `src.negotiation_loop`). A new function there,
`compute_dashboard_data(log_paths, recent_count, merchants_path)`, is
the one shared entry point -- reads the log(s), groups by
`negotiation_id`, and returns a plain JSON-serializable dict (`stats`,
`merchant_breakdown`, `recent`, `merchant_names`, `meta`).

**Two consumers of the same function, zero duplicated logic:**
- `scripts/generate_dashboard.py` -- unchanged CLI/behavior/output path;
  internally now just calls `src.dashboard.compute_dashboard_data()`
  then its own (unchanged) `render_html()` to produce a static snapshot.
  Still the fallback for generating one without the API server running.
- `GET /api/dashboard-data` (`src/api.py`) -- calls the exact same
  function, returns its result as-is. Read-only aggregation over
  existing log data; no negotiation/pricing/guardrail logic added
  anywhere, per the user's explicit constraint.

**`dashboard.html` is now a live page**, structurally like
`frontend/index.html` (plain HTML/CSS/JS, no framework): on load, and
every 30 seconds via `setInterval` (no WebSockets, per the user's
request), it fetches `GET /api/dashboard-data` and renders the stat
cards / by-merchant table / recent-negotiations table client-side. A
small pulsing "live" dot in the subtitle turns solid red if a fetch
fails (e.g. the API server isn't running), with an error banner pointing
at the likely cause -- same UX pattern as `frontend/index.html`'s own
error handling. Running `scripts/generate_dashboard.py` will overwrite
this file with a plain static snapshot again -- deliberate, the explicit
fallback behavior requested, not a bug.

**Verified identical output** before/after the refactor: both the
script's printed stats and a direct `compute_dashboard_data()` call
returned byte-identical numbers (`total_negotiations=155,
accepted=86, rejected=46, rolled_back=7, other=16, avg_rounds=1.15,
avg_discount_pct=11.1, total_revenue=2332132.7`). Live-verified in the
browser: initial load populated correctly from the real API, and the
30-second refresh fired multiple times over an observed ~35s window
(3 separate `GET /api/dashboard-data` requests recorded).

**Tests**: `tests/e2e/negotiation/test_generate_dashboard.py` renamed to
`tests/e2e/negotiation/test_dashboard.py`, import updated from
`scripts.generate_dashboard` to `src.dashboard` (the functions it tests
moved there; the test logic itself is unchanged -- same assertions,
same fixtures).

Full suite: 113 passed, 3 skipped, zero regressions, confirmed across 2
repeated runs.

---

## Section 2AA — Merchant rename: "Voltstream Electronics" overpromised a single category (2026-09-05)

**Reported by the user, twice**, that the product dropdown "doesn't
consist of just the items from that particular merchant" -- re-verified
both times (live API calls and a live frontend click-through, fresh
server, fresh data) that `GET /api/catalog?merchant_id=...` and the
frontend's product dropdown were both filtering correctly: zero cross-
merchant contamination, confirmed by directly inspecting `merchant_id`
on every returned product. Asked the user to disambiguate; confirmed the
actual complaint was the first case I'd already found and described
under Section 2X's product-dropdown investigation: `"Voltstream
Electronics"` genuinely shows Toys, Sporting Goods, and Office &
Stationery items alongside Electronics ones.

**Root cause, traced further this time**: `CATEGORY_TO_MERCHANT`
(`src/personalization.py`) is the single source of truth for which
category goes to which merchant, and it's a deliberate, structural
4-categories-per-merchant split (confirmed with the user back when
multi-merchant support was built, Section 2R) -- NOT an accident:
- MERCH-001: Electronics, Office & Stationery, Toys & Games, Sporting
  Goods & Outdoors
- MERCH-002: Apparel & Fashion, Home & Kitchen, Books & Media, Beauty &
  Personal Care

The bug was never the filter -- it was that `data/merchants.json`'s
`merchant_name`/`business_description` for MERCH-001 ("Voltstream
Electronics") oversold a single category the data never actually
promised, and even its OWN `business_description` was incomplete
(mentioned electronics/office tech/sporting equipment, never mentioned
Toys & Games -- MERCH-002's description similarly never mentioned Books
& Media, despite both being real assigned categories).

**Fix, per the user's explicit choice** (rename + fix descriptions,
NOT restructure into more single-category merchants -- that option was
presented and explicitly declined, given the much larger blast radius:
new merchant records, per-merchant `risk_approval_tier` reassignment,
reassigning `merchant_id` on all 80 catalog products, and Section 2R's
multi-merchant risk-tier tests being built around exactly 2 merchants):

- `MERCH-001`: `"Voltstream Electronics"` -> `"Voltstream Marketplace"`
  (keeps the brand, drops the false single-category promise);
  `business_description` rewritten to name all 4 real categories.
- `MERCH-002`: name unchanged (`"Hearth & Home Living"` doesn't literally
  claim one category the way "Electronics" did); `business_description`
  extended to also name Books & Media.

`CATEGORY_TO_MERCHANT` itself, every product's `merchant_id`, and every
`risk_approval_tier` are completely UNCHANGED -- this is a pure
presentation-layer fix (one JSON file, `data/merchants.json`), zero risk
to existing negotiation history, tests, or guardrail behavior.
`personalization.load_json()` reads this file fresh on every call (no
caching), so the fix took effect immediately without an API server
restart -- confirmed live via `GET /api/merchants` and the frontend
dropdown both showing "Voltstream Marketplace" right away.

`tests/e2e/negotiation/test_dashboard.py`'s references to "Voltstream
Electronics" are a synthetic, inline test fixture
(`merchant_names = {"MERCH-001": "Voltstream Electronics", ...}`), not a
read of the real data file -- confirmed unaffected, no test changes
needed.

Full suite: 113 passed, 3 skipped, zero regressions, confirmed across 2
repeated runs.

---

## Section 2AB — Frontend floor-price breakdown: show the calculation, not just the number (2026-09-05)

**Motivation:** after walking the user through how the floor-price
system works (LTV bonus, liquidation, risk discount cap, and how
`_floor_price()` combines them via `max()`), they asked to surface that
SAME explanation live in the frontend, next to the existing effective-
floor hint -- not just the final number.

**Extraction, not duplication** (same discipline as Section 2Z's
dashboard work): `merchant_agent._floor_price()`'s candidate computation
-- the `max(min_price(+perk_cost), liquidation-relaxed discount floor,
risk ceiling if HIGH)` formula -- was inline in that one function.
Extracted into a new `_floor_price_candidates(policy, qty,
granted_perk_cost=0)`, returning `(value, evidence_path, label, detail)`
4-tuples in the same fixed order the original tie-breaking relied on.
`_floor_price()` itself now just calls this helper and takes the max --
**zero behavior change**, verified by running the full suite immediately
after the extraction (113 passed, 3 skipped, before touching any
consumer) and confirming byte-identical live output on the exact
SKU-ELEC-003 HIGH-risk-vs-liquidation scenario from Section 2T/2V.

**`GET /api/floor-preview`** (`src/api.py`) now also returns a
`breakdown` array -- every candidate `_floor_price_candidates()`
considered, each with a human-readable `label`/`detail`, its raw
`value`, and `is_winner` (computed via the SAME first-occurrence-of-the-
max tie-breaking rule `_floor_price()` itself uses, not a naive
value-equality check, so a genuine tie between two candidates never
double-flags a winner). Still purely a second, display-only read of the
same pure functions run_negotiation() itself uses -- nothing here
decides anything.

**Frontend**: the existing effective-floor hint gets a new collapsible
`<details>` ("How is this calculated?") right below it, rendering each
candidate as a card (label, detail sentence, value), with the winning
candidate visually highlighted (green border/background + a "Wins
(highest)" tag) -- reusing the exact `breakdown` data from
`/api/floor-preview`, no separate computation in JS at all.

**Live-verified**, two real scenarios:
- SKU-ELEC-003 / BUYER-001 / qty=10 (HIGH risk, fully liquidation-
  ramped): all three candidates shown (min_price 5093.53, liquidation-
  relaxed discount floor 5093.53, risk ceiling 7976.05) with the risk
  ceiling correctly marked as the winner -- matching Section 2T/2V's
  documented fix exactly.
- SKU-ELEC-004 / BUYER-022 / qty=1 (no risk, no liquidation): two
  candidates shown (min_price 3018.57, discount-tier floor 3419.33),
  discount-tier floor correctly marked as the winner.

Full suite: 113 passed, 3 skipped, zero regressions, confirmed across 2
repeated runs.

---

## Section 2AC — MRP: a consistent-proportion opening-offer anchor, separate from list_price (2026-09-05)

> **Superseded the same day, Section 2AD below.** The user reconsidered
> and asked to revert this entirely: give the buyer the REAL `list_price`
> instead (safe to reveal -- it's the advertised price, not a
> guardrail-derived number) and have it reason out its own persona-
> appropriate opening discount, rather than anchoring on a synthetic
> `MRP = floor x 1.08` figure. Fully reverted -- `compute_mrp()`,
> `MRP_MARKUP_MULTIPLIER`, the `mrp` parameter on `AIBuyerAgent`, and its
> three dedicated tests are all removed; `_floor_price()` itself was
> never touched by this section in the first place, so nothing to revert
> there. This section is kept for the historical record (per this
> document's own layered-correction discipline) but does NOT describe
> current behavior -- see Section 2AD.

**Motivation:** `list_price`'s distance from the effective floor varies
unpredictably per product -- confirmed live before implementing:
`list_price/floor` ranged from **1.087x** (SKU-ELEC-004) to **1.566x**
(SKU-ELEC-003, fully liquidation-ramped) across a 5-product sample, a
>40 percentage-point spread. Since the AI buyer's prompt showed
`list_price` as its only reference number, its opening-offer anchor was
that same inconsistent, sometimes-wildly-inflated number -- for a fully
liquidation-ramped product, `list_price` could sit 57% above the real
floor, nowhere near a realistic anchor.

**MRP, confirmed with the user against real numbers**: `MRP = effective
floor (qty=1, no buyer-specific LTV/risk adjustment) x 1.08` -- the
multiplier was picked from a 5%/8%/10% choice, shown with real
before/after numbers for 5 products, before implementing.
`merchant_agent.compute_mrp(product)` ([merchant_agent.py](src/agents/merchant_agent.py)):
computes the product's own baseline policy (`product_to_policy()`, NOT
the caller's already-adjusted negotiation policy -- so MRP is a stable,
buyer-independent, per-PRODUCT figure, like `list_price` itself, never
shifting depending on who's asking), reads its floor at qty=1 via the
EXACT SAME `_floor_price()` the negotiation engine uses, and applies the
multiplier. Liquidation IS reflected (aging is a property of the
product, not the buyer); LTV bonus and risk-discount-cap are NOT
(buyer-specific, would make MRP shift per negotiation, defeating the
"stable per-product reference" purpose). Multiplier > 1.0 guarantees MRP
is always strictly greater than the floor -- confirmed by a dedicated
test across the full product sample; the true floor itself is never
computed-and-shown anywhere the buyer can see it.

**MRP vs. floor, the same 5-product sample, at the confirmed 1.08x**
(qty=1, no buyer-specific adjustments):

| SKU | list_price | floor | MRP | list_price/floor | MRP/floor |
|---|---|---|---|---|---|
| SKU-ELEC-001 | 9879.86 | 8694.28 | 9389.82 | 1.136 | **1.080** |
| SKU-ELEC-003 (liquidation-ramped) | 7976.05 | 5093.53 | 5501.01 | 1.566 | **1.080** |
| SKU-ELEC-004 | 3930.26 | 3615.84 | 3905.11 | 1.087 | **1.080** |
| SKU-OFFC-001 | 1170.19 | 994.66 | 1074.23 | 1.176 | **1.080** |
| SKU-SPRT-001 | 1775.24 | 1597.72 | 1725.53 | 1.111 | **1.080** |

`list_price/floor` swings from 1.087 to 1.566 across these five;
`MRP/floor` is exactly 1.080 for every one of them, by construction.

**`AIBuyerAgent`** ([ai_buyer_agent.py](src/agents/ai_buyer_agent.py)) gained an
optional `mrp` parameter (default `None`, so every pre-existing caller
is byte-identical -- confirmed by the full suite passing unchanged
before any test was added). When given, the prompt names it "MRP" and
shows ITS value everywhere the prompt used to say "list price" and show
`list_price`'s value -- consistently, in both the reference-price line
and the "decide your target/walk_away price" sentence, never mixing the
two labels. `list_price` itself never appears in the prompt when `mrp`
is set. Wired into the two real production call sites
(`src/api.py`'s `/api/negotiate`, `src/negotiation_loop.py`'s CLI
`__main__` on the `PRODUCT_ID` path only -- the bare `merchant_policy.json`
fallback has no category/days_in_inventory for `compute_mrp()` to use,
so it keeps the old list_price-anchored behavior unchanged, same as
every pre-Section-2AC caller).

**Real negotiation traces, live Gemini calls, same persona/budget,
before (list_price anchor) vs after (MRP anchor)**:

| Product | list_price | floor | MRP | Before: opening offer (% of floor) | After: opening offer (% of floor) |
|---|---|---|---|---|---|
| SKU-ELEC-001 | 9879.86 | 8694.28 | 9389.82 | 6800.00 (78.2%) | 7000.00 (80.5%) |
| SKU-ELEC-004 | 3930.26 | 3615.84 | 3905.11 | 2800.00 (77.4%) | 2800.00 (77.4%) |
| SKU-ELEC-003 (liquidation-ramped) | 7976.05 | 5093.53 | 5501.01 | **5500.00 (108.0%)** | 4000.00 (78.5%) |

The two products where `list_price` was already reasonably close to the
floor (ELEC-001, ELEC-004) show only a small shift, as expected. The
liquidation-ramped product (ELEC-003) shows the dramatic, motivating
case: under the OLD anchor, the AI buyer's opening offer (5500) landed
**above the real floor (5093.53)** -- an opening offer that leaves zero
negotiating room and would have been instantly acceptable, the exact
"unrealistic anchor" this section exists to fix. Under the NEW anchor,
the opening offer (4000, 78.5% of floor) is a normal, realistic opening
move. Noted honestly, not glossed over: in this specific trace, the
buyer's own resulting `walk_away_price` (4900) landed just BELOW the
real floor (5093.53) -- this buyer, now forming a realistic (lower)
impression of the product's worth from MRP instead of an inflated one
from `list_price`, set correspondingly conservative price expectations
and may not reach agreement on this specific product/persona/budget
combination. That's the LLM's own strategic judgment within its persona
and budget, not a new constraint this change imposes -- and arguably
more correct than before, where the buyer's inflated mental model (from
seeing `list_price` 57% above the true floor) was itself the source of
an unrealistic anchor.

**Tests**: `test_compute_mrp_is_strictly_greater_than_the_floor_and_matches_the_confirmed_multiplier`,
`test_compute_mrp_keeps_a_consistent_proportion_despite_wildly_different_list_price_ratios`,
`test_compute_mrp_includes_liquidation_since_aging_is_a_product_property`
(`tests/e2e/negotiation/test_personalization.py`);
`test_opening_offer_prompt_anchors_on_mrp_when_given_not_list_price`,
`test_opening_offer_prompt_falls_back_to_list_price_when_mrp_not_given`
(`tests/e2e/negotiation/test_ai_buyer_agent.py`).

Full suite: 118 passed, 3 skipped (113 + 5 new), zero regressions,
confirmed across 2 repeated runs. No change to
`check_guardrails()`/`_floor_price()`'s actual computation or behavior --
display/anchoring only, per the user's explicit constraint.

---

## Section 2AD — Revert Section 2AC: list_price anchor + persona-reasoned opening discount (2026-09-05)

**Reverts Section 2AC entirely**, same day. The user reconsidered: give
the buyer the real `list_price` (safe -- it's the advertised price, not
a guardrail-derived number) instead of a synthetic MRP figure, and have
the LLM reason out its own opening offer as a persona-appropriate
discount off it, rather than requiring a manually-typed starting price
for AI mode (which was never actually true -- see below) or anchoring
on MRP.

**Found and removed** (confirmed with a repo-wide grep before reverting):
`merchant_agent.compute_mrp()`, `MRP_MARKUP_MULTIPLIER`, the `mrp`
parameter on `AIBuyerAgent.__init__()`/`_decide()`, the `mrp=` wiring in
`src/api.py` and `src/negotiation_loop.py`'s CLI `__main__`, and all
five dedicated tests. Also noted while removing: this new "MRP" concept
had collided with an existing, unrelated, pre-Milestone-9 convention --
`GET /api/catalog` already renames `list_price` to `"mrp"` in its JSON
response (the standard retail sense: MRP = list price), so Section
2AC's *different* definition (`MRP = floor x 1.08`) was a real naming
conflict on top of being redundant. That pre-existing catalog rename is
untouched -- unrelated to this feature either way.

**What was NOT true and needed no change:** the request described AI
mode as "requiring a manually-typed starting price." It never did --
`frontend/index.html`'s "Starting Offer" field has been disabled (with
placeholder "AI decides its own opening offer") in AI buyer mode since
Milestone 9, and `AIBuyerAgent.initial_offer()` has always asked the LLM
to decide its own opening price. That part of the request was already
satisfied; verified, not re-implemented.

**What changed:** `AIBuyerAgent`'s prompt now unconditionally shows
`list_price` (as it did before Section 2AC), and the first-round
instruction was strengthened with explicit persona-discount guidance:
*"Your OPENING OFFER this round should be a reasoned discount off
list_price, sized to your persona's own negotiating style -- an
aggressive/bargain-focused persona opens well below list_price, while an
easygoing or convenience-focused persona opens closer to it. Vary the
discount naturally based on willingness_to_negotiate and budget; don't
default to the same fixed percentage regardless of persona."* The
effective floor is still never computed for or shown to the buyer at
all -- unchanged from every prior section.

**Incidental discovery while building the verification traces below**:
picking buyer_ids by their CURRENT `persona` label produced flat,
undifferentiated opening offers -- tracing it down found that **27 of
30 buyers in `data/buyers.json` now have a `persona` label that doesn't
match their own `negotiation_style` text** (e.g. `BUYER-027`:
`persona="Bargain Hunter"`, `negotiation_style="moderate -- focused on
quantity discounts over per-unit haggling"` -- verbatim the ORIGINAL
"Bulk Buyer" persona's description). This is a side effect of Section
2Y's relabeling pass, which deliberately left `negotiation_style` (and
`budget_range`, `category_affinity`) untouched while reassigning the
`persona` label field based on real LTV -- a buyer whose label moved to
a different tier than its original index-based assignment now carries a
mismatched style description. `buyer_to_persona()` reads
`negotiation_style`, not the label, so `AIBuyerAgent`'s actual behavior
was governed by the (unmoved) original text throughout -- this doesn't
affect THIS section's correctness, but it does mean the persona LABEL
shown in the frontend dropdown can misdescribe how that buyer actually
negotiates. Flagged to the user, not fixed here -- out of scope for this
request, and Section 2Y's own tests never asserted a label/style
correspondence to begin with.

**Real negotiation traces**, live Gemini calls, merchant in rules mode
(so every round is attributable to the real AI buyer), buyers picked by
their ACTUAL `negotiation_style` text for a clean contrast:

| Product | Buyer (real negotiation_style) | Opening offer | Rounds to close | Final price |
|---|---|---|---|---|
| SKU-ELEC-001 (floor 8694.28) | BUYER-017, "aggressive -- pushes hard for the lowest possible price" | 6800.00 (31.2% off list_price, 78.2% of floor) | **4 rounds** (6800 -> 7200 -> 7650 -> accepts 8694.28) | 8694.28 |
| SKU-ELEC-001 (floor 8694.28) | BUYER-012, "easygoing -- browses often, rarely pushes hard on price" | 9200.00 (6.9% off list_price, 105.8% of floor) | 1 round (already clears the floor) | 9200.00 |
| SKU-ELEC-003 (liquidation-ramped, floor 5093.53) | BUYER-022, "moderate -- open to a fair discount but not desperate" | 6800.00 (14.7% off list_price, but 133.5% of the real floor) | 1 round | 6800.00 |

The aggressive buyer opens well below list_price and genuinely below
the floor, forcing real multi-round convergence exactly as requested;
the easygoing buyer opens close to list_price and settles immediately,
correctly -- it was never trying to lowball. The liquidation-ramped case
illustrates the floor-hiding requirement's actual purpose concretely:
this buyer's "moderate" discount off list_price (14.7%) still landed
33.5% above the real (liquidation-collapsed) floor -- the merchant kept
margin the buyer never knew it could have negotiated away, because the
true floor was never revealed.

**Tests**: the two Section 2AC `AIBuyerAgent` prompt tests were replaced
with `test_opening_offer_prompt_shows_list_price_and_persona_appropriate_discount_guidance`
(`tests/e2e/negotiation/test_ai_buyer_agent.py`) -- confirms `list_price`
appears, `MRP` does not, and the new persona-discount guidance sentences
are present. The three `compute_mrp()` tests in
`tests/e2e/negotiation/test_personalization.py` were deleted outright
(the function no longer exists).

Full suite: 114 passed, 3 skipped (118 - 5 removed + 1 new), zero
regressions, confirmed across 2 repeated runs -- back to the exact
pre-Section-2AC test count plus this section's one consolidated test.

---

## Section 2AE — Guard: `run_negotiation()` requires `buyer_id`/`orders` together, or neither (2026-09-05)

**A real near-miss, not a hypothetical:** a verification script for
Section 2AD called `run_negotiation(policy, buyer, on_event=on_event)`
without passing `buyer_id`/`orders` at all. For the specific buyer/qty
pair used, real risk happened to be `"none"` anyway (confirmed
separately), so the floor claim in that write-up was correct -- but only
by coincidence. Had a different buyer/qty pair been used, the risk-
assessment block (`if buyer_id is not None and orders is not None:`)
would have silently never run, `risk_level` would have stayed Python
`None` (never assessed) rather than the string `"none"` (assessed,
found no risk), and the reported floor would have been wrong without
any indication why.

**The guard added**, at the very top of `run_negotiation()`:

```python
if (buyer_id is None) != (orders is None):
    raise ValueError(
        "run_negotiation() requires buyer_id and orders TOGETHER, or NEITHER -- ..."
    )
```

Passing exactly ONE of the two is now a loud `ValueError`, not a silent
skip -- there's no legitimate reason to pass one without the other,
since the risk block only ever fires when both are present. Passing
**neither** remains completely valid and unchanged: every pre-Milestone-5
caller, and every test that deliberately wants "no personalization,"
relies on exactly that -- the guard only closes the ASYMMETRIC case,
not the "explicitly skip" one.

**Honesty check on scope**: this guard would NOT have caught the exact
mistake in the Section 2AD verification script -- that script omitted
BOTH arguments (a symmetric, still-valid call), not just one. What it
does catch is a closely related, arguably more common mistake: a caller
that has a real `buyer_id` in scope and plumbs it through, but forgets
`orders` (or vice versa) -- e.g. via two separate code paths that should
have stayed in sync. There is no way for `run_negotiation()` itself to
detect "the caller had a buyer_id available elsewhere and simply forgot
to pass it" -- that's a caller-discipline issue no argument-shape check
can catch; the asymmetric guard is the real, checkable half of this
problem.

**Tests** (`tests/e2e/negotiation/test_negotiation_loop.py`):
`test_buyer_id_without_orders_raises_value_error`,
`test_orders_without_buyer_id_raises_value_error`,
`test_neither_buyer_id_nor_orders_is_still_a_valid_no_personalization_call`
(explicit regression guard: `risk_level is None`, not the string
`"none"`, confirming the distinction the guard protects still exists).

Full suite: 117 passed, 3 skipped (114 + 3 new), zero regressions,
confirmed across 2 repeated runs.

---

## Section 2AF — Frontend: move the effective-floor reveal from before to after the negotiation (2026-09-05)

**Restructured what's shown before vs. after**, per the user's explicit
request -- `frontend/index.html` only, no backend logic touched.

**Before negotiation starts (setup form)**:
- **Removed entirely**: the live "Effective floor" hint and its
  "How is this calculated?" breakdown -- no longer shown before Start.
- **Added**: "List price for this product" -- the real, advertised
  `list_price` (safe to reveal, it's not a guardrail-derived number),
  read from `/api/catalog`'s existing response (which already renames
  the raw `list_price` field to `"mrp"` -- a pre-existing, unrelated
  convention from Section 2AD -- so no new API call was needed for
  this). Shown as soon as a product is picked, to help set a sensible
  Maximum Budget.
- **Kept unchanged**: "Minimum order quantity" -- a hard requirement to
  know upfront (a negotiation can't happen below it at all), not a
  pricing signal, so it stays visible before Start.

**After negotiation completes (outcome view)**: a new card, "The
merchant's true floor, revealed," appears for EVERY terminal state
(`COMPLETED`, `REJECTED`, `ROLLBACK`, `APPROVAL_DECLINED` -- wired into
`renderOutcome()`, the one function already called for all of them), framed
as the user requested: *"The merchant's true floor for this negotiation
was X INR (driven by Y, risk=Z)"* -- plus a comparison to the actual
negotiated price when one exists (*"was N% above/below this floor"*),
or, for a REJECTED negotiation with no agreement, *"this is the minimum
the merchant would have needed to accept."* The relocated "How is this
calculated?" breakdown (Section 2AB) lives here now too, unchanged.

**Validation preserved, per the user's explicit check**: `/api/floor-preview`
is still called in the background on every product/buyer/qty change
(`refreshFloorForValidation()`, renamed from `updateFloorHint()` -- same
endpoint, same request shape, same `currentFloor` state variable) so the
budget-warning check (`maxPrice < currentFloor.value`) keeps working
exactly as before. Only the DISPLAYED message changed -- it no longer
states the floor number (`"...may be too low for this product, buyer &
quantity combination... The merchant's exact floor is revealed after
the negotiation completes, not shown here."`), but the underlying
comparison is byte-identical. Live-verified: setting a budget below a
real product's floor still triggers the warning with `currentFloor.value`
correctly populated internally, while the rendered text contains no
digit of that number.

**A correctness detail found while wiring the reveal**: the floor
reveal re-fetches `/api/floor-preview` using `lastNegotiationParams` --
the exact product/buyer/qty captured at the moment `/api/negotiate` was
actually submitted -- NOT the current setup-form field values. This
matters because those fields are never disabled during a long AI-mode
wait (which can take minutes); re-reading them at reveal time could
describe a floor for a DIFFERENT product/buyer/qty than what was
actually negotiated, if the user changed the form mid-wait.

**Live-verified**, both branches:
- Successful negotiation (SKU-ELEC-004, scripted+rules): *"The
  merchant's true floor for this negotiation was 3419.33 INR (driven by
  policy.max_discount_pct). The negotiated price (3419.33 INR) was 0.0%
  above this floor."*
- Instant-rejected negotiation (same product, budget below `min_price`):
  *"The merchant's true floor for this negotiation was 3419.33 INR
  (driven by policy.max_discount_pct). No agreement was reached, so
  there's no final price to compare -- but this is the minimum the
  merchant would have needed to accept."*

No `src/api.py`/`src/agents/merchant_agent.py` changes at all -- confirmed
via `git diff --stat`, zero lines touched by this section. Full suite
(pure frontend change): 117 passed, 3 skipped, unchanged.

---

## Section 2AG — Remove `budget_range`: derive budget as a persona-appropriate % of the real product's `list_price` (2026-09-05)

**The problem, raised by the user before any code changed**: a buyer's
spending ceiling was a fixed absolute `{min, max}` rupee range stored on
the buyer record, with no relationship to whichever product/qty a given
negotiation actually involved. `generate_negotiation_history.py` draws
product and buyer independently (`rng.choice(catalog)`, `rng.choice(buyers)`)
across 130 runs — a cheap-persona buyer could land on an expensive
product and instant-reject every time; a big-spender persona could land
on a cheap product and never have its budget bind at all. The user asked
for a full audit of every `budget_range` consumer before deciding what to
remove vs. replace, then approved a specific 3-step, verified-in-order
implementation plan.

**Every real consumer found** (confirmed via `grep` across the whole
codebase, not just the frontend):
1. `personalization.buyer_to_persona()` — read it unconditionally for the
   `persona.budget` midpoint. Two callers: `src/api.py` (interactive
   frontend, AI mode) immediately overwrote the result with the
   human-typed Maximum Budget (`req.max_price`) right after — the
   midpoint was computed and discarded, never actually used; and
   `src/negotiation_loop.py`'s CLI `BUYER_ID`-only fallback (no
   `BUYER_BUDGET` set), where the midpoint genuinely was the effective
   budget.
2. `generate_negotiation_history.py` — read `budget_range` directly
   (bypassing `buyer_to_persona()`), no fallback. The real bug.
3. `generate_synthetic_data.py`'s `generate_buyers()` — *wrote*
   `budget_range` when building `buyers.json` from scratch.
4. `test_synthetic_data_generator.py` — schema assertion requiring the
   field and `min < max`.

**Confirmed the interactive frontend was never at risk**: `NegotiateRequest.max_price`
is a required field with no default — the human-typed Maximum Budget
fully overrides `buyer_to_persona()`'s output in AI mode, and scripted
mode never reads `budget_range` at all. Removing the field could not
silently change frontend behavior; it could only crash `buyer_to_persona()`
if left reading a now-missing key.

**The fix**: `personalization.PERSONA_DISCOUNT_BANDS` — a
`persona label -> (low_pct, high_pct)` table, and
`budget_from_list_price(persona, list_price, rng)`, which returns
`list_price * (1 - uniform(low_pct, high_pct)/100)`. Keyed by the
buyer's `persona` label (the real, LTV-grounded field — Section 2Y), not
`negotiation_style` free text, because `generate_negotiation_history.py`
draws from all 30 `buyers.json` entries and `negotiation_style` still
carries stale, inconsistent wording for the 23 buyers this task's
persona/style rewrite (below) didn't touch (Section 2AD's "27/30
mismatched" finding). Bands for the 7 curated personas came from the
user's explicit directional requirements (aggressive/cautious/passive/
etc., confirmed against real product/qty walkthroughs — see below);
`Whale` and `Occasional Buyer` (real labels the generator can still
draw, but outside the curated 7) were proposed and confirmed separately,
same session:

| Persona | Band (% off list) |
|---|---|
| Premium Customer | 0–8% |
| First Time Customer | 5–15% |
| Whale | 5–15% |
| Loyal Regular | 10–18% |
| Bulk Buyer | 12–24% |
| Stubborn Negotiator | 12–22% |
| Occasional Buyer | 15–25% |
| Bargain Hunter | 18–30% |
| Window Shopper | 25–40% |

**Bulk Buyer range sanity-checked against real products before
implementing** (per the user's explicit request): at qty 10–20 against
real Electronics-tier products (`_floor_price()` computed with BUYER-003's
real LTV bonus, +12%), the persona-band approach produces genuine
negotiation tension — e.g. SKU-ELEC-004 (USB-C Hub) at qty=20:
list_price total 78,605.20, floor total 60,371.40, comfortably inside a
persona-appropriate budget, requiring real back-and-forth to reach.

**Implemented in the confirmed order, each step verified before the
next**:

1. **`generate_negotiation_history.py`**: `max_acceptable_price` now
   comes from `personalization.budget_from_list_price(buyer["persona"], policy["list_price"], rng)`
   instead of `rng.uniform(budget_range["min"], budget_range["max"])`.
   Verified with real draws (seed=42): every persona's resulting
   `pct_off` landed inside its band regardless of which product it hit
   (e.g. Window Shopper 34.03% off a ₹3990 curtain, Premium Customer
   1.76%/3.68% off a toy and a desk lamp). A real end-to-end batch
   (`--n 40 --seed 7`, real `run_full_transaction()` calls) produced 27
   Accepted / 9 Rejected / 4 Rolled back — a healthy mix, not the old
   failure mode of a fixed persona always instant-rejecting or always
   trivially affording regardless of the product drawn.

2. **`personalization.buyer_to_persona()`**: no longer reads
   `budget_range`; `"budget"` is omitted from the returned dict entirely.
   `src/api.py` is unaffected (it always overwrites the key immediately
   after calling this function, whether or not it existed).
   `src/negotiation_loop.py`'s CLI `BUYER_ID`-only fallback now calls
   `budget_from_list_price(buyer_profile["persona"], policy["list_price"], random)`
   against the same table (not seeded — an interactive CLI demo
   invocation, not the deterministic batch generator) instead of reading
   `persona_from_profile["budget"]` from a field that no longer exists.
   `BUYER_BUDGET`, when set, still overrides it unchanged.
   Live-verified against real buyer/product pairs (no `BUYER_BUDGET`
   set): `BUYER-004` (Premium Customer) against SKU-ELEC-001
   (list_price 9879.86) → budget 9715.77 (1.66% off, inside 0–8%);
   `BUYER-003` (Bulk Buyer) against SKU-ELEC-004 (list_price 3930.26) →
   budget 3234.92 (17.7% off, inside 12–24%); `BUYER-017` (Window
   Shopper) against SKU-OFFC-002 (list_price 1263.03) → budget 845.03
   (33.09% off, inside 25–40%). Also verified: `BUYER_BUDGET` override
   still wins when set; the "neither `BUYER_ID` nor `BUYER_BUDGET` set"
   hardcoded-default path (`4450.0`) is untouched. Full suite: 117
   passed, 3 skipped, unchanged.

3. **`data/buyers.json` + schema/test/doc cleanup**, only after 1 and 2
   were verified:
   - `budget_range` removed from all 30 buyers (no code reads it
     anymore).
   - For exactly the 7 curated `buyer_ids` (Section 2Y), `negotiation_style`
     rewritten to genuinely match its persona label's meaning (the
     original Section 2AD mismatch this task set out to fix — e.g.
     `negotiation_style`/`budget_range` were reassigned by index at
     data-creation time and never touched when `persona` was later
     relabeled by real LTV):

     | buyer_id | Persona | Old `negotiation_style` | New `negotiation_style` |
     |---|---|---|---|
     | BUYER-001 | First Time Customer | `aggressive -- pushes hard for the lowest possible price` | `cautious -- new to this merchant, wary of commitment, pushes for a modest discount before trusting the deal` |
     | BUYER-017 | Window Shopper | `aggressive -- pushes hard for the lowest possible price` | `passive -- browses without urgency, rarely converts, quick to walk away rather than negotiate hard` |
     | BUYER-027 | Bargain Hunter | `moderate -- focused on quantity discounts over per-unit haggling` | `aggressive -- relentlessly price-focused, anchors low and pushes hard for the steepest possible discount` |
     | BUYER-028 | Stubborn Negotiator | `easygoing -- browses often, rarely pushes hard on price` | `unyielding -- slow to concede, holds firm on its position round after round` |
     | BUYER-022 | Loyal Regular | `moderate -- open to a fair discount but not desperate` | `moderate -- fair-minded, open to a reasonable discount but not confrontational, values the ongoing relationship` |
     | BUYER-004 | Premium Customer | `easygoing -- browses often, rarely pushes hard on price` | `easygoing -- budget-insensitive, values convenience and quality over squeezing out a discount` |
     | BUYER-003 | Bulk Buyer | `moderate -- focused on quantity discounts over per-unit haggling` | `moderate -- quantity-focused, negotiates hard on volume discounts but only mildly sensitive to per-unit price` |

     This resolves the BUYER-003/BUYER-027 and BUYER-001/BUYER-017
     byte-identical-style duplicates too. `category_affinity` and
     `buyer_id` were untouched, as scoped. All other 23 buyers' (now
     `budget_range`-free) `negotiation_style` text still carries the
     original Section 2AD mismatch — out of scope for this task, they
     don't back the 7-buyer curated dropdown, and their spending
     ceiling is now driven by `persona` (real) rather than
     `negotiation_style` (stale) anyway.
   - `generate_synthetic_data.py`: `PERSONAS` entries dropped their
     `budget_range` tuple element; `generate_buyers()` no longer writes
     the field.
   - `test_synthetic_data_generator.py`: `required_buyer_fields` no
     longer includes `budget_range`; asserts it's absent instead of
     `min < max`.
   - `NEGOTIATION_SPEC.md`'s `buyers.json` schema table and the
     `BUYER_ID` env-var table (Section 2D) updated to describe the new
     mechanism (this section is the reference target for both).

   Full suite after this step: 117 passed, 3 skipped, unchanged
   throughout all three steps.

---

## Section 2AH — Second, independent free_delivery eligibility path: qty > 10 (2026-09-05)

**Request**: add a second, independent basis for `free_delivery`
eligibility -- `qty > PERK_LARGE_QTY_THRESHOLD` (10), regardless of order
history or persona -- ADDITIONAL to the existing "0 prior orders" rule,
not a replacement. A returning buyer placing a large order (qty > 10)
now qualifies for `free_delivery` on that basis alone. The existing
overrides (Window Shopper persona, HIGH risk) must still win over this
new rule, same as they already win over the original rules.

**`personalization.PERK_LARGE_QTY_THRESHOLD = 10`** -- deliberately its
own constant, not a reuse of `RISK_LARGE_QTY_FALLBACK_THRESHOLD`, even
though both happen to be 10 in the current data. They answer different
questions ("is this order big enough to earn free delivery on its own
merits?" vs. "is this qty, combined with a new buyer, a pricing-abuse
signal?") and were confirmed with the user as intentionally separate
thresholds that could diverge later.

**`personalization.perk_eligibility()` signature changed**: now takes
`qty` as a new required parameter (`perk_eligibility(buyer_id, orders,
persona, risk_level, qty)`). One call site (`negotiation_loop.py`'s
`run_negotiation()`) updated to pass `buyer.qty` -- the same value
`risk_assessment()` a few lines above it already uses.

**Precedence, all five rules** (1-2 unchanged, checked first and still
short-circuit everything below; 3-5 all independently evaluated):
1. `risk_level == "high"` → no perks at all (unchanged).
2. `persona == "Window Shopper"` → no perks at all (unchanged).
3. 0 prior orders → eligible for `free_delivery` (unchanged).
4. `qty > PERK_LARGE_QTY_THRESHOLD` → **also** eligible for
   `free_delivery` (new) -- independent of rule 3, not exclusive with it.
5. `>0` prior orders → eligible for `extended_warranty` (unchanged).

**The "at most one perk by construction" invariant is broken, on
purpose.** Rules 4 and 5 are no longer mutually exclusive: an
established buyer (rule 5, prior_orders > 0) placing a qty > 10 order
(rule 4) is now eligible for **both** `free_delivery` and
`extended_warranty` at once. `merchant_agent._resolve_perks()` required
**zero code changes** -- `candidates = [p for p in requested_perks if p
in eligible_perks]` and `total_cost = sum(...)` already generalize to N
simultaneous candidates with no special-casing for "at most one"; only
that function's docstring claim about the old invariant was stale and
has been corrected. All-or-nothing behavior is preserved: two
simultaneous candidates are still granted or declined **together**,
based on whether the offer clears the floor with their **combined** cost
folded in -- never "grant some, decline others."

**Confirmed with real data (Bulk Buyer, BUYER-003, real order history)**
at qty=11, the exact case the user asked to see:

```
BUYER-003 persona='Bulk Buyer', prior_orders=36
risk_level at qty=11: 'moderate' -- large request (qty 11 vs typical 10);
  discount ceiling reduced to 25% of normal.

perk_eligibility result:
  eligible: ['free_delivery', 'extended_warranty']
  rule:     established_buyer+large_qty
  rationale: buyer_id=BUYER-003 has 36 prior order(s) and
    persona='Bulk Buyer' -- eligible for extended_warranty (established
    buyer). qty 11 also exceeds the large-order threshold (10),
    independently qualifying for free_delivery too -- eligible for both.
```

Both perks apply simultaneously -- confirmed via a full `run_negotiation()`
run too (not just the pure-function result): `granted_perks ==
["free_delivery", "extended_warranty"]`, both granted together in one
acceptance, since the discount-tier floor at MODERATE risk (950.0, list
price 1000.0 × 0.95) already clears both perks' combined cost even
before folding it in.

**Why an established buyer specifically, not a new one**: a genuinely
NEW buyer (0 prior orders) placing a qty > 10 order trips risk_assessment()'s
own `new_buyer` AND `large_request` factors together -- HIGH risk --
which zeroes perk eligibility entirely via rule 1, before rules 3/4 are
ever reached. This is unavoidable and by design (Section 2N): the new
qty-based perk rule can never actually fire for a genuinely new buyer in
practice, since HIGH risk always intercepts that combination first. The
rule's real-world effect is specifically for an ESTABLISHED buyer
ordering qty > 10, who isn't a new buyer and (by construction --
`risk_assessment()`'s HIGH level requires BOTH factors, and an
established buyer can never trip `new_buyer`) can never be HIGH risk
either -- confirmed directly: `test_high_risk_override_still_wins_for_established_buyer_at_large_qty`
forces `risk_level="high"` directly into `perk_eligibility()` anyway
(bypassing `risk_assessment()`, which can't naturally produce that
combination) to prove the override's precedence doesn't silently depend
on that being unreachable.

**Overrides re-verified at qty > 10, not just assumed unchanged**:
- Window Shopper, established buyer (5 prior orders), qty=11: still
  `eligible: []`, `rule: window_shopper` -- the new qty rule does not
  leak through the persona override.
- Window Shopper, NEW buyer (0 prior orders), qty=11: lands on
  `risk_override` instead (this combination is HIGH risk, a different,
  already-covered override) -- not tested as a `window_shopper` case for
  that reason, noted directly in the test.
- HIGH risk (forced), established buyer, qty=11: still `eligible: []`,
  `rule: risk_override`.

**Tests** (`tests/e2e/negotiation/test_perks.py`):
- `test_established_buyer_large_qty_eligible_for_both_perks_simultaneously`
  (new): the exact qty=11/established/non-Window-Shopper/non-HIGH-risk
  case requested -- full `run_negotiation()` run, asserts both perks
  granted together, `risk_level == "moderate"` (confirming NOT high),
  and the `perk_review` audit entry's `evidence_paths`/`rationale`.
- `test_window_shopper_both_perks_declined_regardless_of_order_history`
  (extended): parametrized over `(prior_orders, qty)` pairs now, adding
  `(5, 11)` to prove the persona override still wins over the new rule.
- `test_high_risk_override_still_wins_for_established_buyer_at_large_qty`
  (new): direct unit test of `perk_eligibility()`, forcing the otherwise-
  unreachable HIGH-risk-established-buyer combination, per the "why an
  established buyer" note above.

Full suite: 120 passed, 3 skipped (117 + 3: one new parametrize case,
two new test functions) -- zero regressions.

---

## Section 2AI — Frontend restructure: landing page + persistent nav + 3-step negotiation flow (2026-09-05)

**Pure presentation/navigation change, confirmed with the user before
implementing**: no negotiation/risk/liquidation/perk/payment LOGIC
touched -- every API call and data flow is byte-identical to before.
This is entirely about how existing results are grouped and revealed,
not what they contain. Zero backend files changed except two stale
doc-comment paths in `src/api.py` (see below). Full suite unaffected
throughout (120 passed, 3 skipped, pure-frontend change).

**Chosen approach**: single page, JS-controlled panel show/hide (the
user's own stated preference, given time constraints) — not three
separate HTML pages passing state via `negotiation_id`. Faster and
lower-risk: no new API surface needed, no risk of losing in-memory
state (`lastNegotiationParams`/`lastRequestedPerks`) across a real page
navigation.

**1. Landing page** — `frontend/index.html` was rewritten into a short,
standalone landing page (title, 1-2 sentence intro, a single "Get
Started" link to `negotiate.html`). Chose "make index.html this" (the
user's second offered option) over a separate `landing.html`, since
`index.html` is the default file served for the `frontend/` directory —
keeping it as the entry point matches ordinary static-hosting
conventions. The negotiation tool itself moved to a **new file**,
`frontend/negotiate.html` (see below) — not left in place, since
`index.html` needed to become the landing page instead. Reuses the same
`:root` CSS custom properties (`--bg`, `--text`, `--muted`, `--accent`,
etc.) as every other page — no new color tokens introduced. Deliberately
has **no** persistent nav (the one page excluded from requirement 2 —
there's no dashboard/inventory context to jump to yet from a true entry
point).

**2. Persistent top nav** — every other page (`negotiate.html`,
`dashboard.html`, `frontend/inventory.html`) already had SOME cross-page
navigation from earlier sections (2Z, the inventory-dashboard work); this
pass unified them onto one shared class name, `a.nav-link` (previously
`negotiate.html`/`index.html` used `a.dashboard-link` for the identical
CSS rules `dashboard.html`/`inventory.html` already called `nav-link` —
a naming inconsistency from when those were added separately, not a
visual difference). All three tool pages now use the same class, same
position (top-right of the header), same two destinations available from
each of the other two:
- `negotiate.html`: View Inventory, View Dashboard.
- `frontend/inventory.html`: Negotiate (→ `negotiate.html`, not the new
  landing page — jumping back to a splash screen from a working tool
  would be worse UX than returning straight to it), View Dashboard.
- `dashboard.html`: Negotiate (→ `frontend/negotiate.html`), View
  Inventory.

**3. Three-step negotiation flow**, all inside `negotiate.html`, three
sibling `<div>` containers (`#step-setup`, `#step-negotiate`,
`#step-outcome`) toggled by a single `showStep(step)` function
(`style.display = "block"`/`"none"`, plus `window.scrollTo(0, 0)` on
every switch) — the SAME `.card` elements the old single-scrolling-page
`index.html` already had, just grouped into containers instead of one
continuous scroll:
- **Step 1** (`#step-setup`): the existing `setup-card` (merchant/
  product/buyer/qty/budget/perks inputs, Start Negotiation button) —
  unchanged content.
- **Step 2** (`#step-negotiate`): `loading-card`, `risk-card`,
  `rounds-container`, `round-limit-card`, `approval-card` — the
  negotiation-in-progress cards, including BOTH pause-for-decision cards
  (per requirement 4 below).
- **Step 3** (`#step-outcome`): `outcome-card` (which already contains
  the Perks section, Section 4F), `floor-reveal-card`, `payment-card` —
  the final screen, nothing below it. A "Start New Negotiation" button
  was added here (not explicitly requested, but required for the flow to
  be usable at all — without it there'd be no way back to Step 1 after
  one run); it calls the existing `resetResultPanels()` and returns to
  `showStep("setup")`, deliberately leaving the setup form's field values
  untouched so re-running a similar negotiation doesn't require
  re-entering everything.

Control-flow changes, all in `negotiate.html`'s `<script>` (no function
renamed or removed, only where they're called from):
- `startNegotiation()`: calls `showStep("negotiate")` right after
  validation passes (before the async `/api/negotiate` call) — the
  loading spinner and everything that follows now belongs on Step 2, not
  stacked below the setup form. On a request failure, falls back to
  `showStep("setup")` rather than stranding the user on an empty Step 2.
- `onNegotiationRevealed(data)`: `round_limit_reached` and
  `pending_approval` no longer need to do anything panel-wise (already on
  Step 2) — only the terminal-outcome `else` branch now also calls
  `showStep("outcome")` before `renderOutcome(data)`.
- `resolveApproval()`: `/api/approve` is always terminal (never another
  pause), so its success path always calls `showStep("outcome")`.
- `resolveRoundLimitDecision()`: if resuming lands back in
  `pending_approval` (accepting the round-limit offer can itself trigger
  the ordinary approval gate — Section 2X), delegates to
  `onNegotiationRevealed()` and stays on Step 2; only the genuinely
  terminal branch calls `showStep("outcome")`.

**4. Both pause points stay on Step 2, not a 4th page** — confirmed by
construction: `round-limit-card` and `approval-card` are both inside
`#step-negotiate`, and neither code path that shows them
(`onNegotiationRevealed()`'s `round_limit_reached`/`pending_approval`
branches) calls `showStep()` at all, since Step 2 is already the active
panel by the time either can fire. Live-verified directly (calling
`onNegotiationRevealed()` with synthetic `round_limit_reached` and
`pending_approval` payloads): `#step-negotiate` stayed `display: block`,
`#step-outcome` stayed `display: none`, and the respective card rendered
correctly in both cases.

**Live-verified end to end** (real API, scripted buyer + rules merchant
for an instant run): landing page loads with no nav and a working "Get
Started" link; Step 1 renders alone (Steps 2/3 absent from the page
text); submitting moves to Step 2, which resolves to Step 3 showing
`Outcome COMPLETED`, the floor reveal, and Payment Details together,
`Step 1`'s setup card absent from the page text; "Start New Negotiation"
returns to Step 1 with the merchant/buyer selections preserved; the nav
links on `negotiate.html`, `dashboard.html`, and `frontend/inventory.html`
all resolved to the correct targets.

**Styling**: no new design language — reused every existing CSS custom
property and `.card`/`.badge`/`.hint`/button class as-is. The only new
rules are the panel-visibility toggles (`#step-negotiate, #step-outcome
{ display: none; }`) and the landing page's own minimal, token-reusing
layout. No animations added for the step transitions (plain instant
`display` swap, per the user's explicit "no animations needed, prioritize
correctness" instruction) — the existing round-by-round reveal fade-in
(`@keyframes fadein`, Section 2A-era) was untouched.

**Non-frontend changes**: two stale doc-comment path references in
`src/api.py` (`frontend/index.html` → `frontend/negotiate.html`) —
comments only, zero behavioral change, confirmed via the full suite
staying green.

---

## Section 2AJ — Raw audit trail on the outcome page (2026-09-05)

**The question that prompted this**: did the frontend already expose the
raw audit log entries anywhere, or only a curated/formatted view of
them? Checked before writing anything: every card the frontend already
rendered (`rounds`, `risk_review`, `perk_review`, `granted_perks`/
`declined_perks`, `payment`) is a CURATED subset -- `_extract_rounds_and_
risk()` and the `perk_review = next(...)` lookup in `src/api.py` both
filter/reshape the collected entries before returning them; no endpoint
ever returned the full, unfiltered JSONL entries for a negotiation. So:
add one.

**`GET /api/audit-log?negotiation_id=...`** (new, `src/api.py`) --
read-only, no negotiation/pricing/guardrail logic. Reuses
`src.dashboard.load_entries(DEFAULT_LOG_PATHS)` -- the EXACT SAME reader
`GET /api/dashboard-data` already calls, over the same two files
(`audits/negotiation.log`, `audits/dashboard_seed.log`) -- filtered down
to entries whose `negotiation_id` matches, returned as-is (`{negotiation_id,
count, entries}`). No new log-parsing logic; this can never disagree with
what the dashboard's own aggregation considers "the audit trail" for the
same reason `GET /api/floor-preview` can never disagree with the real
floor (Section 2AB) -- one function, every consumer.

**Frontend** (`frontend/negotiate.html`): a new "Raw Audit Trail" card on
Step 3 (the outcome page), placed after Payment Details and before "Start
New Negotiation" -- the last, most-technical layer of the final screen.
A collapsed-by-default `<details>` (reusing the existing `.floor-breakdown`
class for the same chevron/summary interaction "How is this calculated?"
already uses -- no new collapsible pattern introduced), containing a
`<pre>` with `JSON.stringify(entries, null, 2)` -- deliberately plain,
unstyled JSON, per the request: this proves the data is real and
complete, it isn't meant to be pretty. `renderAuditTrail(data)` is called
from `renderOutcome()` (alongside the existing `renderPaymentCard()`/
`renderFloorReveal()` calls), fetching by `data.negotiation_id` -- present
on every terminal outcome already, no new field needed from the backend
response shape.

**Live-verified** with a real completed negotiation: `GET /api/audit-log`
returned the correct 5 raw entries (`offer`, `accept`, `inventory_hold`,
`payment_initiated`, `payment_completed`), all sharing the real
`negotiation_id`, matching a real full JSONL entry's actual schema
(`timestamp`, `decision_id`, `agent`, `action`, `offer`, `rationale`,
`evidence_paths`, `decision_hash`, `provenance_sha`, `negotiation_id`,
`product_name`, `list_price`, `merchant_id`) -- not a reshaped summary.
Confirmed in the browser: the card appears on Step 3, collapsed by
default, and expands to the same JSON.

Full suite: 120 passed, 3 skipped, unchanged (one single flaky failure
during verification -- `test_layer_2_cannot_counter_past_the_round_cap_
when_the_final_offer_already_clears_the_floor` -- reproduced as an
isolated one-off; passed on its own and on three subsequent full-suite
re-runs, unrelated to this section's changes, which touched neither
`merchant_agent.py` nor that test file).

---

## Section 2AK — Fit each negotiation-flow panel within one viewport, no page scroll (2026-09-05)

**Pure CSS sizing/spacing change, `frontend/negotiate.html` only** -- no
content removed, no layout/control-flow logic touched (the three
`showStep()` panels from Section 2AI are unchanged in structure and
behavior). Goal: Steps 1-3 each fit within a ~1366x768 viewport without
the page needing to scroll, scaling cleanly to larger screens.

**Baseline measured before changing anything** (1366x768, real browser
viewport, real rendered content): Step 1 (setup, all fields/hints
visible) overflowed by 187px; Step 3 (outcome, one real completed
negotiation) overflowed by 91px; Step 2 with a synthetic 8-round
transcript overflowed by 409px (rounds-container alone measured 888px
tall). These numbers set the actual tightening budget, not a guess.

**Page shell**: `body` is now a fixed-height (`height: 100vh`) flex
column with `overflow: hidden` -- `header` and `.error-banner` are
non-shrinking flex items (`flex: 0 0 auto`), and whichever step `<div>`
is active (`#step-setup` / `#step-negotiate` / `#step-outcome`) is a
`flex: 1 1 auto; min-height: 0;` item that fills the remaining space.
This is what keeps the header/nav genuinely fixed while a step's content
scrolls, rather than the whole page scrolling past the header. Each step
div also carries its own `overflow-y: auto` -- a FALLBACK, not the
primary mechanism: in the normal case (after the spacing cuts below)
nothing needs it, but if some edge-case content (a very long rationale
string, a large expanded raw-audit-trail JSON dump) ever runs long, nothing
is clipped -- that step's own content scrolls in place instead of
silently losing information.

**Spacing tightened globally** (not per-step -- these are the shared
rules every card/field/button already used): `body` padding 32px ->
18px/28px; header margin-bottom 24px -> 12px; `.card` padding 20px ->
12px/16px, margin-bottom 18px -> 10px; `.card h2` margin 14px -> 6px;
`.field` margin-bottom 14px -> 6px; `.field label` margin-bottom 5px ->
3px; input/select padding 9px -> 7px; `.row` gap 14px -> 10px; `.hint`/
`.budget-warning` padding and margins each cut by ~2px; button padding
10px -> 8px; and proportionally smaller cuts across `#risk-card`,
`#approval-card`, `#round-limit-card`, `#outcome-perks`, and
`#payment-card`'s internal margins. No font sizes changed anywhere --
every cut was padding/margin/gap, per the request's own suggested lever.

**`#rounds-container`** -- the one section EXPECTED to scroll internally
once a negotiation has many rounds, exactly as requested: `max-height:
40vh; overflow-y: auto;` (vh, not a fixed px number, so it scales with
viewport -- a taller screen shows more rounds before this engages, per
"works fine scaling up on larger screens"). `.round-entry` margin-bottom
also cut 10px -> 6px, so more rounds fit before the internal scroll is
even needed.

**Live-verified at 1366x768** (real browser viewport, real API calls
where practical, synthetic DOM content via the page's own render
functions for the round-count scenarios that need many rounds on
demand):
- Step 1 (all fields/hints populated): fits exactly -- `scrollHeight ==
  clientHeight` on `#step-setup`, zero outer page scroll.
- Step 3, real COMPLETED negotiation with a granted perk shown: fits
  exactly, zero outer scroll.
- Step 3, REJECTED outcome: fits exactly, zero outer scroll.
- Step 2, Risk Agent card + 3 rounds: fits exactly, zero scroll anywhere.
- Step 2, Risk Agent card + round-limit-card + 5 rounds: fits exactly,
  zero scroll anywhere.
- Step 2, 9 rounds: `#step-negotiate` itself still fits with no scroll
  (`scrollHeight == clientHeight`), while `#rounds-container` correctly
  triggers its own internal scroll (942px of content in a 305px/40vh
  box) -- header confirmed still on-screen (`getBoundingClientRect().top
  >= 0`) throughout, page-level `scrollHeight` still equal to
  `innerHeight` (no outer scroll introduced by the many-round case).
- 1920x1080: Step 1 renders correctly, form compact at the top with
  unused space below (expected -- the flex step panel fills leftover
  viewport height; no stretching or breakage).

Full suite: 120 passed, 3 skipped, unchanged (pure CSS, no Python
touched).

---

## Section 3 — State machine

### States

| State | Meaning |
|---|---|
| `OPEN` | Negotiation started, no offer evaluated yet. |
| `BUYER_TURN` | Buyer-agent must produce an offer or counter. |
| `MERCHANT_TURN` | Merchant-agent must evaluate the current offer via `evaluate(offer, policy, round)`. |
| `AGREEMENT_RECORDED` | **Terminal.** Merchant accepted an offer. |
| `REJECTED` | **Terminal.** Explicit policy violation with no viable counter, or the round cap was reached with no agreement. |
| `BUYER_UNAVAILABLE` | **Terminal (Milestone 3a, AI buyer only).** The AI buyer's LLM backend stayed unreachable/rate-limited through all retries — see Section 2A. Never reachable with the scripted buyer. |

`NEGOTIATION_DECLINED` (Milestone 5's original Risk Agent design) is not
listed here — reframed in Section 2N (2026-09-02) before it shipped to
any real demo; a "high" risk verdict no longer produces a distinct
terminal state, it tightens `policy.max_discount_pct` instead and the
negotiation proceeds through the normal states above.

### Transition table

| From state | Trigger | Merchant decision | To state | Notes |
|---|---|---|---|---|
| `OPEN` | Buyer generates initial offer | — | `MERCHANT_TURN` | Round counter starts at 1. |
| `OPEN` / `BUYER_TURN` | AI buyer's LLM call fails after all retries | — | `BUYER_UNAVAILABLE` | Terminal. AI buyer only (Section 2A); logs `buyer_unavailable`. |
| `MERCHANT_TURN` | `evaluate()` called | `accept` | `AGREEMENT_RECORDED` | Terminal. |
| `MERCHANT_TURN` | `evaluate()` called | `reject` | `REJECTED` | Terminal — explicit-violation variant. |
| `MERCHANT_TURN` | `evaluate()` called | `counter` AND `round >= max_negotiation_rounds` | `REJECTED` | Terminal — round-limit variant. |
| `MERCHANT_TURN` | `evaluate()` called | `counter` AND `round < max_negotiation_rounds` | `BUYER_TURN` | Merchant's counter becomes the current offer; round increments. |
| `BUYER_TURN` | Buyer-agent reacts to merchant's counter | — | `MERCHANT_TURN` | Buyer accepts the counter, or proposes a new counter via its scripted strategy. |

### Terminal states (exactly one per run)

1. **`AGREEMENT_RECORDED`** — merchant accepted an offer within the round cap.
2. **`REJECTED` (explicit)** — merchant rejected on policy grounds with no viable counter.
3. **`REJECTED` (round-limit)** — `max_negotiation_rounds` reached without agreement.
4. **`BUYER_UNAVAILABLE`** (Milestone 3a, AI buyer only) — the AI buyer's
   LLM backend failed through all retries; not reachable with the
   scripted buyer.

Both `REJECTED` variants write the same terminal `action: "reject"` audit
entry; the distinguishing detail lives in `rationale` and `evidence_paths`
(round-limit variant's evidence path is `policy.max_negotiation_rounds`).

---

## Section 3A — Payment state machine (Milestone 2)

`AGREEMENT_RECORDED` is still the negotiation phase's own terminal state
(CAP-2 / `run_negotiation()` is unchanged). A separate orchestration layer
picks up from there and carries the deal through payment.

### States

| State | Meaning |
|---|---|
| `PENDING_APPROVAL` | `agreed_price * qty` exceeds `policy.transaction_approval_threshold`; waiting on a human y/n. |
| `PAYMENT_INITIATED` | A real Razorpay test-mode order has been created (`orders.create`); the payment outcome is about to be simulated. |
| `COMPLETED` | **Terminal.** Simulated payment succeeded. |
| `ROLLBACK` | **Terminal.** Simulated payment failed; inventory hold released, `human_notification` emitted. |
| `APPROVAL_DECLINED` | **Terminal.** A human declined the approval gate; no order was ever created. |

### Transition table

| From state | Trigger | To state | Notes |
|---|---|---|---|
| `AGREEMENT_RECORDED` | `agreed_price * qty <= policy.transaction_approval_threshold` | `PAYMENT_INITIATED` | Approval gate skipped; also logs an `inventory_hold` audit entry. |
| `AGREEMENT_RECORDED` | `agreed_price * qty > policy.transaction_approval_threshold` | `PENDING_APPROVAL` | Logs an `approval_requested` audit entry; also logs `inventory_hold`. |
| `PENDING_APPROVAL` | Human confirms (CLI `y`) | `PAYMENT_INITIATED` | Logs `approval_granted`. |
| `PENDING_APPROVAL` | Human declines (CLI `n`) | `APPROVAL_DECLINED` | Terminal. Logs `approval_declined`. `payment_service` is never called. |
| `PAYMENT_INITIATED` | Order created; simulated outcome = success | `COMPLETED` | Terminal. Logs `payment_completed`. |
| `PAYMENT_INITIATED` | Order created; simulated outcome = forced failure | `ROLLBACK` | Terminal. Logs `payment_rollback` (with Razorpay error code), `inventory_release`, and `human_notification`. |

### Payment simulation boundary (why, not a hidden shortcut)

Razorpay's standard flow requires a browser Checkout step between order
creation and payment capture — a payment only becomes capturable once a
customer completes checkout. Parley's negotiation loop is a headless CLI
flow with no browser step, so:

- `payment_service.create_order(amount, currency)` makes a **real** call to
  Razorpay's test-mode Orders API and returns a real `order_id`.
- The payment **outcome** (success vs. forced failure) is decided by
  `payment_service.simulate_payment(order_id, force_failure)` — an
  in-process simulation, not a real `payments.capture` call, since no
  browser checkout ever produced a real `payment_id` to capture. A forced
  failure produces a synthetic Razorpay-style error code
  (`"BAD_REQUEST_ERROR"`, description `"Payment failed (simulated test-mode
  failure)."`) attached to the `ROLLBACK` audit entry.

### Amount units

Razorpay's API expects amounts in the currency's smallest subunit (paise
for INR): `amount = round(agreed_price * qty * 100)`. Every other Parley
field (`list_price`, `min_price`, offer `price`, etc.) stays in decimal
rupees; only `payment.amount` (Section 4A) and the `payment_service` call
use paise.

---

## Section 3B — Inventory fulfillment check (Milestone 3c)

Only active when `run_full_transaction()` is given `product`
(a `catalog.json` entry) and `catalog_path` — i.e. only on the
`PRODUCT_ID` path (Section 2D). Both default to `None`, under which this
whole section is skipped and behavior is byte-for-byte Milestone 1/2/3a/3b.

Inserted into the Section 3A payment flow **before** `inventory_hold`
(and therefore before the approval gate and before any Razorpay call):

| From state | Trigger | To state | Notes |
|---|---|---|---|
| `AGREEMENT_RECORDED` | `negotiated qty > product.current_inventory` | `ROLLBACK` | Terminal. Logs `insufficient_inventory` then `human_notification`. `payment_service` is **never** called — this is caught earlier than, and for a different reason than, the existing payment-failure `ROLLBACK` (Section 3A). |
| `AGREEMENT_RECORDED` | `negotiated qty <= product.current_inventory` | `PAYMENT_INITIATED` | Proceeds exactly as Section 3A. |
| `PAYMENT_INITIATED` | Payment `COMPLETED` | `COMPLETED` | `catalog.json`'s `current_inventory` for this `sku_id` is decremented by the ordered qty (`personalization.decrement_inventory` — read-modify-write the file) and an `inventory_decremented` entry is logged. |

Both `ROLLBACK` variants (this one and Section 3A's payment failure) now
carry a `"reason"` key on the `run_full_transaction()` return value:
`"insufficient_inventory"` or `"payment_failure"` — purely additive to
the outcome dict, no audit-schema change, and no test ever asserted an
exhaustive key set on it, so this didn't touch Milestone 1/2/3a/3b
behavior.

### Example outcome (insufficient inventory)

```json
{"state": "ROLLBACK", "offer": {"price": 4399.12, "qty": 5, ...}, "payment": null, "reason": "insufficient_inventory"}
```

No `payment` object at all here (`null`, not the Section 4A shape) —
unlike the payment-failure `ROLLBACK`, no order was ever created, so
there's nothing Razorpay-shaped to report.

---

## Section 3C — Payment-failure demo output, no-auto-retry guarantee, and explicit retry (Milestone 3c follow-up)

### Console demo block

`run_full_transaction()`'s payment-attempt logic was refactored into a
shared `_attempt_payment()` (used by both the first, automatic attempt
and `retry_payment()` below). When `__main__` sees
`outcome.get("reason") == "payment_failure"`, it prints a formatted block
(ASCII-only — Windows consoles mangle `—` without a UTF-8 codepage set,
confirmed by a mojibake test run) distinct from the per-entry
`_print_event()` trail:

```
============================================================
  PAYMENT FAILED - ROLLBACK
============================================================
  Order:   order_TWTXV8PKgRpEmw
  Reason:  BAD_REQUEST_ERROR - Payment failed (simulated test-mode failure).
  Amount:  13197.36 INR

  Checklist:
    [x] Order remains unpaid (no capture occurred)
    [x] No duplicate payment attempt was made
    [x] Buyer-agent was notified (buyer_notification logged)
    [x] Human/operator was notified (human_notification logged)
    [x] Simulated inventory hold released
============================================================
```

Every checklist line is true **by construction**, not asserted text:
`_attempt_payment()`'s rollback branch always logs `payment_rollback`,
`inventory_release`, `buyer_notification`, and `human_notification`
together in the exact code path that produces this outcome — the console
block runs after all four are already written.

Set `FORCE_PAYMENT_FAILURE=1` (also `true`/`yes`) to trigger this from
the CLI for a demo — previously there was no way to force a failure from
`__main__` at all, only from test code calling `run_full_transaction(...,
force_payment_failure=True)` directly.

### No-auto-retry guarantee

`_attempt_payment()` makes exactly one `payment_service.create_order()`
call per invocation and never calls itself or loops — "a failed payment
is never automatically retried" is true by construction, not convention.
Tested directly: one `run_full_transaction()` call with a forced failure
produces exactly one `payment_client.order.calls` entry and zero
`payment_retry_approved` audit entries.

### `retry_payment(offer, policy, ...)` — the explicit re-authorization step

The **only** way a previously-failed payment is ever retried. Never
called by `run_full_transaction()` or `_attempt_payment()` — a caller
(a human operator today; a future explicit CLI flag) must invoke it
separately, once per retry attempt. Logs `payment_retry_approved` (the
explicit-authorization marker) and re-places `inventory_hold` (the
original hold was already released on the first failure — Section 3A),
then calls `_attempt_payment(..., is_retry=True)`, which logs the same
lifecycle entries as the first attempt with `" (retry)"` appended to
their rationale text for audit-trail clarity.

**Hold duration — reuses the offer's own `expiration`, no second timer.**
The user's explicit choice: `merchant_policy.json` has no
`offer_expiration_seconds` field (a corrected misconception — the 5
minutes in question is `offer_utils.new_offer()`'s hardcoded
`ttl_minutes=5` default, which governs *offer* validity, not a payment-
retry concept). Rather than invent a second, unrelated timer,
`retry_payment()` simply checks `now < offer.expiration` — the same
window the accepted offer already had. Past that, it raises `ValueError`
requiring a fresh negotiation instead of resurrecting an expired offer;
inventory sufficiency is deliberately **not** re-checked on retry (the
window is short and this wasn't asked for — flagged as an assumption
below, not silently expanded scope).

### `buyer_notification` (distinct from `human_notification`)

New action, logged alongside (not instead of) the existing
`human_notification` on every payment failure — same base schema
(Section 4), `agent: "buyer-agent"` instead of `"merchant-agent"`,
distinct rationale text aimed at the buyer's side of the failure rather
than the operator's. No schema change; reuses `log_entry()` unmodified,
same discipline as Section 4B's `buyer_strategy`.

---

## Section 4 — Audit log schema

One JSON object per line, appended to `audits/negotiation.log` for every
negotiation action (initial offer, each counter, and the terminal
decision).

| Field | Type | Description |
|---|---|---|
| `timestamp` | string | ISO 8601 UTC, when this action was recorded. |
| `decision_id` | string | Unique id for this audit entry (e.g. `uuid4` hex). |
| `agent` | string | `"buyer-agent"` or `"merchant-agent"`. |
| `action` | string | One of `"offer"`, `"counter"`, `"accept"`, `"reject"`. |
| `offer` | object | The offer this entry concerns (Section 2 shape). |
| `rationale` | string | 1–2 sentence explanation naming the policy field that drove it. `""` for plain buyer `"offer"` actions. |
| `evidence_paths` | array of strings | Dotted paths into the policy object. `[]` for plain buyer `"offer"` actions. |
| `decision_hash` | string | sha256 hex digest — see formula below. |
| `provenance_sha` | string | SHA of the code/docs version informing this decision. Milestone 1 uses the literal placeholder `"UNVERIFIED"`. |

### `decision_hash` formula

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

Independently recomputable from any logged entry's `offer`, `rationale`,
and `evidence_paths` fields.

### Example entry (merchant rejects below `min_price`)

```json
{"timestamp": "2026-08-29T10:15:03Z", "decision_id": "8f14e45f-ceea-467e-9998-1234567890ab", "agent": "merchant-agent", "action": "reject", "offer": {"offer_id": "a1b2c3", "price": 3500.0, "qty": 3, "terms": "", "expiration": "2026-08-29T10:20:03Z", "timestamp": "2026-08-29T10:15:00Z"}, "rationale": "Offer price 3500.0 is below policy.min_price 3799.0 for qty 3.", "evidence_paths": ["policy.min_price"], "decision_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b85", "provenance_sha": "UNVERIFIED"}
```

(The `decision_hash` value above is illustrative — the real value is
whatever the formula produces for this exact `offer`/`rationale`/
`evidence_paths` triple.)

---

## Section 4A — Payment audit fields (Milestone 2)

Same base schema as Section 4 (all nine fields still present on every
entry). Payment-lifecycle actions additionally carry a `payment` field;
`offer` still carries the underlying negotiated offer for traceability.

| `action` value | Meaning |
|---|---|
| `"inventory_hold"` | Simulated hold placed after `AGREEMENT_RECORDED`. |
| `"approval_requested"` | Value exceeds `transaction_approval_threshold`; pausing for human input. |
| `"approval_granted"` | Human confirmed via the CLI y/n prompt. |
| `"approval_declined"` | Human declined; `payment_service` never called. |
| `"payment_initiated"` | Real Razorpay test-mode order created. |
| `"payment_completed"` | Simulated payment succeeded. |
| `"payment_rollback"` | Simulated payment failed. |
| `"inventory_release"` | Hold released as part of rollback. |
| `"human_notification"` | Structured stand-in for an email/SMS alert on rollback. |
| `"insufficient_inventory"` | Milestone 3c (Section 3B): negotiated qty exceeds `catalog.json` stock; rolled back before any payment call. |
| `"inventory_decremented"` | Milestone 3c (Section 3B): `catalog.json` stock reduced by the ordered qty after `COMPLETED`. |
| `"buyer_notification"` | Milestone 3c (Section 3C): the buyer-agent's side of a payment failure — `agent: "buyer-agent"`, distinct from `human_notification`. |
| `"payment_retry_approved"` | Milestone 3c (Section 3C): explicit re-authorization marker, logged only inside `retry_payment()` — never on the first, automatic attempt. |
| `"liquidation_applied"` | Milestone 3c follow-up (Section 2E): logged once when a `PRODUCT_ID`'s `days_in_inventory` exceeds its category's threshold; names the category-specific threshold and the before/after floor. |

### `payment` field shape

```json
{
  "order_id": "order_ABC123",
  "payment_id": null,
  "amount": 439912,
  "currency": "INR",
  "status": "created",
  "error_code": null,
  "error_description": null
}
```

- `amount`: integer, paise (Section 3A).
- `status`: one of `"created"`, `"completed"`, `"failed"`.
- `payment_id`: always `null` in Milestone 2 (Section 3A — no real capture
  happens); reserved for a future milestone that adds real checkout.
- `error_code` / `error_description`: populated only on `payment_rollback`
  entries, with the synthetic error from Section 3A.
- **Never present anywhere:** `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, or
  the raw Razorpay API request/response body.

### `decision_hash` extension

`decision_hash(offer, rationale, evidence_paths, payment=None)` — when
`payment` is not `None`, it is included as a fourth key in the canonical
JSON object before hashing:

```python
def decision_hash(offer, rationale, evidence_paths, payment=None):
    obj = {"offer": offer, "rationale": rationale, "evidence_paths": evidence_paths}
    if payment is not None:
        obj["payment"] = payment
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Every Milestone-1 entry (no `payment`) hashes identically to before —
this is a strict extension, not a breaking change.

---

## Section 4B — Buyer strategy audit entry (Milestone 3a)

The AI buyer's private reasoning gets its own entry, logged via the
**existing, unmodified** `log_entry()` — no schema change, no new field.
It reuses the same nine base fields from Section 4:

| Field | Value for this entry |
|---|---|
| `agent` | `"buyer-agent"` |
| `action` | `"buyer_strategy"` |
| `offer` | `null` — this entry isn't an offer; the adjacent `"offer"`-action entry (Section 2) carries the real one. |
| `rationale` | `strategy_note`, with `target_price` and `walk_away_price` appended, e.g. `"Opening low since list price leaves room to negotiate; will concede toward the merchant's counter. (private: target_price=4300.0, walk_away_price=4550.0)"` |
| `evidence_paths` | `[]` — not a policy-driven decision. |
| `decision_hash` | Computed the normal way over `(null, rationale, [])` — still independently recomputable. |

Logged once per AI-buyer round, immediately before that round's `"offer"`
(or the accept) entry — chronologically, the buyer reasons privately, then
sends the offer. Never logged for the scripted buyer (it has no private
reasoning to log — `run_negotiation()` checks for a `last_strategy`
attribute the scripted `BuyerAgent` doesn't have, and no-ops if absent).

This keeps `target_price` / `walk_away_price` / `strategy_note` fully
explainable in the audit trail while the `audit_logger.py` code and the
Section 4 field list stay byte-for-byte what they were in Milestone 2 —
satisfies "do not touch the audit schema."

---

## Section 4C — Guardrail-clamp audit field (Milestone 3b)

One real schema addition (the user's explicit ask, unlike Section 4B's
reuse-only approach): `guardrail_clamped: bool`, present **only** on
AI-merchant (`MERCHANT_MODE=ai`) decision entries — absent from every
rules-only, buyer, and payment entry, so Milestone 1/2/3a audit entries
hash identically to before (`decision_hash`'s `guardrail_clamped=None`
default omits it from the canonical JSON, exactly like `payment=None`).

| Field | Value |
|---|---|
| `guardrail_clamped` | `true` if Layer 1 overrode or adjusted Layer 2's proposal this round (Section 2B's table); `false` if the AI merchant's proposal passed through unchanged (including a Gemini-outage fallback — nothing was clamped, Layer 2 just didn't run). |

`rationale` on a clamped entry states which policy field was violated
(`evidence_paths` too) and that a clamp occurred — **never** the rejected
raw price/qty Layer 2 proposed, and never `concession_reasoning` verbatim
(free text could echo the rejected value in prose, which code can't fully
sanitize). `concession_reasoning` is logged as `rationale` only when the
proposal needed no clamp at all.

### Example entry (clamped)

```json
{"timestamp": "2026-08-29T14:02:11Z", "decision_id": "c1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6", "agent": "merchant-agent", "action": "counter", "offer": {"offer_id": "f7e6d5c4b3a2918070605040302010f", "price": 4399.12, "qty": 1, "terms": "", "expiration": "2026-08-29T14:07:11Z", "timestamp": "2026-08-29T14:02:11Z"}, "rationale": "Layer 2's proposed counter violated policy.max_discount_pct; clamped to the policy floor before being sent to the buyer.", "evidence_paths": ["policy.max_discount_pct"], "decision_hash": "...", "provenance_sha": "UNVERIFIED", "guardrail_clamped": true}
```

Note `offer.price` is the valid, clamped `4399.12` — the invalid value
Layer 2 actually proposed appears nowhere in this entry.

### `decision_hash` extension

`decision_hash(offer, rationale, evidence_paths, payment=None,
guardrail_clamped=None)` — `guardrail_clamped` is included as a fifth
canonical-JSON key when not `None`, alongside the existing `payment`
extension (Section 4A). Independent of each other; either, both, or
neither may be present on a given entry.

---

## Section 4D — `negotiation_id` grouping field (Milestone 7)

Before this section, audit entries carried only a per-action `decision_id`
(a fresh UUID on every single `log_entry()` call) — no field linked
together the entries produced by one negotiation, including its later
payment-phase entries. Milestone 7's dashboard needs to tell where one
negotiation ends and the next begins across a combined multi-file log, so
this section adds exactly one new field to close that gap.

| Field | Value |
|---|---|
| `negotiation_id` | 32-char lowercase hex (`uuid.uuid4().hex`), generated **once**, as the very first thing `run_negotiation()` does — before any entry that negotiation produces is logged. Threaded through every `log_entry()` call for that negotiation's rounds, and carried forward by `run_full_transaction()` into every entry of that negotiation's payment phase (`inventory_hold`, `payment_initiated`, `payment_completed`/`payment_rollback`, `inventory_release`, notifications). An explicit `retry_payment()` call also carries it, if the caller passes the original negotiation's id back in — `retry_payment()`'s `negotiation_id` parameter defaults to `None` for backward compatibility with any pre-Milestone-7 caller that never captured one. |

A plain top-level field, sibling to `decision_id` — **not** nested inside
`offer`/`payment`/anything else — so grouping a combined log is a single
`groupby("negotiation_id")` (after sorting by timestamp, since two
separately-written log files won't keep one negotiation's entries
contiguous), no nested lookups required.

Treated as identity/session metadata, the same as `decision_id` and
`timestamp`: it describes *which run* produced an entry, not *what was
decided*, so it is **not** part of `decision_hash`'s input — `decision_hash`
stays exactly as defined in Section 4C. Omitted from the entry dict
entirely when `None` (every pre-Milestone-7 caller/test, and any entry
logged outside a `run_negotiation()`/`run_full_transaction()` context),
matching the existing `payment`/`guardrail_clamped` "only present when
meaningful" convention — so no prior entry shape or hash changes.

### Example entry

```json
{"timestamp": "2026-09-03T09:12:04Z", "decision_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", "agent": "merchant-agent", "action": "counter", "offer": {"offer_id": "...", "price": 4399.12, "qty": 1, "terms": "", "expiration": "...", "timestamp": "..."}, "rationale": "...", "evidence_paths": ["policy.max_discount_pct"], "decision_hash": "...", "provenance_sha": "UNVERIFIED", "negotiation_id": "01ba523c71984b13b77f7f3f28cad922"}
```

Live-verified: a full negotiation-to-payment run (offer → counter → accept
→ inventory_hold → payment_initiated → payment_completed) produced 6
entries sharing one `negotiation_id`; a rollback-then-explicit-retry run
produced 13 entries (9 from the failed attempt, 4 from the retry) all
sharing the same `negotiation_id` when the caller passed it through to
`retry_payment()`.

### Follow-up — `product_name` / `list_price` (same day, before bulk seed generation)

Building the dashboard generator surfaced a second gap of the same kind:
no entry recorded *which product* a negotiation was for, or its
`list_price` — both needed for the dashboard's "Recent Negotiations"
product column and its "average discount % vs. list_price" stat. Neither
required new plumbing: both already sit on the `policy` dict passed into
`run_negotiation()`, so they're captured at the same place/time as
`negotiation_id` (`policy.get("product_name")`, `policy.get("list_price")`)
and threaded through every entry — negotiation rounds and payment phase —
exactly like `negotiation_id` above, including into `retry_payment()` as
two more optional (default `None`) parameters.

| Field | Value |
|---|---|
| `product_name` | `policy["product_name"]` at negotiation start, or absent if the policy dict has none. |
| `list_price` | `policy["list_price"]` at negotiation start, or absent if the policy dict has none. |

Same rules as `negotiation_id`: plain top-level fields, **not** part of
`decision_hash`'s input (context, not decision content), omitted from the
entry entirely when `None`. `run_full_transaction()` reads them back from
`run_negotiation()`'s own outcome dict (not by re-reading `policy`), since
a risk-driven repricing inside `run_negotiation()` reassigns its local
`policy` reference without mutating the caller's original dict.

Live-verified alongside `negotiation_id`: all 6 entries of a completed
negotiation-to-payment run carried the same `product_name`/`list_price`
pair end to end.

An entry logged before this fix (or via any policy dict lacking these
keys) simply has no `product_name`/`list_price` — a dashboard consuming
older log data should treat their absence as "unknown," never substitute
a placeholder value.

### Second follow-up — `merchant_id` (same day, after the dashboard shipped)

The dashboard as first shipped had no way to tell which merchant a
negotiation belonged to — every negotiation across both of Milestone 6's
merchants (MERCH-001, MERCH-002) aggregated together with no visible
distinction. `policy["merchant_id"]` (Milestone 6) was already available
at the exact same capture point as `product_name`/`list_price`, so it
gets the identical treatment: captured once at the top of
`run_negotiation()`, threaded through every entry (negotiation rounds and
payment phase alike), read back from `run_negotiation()`'s outcome dict
by `run_full_transaction()` rather than re-read from `policy` (same
risk-repricing-reassignment reasoning as above), not part of
`decision_hash`'s input, omitted from the entry when `None`.

| Field | Value |
|---|---|
| `merchant_id` | `policy["merchant_id"]` at negotiation start (e.g. `"MERCH-001"`), or absent if the policy dict has none (e.g. the single-SKU `merchant_policy.json` fallback, which predates multi-merchant support and carries no merchant_id at all). |

Deliberately just the id, not the human-readable merchant name — resolving
`MERCH-001` → `"Voltstream Electronics"` against `data/merchants.json` is
left to whatever reads the log (the dashboard generator does this at
render time), so the audit log itself doesn't repeat a static lookup's
data on every entry.

Live-verified: a real PRODUCT_ID-driven negotiation + payment run showed
`merchant_id="MERCH-001"` (matching the catalog product's own
`merchant_id`) on every logged entry, negotiation and payment phase both.

`scripts/generate_dashboard.py` was updated to show a Merchant column on
the Recent Negotiations table and a per-merchant stats breakdown, and
`audits/dashboard_seed.log` was regenerated (same `--seed 42`, same 130
negotiations, same outcome mix) so its entries carry `merchant_id` too —
the previous seed batch predated this field.

---

## Section 4E — Multi-dimensional negotiation: perks (Milestone 9)

Buyers can request perks (`free_delivery`, `extended_warranty`) alongside
price. Eligibility is fully deterministic — same "bounded input, pure
function, no LLM discretion at the boundary" pattern as LTV/risk/
liquidation. The one genuinely new mechanism (and the one this section
exists to prove correct) is threading a granted perk's cost through the
*same* margin-floor calculation everything else already goes through,
rather than adding a second, separate check — the exact bug class that
already bit this project once for liquidation (Section 2F/2J) and once
for the Risk Agent's `qty_breaks` scaling (Section 2P/2Q): a protection
added in one place that a parallel pathway silently bypasses.

### Cost fields

`shipping_cost`/`warranty_cost` (INR, what each perk actually costs the
merchant to provide) were added to every catalog product and to
`merchant_policy.json`. `shipping_cost` is a flat per-category table (physical
bulk, not price-driven); `warranty_cost = round(cost * 0.06, 2)` (price-
driven — reserve rate for 1-year extended coverage). Regenerating
`data/catalog.json` (`--seed 42`) was purely additive: the generator's RNG
consumption sequence is unchanged (both new fields are deterministic
lookups/formulas over already-computed values, no new `rng.*` calls), so
`data/buyers.json`/`data/orders.json` came back byte-identical and every
other catalog field was untouched.

| Category | `shipping_cost` |
|---|---|
| Books & Media | 35 |
| Office & Stationery | 40 |
| Beauty & Personal Care | 50 |
| Toys & Games | 55 |
| Apparel & Fashion | 60 |
| Home & Kitchen | 90 |
| Sporting Goods & Outdoors | 100 |
| Electronics | 130 |

`merchant_policy.json` (the single-SKU, non-catalog fixture): `shipping_cost:
130`, `warranty_cost: 200`.

### Removed: `COST_MARGIN_MULTIPLIER` / `cost_floor_price()`

Discovered while designing the perk-cost floor check: `personalization.py`
carried a SECOND, independently-computed margin floor —
`COST_MARGIN_MULTIPLIER = 1.02` (confirmed with the user back in Milestone
3c, Section 2E) — that `product_to_policy()` used to defensively re-clamp
`min_price` (`max(cost_floor_price(product), product["min_price"])`). But
`scripts/generate_synthetic_data.py` already computes `min_price = cost *
1.15` at data-creation time (a DIFFERENT multiplier, 15% not 2%) — since
1.15 > 1.02, the runtime re-clamp was never actually binding for any real
generated product; it was vestigial, duplicating a concept the generator
already owned, with a conflicting number.

Confirmed with the user: removed entirely, not reconciled to match. Exactly
one place now computes the cost-derived floor — the generator, at
data-creation time. `product_to_policy()` trusts `product["min_price"]` as
given. Accepted tradeoff (explicitly confirmed, not an oversight): this
removes a defensive clamp against a malformed catalog entry (raw `min_price`
set unsafely low relative to `cost`) — a real, if narrow, protection this
project is choosing to give up in exchange for a single source of truth.
Three tests referencing the removed function were updated: one deleted
(tested the function directly), two rewritten to assert the new pass-
through behavior instead of the old re-clamp.

### Eligibility

`personalization.perk_eligibility(buyer_id, orders, persona, risk_level, qty)` —
pure function, called once per negotiation (same place/timing as
`risk_assessment()`), reusing the risk_level `risk_assessment()` already
computed rather than re-deriving it. `qty` (Section 2AH, 2026-09-05) is
`buyer.qty`, the same value already passed to `risk_assessment()`.
Checked in this order, rules 1-2 overriding everything below them, rules
3-5 each independently evaluated:

| Rule | Condition | Eligible perks |
|---|---|---|
| 1 | `risk_level == "high"` | `[]` — overrides everything, consistent with the discount ceiling also being zeroed at HIGH risk |
| 2 | `persona == "Window Shopper"` | `[]` — regardless of order history or qty |
| 3 | `count_prior_orders(buyer_id, orders) == 0` | `["free_delivery"]` |
| 4 | `qty > PERK_LARGE_QTY_THRESHOLD` (10) — Section 2AH | **also** `["free_delivery"]` — independent of rule 3, not exclusive with it |
| 5 | `count_prior_orders(buyer_id, orders) > 0` | `["extended_warranty"]` |

**A buyer is no longer guaranteed at most one perk** (Section 2AH broke
this on purpose): rules 4 and 5 can both fire for the same buyer — an
established buyer (rule 5) placing a qty > 10 order (rule 4) is eligible
for **both** `free_delivery` and `extended_warranty` at once. See
Section 2AH for the full rationale, the Bulk Buyer worked example, and
why `merchant_agent._resolve_perks()` needed no code changes to handle
this. The order-count threshold (rules 3/5) is DELIBERATELY not
`RISK_NEW_BUYER_MAX_PRIOR_ORDERS` (2, the Risk Agent's own "new buyer"
factor) — a different question (has this buyer ever ordered here at all,
vs. risk of abusive lowballing) — confirmed with the user as an
intentional distinction. `PERK_LARGE_QTY_THRESHOLD` (rule 4) is likewise
deliberately its own constant, not a reuse of
`RISK_LARGE_QTY_FALLBACK_THRESHOLD`, despite both being 10 today.

Resolved once, upfront, in `run_negotiation()` (only when `buyer_id`/
`orders`/`persona` are all given and `requested_perks` is non-empty — every
pre-Milestone-9 caller skips this block entirely, byte-identical
behavior), and merged into a local `policy` copy as
`policy["eligible_perks"]` — same "local reassignment, never mutate the
caller's dict" discipline as `apply_risk_discount_cap()`. Logged as a new
`perk_review` audit entry (same shape/spirit as `risk_review`): which
perks were requested, which rule applied, and which requests were
immediately declined as ineligible (no LLM/margin discretion on that
boundary at all — this is resolved before the negotiation ever produces
an offer).

### The margin-floor check (the part this section exists to get right)

`merchant_agent._floor_price(policy, qty, granted_perk_cost=0)` — new
optional parameter, default 0 (byte-identical for every existing caller).
When given: `floor = max(existing_discount_derived_floor, policy["min_price"]
+ granted_perk_cost)`. The SAME `max()` calculation everything else already
flows through — not a second, parallel check.

Perks are checked for affordability ONLY at the moment of acceptance, never
during counter-price computation — using whatever price the negotiation
actually landed on. `merchant_agent._resolve_perks(offer, policy,
requested_perks)`: filters `requested_perks` down to `policy["eligible_perks"]`
(ineligible requests were already conclusively declined upstream), sums
the candidates' cost (`PERK_COST_FIELDS` maps `free_delivery` →
`shipping_cost`, `extended_warranty` → `warranty_cost`), and re-checks the
floor with that cost folded in. If the accepted price still clears it, all
candidates are granted; otherwise all are declined (all-or-nothing — a
buyer is eligible for at most one perk by construction, so there is no
meaningful "grant some, decline others" case). This keeps the margin check
contained to a single, reused function, and never touches the existing
counter-price logic at all.

Three acceptance pathways all resolve perks the same way:
1. Merchant directly accepts the buyer's offer (`check_guardrails()`'s own
   `_accept()` branch calls `_resolve_perks()` internally).
2. Buyer accepts the merchant's counter (`run_negotiation()`'s while loop
   calls `merchant_agent._resolve_perks()` directly, since `check_guardrails()`
   never re-runs against a price it didn't itself propose).
3. The `on_round_limit=accept_final` round-cap fallback (Section 4D
   follow-up) — same direct `_resolve_perks()` call against the merchant's
   last real counter-offer.

`granted_perks`/`declined_perks` (lists) are threaded through every
downstream state dict (`AGREEMENT_RECORDED`, `PENDING_APPROVAL`,
`ROLLBACK`, `APPROVAL_DECLINED`, `COMPLETED`) the same way `negotiation_id`/
`product_name`/`list_price`/`merchant_id` already are, and surfaced
structurally in `src/api.py`'s `/api/negotiate`/`/api/approve` responses —
never left for a consumer to regex out of rationale prose.

### AI mode (`MERCHANT_MODE=ai`)

`evaluate_ai()`/`check_guardrails()` both take `requested_perks` and behave
identically to rules mode: Layer 1 (`check_guardrails()`) makes the
definitive eligibility+affordability decision unconditionally, regardless
of merchant mode. The LLM's `MerchantDecision` schema was deliberately NOT
extended with a "grant this perk" field — Layer 2 only ever decides
accept/counter/reject on price, as before; whichever grant/decline
`check_guardrails()` already computed rides through unchanged whenever an
accept is validated. No new LLM-trust surface for a money-relevant
decision, matching "no LLM discretion" already established for eligibility.
In `MERCHANT_MODE=rules` specifically (no LLM to exercise "whether/when"
discretion at all — confirmed with the user): an eligible, affordable perk
is auto-granted by the same deterministic default.

### Live-verified

A buyer negotiated down to EXACTLY the combined min_price/discount-cap
floor (both deliberately tied at 800.0 in the test fixture), eligible for
`extended_warranty` (cost 40.0): the merchant accepted the price (800.0
clears the non-perk floor) but declined the perk, rationale citing the
margin floor explicitly — `granted_perks: []`, `declined_perks:
["extended_warranty"]`. Also live-verified end-to-end through the real
frontend (checkbox → `/api/negotiate` → real Razorpay payment): a granted
`extended_warranty` shows up both inline in the round transcript's accept
rationale and in the outcome card ("Granted: Extended warranty.").

### Frontend

Two checkboxes ("Request free delivery", "Request extended warranty"),
always both available regardless of buyer/persona — deliberately not
pre-filtered by eligibility, since an ineligible request being correctly
declined with a clear reason is a good, honest demo moment, not something
to hide. `requested_perks` threaded into `POST /api/negotiate`'s body;
`granted_perks`/`declined_perks` surfaced in the round transcript's accept
rationale and in the final outcome card.

---

## Assumptions

- Currency is INR: Razorpay is India-focused and `PROJECT_GUIDANCE.md` left
  currency an open question with only a numeric threshold (5000) stated.
- `qty_breaks` entries define the maximum `discount_pct` allowed at that
  quantity tier, overriding (not stacking with) the base
  `max_discount_pct`.
- Merchant counter-offer price is the most generous price permitted under
  policy at the buyer's requested quantity; quantity is unchanged in a
  counter.
- `offer.terms` is a free-form string reserved for future milestones.
- `provenance_sha` uses the literal placeholder `"UNVERIFIED"` for
  Milestone 1, matching the precedent `AGENTS.md` sets for its own
  provenance line.
- Milestone 1 negotiates a single product/SKU per `merchant_policy.json`.
- `decision_hash` = sha256 hex digest of UTF-8 canonical JSON
  (`sort_keys=True, separators=(',', ':')`) of `{offer, rationale,
  evidence_paths}`.
- The buyer-agent's scripted opening offer and split-the-difference
  concession strategy is a Milestone-1 test-harness detail, not part of
  the cross-milestone contract.

All confirmed by the user on 2026-08-29.

### Milestone 2 additions (confirmed by the user on 2026-08-29)

- `transaction_approval_threshold` lives in `merchant_policy.json`, not as
  a `TRANSACTION_APPROVAL_THRESHOLD` env var — supersedes the env-var line
  in `AGENTS.md` (updated to match).
- Payment outcome is simulated in-process (Section 3A); only order
  creation is a real Razorpay test-mode API call. This is a deliberate,
  documented demo boundary, not a placeholder to fix later in this
  milestone.
- Inventory hold/release are logged/audited events only — no real stock
  counter anywhere.
- Redaction scope: `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` and the raw
  Razorpay API response are never logged; only the curated `payment`
  field subset (Section 4A) appears in audit entries.
- `APPROVAL_DECLINED` (Section 3A) is an added terminal state for the
  human-declines-approval path, not explicitly named in the Milestone-2
  request but required to give that path a defined outcome — flagged here
  rather than silently invented.

### Milestone 3a additions

- **Superseded 2026-08-29**: the buyer LLM backend was rebuilt on Gemini,
  replacing the Claude-based build from this same milestone's first
  attempt (user's explicit direction — provider swap, not an addition).
  `anthropic` stays in `requirements.txt` at the user's request (in case
  the Claude-backed buyer is revisited later) but nothing in the codebase
  calls it anymore.
- Model: `gemini-3.5-flash-lite`, via `google-genai`'s
  `client.models.generate_content` with `response_schema=BuyerDecision`
  (`response.parsed` is used when non-`None`, else `response.text` is
  parsed manually) — the user's explicit choice from three grounded
  options (Google's own docs were inconsistent about which Flash-tier
  model is "current," so this was asked rather than guessed).
- Persona config (`budget`, `target_product`, `willingness_to_negotiate`)
  is passed as constructor args to `AIBuyerAgent`, mirroring the scripted
  `BuyerAgent`'s constructor-arg pattern — not a new top-level JSON file
  and not a `merchant_policy.json` field (it describes the buyer, not the
  merchant). `willingness_to_negotiate` is a free-text descriptor fed
  directly into the prompt (e.g. `"moderate -- open to a fair discount but
  not desperate"`), not a constrained enum.
- The LLM-call seam is dependency-injected (`llm_call` constructor param,
  same pattern as `payment_service`'s `client` param and the approval
  gate's `approval_confirm` param) so tests never make live network calls
  and never construct real `google.genai.errors` instances — an injected
  test double raises the module's own `TransientLLMError` directly to
  simulate a retryable Gemini failure.
- `BUYER_MODE` is read by the demo script (`negotiation_loop.py`'s
  `__main__`), defaulting to `scripted` — not by `run_negotiation()`
  itself, which stays buyer-implementation-agnostic and accepts whichever
  buyer object it's given.
- Retry policy (Section 2A) defaults to 3 retries, 1s base delay, 20s cap,
  exponential backoff — not specified by the user, chosen as a reasonable
  default for a free-tier-quota demo; `sleep_fn` is injectable so tests
  never actually sleep.
- A non-429 4xx Gemini error (bad request, auth, permission) is treated as
  a real bug and propagates uncaught rather than being retried or
  downgraded to `BUYER_UNAVAILABLE` — only 429/5xx/network failures are
  "transient" (see Section 2A's Error handling and retries).
- The one live-LLM integration test is gated by a custom `live_llm` pytest
  marker (registered in `tests/conftest.py`, unchanged from the Claude
  attempt), skipped by default and run with `pytest --run-live-llm`. Needs
  a real `GEMINI_API_KEY` in the environment to actually run.

### Milestone 3b additions (confirmed by the user on 2026-08-29)

- `inventory_floor` means minimum order quantity, not a stock/safety-
  reserve counter — the user's explicit choice, keeps Milestone 2's "no
  real inventory tracking" non-goal intact. Optional in the policy schema
  (default `1`) so it never breaks a Milestone 1/2/3a policy fixture that
  predates it.
- Guardrail clamps always target `check_guardrails`'s own qty-tier-aware
  floor, never a flat `min_price` — the user's explicit choice, avoids
  under-clamping (handing away more discount than the tier structure
  intends) at higher qty tiers.
- `TransientLLMError`/`LLMUnavailableError`/`call_llm_with_retry` were
  extracted from `ai_buyer_agent.py` into a shared `src/agents/llm_utils.py`
  so the merchant's Gemini calls reuse the exact same retry mechanics as
  the buyer's, per "same fallback pattern as buyer-agent." Not requested
  verbatim, but the natural reading of that instruction; `ai_buyer_agent.py`
  re-raises the shared `LLMUnavailableError` as `BuyerUnavailableError` so
  every existing import/test keeps working unchanged.
- Unlike the buyer (`BuyerUnavailableError` -> negotiation-ending terminal
  state), a merchant Gemini outage is a **per-round** fallback to
  `check_guardrails`'s own verdict — the user's deliverable explicitly
  distinguished this ("fall back... for that round rather than blocking or
  crashing"), so it's spec, not an assumption.
- `concession_reasoning` is discarded (never logged) whenever a clamp
  occurs, even though the deliverable only explicitly named the raw
  clamped *value* as forbidden from the audit log. Free text could echo
  that value in prose, which code can't reliably strip -- so the stricter
  reading was chosen to keep "architecturally impossible... even if the
  LLM's output tries to" true in practice, not just against the specific
  mocked test case. Flagged here since it goes beyond the literal ask.
- A Layer-2 "accept" on an offer that fails `check_guardrails` is treated
  as a guardrail violation requiring an override (Section 2B's table),
  even though the deliverable's own guardrail-test example was specifically
  about a violating `counter_offer` price — accepting a sub-floor offer is
  the same class of violation and the architecture would be incomplete
  without covering it too.
- Reusing `check_guardrails`'s own logic/thresholds for the merchant's
  `_build_merchant_prompt` (rather than re-deriving policy math in the
  prompt-construction code) was not specified but is a straightforward
  DRY choice, not treated as a judgment call worth flagging further.

### Milestone 3c additions (confirmed by the user on 2026-08-31)

- Categories: 8, general-retail (Electronics, Apparel & Fashion, Home &
  Kitchen, Sporting Goods & Outdoors, Books & Media, Beauty & Personal
  Care, Toys & Games, Office & Stationery) — the user's "broader general
  retail" choice over a tech-only catalog.
- LTV tiers: the finer-grained 4-tier table (Section 2C) — the user's
  choice over their own 3-tier example.
- Hard discount ceiling: 30% — the user's choice, over 25%/35%.
- Order distribution: skewed ("whale" buyers get ~8x order weight) — the
  user's choice, so all 4 LTV tiers are actually exercised in the demo
  rather than everyone landing in the same tier (verified: the seed-42
  data's top buyer's LTV is 341,244 vs. a median around 8,000).
- The LTV bonus stacks additively onto **whichever** discount is in play
  for a given qty (the base `max_discount_pct` OR a matched `qty_breaks`
  tier), each independently capped at the ceiling — not just onto the
  base field. The deliverable's literal wording ("raises max_discount_pct
  for that specific negotiation") could be read narrower (base field
  only), but a loyal high-LTV buyer getting no bonus at all when ordering
  in bulk (i.e. exactly when a qty_breaks tier applies) would undermine
  the feature's own point. Flagged here as a judgment call, not asked
  about since it wasn't in the user's explicit list of things to confirm.
- `min_price = cost * 1.15` (15% minimum acceptable margin) in the
  generator — a concrete number for "derived from cost + a minimum
  acceptable margin" that the deliverable left unspecified. Reversible by
  regenerating with a different constant; not asked about since it's
  generator-internal and doesn't affect the negotiation contract.
- `max_negotiation_rounds`/`transaction_approval_threshold` aren't part
  of the `catalog.json` schema (deliverable 1 doesn't list them as
  per-product fields) — the `PRODUCT_ID` path defaults them to `5`/
  `20000`, matching `merchant_policy.json`'s existing demo values, rather
  than adding new env vars for them (out of scope of what was asked).
- A buyer's concrete `persona.budget`/`max_acceptable_price` is the
  midpoint of their `budget_range` — a deterministic single value was
  needed since `AIBuyerAgent`/`BuyerAgent` both expect one number, not a
  range; midpoint was the least-arbitrary reduction. `BUYER_BUDGET`
  overrides it when set, same as every other buyer-construction path.
- `run_full_transaction()`'s existing payment-failure `ROLLBACK` return
  also gained a `"reason": "payment_failure"` key, for symmetry with the
  new `"insufficient_inventory"` reason — purely additive, confirmed no
  existing test asserts an exhaustive key set on the outcome dict.
- The inventory check also emits a `human_notification` entry (mirroring
  the existing payment-failure path's business logic: alert a human on
  any rollback) — not explicitly requested, a reasonable extension of the
  established pattern rather than a new invented behavior.
- `scripts/generate_synthetic_data.py`'s order timestamps are drawn from
  a **fixed** reference window (2025-08-31 to 2026-08-31), not
  `datetime.now()` — required for "same seed -> same data" to actually
  hold regardless of when the script is run, not just within one sitting.

### Milestone 3c follow-up (payment-failure demo output; confirmed by the user on 2026-08-31)

- The `offer_expiration_seconds` field the user referenced doesn't exist
  in `merchant_policy.json` — corrected before proceeding rather than
  building against a field that isn't there; see Section 3C.
- Post-failure hold reuses the offer's own `expiration` (the user's
  explicit choice over a distinct, longer timer), so `retry_payment()`
  needed no new policy field and no new timer concept at all.
- Console block text is ASCII-only, not the em-dash used elsewhere in
  this spec's prose — a live demo run on this Windows environment showed
  `—` rendering as mojibake in the actual console without a UTF-8
  codepage set; caught and fixed during verification, not assumed safe.
- `retry_payment()` does not re-check inventory sufficiency (Section 3B)
  before re-attempting — not explicitly requested, and the retry window
  is short (bounded by the original offer's ~5-minute expiration), so
  treated as acceptable simplicity rather than scope worth expanding
  without being asked.
- `_attempt_payment()`'s rollback branch logs `buyer_notification` right
  after `inventory_release` and before `human_notification` — ordering
  wasn't specified; chosen so the buyer-facing entry appears before the
  operator-facing one, matching "record the buyer-agent's side too, not
  just the merchant/operator side" being the newer, less-covered half of
  the existing behavior.
- Added `FORCE_PAYMENT_FAILURE` (`__main__` only, values `1`/`true`/`yes`)
  since there was previously no way to trigger this scenario from the CLI
  at all, only from test code — a gap noticed while verifying the
  console output actually renders during a live demo run, not part of
  the original ask but necessary to fulfill it.

### Milestone 3c: margin floor + inventory liquidation (confirmed by the user on 2026-09-01)

- Cost margin multiplier: `1.02` (2% minimum margin) — the user's own
  example, confirmed as-is over a less razor-thin `1.05`.
- Liquidation threshold: `100` days — the user's own example, confirmed
  as-is.
- Aged outliers in the generated catalog: `8` (one per category,
  `days_in_inventory` 120-400), the other 72 products at 1-90 days — the
  user's confirmed count over a sparser 4-outlier option.
- `LIQUIDATION_RAMP_DAYS = 100` (full relaxation to the cost floor by
  `threshold + ramp` = 200 days) was **not** asked about — the user's
  explicit list was margin percentage, threshold, and outlier count only.
  Chosen as the simplest explainable shape (a linear ramp exactly as long
  as the threshold itself) rather than expanding the question set further.
- `product_to_policy()` applies liquidation relaxation, then clamps with
  the cost floor as a second, defensive step — redundant given the ramp
  formula's own internal `max(floor, relaxed)`, but keeps "the cost floor
  always wins" true from reading `product_to_policy()` alone, without
  needing to trust the ramp math elsewhere is bug-free. Not asked about;
  a straightforward defense-in-depth choice matching this project's
  existing "validate in code, don't just trust the formula" discipline
  (Section 2B's guardrail re-validation is the same instinct).
- Two existing tests broke from this change and were fixed, not the
  spec/behavior: `test_ltv_is_computable_and_produces_a_meaningfully_
  skewed_distribution`'s `10x` threshold (relaxed to `5x` — adding a new
  RNG draw per product for `days_in_inventory` shifted the seeded random
  sequence for every *later* draw too, including buyer/order generation,
  changing the realized skew ratio from a prior run's ~9.5x to something
  that happened to sit just under the old, arbitrarily-strict threshold);
  and `test_personalization.py`'s `_demo_product()` helper (needed a
  `cost` field, now required by `product_to_policy()`). Neither reflects
  a behavior change the user needs to know about beyond this note.

### Milestone 3c: category-specific liquidation thresholds (confirmed by the user on 2026-09-01)

- The 8-category threshold table (180 through 380) was proposed and
  confirmed in full before any code changed, per the user's explicit
  process ask — see the table in Section 2E.
- Two corrections made before/while implementing, not silently absorbed:
  - The user's deliverable said "the margin floor (cost + 15%) still
    overrides liquidation" — but `cost * 1.15` is `product_to_policy()`'s
    raw, *unrelaxed* `min_price` baseline in the generator, a different
    number from the actual hard floor, `cost_floor_price()` = `cost *
    1.02` (Section 2E). The tests for this deliverable use the real
    invariant (`cost_floor_price`), not the mixed-up figure.
  - The user asked to update "the runtime liquidation/floor-relaxation
    check in `merchant_agent.py`" — but that logic has always lived in
    `src/personalization.py`, never `merchant_agent.py` (deliberately,
    across both this milestone and 3b, to keep `check_guardrails()`
    untouched). Updated in its actual location; `merchant_agent.py`
    remains unchanged.
- `CATEGORY_LIQUIDATION_THRESHOLDS` was placed in `src/personalization.py`
  as the single source of truth, with `scripts/generate_synthetic_data.py`
  importing it (new cross-package import, `sys.path` adjusted so the
  script runs standalone via `python scripts/generate_synthetic_data.py`)
  rather than duplicating the eight numbers — not asked about specifically,
  chosen to make drift between generated data and the runtime check
  structurally impossible rather than just documented.
- `DEFAULT_LIQUIDATION_THRESHOLD = 250` (fallback for an unlisted
  category) was not asked about — chosen as roughly the table's midpoint,
  a reasonable default that should never actually fire against real
  generated data (all 8 real categories are in the table).
- `liquidation_applied` is logged as its own standalone audit entry
  (`offer: null`, mirroring `buyer_strategy`), not woven into
  `check_guardrails()`'s own rationale text for whichever accept/counter/
  reject entry the relaxed `min_price` happens to drive — the cleanest
  way to satisfy "the audit log rationale text ... names the threshold"
  without touching `merchant_agent.py` at all.
- Aged-outlier ranges in the generator changed from a flat `120-400` to
  `category_threshold + 20..220` (per category) — not given exact numbers
  by the user, chosen so every outlier lands comfortably past its own
  threshold (never barely past it) while still varying by category scale.
- Section 2F bug fix: investigated first, confirmed it was not a defect
  in `evaluate_ai()`/round-handling before proposing any change — the
  instant reject was `check_guardrails()`'s own Milestone-1 rule,
  correctly firing on an interaction between two individually-correct
  features (absolute `min_price` floor, liquidation relaxation) that had
  never been reconciled. Two design decisions confirmed with the user
  before implementing: (1) only a liquidation-relaxed `min_price` becomes
  counter-able, a merchant-set one stays absolute everywhere else; (2)
  the counter price is unchanged from the existing floor formula
  (`max(min_price, discount-computed)`), so it can land above the
  relaxed `min_price` itself when the discount cap is the binding
  constraint — surfaced explicitly since it meant the fixed behavior
  would not match the bug report's originally assumed counter price.
