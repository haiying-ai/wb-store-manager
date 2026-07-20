import pytest

from wb_manager.sku_alias import canonical_sku


@pytest.mark.parametrize(
    ("corrected", "live_seller_article"),
    [
        ("IPHONE 13-128GB-BLUE", "IPHONE 13-128GB-BULE"),
        ("IPHONE 13-256GB-BLUE", "IPHONE 13-256GB-BULE"),
        ("IPHONE 16PRO-128GB-GOLD", "IPHONE 16PRO-128GB-GOID"),
        ("IPHONE 16PRO-256GB-GOLD", "IPHONE 16PRO-256GB-GOID"),
        ("IPHONE 16PRO-512GB-GOLD", "IPHONE 16PRO-512GB-GOID"),
        ("SAMSUNG FOLD 6-256GB-SILVER", "SAMSUNG FOLD 6-256GB-SLIVER"),
        ("SAMSUNG FOLD 6-512GB-SILVER", "SAMSUNG FOLD 6-512GB-SLIVER"),
        ("SAMSUNG FOLD 6-1TB-SILVER", "SAMSUNG FOLD 6-1TB-SLIVER"),
    ],
)
def test_corrected_phone_sku_resolves_to_live_seller_article(
    corrected: str,
    live_seller_article: str,
) -> None:
    assert canonical_sku(corrected, category="手机") == live_seller_article
    assert canonical_sku(live_seller_article, category="手机") == live_seller_article
