from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

from .client import WbClient
from .endpoints import ApiDomain, PING_DOMAINS


def ping_all(client: WbClient) -> list[dict[str, Any]]:
    results = []
    for domain in PING_DOMAINS:
        url = f"{domain}/ping"
        result = client.request("GET", url)
        results.append({"domain": domain.value, "status": result.status, "ok": result.ok, "data": result.data})
    return results


def content_snapshot(client: WbClient) -> dict[str, Any]:
    cards_body = {
        "settings": {
            "sort": {"ascending": True},
            "cursor": {"limit": 100},
            "filter": {"withPhoto": -1},
        }
    }
    errors_body = {"cursor": {"limit": 100}, "order": {"ascending": True}}

    cards = client.request(
        "POST",
        f"{ApiDomain.CONTENT}/content/v2/get/cards/list?locale=en",
        body=cards_body,
    )
    trash = client.request(
        "POST",
        f"{ApiDomain.CONTENT}/content/v2/get/cards/trash?locale=en",
        body=cards_body,
    )
    errors = client.request(
        "POST",
        f"{ApiDomain.CONTENT}/content/v2/cards/error/list?locale=en",
        body=errors_body,
    )
    tags = client.request("GET", f"{ApiDomain.CONTENT}/content/v2/tags")
    limits = client.request("GET", f"{ApiDomain.CONTENT}/content/v2/cards/limits")

    return {
        "cards": summarize_cards(cards),
        "trash": summarize_cards(trash),
        "errors": summarize_error_cards(errors),
        "tags": summarize_list_response(tags, "data"),
        "limits": {"status": limits.status, "ok": limits.ok, "data": limits.data},
    }


def prices_snapshot(client: WbClient) -> dict[str, Any]:
    goods = client.request("GET", f"{ApiDomain.PRICES}/api/v2/list/goods/filter?limit=1000&offset=0")
    quarantine = client.request("GET", f"{ApiDomain.PRICES}/api/v2/quarantine/goods?limit=1000&offset=0")
    return {
        "goods": summarize_nested_list(goods, ["data", "listGoods"]),
        "quarantine": summarize_nested_list(quarantine, ["data", "quarantineGoods"]),
    }


def marketplace_snapshot(client: WbClient) -> dict[str, Any]:
    return {
        "warehouses": summarize_plain(client.request("GET", f"{ApiDomain.MARKETPLACE}/api/v3/warehouses")),
        "newOrders": summarize_plain(client.request("GET", f"{ApiDomain.MARKETPLACE}/api/v3/orders/new")),
        "orders": summarize_plain(client.request("GET", f"{ApiDomain.MARKETPLACE}/api/v3/orders?limit=1000&next=0")),
    }


def statistics_snapshot(client: WbClient, *, days: int = 30) -> dict[str, Any]:
    date_from = (date.today() - timedelta(days=days)).isoformat()
    stocks = client.request("GET", f"{ApiDomain.STATISTICS}/api/v1/supplier/stocks?dateFrom=2019-06-20")
    time.sleep(61)
    orders = client.request("GET", f"{ApiDomain.STATISTICS}/api/v1/supplier/orders?dateFrom={date_from}&flag=0")
    time.sleep(61)
    sales = client.request("GET", f"{ApiDomain.STATISTICS}/api/v1/supplier/sales?dateFrom={date_from}&flag=0")
    return {
        "dateFrom": date_from,
        "stocks": summarize_plain(stocks),
        "orders": summarize_plain(orders),
        "sales": summarize_plain(sales),
    }


def feedbacks_snapshot(client: WbClient) -> dict[str, Any]:
    return {
        "feedbacksUnanswered": summarize_nested_list(
            client.request("GET", f"{ApiDomain.FEEDBACKS}/api/v1/feedbacks?isAnswered=false&take=100&skip=0"),
            ["data", "feedbacks"],
        ),
        "feedbacksAnswered": summarize_nested_list(
            client.request("GET", f"{ApiDomain.FEEDBACKS}/api/v1/feedbacks?isAnswered=true&take=100&skip=0"),
            ["data", "feedbacks"],
        ),
        "questionsUnanswered": summarize_nested_list(
            client.request("GET", f"{ApiDomain.FEEDBACKS}/api/v1/questions?isAnswered=false&take=100&skip=0"),
            ["data", "questions"],
        ),
        "questionsAnswered": summarize_nested_list(
            client.request("GET", f"{ApiDomain.FEEDBACKS}/api/v1/questions?isAnswered=true&take=100&skip=0"),
            ["data", "questions"],
        ),
    }


def advertising_snapshot(client: WbClient) -> dict[str, Any]:
    result = client.request("GET", f"{ApiDomain.ADVERTISING}/adv/v1/promotion/count")
    return summarize_plain(result)


def full_snapshot(client: WbClient) -> dict[str, Any]:
    return {
        "pings": ping_all(client),
        "content": content_snapshot(client),
        "prices": prices_snapshot(client),
        "marketplace": marketplace_snapshot(client),
        "statistics": statistics_snapshot(client),
        "feedbacks": feedbacks_snapshot(client),
        "advertising": advertising_snapshot(client),
    }


def summarize_cards(result: Any) -> dict[str, Any]:
    cards = _get(result.data, ["cards"], [])
    return {
        "status": result.status,
        "ok": result.ok,
        "count": len(cards) if isinstance(cards, list) else 0,
        "cursor": _get(result.data, ["cursor"], None),
        "sample": cards[:3] if isinstance(cards, list) else [],
        "bodyIfError": None if result.ok else result.data,
    }


def summarize_error_cards(result: Any) -> dict[str, Any]:
    items = _get(result.data, ["data", "items"], [])
    return {
        "status": result.status,
        "ok": result.ok,
        "count": len(items) if isinstance(items, list) else 0,
        "cursor": _get(result.data, ["data", "cursor"], None),
        "sample": items[:3] if isinstance(items, list) else [],
        "bodyIfError": None if result.ok else result.data,
    }


def summarize_nested_list(result: Any, path: list[str]) -> dict[str, Any]:
    items = _get(result.data, path, [])
    return {
        "status": result.status,
        "ok": result.ok,
        "count": len(items) if isinstance(items, list) else 0,
        "sample": items[:3] if isinstance(items, list) else [],
        "bodyIfError": None if result.ok else result.data,
    }


def summarize_list_response(result: Any, key: str) -> dict[str, Any]:
    items = result.data.get(key, []) if isinstance(result.data, dict) else []
    return {
        "status": result.status,
        "ok": result.ok,
        "count": len(items) if isinstance(items, list) else 0,
        "sample": items[:10] if isinstance(items, list) else [],
        "bodyIfError": None if result.ok else result.data,
    }


def summarize_plain(result: Any) -> dict[str, Any]:
    data = result.data
    count = len(data) if isinstance(data, list) else None
    return {"status": result.status, "ok": result.ok, "count": count, "data": data if not result.ok else data}


def _get(value: Any, path: list[str], default: Any) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current
