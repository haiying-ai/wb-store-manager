from __future__ import annotations

import json

import pytest

from wb_store_intelligence.client import ReadOnlyWbClient
from wb_store_intelligence.config import StoreConfig, StoreRegistry, load_token_file
from wb_store_intelligence.policy import ReadOnlyPolicy, SafetyViolation


@pytest.mark.parametrize(
    ("method", "url", "endpoint"),
    [
        (
            "POST",
            "https://content-api.wildberries.ru/content/v2/get/cards/list",
            "cards-list",
        ),
        (
            "GET",
            "https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=100",
            "prices",
        ),
        (
            "POST",
            "https://marketplace-api.wildberries.ru/api/v3/stocks/12345",
            "stocks",
        ),
        (
            "GET",
            "https://marketplace-api.wildberries.ru/api/v3/orders?limit=1000&next=0",
            "marketplace-orders",
        ),
        (
            "POST",
            "https://marketplace-api.wildberries.ru/api/v3/orders/status",
            "marketplace-order-statuses",
        ),
        (
            "POST",
            "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products",
            "sales-funnel-products",
        ),
        (
            "POST",
            "https://seller-analytics-api.wildberries.ru/api/v2/nm-report/downloads",
            "analytics-report-create",
        ),
        (
            "GET",
            "https://seller-analytics-api.wildberries.ru/api/v2/nm-report/downloads?filter%5BdownloadIds%5D=06eae887-9d9f-491f-b16a-bb1766fcb8d2",
            "analytics-report-list",
        ),
        (
            "GET",
            "https://seller-analytics-api.wildberries.ru/api/v2/nm-report/downloads/file/06eae887-9d9f-491f-b16a-bb1766fcb8d2",
            "analytics-report-file",
        ),
        (
            "POST",
            "https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses",
            "wb-warehouse-stocks",
        ),
        (
            "POST",
            "https://seller-analytics-api.wildberries.ru/api/v2/search-report/product/search-texts",
            "product-search-texts",
        ),
        (
            "POST",
            "https://seller-analytics-api.wildberries.ru/api/v2/search-report/report",
            "search-report",
        ),
        (
            "POST",
            "https://seller-analytics-api.wildberries.ru/api/v2/search-report/table/details",
            "search-report-details",
        ),
        (
            "POST",
            "https://seller-analytics-api.wildberries.ru/api/v2/search-report/product/orders",
            "product-search-orders",
        ),
        (
            "GET",
            "https://advert-api.wildberries.ru/adv/v1/promotion/count",
            "advert-campaign-list",
        ),
        (
            "GET",
            "https://advert-api.wildberries.ru/api/advert/v2/adverts",
            "advert-campaign-details",
        ),
        (
            "GET",
            "https://advert-api.wildberries.ru/adv/v3/fullstats?ids=1&beginDate=2026-07-01&endDate=2026-07-02",
            "advert-campaign-stats",
        ),
        (
            "POST",
            "https://advert-api.wildberries.ru/adv/v0/normquery/stats",
            "advert-search-cluster-stats",
        ),
        (
            "GET",
            "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions",
            "promotion-calendar",
        ),
        (
            "GET",
            "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions/details?promotionIDs=1",
            "promotion-details",
        ),
        (
            "GET",
            "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions/nomenclatures",
            "promotion-products",
        ),
        (
            "GET",
            "https://common-api.wildberries.ru/api/v1/seller-info",
            "seller-info",
        ),
        (
            "GET",
            "https://common-api.wildberries.ru/api/common/v1/subscriptions",
            "jam-subscription",
        ),
        (
            "GET",
            "https://feedbacks-api.wildberries.ru/api/common/v1/rating",
            "seller-rating",
        ),
        (
            "GET",
            "https://statistics-api.wildberries.ru/api/v1/supplier/orders?dateFrom=2026-05-05&flag=0",
            "orders",
        ),
    ],
)
def test_allows_only_declared_reads(method: str, url: str, endpoint: str) -> None:
    assert ReadOnlyPolicy().authorize(method, url).name == endpoint


@pytest.mark.parametrize(('method', 'path', 'name'), [
    ('GET', '/adv/v1/balance', 'advert-balance'),
    ('GET', '/adv/v1/budget?id=39907863', 'advert-campaign-budget'),
    ('POST', '/api/advert/v1/bids/min', 'advert-minimum-bids'),
])
def test_ad_funding_preflight_exact_reads(method, path, name):
    assert ReadOnlyPolicy().authorize(method, 'https://advert-api.wildberries.ru'+path).name == name


@pytest.mark.parametrize(('method', 'path'), [
    ('POST', '/adv/v1/balance'), ('POST', '/adv/v1/budget'),
    ('POST', '/adv/v1/budget/deposit?id=39907863'),
    ('GET', '/adv/v1/budget/deposit?id=39907863'),
    ('GET', '/adv/v0/start?id=39907863'), ('GET', '/adv/v0/pause?id=39907863'),
    ('PATCH', '/api/advert/v1/bids'), ('POST', '/api/advert/v1/bids'),
    ('GET', '/api/advert/v1/bids/min'), ('POST', '/api/advert/v1/bids/min/extra'),
    ('POST', '/adv/v0/normquery/bids'), ('POST', '/adv/v0/normquery/set-minus'),
])
def test_ad_funding_preflight_never_allows_mutations_or_nearby_routes(method, path):
    def fail_if_called(*args, **kwargs):
        raise AssertionError('Network must not be reached')
    client = ReadOnlyWbClient(StoreConfig('owner-store', 'owner_readonly', 'WB_API_TOKEN'), opener=fail_if_called)
    with pytest.raises(SafetyViolation):
        client.request(method, 'https://advert-api.wildberries.ru'+path)


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("PUT", "https://marketplace-api.wildberries.ru/api/v3/stocks/123"),
        ("PATCH", "https://marketplace-api.wildberries.ru/api/v3/orders/123/cancel"),
        ("POST", "https://marketplace-api.wildberries.ru/api/v3/orders"),
        ("GET", "https://marketplace-api.wildberries.ru/api/v3/orders/status"),
        ("POST", "https://marketplace-api.wildberries.ru/api/v3/orders/stickers"),
        ("PATCH", "https://content-api.wildberries.ru/content/v2/cards/update"),
        ("POST", "https://content-api.wildberries.ru/content/v2/cards/update"),
        ("POST", "https://discounts-prices-api.wildberries.ru/api/v2/upload/task"),
        ("POST", "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions/upload"),
        ("POST", "https://advert-api.wildberries.ru/adv/v1/save-ad"),
        ("POST", "https://seller-analytics-api.wildberries.ru/api/v2/nm-report/downloads/retry"),
        ("GET", "https://seller-analytics-api.wildberries.ru/api/v2/nm-report/downloads/file/not-a-uuid"),
        ("GET", "https://statistics-api.wildberries.ru/api/v1/supplier/stocks"),
        ("GET", "https://evil.example/api/v1/supplier/orders"),
        ("GET", "http://statistics-api.wildberries.ru/api/v1/supplier/orders"),
        (
            "GET",
            "https://statistics-api.wildberries.ru.evil.example/api/v1/supplier/orders",
        ),
        (
            "GET",
            "https://statistics-api.wildberries.ru/%61pi/v1/supplier/orders",
        ),
        (
            "GET",
            "https://statistics-api.wildberries.ru/api/v1/../v1/supplier/orders",
        ),
    ],
)
def test_blocks_writes_unknown_hosts_and_ambiguous_paths(method: str, url: str) -> None:
    with pytest.raises(SafetyViolation):
        ReadOnlyPolicy().authorize(method, url)


def test_client_blocks_before_token_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    touched = False

    def opener(*args, **kwargs):
        nonlocal touched
        touched = True
        raise AssertionError("network must not be reached")

    store = StoreConfig(
        alias="benchmark-a",
        role="benchmark_readonly",
        token_env="WB_INTEL_BENCHMARK_A_TOKEN",
    )
    client = ReadOnlyWbClient(store, opener=opener)
    with pytest.raises(SafetyViolation):
        client.request(
            "PUT",
            "https://marketplace-api.wildberries.ru/api/v3/stocks/123",
            body={"stocks": [{"sku": "x", "amount": 1}]},
        )
    assert touched is False


def test_registry_rejects_literal_tokens_and_reused_env(tmp_path) -> None:
    literal = tmp_path / "literal.json"
    literal.write_text(
        json.dumps(
            {
                "stores": [
                    {
                        "alias": "benchmark-a",
                        "role": "benchmark_readonly",
                        "token": "secret",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        StoreRegistry.from_file(literal)

    reused = tmp_path / "reused.json"
    reused.write_text(
        json.dumps(
            {
                "stores": [
                    {
                        "alias": "benchmark-a",
                        "role": "benchmark_readonly",
                        "token_env": "WB_INTEL_SHARED_TOKEN",
                    },
                    {
                        "alias": "benchmark-b",
                        "role": "benchmark_readonly",
                        "token_env": "WB_INTEL_SHARED_TOKEN",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        StoreRegistry.from_file(reused)


def test_accepts_token_regardless_of_platform_permission_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StoreConfig(
        alias="benchmark-a",
        role="benchmark_readonly",
        token_env="WB_INTEL_BENCHMARK_A_TOKEN",
    )
    monkeypatch.setenv("WB_INTEL_BENCHMARK_A_TOKEN", "token-with-write-permissions")
    assert store.load_token() == "token-with-write-permissions"


def test_load_token_file_without_exposing_values(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "tokens.env"
    token_file.write_text(
        "# local only\n"
        "WB_INTEL_BENCHMARK_A_TOKEN=token-a\n"
        "WB_INTEL_BENCHMARK_B_TOKEN=token-b\n",
        encoding="utf-8",
    )
    loaded = load_token_file(token_file, override=True)
    assert loaded == (
        "WB_INTEL_BENCHMARK_A_TOKEN",
        "WB_INTEL_BENCHMARK_B_TOKEN",
    )
    assert StoreConfig(
        alias="benchmark-a",
        role="benchmark_readonly",
        token_env="WB_INTEL_BENCHMARK_A_TOKEN",
    ).load_token() == "token-a"
    monkeypatch.delenv("WB_INTEL_BENCHMARK_A_TOKEN")
    monkeypatch.delenv("WB_INTEL_BENCHMARK_B_TOKEN")


def test_token_file_rejects_placeholders(tmp_path) -> None:
    token_file = tmp_path / "tokens.env"
    token_file.write_text(
        "WB_INTEL_BENCHMARK_A_TOKEN=REPLACE_WITH_BENCHMARK_A_TOKEN\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="has not been configured"):
        load_token_file(token_file)


def test_accepts_existing_owner_token_variable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "owner.env"
    token_file.write_text("WB_API_TOKEN=owner-token\n", encoding="utf-8")
    assert load_token_file(token_file, override=True) == ("WB_API_TOKEN",)
    assert StoreConfig(
        alias="owner-store",
        role="owner_readonly",
        token_env="WB_API_TOKEN",
    ).load_token() == "owner-token"
    monkeypatch.delenv("WB_API_TOKEN")


def test_accepts_secondary_owner_store_token_variable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "new-owner.env"
    token_file.write_text(
        "WB_NEW_STORE_API_TOKEN=new-owner-token\n", encoding="utf-8"
    )
    assert load_token_file(token_file, override=True) == (
        "WB_NEW_STORE_API_TOKEN",
    )
    assert StoreConfig(
        alias="new-store",
        role="owner_readonly",
        token_env="WB_NEW_STORE_API_TOKEN",
    ).load_token() == "new-owner-token"
    monkeypatch.delenv("WB_NEW_STORE_API_TOKEN")
