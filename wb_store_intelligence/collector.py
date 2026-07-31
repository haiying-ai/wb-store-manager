from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .client import ReadOnlyWbClient, ReadResult
from .config import StoreConfig


@dataclass(frozen=True)
class CollectionStatus:
    source: str
    status: int | None
    ok: bool
    records: int | None
    error: str | None = None


class BaselineCollector:
    def __init__(self, store: StoreConfig, output_dir: Path):
        self.store = store
        self.client = ReadOnlyWbClient(store)
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.statuses: list[CollectionStatus] = []

    def collect(self, *, days: int = 7) -> list[CollectionStatus]:
        cards = self._collect_cards()
        self._collect_simple(
            "seller-info",
            "GET",
            "https://common-api.wildberries.ru/api/v1/seller-info",
        )
        self._collect_simple(
            "jam-subscription",
            "GET",
            "https://common-api.wildberries.ru/api/common/v1/subscriptions",
        )
        self._collect_simple(
            "seller-rating",
            "GET",
            "https://feedbacks-api.wildberries.ru/api/common/v1/rating",
        )
        warehouses = self._collect_simple(
            "warehouses",
            "GET",
            "https://marketplace-api.wildberries.ru/api/v3/warehouses",
        )
        self._collect_prices()
        self._collect_funnel(days=days)
        self._collect_fbs_stocks(cards, warehouses)
        self._collect_wb_stocks()
        self._write_json(
            "manifest.json",
            {
                "store": self.store.alias,
                "role": self.store.role,
                "collectedAt": datetime.now(timezone.utc).isoformat(),
                "sources": [status.__dict__ for status in self.statuses],
            },
        )
        return self.statuses

    def _collect_simple(
        self, name: str, method: str, url: str, *, body: Any = None
    ) -> Any:
        try:
            result = self.client.request(method, url, body=body)
            records = self._record_count(result.data)
            self._save_result(name, result)
            self.statuses.append(
                CollectionStatus(name, result.status, result.ok, records)
            )
            return result.data if result.ok else None
        except Exception as exc:
            self.statuses.append(
                CollectionStatus(name, None, False, None, type(exc).__name__)
            )
            self._write_json(
                f"{name}.json",
                {
                    "store": self.store.alias,
                    "source": name,
                    "ok": False,
                    "errorType": type(exc).__name__,
                },
            )
            return None

    def _collect_cards(self) -> list[dict[str, Any]]:
        url = "https://content-api.wildberries.ru/content/v2/get/cards/list"
        cursor: dict[str, Any] = {"limit": 100}
        cards: list[dict[str, Any]] = []
        page_statuses: list[int] = []
        for _ in range(200):
            body = {
                "settings": {
                    "sort": {"ascending": True},
                    "filter": {"withPhoto": -1},
                    "cursor": cursor,
                }
            }
            result = self.client.request("POST", url, body=body)
            page_statuses.append(result.status)
            if not result.ok or not isinstance(result.data, dict):
                self._save_result("cards-list", result)
                self.statuses.append(
                    CollectionStatus(
                        "cards-list", result.status, False, len(cards)
                    )
                )
                return cards
            page_cards = result.data.get("cards") or []
            cards.extend(page_cards)
            response_cursor = result.data.get("cursor") or {}
            if len(page_cards) < 100:
                break
            updated_at = response_cursor.get("updatedAt")
            nm_id = response_cursor.get("nmID")
            if not updated_at or not nm_id:
                break
            cursor = {"limit": 100, "updatedAt": updated_at, "nmID": nm_id}

        self._write_json(
            "cards-list.json",
            {
                "store": self.store.alias,
                "source": "cards-list",
                "ok": all(200 <= status < 300 for status in page_statuses),
                "pageStatuses": page_statuses,
                "records": len(cards),
                "data": cards,
            },
        )
        self.statuses.append(CollectionStatus("cards-list", 200, True, len(cards)))
        return cards

    def _collect_prices(self) -> None:
        url = (
            "https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?"
            + urlencode({"limit": 1000, "offset": 0})
        )
        self._collect_simple("prices", "GET", url)

    def _collect_funnel(self, *, days: int) -> None:
        selected_end = date.today() - timedelta(days=1)
        selected_start = selected_end - timedelta(days=days - 1)
        past_end = selected_start - timedelta(days=1)
        past_start = past_end - timedelta(days=days - 1)
        body = {
            "selectedPeriod": {
                "start": selected_start.isoformat(),
                "end": selected_end.isoformat(),
            },
            "pastPeriod": {
                "start": past_start.isoformat(),
                "end": past_end.isoformat(),
            },
            "nmIds": [],
            "subjectIds": [],
            "tagIds": [],
            "orderBy": {"field": "openCard", "mode": "desc"},
            "limit": 1000,
            "offset": 0,
        }
        self._collect_simple(
            "sales-funnel-products",
            "POST",
            "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products",
            body=body,
        )

    def _collect_fbs_stocks(self, cards: list[dict[str, Any]], warehouses: Any) -> None:
        chrt_ids = sorted(
            {
                int(size["chrtID"])
                for card in cards
                for size in card.get("sizes", [])
                if size.get("chrtID")
            }
        )
        if not isinstance(warehouses, list) or not chrt_ids:
            self.statuses.append(
                CollectionStatus("fbs-stocks", None, False, 0, "missing-input")
            )
            return
        collected: list[dict[str, Any]] = []
        statuses: list[int] = []
        for warehouse in warehouses:
            warehouse_id = warehouse.get("id")
            if not warehouse_id:
                continue
            for start in range(0, len(chrt_ids), 1000):
                result = self.client.request(
                    "POST",
                    f"https://marketplace-api.wildberries.ru/api/v3/stocks/{warehouse_id}",
                    body={"chrtIds": chrt_ids[start : start + 1000]},
                )
                statuses.append(result.status)
                if result.ok and isinstance(result.data, dict):
                    for row in result.data.get("stocks", []):
                        collected.append({"warehouseId": warehouse_id, **row})
        ok = bool(statuses) and all(200 <= status < 300 for status in statuses)
        self._write_json(
            "fbs-stocks.json",
            {
                "store": self.store.alias,
                "source": "fbs-stocks",
                "ok": ok,
                "statuses": statuses,
                "records": len(collected),
                "data": collected,
            },
        )
        self.statuses.append(
            CollectionStatus(
                "fbs-stocks", statuses[-1] if statuses else None, ok, len(collected)
            )
        )

    def _collect_wb_stocks(self) -> None:
        self._collect_simple(
            "wb-warehouse-stocks",
            "POST",
            "https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses",
            body={"nmIds": [], "chrtIds": [], "limit": 250000, "offset": 0},
        )

    def _save_result(self, name: str, result: ReadResult) -> None:
        self._write_json(
            f"{name}.json",
            {
                "store": self.store.alias,
                "source": name,
                "status": result.status,
                "ok": result.ok,
                "collectedAt": datetime.now(timezone.utc).isoformat(),
                "data": result.data,
            },
        )

    def _write_json(self, name: str, payload: Any) -> None:
        (self.output_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _record_count(data: Any) -> int | None:
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            for key in ("products", "goods", "listGoods", "stocks", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    return len(value)
                if isinstance(value, dict):
                    for nested in ("products", "items", "listGoods"):
                        nested_value = value.get(nested)
                        if isinstance(nested_value, list):
                            return len(nested_value)
        return None
