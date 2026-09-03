"""Generates data/catalog.json, data/buyers.json, and data/orders.json for
Parley Milestone 3c (synthetic data + personalization layer).

Deterministic: the same --seed always produces byte-identical output.
Timestamps are drawn from a fixed reference window (not wall-clock "now"),
so reproducibility holds regardless of when the script is actually run.

Usage:
    python scripts/generate_synthetic_data.py [--seed 42] [--output-dir data]
"""
import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.personalization import CATEGORY_LIQUIDATION_THRESHOLDS, CATEGORY_TO_MERCHANT  # noqa: E402

# Fixed reference window for order timestamps -- NOT datetime.now(), so
# output is reproducible regardless of when the script actually runs.
ORDERS_WINDOW_END = datetime(2026, 8, 31, tzinfo=timezone.utc)
ORDERS_WINDOW_START = ORDERS_WINDOW_END - timedelta(days=365)

NUM_PRODUCTS = 80
NUM_BUYERS = 30
NUM_ORDERS = 200

CATEGORIES = {
    "Electronics": {
        "sku_prefix": "ELEC",
        "price_range": (800, 15000),
        "products": [
            "Wireless Mechanical Keyboard", "Bluetooth Over-Ear Headphones", "27-inch 4K Monitor",
            "USB-C Hub Adapter", "Portable SSD 1TB", "Smart LED Desk Lamp", "Noise-Cancelling Earbuds",
            "1080p Webcam", "15W Wireless Charging Pad", "2m HDMI Cable", "Compact Bluetooth Speaker",
        ],
    },
    "Apparel & Fashion": {
        "sku_prefix": "APRL",
        "price_range": (300, 4000),
        "products": [
            "Cotton Crew-Neck T-Shirt", "Slim-Fit Denim Jeans", "Wool-Blend Overcoat", "Running Sneakers",
            "Leather Belt", "Zip-Up Hoodie", "Formal Dress Shirt", "Canvas Tote Bag", "Wool Beanie",
            "Ankle-Length Chino Trousers", "Polarized Sunglasses",
        ],
    },
    "Home & Kitchen": {
        "sku_prefix": "HOME",
        "price_range": (200, 6000),
        "products": [
            "Stainless Steel Cookware Set", "Non-Stick Frying Pan", "Electric Kettle", "Ceramic Dinner Set",
            "Memory Foam Pillow", "Cotton Bedsheet Set", "Air-Tight Storage Container Set", "Table Lamp",
            "Wall Clock", "Blackout Curtain Pair", "Bamboo Cutting Board",
        ],
    },
    "Sporting Goods & Outdoors": {
        "sku_prefix": "SPRT",
        "price_range": (300, 8000),
        "products": [
            "Yoga Mat", "Adjustable Dumbbell Set", "Camping Tent (2-Person)", "Insulated Water Bottle",
            "Trekking Backpack", "Resistance Band Set", "Cycling Helmet", "Badminton Racquet Pair",
            "Sleeping Bag", "Football", "Foldable Camping Chair",
        ],
    },
    "Books & Media": {
        "sku_prefix": "BOOK",
        "price_range": (150, 1500),
        "products": [
            "Bestselling Fiction Novel", "Illustrated Cookbook", "Personal Finance Guide",
            "Children's Picture Book Set", "Graphic Novel Collection", "Self-Help Bestseller",
            "History Non-Fiction Hardcover", "Puzzle & Brain Games Book", "Travel Guidebook",
            "Poetry Anthology", "Business Strategy Book",
        ],
    },
    "Beauty & Personal Care": {
        "sku_prefix": "BEAU",
        "price_range": (150, 3000),
        "products": [
            "Vitamin C Face Serum", "Electric Trimmer", "Hair Dryer", "Herbal Shampoo & Conditioner Set",
            "Moisturizing Body Lotion", "Sunscreen SPF 50", "Electric Toothbrush", "Perfume Eau de Parfum",
            "Facial Cleansing Brush", "Lip Balm Set", "Nail Care Kit",
        ],
    },
    "Toys & Games": {
        "sku_prefix": "TOYS",
        "price_range": (200, 3500),
        "products": [
            "Building Block Set", "Remote-Control Car", "Board Game (Family Edition)", "Plush Teddy Bear",
            "Jigsaw Puzzle (1000-piece)", "Kids' Art & Craft Kit", "Action Figure Set", "Educational STEM Kit",
            "Card Game Deck", "Ride-On Toy Car", "Wooden Toy Train Set",
        ],
    },
    "Office & Stationery": {
        "sku_prefix": "OFFC",
        "price_range": (100, 2500),
        "products": [
            "Ergonomic Office Chair", "A4 Notebook Pack", "Gel Pen Set", "Desk Organizer",
            "Whiteboard (Small)", "Sticky Note Pack", "Laminator Machine", "Stapler & Punch Set",
            "Adjustable Laptop Stand", "Cable Management Box", "Desk Calendar",
        ],
    },
}

PERSONAS = [
    ("Bargain Hunter", (2000, 6000), "aggressive -- pushes hard for the lowest possible price"),
    ("Loyal Regular", (4000, 12000), "moderate -- fair but expects recognition for repeat business"),
    ("Bulk Buyer", (10000, 40000), "moderate -- focused on quantity discounts over per-unit haggling"),
    ("Window Shopper", (1000, 3000), "easygoing -- browses often, rarely pushes hard on price"),
    ("Premium Customer", (8000, 25000), "easygoing -- values convenience and quality over discounts"),
    ("Occasional Buyer", (1500, 5000), "moderate -- open to a fair discount but not desperate"),
    ("Whale", (30000, 80000), "moderate -- big spender, expects VIP-tier treatment"),
    ("Stubborn Negotiator", (2000, 8000), "aggressive -- anchors low and concedes very slowly"),
]

# A handful of buyers get outsized order weight -- "whales" -- so the LTV
# tiers are actually exercised across the buyer population (per user's
# choice: skewed/realistic distribution, not evenly spread).
WHALE_BUYER_INDICES = {2, 7, 15}  # 0-indexed into the 30 generated buyers

# Milestone 3c follow-up: one aged-inventory outlier per category (indices
# 2, 12, 22, ... -- local index 2 within each category's 10-item block),
# 8 total -- the user's explicit choice, spread evenly rather than
# clustered in one category. Each outlier's days_in_inventory is
# comfortably past ITS OWN category's threshold (CATEGORY_LIQUIDATION_
# THRESHOLDS, imported from src.personalization -- the single source of
# truth so the generator and the runtime check can never drift apart),
# not one shared flat range -- a Books & Media outlier lands much later
# than an Electronics one, proportionate to each category's own bar.
AGED_PRODUCT_LOCAL_INDEX = 2
AGED_PRODUCT_MIN_DAYS_PAST_THRESHOLD = 20   # comfortably past the threshold...
AGED_PRODUCT_MAX_DAYS_PAST_THRESHOLD = 220  # ...up to genuinely extreme


def _round_price(value):
    return round(value, 2)


def generate_catalog(rng):
    catalog = []
    for category, spec in CATEGORIES.items():
        names = spec["products"]
        assert len(names) >= 10, f"{category} needs >= 10 product names"
        for i in range(10):
            name = names[i]
            list_price = _round_price(rng.uniform(*spec["price_range"]))
            cost = _round_price(list_price * rng.uniform(0.55, 0.75))
            min_price = _round_price(cost * 1.15)  # cost + 15% minimum acceptable margin
            max_discount_pct = rng.randint(8, 15)
            tier1_discount = min(max_discount_pct + rng.randint(5, 10), 28)
            tier2_discount = min(max_discount_pct + rng.randint(12, 18), 30)
            sku_id = f"SKU-{spec['sku_prefix']}-{i + 1:03d}"
            # Aged-inventory outlier (8 total, one per category): well past
            # THIS category's own liquidation threshold. Everything else is
            # recent (1-90 days is comfortably under every category's
            # threshold -- the lowest, Electronics, is still 180).
            if i == AGED_PRODUCT_LOCAL_INDEX:
                category_threshold = CATEGORY_LIQUIDATION_THRESHOLDS[category]
                days_in_inventory = category_threshold + rng.randint(
                    AGED_PRODUCT_MIN_DAYS_PAST_THRESHOLD, AGED_PRODUCT_MAX_DAYS_PAST_THRESHOLD,
                )
            else:
                days_in_inventory = rng.randint(1, 90)
            catalog.append({
                "sku_id": sku_id,
                "product_name": name,
                "category": category,
                "merchant_id": CATEGORY_TO_MERCHANT[category],
                "currency": "INR",
                "list_price": list_price,
                "cost": cost,
                "min_price": min_price,
                "max_discount_pct": max_discount_pct,
                "qty_breaks": [
                    {"min_qty": 10, "discount_pct": tier1_discount},
                    {"min_qty": 25, "discount_pct": tier2_discount},
                ],
                "current_inventory": rng.randint(5, 150),
                "inventory_floor": rng.choice([1, 1, 1, 2, 2, 3]),
                "days_in_inventory": days_in_inventory,
            })
    assert len(catalog) == NUM_PRODUCTS
    return catalog


def generate_buyers(rng):
    buyers = []
    categories = list(CATEGORIES.keys())
    for i in range(NUM_BUYERS):
        persona_label, budget_range, style = PERSONAS[i % len(PERSONAS)]
        buyers.append({
            "buyer_id": f"BUYER-{i + 1:03d}",
            "persona": persona_label,
            "budget_range": {"min": budget_range[0], "max": budget_range[1]},
            "category_affinity": categories[rng.randrange(len(categories))],
            "negotiation_style": style,
        })
    return buyers


def generate_orders(rng, catalog, buyers):
    products_by_category = {}
    for product in catalog:
        products_by_category.setdefault(product["category"], []).append(product)

    weights = [8 if i in WHALE_BUYER_INDICES else 1 for i in range(len(buyers))]
    window_seconds = int((ORDERS_WINDOW_END - ORDERS_WINDOW_START).total_seconds())

    orders = []
    for i in range(NUM_ORDERS):
        buyer = rng.choices(buyers, weights=weights, k=1)[0]
        # 70% of the time, order from the buyer's affinity category.
        if rng.random() < 0.7 and buyer["category_affinity"] in products_by_category:
            product = rng.choice(products_by_category[buyer["category_affinity"]])
        else:
            product = rng.choice(catalog)

        qty = rng.randint(1, 5)
        # Simulate a past deal at a modest historical discount off list price.
        unit_price = _round_price(product["list_price"] * rng.uniform(0.85, 1.0))
        amount = _round_price(unit_price * qty)
        order_time = ORDERS_WINDOW_START + timedelta(seconds=rng.randint(0, window_seconds))

        orders.append({
            "order_id": f"ORD-{i + 1:04d}",
            "buyer_id": buyer["buyer_id"],
            "product_id": product["sku_id"],
            "category": product["category"],
            "amount": amount,
            "currency": "INR",
            "timestamp": order_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    orders.sort(key=lambda o: o["timestamp"])
    return orders


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", default="data")
    args = parser.parse_args()

    rng = random.Random(args.seed)

    catalog = generate_catalog(rng)
    buyers = generate_buyers(rng)
    orders = generate_orders(rng, catalog, buyers)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "catalog.json").write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    (output_dir / "buyers.json").write_text(json.dumps(buyers, indent=2) + "\n", encoding="utf-8")
    (output_dir / "orders.json").write_text(json.dumps(orders, indent=2) + "\n", encoding="utf-8")

    print(f"Generated {len(catalog)} products, {len(buyers)} buyers, {len(orders)} orders "
          f"(seed={args.seed}) into {output_dir}/")


if __name__ == "__main__":
    main()
