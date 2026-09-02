# Negotiation State Machine

Per [SPEC.md](SPEC.md) CAP-2. Governs the negotiation loop that ties buyer-agent and merchant-agent together.

## States

| State | Meaning |
|---|---|
| `OPEN` | Negotiation started, no offer evaluated yet. |
| `BUYER_TURN` | Buyer-agent must produce an offer or counter. |
| `MERCHANT_TURN` | Merchant-agent must evaluate the current offer via `evaluate(offer, policy, round)`. |
| `AGREEMENT_RECORDED` | **Terminal.** Merchant accepted an offer. |
| `REJECTED` | **Terminal.** Either an explicit policy violation with no viable counter, or the round cap was reached with no agreement. |

## Offer shape

Every offer/counter is a structured object, per `AGENTS.md`:

```
{offer_id, price, qty, terms, expiration, timestamp}
```

- `offer_id`: string, unique per offer (e.g. `uuid4` hex).
- `price`: number, proposed unit price.
- `qty`: integer, requested quantity.
- `terms`: string, free-form, unexamined by Milestone-1 logic (see SPEC.md Assumptions).
- `expiration`: ISO 8601 UTC timestamp string.
- `timestamp`: ISO 8601 UTC timestamp string, when the offer was created.

## Transition table

| From state | Trigger | Merchant decision | To state | Notes |
|---|---|---|---|---|
| `OPEN` | Buyer generates initial offer | — | `MERCHANT_TURN` | Round counter starts at 1. |
| `MERCHANT_TURN` | `evaluate()` called | `accept` | `AGREEMENT_RECORDED` | Terminal. Agreement recorded with the accepted offer. |
| `MERCHANT_TURN` | `evaluate()` called | `reject` | `REJECTED` | Terminal. Explicit-violation variant — `evaluate()` found no viable counter (e.g. buyer's qty makes any legal price above what buyer will pay, or `round > max_negotiation_rounds` was already true going in). |
| `MERCHANT_TURN` | `evaluate()` called | `counter` AND `round >= max_negotiation_rounds` | `REJECTED` | Terminal. Round-limit variant — a counter would be produced but the round cap is already exhausted, so negotiation ends without agreement instead. |
| `MERCHANT_TURN` | `evaluate()` called | `counter` AND `round < max_negotiation_rounds` | `BUYER_TURN` | Merchant's counter-offer becomes the current offer; round increments. |
| `BUYER_TURN` | Buyer-agent reacts to merchant's counter | — | `MERCHANT_TURN` | Buyer either accepts the counter as-is (treated as agreement — merchant already committed to that price) or proposes a new counter-offer using its scripted strategy. |

## Terminal states (exactly one per run, per SPEC.md CAP-2)

1. **`AGREEMENT_RECORDED`** — merchant accepted an offer within the round cap.
2. **`REJECTED` (explicit)** — merchant rejected on policy grounds with no viable counter to offer.
3. **`REJECTED` (round-limit)** — `max_negotiation_rounds` reached without agreement; negotiation forcibly ends.

Both `REJECTED` variants write the same terminal `action: "reject"` audit entry; the distinguishing detail lives in the entry's `rationale` and `evidence_paths` (round-limit variant's evidence path is `policy.max_negotiation_rounds`).
