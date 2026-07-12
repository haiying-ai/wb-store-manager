from __future__ import annotations

from typing import Any


def build_summary(snapshot: dict[str, Any]) -> str:
    lines = []
    pings = snapshot.get("pings", [])
    ok_pings = sum(1 for item in pings if item.get("ok"))
    lines.append(f"API domains reachable: {ok_pings}/{len(pings)}")

    content = snapshot.get("content", {})
    lines.append(f"Product cards: {_count(content, 'cards')}")
    lines.append(f"Cards in trash: {_count(content, 'trash')}")
    lines.append(f"Card errors: {_count(content, 'errors')}")

    prices = snapshot.get("prices", {})
    lines.append(f"Price goods: {_count(prices, 'goods')}")
    lines.append(f"Price quarantine: {_count(prices, 'quarantine')}")

    marketplace = snapshot.get("marketplace", {})
    lines.append(_status_line("Marketplace warehouses", marketplace.get("warehouses")))
    lines.append(_status_line("FBS new orders", marketplace.get("newOrders")))
    lines.append(_status_line("FBS recent orders", marketplace.get("orders")))

    statistics = snapshot.get("statistics", {})
    lines.append(_status_line("Statistic stocks", statistics.get("stocks")))
    lines.append(_status_line("Statistic orders", statistics.get("orders")))
    lines.append(_status_line("Statistic sales", statistics.get("sales")))

    feedbacks = snapshot.get("feedbacks", {})
    lines.append(f"Unanswered feedbacks: {_count(feedbacks, 'feedbacksUnanswered')}")
    lines.append(f"Unanswered questions: {_count(feedbacks, 'questionsUnanswered')}")

    advertising = snapshot.get("advertising", {})
    ad_data = advertising.get("data") if isinstance(advertising, dict) else None
    if isinstance(ad_data, dict) and "all" in ad_data:
        lines.append(f"Advertising campaigns: {ad_data['all']}")
    else:
        lines.append(_status_line("Advertising campaigns", advertising))

    return "\n".join(lines)


def _count(parent: dict[str, Any], key: str) -> Any:
    value = parent.get(key, {})
    if isinstance(value, dict):
        return value.get("count", "unknown")
    return "unknown"


def _status_line(label: str, value: Any) -> str:
    if not isinstance(value, dict):
        return f"{label}: unknown"
    if value.get("ok"):
        count = value.get("count")
        return f"{label}: {count if count is not None else 'ok'}"
    status = value.get("status", "unknown")
    body = value.get("data") or value.get("bodyIfError")
    if isinstance(body, dict):
        message = body.get("message") or body.get("detail") or body.get("title")
        if message:
            return f"{label}: unavailable ({status}, {message})"
    return f"{label}: unavailable ({status})"
