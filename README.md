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

## Getting Started (Windows, PowerShell, from a fresh clone)

A complete, literal walkthrough — assumes you know Python but nothing
else about this project. Every command below is PowerShell syntax.

### 1. Prerequisites

- **Python 3.10 or later.** Check with:
  ```powershell
  python --version
  ```
- **git.** Check with:
  ```powershell
  git --version
  ```

### 2. Clone and install

```powershell
git clone https://github.com/RheaDeepak/Parley---AI-Negotiator.git
cd "Parley---AI-Negotiator"
pip install -r requirements.txt
```

(`cd` into whatever folder name git actually created — pass a name as a
third argument to `git clone` if you want to control it, e.g.
`git clone <url> Parley`.)

### 3. API keys

Two independent integrations, both optional — read the note under each
before deciding whether you need it.

**`GEMINI_API_KEY`** — needed only for `BUYER_MODE=ai` / `MERCHANT_MODE=ai`
(the LLM-driven buyer/merchant). Without it, everything still works with
the scripted buyer and rules-only merchant (the defaults for the CLI's
minimal smoke test).

1. Go to [Google AI Studio](https://aistudio.google.com/apikey) and sign
   in with a Google account.
2. Click **Create API key**. This is free-tier, no card required.
3. Set it:
   ```powershell
   setx GEMINI_API_KEY "your-key-here"
   ```
   **`setx` writes to the Windows registry for future sessions — it does
   NOT update your current terminal.** Close this terminal and open a
   new one before `$env:GEMINI_API_KEY` will show the value. If you'd
   rather not restart your terminal, set it for just this session
   instead (no restart needed, but you'll have to repeat it every time
   you open a new terminal):
   ```powershell
   $env:GEMINI_API_KEY = "your-key-here"
   ```

**`RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET`** — needed only for the
payment phase (after a negotiated agreement, before a real Razorpay
test-mode order is created). **Optional** — without both set, the CLI
prints `RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET not set -- running
negotiation only, no payment phase.` and stops cleanly right after an
agreement; no payment call is ever attempted, nothing breaks.

1. Go to the [Razorpay Dashboard](https://dashboard.razorpay.com/) and
   sign in (or sign up — no business verification needed to get test
   keys).
2. Toggle **Test Mode** (top of the dashboard) — confirm it's on before
   generating keys. **Never use anything but Test Mode keys with this
   project.**
3. Go to **Settings → API Keys → Generate Test Key**.
4. Set both, same pattern as above:
   ```powershell
   setx RAZORPAY_KEY_ID "rzp_test_..."
   setx RAZORPAY_KEY_SECRET "..."
   ```
   Same restart-required caveat — new terminal needed before these take
   effect.

### 4. Generate the synthetic data

```powershell
python scripts/generate_synthetic_data.py
python scripts/generate_negotiation_history.py
```

Run in that order. The first writes `data/catalog.json` (80 products),
`data/buyers.json` (30 buyer personas), and `data/orders.json` (~200
historical orders) — the dataset every catalog-driven negotiation, the
LTV bonus, liquidation, and the Risk Agent all read from. The second
writes `audits/dashboard_seed.log` — ~130 pre-run negotiations (no real
API calls, no cost) so `dashboard.html` has something to show
immediately instead of an empty table.

Both are deterministic (`--seed 42` by default — same seed always
produces byte-identical output), so re-running them is always safe.

> **Note, verified while writing this guide:** this repo actually ships
> with `data/*.json` and both `audits/*.log` files already committed, so
> technically the app runs without this step. Run it anyway — it's
> instant, free, and guarantees you're starting from the same
> undepleted, reproducible dataset every example in this README assumes
> (repeated demo runs permanently decrement `current_inventory` on a
> `COMPLETED` sale — see step 7).

### 5. Running a negotiation via CLI

```powershell
$env:PRODUCT_ID = "SKU-ELEC-001"
$env:BUYER_ID = "BUYER-001"
$env:BUYER_MODE = "ai"
$env:MERCHANT_MODE = "ai"
python -m src.negotiation_loop
```

This one actually works end-to-end (verified) — negotiates a real
catalog product against a real buyer persona, both sides Gemini-driven
(needs `GEMINI_API_KEY`), and — if both Razorpay vars are also set —
automatically proceeds through a real test-mode payment. No separate
step is needed to trigger payment after agreement.

Every relevant environment variable, all optional:

| Variable | Values | What it does |
|---|---|---|
| `PRODUCT_ID` | e.g. `SKU-ELEC-001` | Negotiates a real synthetic-catalog product instead of the minimal fixture in `merchant_policy.json`. Required to exercise LTV, liquidation, risk, and the inventory check at all. |
| `BUYER_ID` | e.g. `BUYER-001` | Uses that buyer's real persona, order history, and LTV-based discount bonus instead of a generic hardcoded buyer. |
| `BUYER_BUDGET` | a number | Overrides the buyer's max acceptable price directly, instead of deriving it from the persona. |
| `BUYER_MODE` | `scripted` (default) or `ai` | `ai` uses the Gemini-driven buyer; needs `GEMINI_API_KEY`. `scripted` is deterministic and instant. |
| `MERCHANT_MODE` | `rules` (default) or `ai` | `ai` adds the Gemini strategy layer on top of the same hard guardrails; needs `GEMINI_API_KEY`. `rules` is deterministic and instant. |
| `FORCE_PAYMENT_FAILURE` | `1`/`true`/`yes` | Forces the (simulated) payment outcome to fail, to demo the rollback path. |
| `FORCE_INSUFFICIENT_INVENTORY` | `1`/`true`/`yes` | Forces the inventory-shortfall rollback without touching real catalog stock. Requires `PRODUCT_ID` — a warning prints if it's missing. |

(`$env:VAR = "value"` sets a variable for the current PowerShell session
only — no restart needed, unlike `setx` above.)

### 6. Running the interactive frontend

This needs **two terminals running at the same time**, plus a browser.

**Terminal 1 — the API backend:**
```powershell
python -m uvicorn src.api:app --reload --port 8000
```

**Terminal 2 — the static frontend server (run from the repo root):**
```powershell
python -m http.server 8080
```

**Then open in a browser:**
```
http://localhost:8080/index.html
```

> The negotiation form links to two other pages via the nav bar once
> you're inside them: **View Dashboard** (`dashboard.html` — live
> negotiation history and stats, auto-refreshing) and **View Inventory**
> (`frontend/inventory.html` — live stock levels). Both are separate
> pages, not tabs on the same page.

Leave both terminals running the whole time you're using the frontend —
closing either one breaks it (Terminal 1 serves every `/api/...` call
the page makes; Terminal 2 serves the HTML/CSS/JS itself).

### 7. Resetting data

Every `COMPLETED` payment permanently decrements the sold product's
`current_inventory` in `data/catalog.json`. After testing has depleted
some stock, restore it to the original seeded values (without touching
`buyers.json`/`orders.json`, so LTV/persona data stays put):

```powershell
python scripts/reset_data.py
```

### 8. Running the tests

```powershell
python -m pytest -v
```

Fully mocked/offline by default — no live LLM or Razorpay calls, no
`GEMINI_API_KEY` needed, no network access required. To also run the
handful of tests that call the real Gemini API:

```powershell
python -m pytest --run-live-llm -v
```

Needs a real `GEMINI_API_KEY` in the environment and consumes real
(free-tier) API quota — skipped by default for exactly that reason.

### 9. Troubleshooting

**`uvicorn` / `pytest` : The term '...' is not recognized...`**
Windows can't find the console-script shim on your `PATH`, even though
the package installed fine. Run it as a module through Python instead —
this always works regardless of `PATH`:
```powershell
python -m uvicorn src.api:app --reload --port 8000
python -m pytest -v
```

**`error while attempting to bind on address ('127.0.0.1', 8000):
only one usage of each socket address is normally permitted`**
Something is already listening on port 8000 — usually a `uvicorn` from
an earlier terminal you forgot to close. Find and stop it:
```powershell
netstat -ano | findstr :8000
```
The last column of the matching line(s) is the PID. Then:
```powershell
taskkill /PID <pid> /F
```
and re-run `uvicorn`. (If you genuinely can't find/kill it, the simplest
fix is just closing that terminal window, or restarting your machine —
the process will not survive either.)

## Architecture

> **For the full architecture writeup** — the floor-price computation,
> the Risk Agent, perks, failure/rollback modes, the audit trail,
> multi-merchant support, and the frontend/API layer, all explained with
> real worked examples and bug histories — see
> [`ARCHITECTURE.md`](ARCHITECTURE.md). The summary below is a quick
> reference.

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
(`Voltstream Marketplace`, tech/gear-adjacent categories; `Hearth & Home
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
