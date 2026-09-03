# Parley

An AI merchant-negotiator built for the Razorpay AI Buildathon (AI Growth &
Agentic Commerce track). An AI buyer-agent and an AI merchant-agent
negotiate a deal under merchant-defined constraints, then settle it via
Razorpay test-mode payment APIs -- with every money-related decision
recorded in an explainable, line-delimited audit trail.

The merchant side is deliberately two-layered: a deterministic,
rule-based guardrail layer that is the sole final word on what price is
actually allowed, and an LLM strategy layer that proposes *how* to
negotiate within whatever room the guardrails leave. The guardrail layer
never trusts the LLM's output -- every proposal is re-validated in code
before it can reach the buyer or the audit log.

## Architecture

```
                    ┌───────────────────────────────────────────┐
                    │            negotiation_loop.py             │
                    │   (orchestrator: run_negotiation(),         │
                    │    run_full_transaction(), retry_payment()) │
                    └───────────────┬─────────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        │                            │                            │
        ▼                            ▼                            ▼
┌───────────────┐          ┌──────────────────────┐      ┌───────────────────┐
│  buyer-agent    │          │   merchant-agent       │      │  payment_service    │
│                 │          │                        │      │                    │
│ BuyerAgent      │◄────────►│ Layer 1: check_        │      │ create_order()      │
│ (scripted) or   │  offers/ │ guardrails() -- pure,  │      │ (real Razorpay      │
│ AIBuyerAgent    │  counters│ deterministic, no LLM. │      │ test-mode API call) │
│ (Gemini-driven) │          │ THE authority on what  │      │                    │
│                 │          │ price is ever allowed. │      │ simulate_payment()  │
└───────────────┘          │                        │      │ (in-process outcome │
                              │ Layer 2: decide_       │      │ simulation -- see   │
                              │ strategy() -- Gemini    │      │ "Payment simulation │
                              │ proposes HOW to         │      │ boundary" below)    │
                              │ negotiate; ALWAYS       │      └───────────────────┘
                              │ re-validated against    │
                              │ Layer 1 before use.      │
                              └──────────────────────┘
                                    │
                                    ▼
                          ┌──────────────────────┐
                          │    audit_logger.py     │
                          │  one JSON object per    │
                          │  line -> audits/         │
                          │  negotiation.log         │
                          │  (decision_hash,          │
                          │  provenance_sha, ...)     │
                          └──────────────────────┘
```

**`personalization.py`** sits alongside this as a pure, deterministic data
layer: it turns a `data/catalog.json` product entry into the policy dict
the guardrails read (min_price, max_discount_pct, qty_breaks, ...),
applying an LTV-based discount bonus and an inventory-liquidation floor
relaxation for aged stock -- both computed in code, never left to the LLM.

**Multi-merchant support (`data/merchants.json`):** two merchant
profiles, each tagged onto a whole slice of the catalog by category
(`Voltstream Electronics`, tech/gear-adjacent categories; `Hearth & Home
Living`, home/lifestyle categories). What actually varies between them
today is one behavioral field, `risk_approval_tier`: `"standard"` (only
HIGH risk forces human approval, the Section 2O default) or `"strict"`
(MODERATE risk forces it too -- the exact same approval-gate mechanism
in `run_full_transaction()`, just triggered at a lower risk tier for
that merchant). The other profile fields (payment methods, shipping
rules, return policy) are descriptive data only -- they're displayed and
carried through the profile, not yet wired into any guardrail or pricing
decision. Negotiation/guardrail/liquidation/LTV logic itself is
completely unaware of which merchant is involved; `risk_assessment()`'s
none/moderate/high computation stays purely about the buyer and
quantity, same as before this feature existed.

**Payment simulation boundary:** Razorpay's real flow needs a browser
Checkout step between order creation and capture, which a headless CLI
loop doesn't have. So `payment_service.create_order()` makes a real
Razorpay test-mode API call and gets back a real `order_id`; the
success/failure *outcome* is then decided by an in-process
`simulate_payment()`, not a real capture call. See
[`NEGOTIATION_SPEC.md`](NEGOTIATION_SPEC.md) Section 3A for the full
rationale.

## Setup

```bash
pip install -r requirements.txt
```

### Environment variables

| Variable | Required for | Notes |
|---|---|---|
| `GEMINI_API_KEY` | `BUYER_MODE=ai` / `MERCHANT_MODE=ai` | Google Gemini API key -- powers the AI buyer/merchant strategy layers. |
| `RAZORPAY_KEY_ID` | Payment phase | Razorpay **test-mode** key id. Without both Razorpay vars, the pipeline stops after negotiation (no payment phase). |
| `RAZORPAY_KEY_SECRET` | Payment phase | Razorpay test-mode key secret. Never call live payment endpoints with these. |

On Windows, set them for the current shell before running:

```powershell
$env:GEMINI_API_KEY = "..."
$env:RAZORPAY_KEY_ID = "rzp_test_..."
$env:RAZORPAY_KEY_SECRET = "..."
```

### Generating synthetic data

Milestone 3c's personalization features (LTV discount bonus, inventory
liquidation) need a synthetic catalog/buyer/order dataset:

```bash
python scripts/generate_synthetic_data.py
```

Deterministic (`--seed 42` by default) -- writes `data/catalog.json`,
`data/buyers.json`, `data/orders.json`. Without this, the negotiation
loop still runs against the single hardcoded demo product in
`merchant_policy.json` (no `PRODUCT_ID`/`BUYER_ID`, no personalization).

### Resetting depleted demo stock

Every `COMPLETED` payment permanently decrements the negotiated product's
`current_inventory`. Before a demo recording, restore the catalog to its
originally generated stock levels without touching `buyers.json`/
`orders.json`:

```bash
python scripts/reset_data.py
```

## Running a negotiation

> **Note:** running with no `PRODUCT_ID` uses a minimal fixture product
> for a fast smoke test and does **NOT** exercise the catalog, LTV, or
> liquidation systems. For the full system, always pass `PRODUCT_ID` and
> `BUYER_ID` -- see the example below.

```bash
PRODUCT_ID=SKU-ELEC-001 BUYER_ID=BUYER-001 BUYER_MODE=ai MERCHANT_MODE=ai python -m src.negotiation_loop
```

This negotiates a real synthetic-catalog product (`SKU-ELEC-001`) against
a real buyer persona with its LTV-based discount bonus (`BUYER-001`),
both AI-driven -- exercising the full personalization stack (catalog,
LTV, liquidation). The full pipeline -- negotiation through to a real
Razorpay test-mode payment -- runs automatically in this same command
whenever both `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` are also set;
no separate manual step is needed to trigger payment after agreement.

Configure via environment variables, all optional:

| Variable | Values | Effect |
|---|---|---|
| `PRODUCT_ID` | e.g. `SKU-ELEC-001` | Negotiate a real synthetic-catalog product. Requires `data/catalog.json` (see above). Also enables the inventory fulfillment check and post-sale decrement. Omit to fall back to the minimal `merchant_policy.json` fixture (smoke-test only -- see note above). |
| `BUYER_ID` | e.g. `BUYER-001` | Uses that buyer's real persona/budget and LTV-based discount bonus from `data/buyers.json`/`orders.json`. |
| `BUYER_BUDGET` | a number | Overrides the buyer's max acceptable price / persona budget directly. |
| `BUYER_MODE` | `scripted` (default) or `ai` | `ai` uses `AIBuyerAgent` (Gemini-driven); needs `GEMINI_API_KEY`. |
| `MERCHANT_MODE` | `rules` (default) or `ai` | `ai` adds the Layer 2 Gemini strategy layer on top of the same Layer 1 guardrails; needs `GEMINI_API_KEY`. |
| `DEMO_QTY` | an integer (default `3`) | Negotiated quantity. Set to `10`+ alongside a new (never-ordered) `BUYER_ID` to trigger the Risk Agent's HIGH level end to end -- see below. |

Console output streams each round live (`[Round N] agent: decision`),
followed by a plain-language summary block for the final outcome
(agreed price/qty, then `PAYMENT COMPLETED` with the Razorpay order id,
or the specific `ROLLBACK` reason) -- readable without parsing the raw
JSON dump that follows it.

### Minimal smoke test (no data generation needed)

```bash
python -m src.negotiation_loop
```

Runs the fixture product from `merchant_policy.json` with a scripted
(non-AI) buyer and rules-only merchant, negotiation phase only (no
payment). Fastest way to sanity-check the negotiation loop itself with
zero setup -- not a substitute for the catalog-driven run above.

### Triggering each failure mode on demand

For a live demo, without needing to hand-craft scenarios each time:

```bash
# Payment failure -- Razorpay test-mode call is forced to fail, rolls back.
FORCE_PAYMENT_FAILURE=1 PRODUCT_ID=SKU-ELEC-001 python -m src.negotiation_loop

# Insufficient inventory -- forces the fulfillment check to fail without
# touching real catalog data. Requires PRODUCT_ID (the check only runs on
# the PRODUCT_ID path); a warning prints if PRODUCT_ID is missing.
FORCE_INSUFFICIENT_INVENTORY=1 PRODUCT_ID=SKU-ELEC-001 python -m src.negotiation_loop
```

Both flags default to off and only change behavior when explicitly set.

### Triggering each Risk Agent level on demand

Requires `BUYER_ID` (the check only runs when a buyer is identifiable).
`BUYER-001` is the seed=42 dataset's one buyer with zero prior orders:

```bash
# MODERATE -- one factor (new buyer). No forced approval (by default --
# see the strict-merchant example above), but the discount ceiling IS
# reduced to 25% of normal (not zeroed) -- still logs a risk_review audit
# entry naming the factor and the reduced ceiling.
PRODUCT_ID=SKU-ELEC-007 BUYER_ID=BUYER-001 python -m src.negotiation_loop

# HIGH -- both factors (new buyer + large request, via DEMO_QTY >= 10).
# Negotiation still proceeds -- max_discount_pct is forced to 0 for it
# (full list price only), and human approval IS required (needs both
# Razorpay env vars set to reach that gate).
PRODUCT_ID=SKU-ELEC-007 BUYER_ID=BUYER-001 DEMO_QTY=10 python -m src.negotiation_loop
```

Watch for `[Round 0] risk-agent: risk_review` near the top of the
output either way. Only the HIGH run reaches an `approval_requested`
line -- compare its rationale, which names "risk", not
`policy.transaction_approval_threshold`, when risk is what
triggered the gate.

## Running the tests

```bash
pytest tests/
```

Everything runs mocked/offline by default (no live LLM or Razorpay
calls, no network access needed). A handful of tests are marked
`live_llm` and skipped unless you opt in with a real `GEMINI_API_KEY`:

```bash
pytest tests/ --run-live-llm
```

## Known scope decisions

Deliberately deferred for this competition build -- not gaps, but scoped
choices given the time available:

- **Single-process, not networked agents.** The buyer-agent and
  merchant-agent are Python objects called in-process by one
  orchestrator, not separate services exchanging messages over a network.
  The negotiation protocol (structured offers, rationale, evidence paths)
  is already shaped so it *could* cross a wire later without changing the
  decision logic itself.
- **JSON files, not a database.** `data/catalog.json`,
  `data/buyers.json`, `data/orders.json`, and the `audits/` log are flat
  files. This keeps the demo self-contained and the audit trail
  trivially inspectable (`cat`/`grep`/a JSONL viewer), at the cost of no
  concurrent-write safety or query layer -- fine for a single-operator
  demo, not for production multi-tenant use.
- **Risk Agent: implemented (Milestone 5).** Deterministic and
  code-only -- no LLM call, same "bounded input feeding into existing
  guardrails" pattern already used for LTV and liquidation. Runs first,
  before any offer is generated, via `personalization.risk_assessment()`:
  a buyer with fewer than 2 prior orders is flagged "new"; a request at
  or above the product's own bulk-tier quantity (`qty_breaks`, 10 units
  in the generated catalog) is flagged "large". Reframed as a
  PRICING-ABUSE signal, not a trust/fraud one -- both non-`none` levels
  tighten the discount ceiling via `apply_risk_discount_cap()` (feeding
  straight into the same `check_guardrails()`/`_floor_price()` mechanism
  every other policy field already flows through), as a genuine
  three-level gradient: **HIGH** (both factors) scales `max_discount_pct`
  and every `qty_breaks` tier to 0% (full list price, no room to
  haggle), plus forces the human-approval gate. **MODERATE** (exactly
  one factor) scales them to 25% of normal -- a real, partial reduction,
  not zero -- with no forced approval by default (a "strict" merchant
  changes that, see above; pricing is unaffected by the merchant tier
  either way). Neither -> no effect. Every flagged assessment (MODERATE
  and HIGH) is logged as a `risk_review` audit entry naming the specific
  factors, the reduction applied, and the resulting effective floor.
  `audit_logger.log_entry`'s shared call, used directly by every agent
  including this one, still
  plays the role `AGENTS.md`'s `auditor-agent` name describes -- there's
  no separate networked agent
  process, consistent with the single-process scoping above.
- **Multi-merchant: implemented for one behavioral field (Milestone 6).**
  `data/merchants.json` holds two full profiles (name, description,
  payment methods, shipping rules, return policy), and every catalog
  product is tagged with a `merchant_id` by category. Only
  `risk_approval_tier` is actually wired into behavior today -- it
  varies whether the Risk Agent's approval gate fires on MODERATE risk,
  per merchant (see the Architecture section above). The other profile
  fields are real data, displayed at negotiation start and available to
  read, but not yet enforced anywhere (e.g. `supported_payment_methods`
  isn't checked against how a payment is made; `shipping_rules`/
  `return_policy` aren't referenced by any guardrail).
  `transaction_approval_threshold` and `max_negotiation_rounds` also stay
  global constants, not per-merchant fields, in this build.
- **Payment outcome is simulated, not a real Checkout capture.** See
  "Payment simulation boundary" above -- a deliberate consequence of this
  being a headless CLI flow with no browser step, not a shortcut around
  Razorpay's real API (order creation is real).
