"""Resets data/catalog.json's current_inventory back to each product's
originally generated value (Milestone 4 demo hygiene).

Completed negotiations permanently decrement current_inventory
(personalization.decrement_inventory, called from
negotiation_loop._attempt_payment on a COMPLETED payment) -- so repeated
demo runs against the same catalog gradually deplete real stock. Run this
before a demo recording to start from a clean slate without regenerating
buyers.json/orders.json (which would also shuffle LTV/persona data those
demos may already be tuned around).

Only touches current_inventory. Regenerates the catalog in-memory with
generate_synthetic_data.generate_catalog(random.Random(seed)) -- the same
deterministic generator the real data was built from -- and matches
products by sku_id, so any other field a product happens to carry on disk
is left exactly as-is; only current_inventory is overwritten back to the
value that seed originally produced.

Usage:
    python scripts/reset_data.py [--seed 42] [--catalog-path data/catalog.json]
"""
import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.generate_synthetic_data import generate_catalog  # noqa: E402


def reset_inventory(catalog_path, seed):
    """Returns (updated_catalog, reset_count). Raises SystemExit if a
    sku_id on disk has no match in the freshly generated reference
    catalog -- that means the on-disk file wasn't generated from this
    seed, and silently "resetting" it to unrelated numbers would be worse
    than refusing."""
    on_disk = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    reference = {p["sku_id"]: p for p in generate_catalog(random.Random(seed))}

    missing = [p["sku_id"] for p in on_disk if p["sku_id"] not in reference]
    if missing:
        raise SystemExit(
            f"{catalog_path} contains sku_id(s) not produced by seed={seed}: {missing[:5]}"
            f"{'...' if len(missing) > 5 else ''}. This file may have been generated with a "
            "different --seed; pass the matching --seed instead of resetting against the wrong one."
        )

    reset_count = 0
    for product in on_disk:
        original = reference[product["sku_id"]]["current_inventory"]
        if product["current_inventory"] != original:
            reset_count += 1
        product["current_inventory"] = original
    return on_disk, reset_count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--catalog-path", default="data/catalog.json")
    args = parser.parse_args()

    catalog_path = Path(args.catalog_path)
    if not catalog_path.exists():
        raise SystemExit(f"{catalog_path} does not exist -- run scripts/generate_synthetic_data.py first.")

    updated_catalog, reset_count = reset_inventory(catalog_path, args.seed)
    catalog_path.write_text(json.dumps(updated_catalog, indent=2) + "\n", encoding="utf-8")

    print(
        f"Reset current_inventory for {reset_count}/{len(updated_catalog)} product(s) in "
        f"{catalog_path} back to their seed={args.seed} originals."
    )


if __name__ == "__main__":
    main()
