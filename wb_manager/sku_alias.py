from __future__ import annotations


# Preserve known historical seller-article aliases so existing listing and
# image packages remain addressable after moving the WB category root.
SKU_ALIASES_BY_CATEGORY: dict[str, dict[str, str]] = {
    "脱毛仪": {
        "A109B": "AI09B",
    },
    # WB seller articles are immutable operational identifiers.  These eight
    # historical articles contain spelling mistakes, so accept the corrected
    # spelling at every workflow entry point while continuing to address the
    # existing live card, barcode, stock, price, media, and order history.
    "手机": {
        "IPHONE 13-128GB-BLUE": "IPHONE 13-128GB-BULE",
        "IPHONE 13-256GB-BLUE": "IPHONE 13-256GB-BULE",
        "IPHONE 16PRO-128GB-GOLD": "IPHONE 16PRO-128GB-GOID",
        "IPHONE 16PRO-256GB-GOLD": "IPHONE 16PRO-256GB-GOID",
        "IPHONE 16PRO-512GB-GOLD": "IPHONE 16PRO-512GB-GOID",
        "SAMSUNG FOLD 6-256GB-SILVER": "SAMSUNG FOLD 6-256GB-SLIVER",
        "SAMSUNG FOLD 6-512GB-SILVER": "SAMSUNG FOLD 6-512GB-SLIVER",
        "SAMSUNG FOLD 6-1TB-SILVER": "SAMSUNG FOLD 6-1TB-SLIVER",
    },
}


def canonical_sku(sku: str, *, category: str | None = None) -> str:
    value = sku.strip()
    upper = value.upper()
    if category:
        return SKU_ALIASES_BY_CATEGORY.get(category, {}).get(upper, upper)
    return upper
