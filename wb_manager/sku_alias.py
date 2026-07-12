from __future__ import annotations


SKU_ALIASES_BY_CATEGORY: dict[str, dict[str, str]] = {}


def canonical_sku(sku: str, *, category: str | None = None) -> str:
    value = sku.strip()
    upper = value.upper()
    if category:
        return SKU_ALIASES_BY_CATEGORY.get(category, {}).get(upper, upper)
    return upper
