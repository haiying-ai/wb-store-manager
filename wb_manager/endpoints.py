from __future__ import annotations

from enum import StrEnum


class ApiDomain(StrEnum):
    CONTENT = "https://content-api.wildberries.ru"
    PRICES = "https://discounts-prices-api.wildberries.ru"
    MARKETPLACE = "https://marketplace-api.wildberries.ru"
    STATISTICS = "https://statistics-api.wildberries.ru"
    ANALYTICS = "https://seller-analytics-api.wildberries.ru"
    ADVERTISING = "https://advert-api.wildberries.ru"
    FINANCE = "https://finance-api.wildberries.ru"
    RETURNS = "https://seller-analytics-api.wildberries.ru"
    FEEDBACKS = "https://feedbacks-api.wildberries.ru"
    COMMON = "https://common-api.wildberries.ru"


PING_DOMAINS = [
    ApiDomain.CONTENT,
    ApiDomain.PRICES,
    ApiDomain.MARKETPLACE,
    ApiDomain.STATISTICS,
    ApiDomain.ANALYTICS,
    ApiDomain.ADVERTISING,
    ApiDomain.FEEDBACKS,
    ApiDomain.COMMON,
]
