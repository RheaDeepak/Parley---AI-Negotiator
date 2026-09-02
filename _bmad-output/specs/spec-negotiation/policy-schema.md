# Merchant Policy Schema

`merchant_policy.json` at the repo root. One file, one product/SKU, per [SPEC.md](SPEC.md) non-goal (no multi-SKU in Milestone 1).

## Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `sku_id` | string | yes | Plain string identifier for the product being negotiated. |
| `product_name` | string | yes | Human-readable name, for rationale/audit text. |
| `currency` | string | yes | ISO 4217 code. Milestone 1 assumes `"INR"` (see SPEC.md Assumptions). |
| `list_price` | number | yes | Reference/list price the offer is discounted against. Positive number, currency's minor-unit-free decimal (e.g. `2499.00`). |
| `min_price` | number | yes | Absolute floor. Any offer price below this is rejected regardless of quantity. Must be `<= list_price`. |
| `max_discount_pct` | number | yes | Base maximum discount off `list_price`, as a percentage (e.g. `15` = 15%), applied when no `qty_breaks` tier matches the offer's quantity. |
| `qty_breaks` | array of objects | yes (may be empty `[]`) | Quantity-tiered discount overrides. See **qty_breaks shape** below. |
| `max_negotiation_rounds` | integer | yes | Hard cap on negotiation rounds (default demo value: `5`, per `AGENTS.md`). Overriding this requires explicit human approval outside the code (not enforced in Milestone 1 code). |

## `qty_breaks` shape

```json
"qty_breaks": [
  { "min_qty": 10, "discount_pct": 20 },
  { "min_qty": 25, "discount_pct": 28 }
]
```

- Each entry: `min_qty` (integer, quantity threshold) and `discount_pct` (number, percentage).
- Entries need not be pre-sorted by the file author; the merchant-agent selects the entry with the **largest `min_qty` that is `<= offer.qty`**.
- If the offer's quantity meets no tier's `min_qty`, the base `max_discount_pct` applies instead.
- The selected tier's `discount_pct` **overrides** (does not stack with) `max_discount_pct` for that offer (per SPEC.md Assumptions).
- The effective floor price at a given quantity is `max(min_price, list_price * (1 - applicable_discount_pct / 100))`.

## Example (`merchant_policy.json` demo values)

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
  "max_negotiation_rounds": 5
}
```

At qty 1–9: floor = `max(3799, 4999*0.88)` = `max(3799, 4399.12)` = `4399.12`.
At qty 10–24: floor = `max(3799, 4999*0.82)` = `max(3799, 4099.18)` = `4099.18`.
At qty 25+: floor = `max(3799, 4999*0.76)` = `max(3799, 3799.24)` = `3799.24`.
