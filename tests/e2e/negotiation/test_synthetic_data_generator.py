import random

from scripts.generate_synthetic_data import (
    NUM_BUYERS,
    NUM_ORDERS,
    NUM_PRODUCTS,
    generate_buyers,
    generate_catalog,
    generate_orders,
)
from src.personalization import CATEGORY_LIQUIDATION_THRESHOLDS, CATEGORY_TO_MERCHANT


def _generate_all(seed):
    rng = random.Random(seed)
    catalog = generate_catalog(rng)
    buyers = generate_buyers(rng)
    orders = generate_orders(rng, catalog, buyers)
    return catalog, buyers, orders


def test_generator_produces_schema_valid_data_at_expected_scale():
    catalog, buyers, orders = _generate_all(seed=42)

    assert len(catalog) == NUM_PRODUCTS == 80
    assert len(buyers) == NUM_BUYERS == 30
    assert len(orders) == NUM_ORDERS == 200

    categories = {p["category"] for p in catalog}
    assert 6 <= len(categories) <= 8

    required_product_fields = {
        "sku_id", "category", "merchant_id", "list_price", "cost", "min_price", "max_discount_pct",
        "qty_breaks", "current_inventory", "inventory_floor", "days_in_inventory",
    }
    for product in catalog:
        assert required_product_fields.issubset(product.keys())
        assert product["min_price"] > product["cost"]  # margin actually enforced above cost
        assert product["min_price"] <= product["list_price"]
        assert product["current_inventory"] >= 0
        assert product["inventory_floor"] >= 1
        assert product["days_in_inventory"] >= 1
        for tier in product["qty_breaks"]:
            assert tier["discount_pct"] <= 30  # never generated above the hard ceiling
        # Milestone 6: merchant_id must match this product's category via
        # the single source of truth (CATEGORY_TO_MERCHANT), never
        # independently assigned -- generator and runtime can't drift.
        assert product["merchant_id"] == CATEGORY_TO_MERCHANT[product["category"]]

    sku_ids = [p["sku_id"] for p in catalog]
    assert len(sku_ids) == len(set(sku_ids))

    # Milestone 6: every category maps to exactly one merchant, and the
    # whole-category split lands at 40/40 products (10 per category, 4
    # categories per merchant) -- confirmed with the user before generating.
    from collections import Counter
    merchant_counts = Counter(p["merchant_id"] for p in catalog)
    assert merchant_counts == {"MERCH-001": 40, "MERCH-002": 40}
    assert set(CATEGORY_TO_MERCHANT.values()) == {"MERCH-001", "MERCH-002"}

    # Each product's "aged" status is judged against its OWN category's
    # threshold, not one flat number -- Milestone 3c follow-up.
    assert set(CATEGORY_LIQUIDATION_THRESHOLDS.keys()) == categories
    aged_products = [
        p for p in catalog if p["days_in_inventory"] > CATEGORY_LIQUIDATION_THRESHOLDS[p["category"]]
    ]
    assert len(aged_products) == 8  # the user's confirmed count, one per category
    assert {p["category"] for p in aged_products} == categories  # spread across every category
    for product in aged_products:
        threshold = CATEGORY_LIQUIDATION_THRESHOLDS[product["category"]]
        assert product["days_in_inventory"] >= threshold + 20  # comfortably past, not just barely

    # budget_range removed (Section 2AG): a buyer's spending ceiling is now
    # derived at negotiation time from PERSONA_DISCOUNT_BANDS against the
    # real product's list_price, not stored on the buyer record.
    required_buyer_fields = {"buyer_id", "persona", "category_affinity", "negotiation_style"}
    for buyer in buyers:
        assert required_buyer_fields.issubset(buyer.keys())
        assert "budget_range" not in buyer

    buyer_ids = [b["buyer_id"] for b in buyers]
    assert len(buyer_ids) == len(set(buyer_ids))

    required_order_fields = {"order_id", "buyer_id", "product_id", "amount", "timestamp"}
    valid_buyer_ids = set(buyer_ids)
    valid_product_ids = set(sku_ids)
    for order in orders:
        assert required_order_fields.issubset(order.keys())
        assert order["buyer_id"] in valid_buyer_ids
        assert order["product_id"] in valid_product_ids
        assert order["amount"] > 0


def test_generator_is_reproducible_with_a_fixed_seed():
    catalog_a, buyers_a, orders_a = _generate_all(seed=7)
    catalog_b, buyers_b, orders_b = _generate_all(seed=7)

    assert catalog_a == catalog_b
    assert buyers_a == buyers_b
    assert orders_a == orders_b


def test_different_seeds_produce_different_data():
    catalog_a, _buyers_a, _orders_a = _generate_all(seed=1)
    catalog_b, _buyers_b, _orders_b = _generate_all(seed=2)

    assert catalog_a != catalog_b


def test_ltv_is_computable_and_produces_a_meaningfully_skewed_distribution():
    """Sanity check on the 'skewed/realistic' distribution choice: order
    volume should differ substantially across buyers, not be uniform --
    otherwise the LTV-tiering feature would have nothing to differentiate
    in the demo."""
    _catalog, buyers, orders = _generate_all(seed=42)

    from src.personalization import compute_ltv

    ltvs = [compute_ltv(b["buyer_id"], orders) for b in buyers]
    # 5x (not a tighter bound) so this stays robust to any future generator
    # tweak that shifts the RNG draw sequence -- the point is "genuinely
    # skewed," not an exact ratio.
    assert max(ltvs) > 5 * sorted(ltvs)[len(ltvs) // 2]  # top buyer >> median buyer
    assert all(ltv >= 0 for ltv in ltvs)
