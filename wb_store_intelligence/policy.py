from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


class SafetyViolation(RuntimeError):
    """Raised before any network call that violates the read-only policy."""


@dataclass(frozen=True)
class EndpointRule:
    name: str
    method: str
    host: str
    path_pattern: re.Pattern[str]

    @classmethod
    def create(cls, name: str, method: str, host: str, path_pattern: str) -> "EndpointRule":
        return cls(name, method.upper(), host.lower(), re.compile(path_pattern))


# POST is allowed only where WB uses it for reads. Every rule is exact and anchored.
READ_ONLY_ENDPOINTS: tuple[EndpointRule, ...] = (
    EndpointRule.create(
        "cards-list",
        "POST",
        "content-api.wildberries.ru",
        r"/content/v2/get/cards/list",
    ),
    EndpointRule.create(
        "cards-error-list",
        "POST",
        "content-api.wildberries.ru",
        r"/content/v2/cards/error/list",
    ),
    EndpointRule.create(
        "prices",
        "GET",
        "discounts-prices-api.wildberries.ru",
        r"/api/v2/list/goods/filter",
    ),
    EndpointRule.create(
        "warehouses",
        "GET",
        "marketplace-api.wildberries.ru",
        r"/api/v3/warehouses",
    ),
    EndpointRule.create(
        "stocks",
        "POST",
        "marketplace-api.wildberries.ru",
        r"/api/v3/stocks/[1-9][0-9]*",
    ),
    EndpointRule.create(
        "sales-funnel-products",
        "POST",
        "seller-analytics-api.wildberries.ru",
        r"/api/analytics/v3/sales-funnel/products",
    ),
    EndpointRule.create(
        "sales-funnel-history",
        "POST",
        "seller-analytics-api.wildberries.ru",
        r"/api/analytics/v3/sales-funnel/products/history",
    ),
    EndpointRule.create(
        "orders",
        "GET",
        "statistics-api.wildberries.ru",
        r"/api/v1/supplier/orders",
    ),
    EndpointRule.create(
        "sales",
        "GET",
        "statistics-api.wildberries.ru",
        r"/api/v1/supplier/sales",
    ),
    EndpointRule.create(
        "wb-warehouse-stocks",
        "POST",
        "seller-analytics-api.wildberries.ru",
        r"/api/analytics/v1/stocks-report/wb-warehouses",
    ),
    EndpointRule.create(
        "search-report",
        "POST",
        "seller-analytics-api.wildberries.ru",
        r"/api/v2/search-report/report",
    ),
    EndpointRule.create(
        "search-report-details",
        "POST",
        "seller-analytics-api.wildberries.ru",
        r"/api/v2/search-report/table/details",
    ),
    EndpointRule.create(
        "product-search-texts",
        "POST",
        "seller-analytics-api.wildberries.ru",
        r"/api/v2/search-report/product/search-texts",
    ),
    EndpointRule.create(
        "product-search-orders",
        "POST",
        "seller-analytics-api.wildberries.ru",
        r"/api/v2/search-report/product/orders",
    ),
    EndpointRule.create(
        "advert-campaign-list",
        "GET",
        "advert-api.wildberries.ru",
        r"/adv/v1/promotion/count",
    ),
    EndpointRule.create(
        "advert-campaign-details",
        "GET",
        "advert-api.wildberries.ru",
        r"/api/advert/v2/adverts",
    ),
    EndpointRule.create(
        "advert-campaign-stats",
        "GET",
        "advert-api.wildberries.ru",
        r"/adv/v3/fullstats",
    ),
    EndpointRule.create(
        "advert-search-cluster-stats",
        "POST",
        "advert-api.wildberries.ru",
        r"/adv/v0/normquery/stats",
    ),
    EndpointRule.create(
        "promotion-calendar",
        "GET",
        "dp-calendar-api.wildberries.ru",
        r"/api/v1/calendar/promotions",
    ),
    EndpointRule.create(
        "promotion-products",
        "GET",
        "dp-calendar-api.wildberries.ru",
        r"/api/v1/calendar/promotions/nomenclatures",
    ),
    EndpointRule.create(
        "seller-info",
        "GET",
        "common-api.wildberries.ru",
        r"/api/v1/seller-info",
    ),
    EndpointRule.create(
        "jam-subscription",
        "GET",
        "common-api.wildberries.ru",
        r"/api/common/v1/subscriptions",
    ),
    EndpointRule.create(
        "seller-rating",
        "GET",
        "feedbacks-api.wildberries.ru",
        r"/api/common/v1/rating",
    ),
    EndpointRule.create(
        "feedbacks",
        "GET",
        "feedbacks-api.wildberries.ru",
        r"/api/v1/feedbacks",
    ),
    EndpointRule.create(
        "questions",
        "GET",
        "feedbacks-api.wildberries.ru",
        r"/api/v1/questions",
    ),
)


class ReadOnlyPolicy:
    def __init__(self, rules: tuple[EndpointRule, ...] = READ_ONLY_ENDPOINTS):
        self._rules = rules

    @property
    def endpoint_names(self) -> tuple[str, ...]:
        return tuple(rule.name for rule in self._rules)

    def authorize(self, method: str, url: str) -> EndpointRule:
        method = method.upper().strip()
        if method not in {"GET", "POST"}:
            raise SafetyViolation(f"HTTP method {method!r} is forbidden in read-only mode")

        parsed = urlsplit(url)
        if parsed.scheme != "https":
            raise SafetyViolation("Only HTTPS WB endpoints are allowed")
        if parsed.username or parsed.password or parsed.port or parsed.fragment:
            raise SafetyViolation("Credentials, custom ports, and fragments are forbidden")
        if not parsed.hostname:
            raise SafetyViolation("Endpoint host is missing")
        if unquote(parsed.path) != parsed.path:
            raise SafetyViolation("Encoded paths are forbidden")
        if any(segment in {".", ".."} for segment in parsed.path.split("/")):
            raise SafetyViolation("Dot path segments are forbidden")

        host = parsed.hostname.lower()
        for rule in self._rules:
            if (
                method == rule.method
                and host == rule.host
                and rule.path_pattern.fullmatch(parsed.path)
            ):
                return rule
        raise SafetyViolation(
            f"Endpoint is not on the read-only allowlist: {method} {host}{parsed.path}"
        )
