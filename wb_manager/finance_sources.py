from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlencode

from .client import WbClient


FINANCE_DETAILED_URL = (
    "https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed"
)
STATISTICS_BASE_URL = "https://statistics-api.wildberries.ru"
ANALYTICS_BASE_URL = "https://seller-analytics-api.wildberries.ru"
ADVERTISING_BASE_URL = "https://advert-api.wildberries.ru"
SELLER_BALANCE_URL = "https://finance-api.wildberries.ru/api/v1/account/balance"


class FinanceSourceError(RuntimeError):
    """Invalid local configuration or a caller contract violation."""


class ApprovalRequiredError(FinanceSourceError):
    """A money-moving request did not pass its explicit approval gate."""


@dataclass(frozen=True)
class StoreSpec:
    """Credential-free description of one configured WB store."""

    alias: str
    token_env: str
    token_file: Path
    enabled: bool = True


@dataclass(frozen=True, repr=False)
class StoreCredentials:
    """Runtime credentials whose repr never exposes the token value."""

    store: StoreSpec
    token: str = field(repr=False)

    @property
    def alias(self) -> str:
        return self.store.alias

    def __repr__(self) -> str:
        return f"StoreCredentials(alias={self.alias!r}, token=<redacted>)"


@dataclass(frozen=True)
class RequestSpec:
    method: str
    url: str
    body: Mapping[str, Any] | None = None


@dataclass
class SourceResult:
    """Result contract shared by every read source.

    Empty rows are meaningful only when ``complete`` is true. A failed source
    always keeps its HTTP/transport status and any already-read partial rows.
    """

    source: str
    status: int | None
    complete: bool
    rows: list[dict[str, Any]]
    requests: list[dict[str, Any]] = field(default_factory=list)
    blocker: str | None = None


@dataclass(frozen=True)
class OrderMatch:
    matched: bool
    key: str | None = None
    value: str | None = None


@dataclass(frozen=True)
class LogisticsClassification:
    direction: str
    amount: Decimal
    blocker: str | None
    match: OrderMatch | None = None


class CampaignFundingSource(IntEnum):
    ACCOUNT = 0
    BALANCE = 1
    BONUS = 3


@dataclass
class CampaignDepositResult:
    campaign_id: int
    amount: int
    source: str
    approved_by: str
    status: int | None
    state: str
    requires_readback: bool
    pnl: bool = False
    response: Any = None
    error: str | None = None


RequestCallable = Callable[..., Any]
SleepCallable = Callable[[float], None]
GoodsReturnRequestBuilder = Callable[[date, date], RequestSpec]


def load_store_specs(
    scope: Mapping[str, Any] | str | Path,
    *,
    repo_root: str | Path,
) -> tuple[StoreSpec, ...]:
    """Parse every enabled store without reading or storing credential values."""

    root = Path(repo_root).resolve()
    data = _read_scope(scope, repo_root=root)
    raw_stores = data.get("stores")
    if not isinstance(raw_stores, list):
        raise FinanceSourceError("Store scope must contain a stores list")

    stores: list[StoreSpec] = []
    aliases: set[str] = set()
    token_envs: set[str] = set()
    for item in raw_stores:
        if not isinstance(item, Mapping):
            raise FinanceSourceError("Every store entry must be an object")
        forbidden = {
            str(key).lower()
            for key in item
            if str(key).lower() in {"token", "tokenvalue", "apikey", "apikeyvalue"}
        }
        if forbidden:
            raise FinanceSourceError("Store scope must not contain credential values")

        enabled = item.get("enabled", True) is not False and str(
            item.get("status", "enabled")
        ).lower() not in {"disabled", "inactive"}
        if not enabled:
            continue
        alias = str(item.get("alias") or "").strip()
        token_env = str(item.get("tokenEnv") or "").strip()
        token_file_raw = str(item.get("tokenFile") or "").strip()
        if not alias or not token_env or not token_file_raw:
            raise FinanceSourceError("Enabled stores require alias, tokenEnv and tokenFile")
        if alias in aliases:
            raise FinanceSourceError(f"Duplicate enabled store alias: {alias}")
        if token_env in token_envs:
            raise FinanceSourceError(f"Duplicate enabled store tokenEnv: {token_env}")
        aliases.add(alias)
        token_envs.add(token_env)
        token_file = Path(token_file_raw)
        if not token_file.is_absolute():
            token_file = root / token_file
        stores.append(
            StoreSpec(
                alias=alias,
                token_env=token_env,
                token_file=token_file.resolve(),
                enabled=True,
            )
        )
    return tuple(stores)


def resolve_store_credentials(
    store: StoreSpec,
    *,
    environ: Mapping[str, str] | None = None,
) -> StoreCredentials:
    """Resolve a token only from its named environment variable or env file."""

    environment = os.environ if environ is None else environ
    token = str(environment.get(store.token_env, "") or "").strip()
    if not token:
        token = _read_env_file(store.token_file).get(store.token_env, "").strip()
    if not token:
        raise FinanceSourceError(f"Token is unavailable for store {store.alias}")
    return StoreCredentials(store=store, token=token)


def resolve_enabled_store_credentials(
    scope: Mapping[str, Any] | str | Path,
    *,
    repo_root: str | Path,
    environ: Mapping[str, str] | None = None,
) -> tuple[StoreCredentials, ...]:
    return tuple(
        resolve_store_credentials(store, environ=environ)
        for store in load_store_specs(scope, repo_root=repo_root)
    )


class FinanceSources:
    """Read-only WB finance source adapter around :class:`WbClient`."""

    def __init__(
        self,
        client: WbClient,
        *,
        store_alias: str,
        requester: RequestCallable | None = None,
        sleep: SleepCallable = time.sleep,
        finance_throttle_seconds: float = 60.0,
        statistics_throttle_seconds: float = 60.0,
        goods_return_throttle_seconds: float = 60.0,
        chunk_throttle_seconds: float = 1.05,
        goods_return_request_builder: GoodsReturnRequestBuilder | None = None,
    ) -> None:
        if not store_alias.strip():
            raise FinanceSourceError("store_alias is required")
        self.client = client
        self.store_alias = store_alias.strip()
        self.requester = requester or client.request
        self.sleep = sleep
        self.finance_throttle_seconds = max(0.0, finance_throttle_seconds)
        self.statistics_throttle_seconds = max(0.0, statistics_throttle_seconds)
        self.goods_return_throttle_seconds = max(0.0, goods_return_throttle_seconds)
        self.chunk_throttle_seconds = max(0.0, chunk_throttle_seconds)
        self.goods_return_request_builder = (
            goods_return_request_builder or _default_goods_return_request
        )

    def finance_detailed(
        self,
        date_from: date | str,
        date_to: date | str,
        *,
        fields: Sequence[str] | None = None,
        limit: int = 100_000,
        max_pages: int = 10_000,
    ) -> SourceResult:
        if not 1 <= limit <= 100_000:
            raise FinanceSourceError("Finance detail limit must be between 1 and 100000")
        start, end = _validated_period(date_from, date_to)
        selected_fields = list(dict.fromkeys(fields or ()))
        if selected_fields and "rrdId" not in selected_fields:
            selected_fields.insert(0, "rrdId")

        rows: list[dict[str, Any]] = []
        requests: list[dict[str, Any]] = []
        seen_rrd_ids: set[str] = set()
        rrd_id = 0
        status: int | None = None
        for page in range(max_pages):
            if page:
                self.sleep(self.finance_throttle_seconds)
            body: dict[str, Any] = {
                "dateFrom": start.isoformat(),
                "dateTo": end.isoformat(),
                "limit": limit,
                "rrdId": rrd_id,
            }
            if selected_fields:
                body["fields"] = selected_fields
            response, error = self._call("POST", FINANCE_DETAILED_URL, body=body)
            status = _status(response)
            requests.append(
                {
                    "page": page + 1,
                    "status": status,
                    "rrdId": rrd_id,
                    "error": error,
                }
            )
            if error:
                return SourceResult(
                    "finance_detailed", status, False, rows, requests, error
                )
            if status == 204:
                return SourceResult("finance_detailed", status, True, rows, requests)
            if not _ok(response):
                return SourceResult(
                    "finance_detailed",
                    status,
                    False,
                    rows,
                    requests,
                    f"Finance detail request failed with HTTP {status}",
                )
            page_rows = _list_rows(_data(response))
            if page_rows is None:
                return SourceResult(
                    "finance_detailed",
                    status,
                    False,
                    rows,
                    requests,
                    "Finance detail response is not a row list",
                )
            if not page_rows:
                return SourceResult(
                    "finance_detailed",
                    status,
                    False,
                    rows,
                    requests,
                    "Finance detail returned HTTP 200 with no rows before terminal 204",
                )
            for row in page_rows:
                key = _identifier(row.get("rrdId"))
                if key and key not in seen_rrd_ids:
                    seen_rrd_ids.add(key)
                    rows.append(row)
                elif not key:
                    rows.append(row)
            last_id = _identifier(page_rows[-1].get("rrdId"))
            if not last_id or last_id == str(rrd_id):
                return SourceResult(
                    "finance_detailed",
                    status,
                    False,
                    rows,
                    requests,
                    "Finance detail pagination did not advance rrdId",
                )
            rrd_id = int(last_id)
        return SourceResult(
            "finance_detailed",
            status,
            False,
            rows,
            requests,
            f"Finance detail exceeded max_pages={max_pages}",
        )

    def statistics_sales(
        self, date_from: str, *, max_pages: int = 10_000
    ) -> SourceResult:
        return self._statistics_changes("sales", date_from, max_pages=max_pages)

    def statistics_orders(
        self, date_from: str, *, max_pages: int = 10_000
    ) -> SourceResult:
        return self._statistics_changes("orders", date_from, max_pages=max_pages)

    def _statistics_changes(
        self, kind: str, date_from: str, *, max_pages: int
    ) -> SourceResult:
        if kind not in {"sales", "orders"}:
            raise FinanceSourceError(f"Unsupported statistics source: {kind}")
        cursor = str(date_from or "").strip()
        if not cursor:
            raise FinanceSourceError("date_from is required for statistics changes")
        rows: list[dict[str, Any]] = []
        requests: list[dict[str, Any]] = []
        seen_events: set[str] = set()
        status: int | None = None
        for page in range(max_pages):
            if page:
                self.sleep(self.statistics_throttle_seconds)
            query = urlencode({"dateFrom": cursor, "flag": 0})
            url = f"{STATISTICS_BASE_URL}/api/v1/supplier/{kind}?{query}"
            response, error = self._call("GET", url)
            status = _status(response)
            requests.append(
                {
                    "page": page + 1,
                    "status": status,
                    "dateFrom": cursor,
                    "error": error,
                }
            )
            if error:
                return SourceResult(
                    f"statistics_{kind}", status, False, rows, requests, error
                )
            if status == 204:
                return SourceResult(f"statistics_{kind}", status, True, rows, requests)
            if not _ok(response):
                return SourceResult(
                    f"statistics_{kind}",
                    status,
                    False,
                    rows,
                    requests,
                    f"Statistics {kind} request failed with HTTP {status}",
                )
            page_rows = _list_rows(_data(response))
            if page_rows is None:
                return SourceResult(
                    f"statistics_{kind}",
                    status,
                    False,
                    rows,
                    requests,
                    f"Statistics {kind} response is not a row list",
                )
            if not page_rows:
                return SourceResult(f"statistics_{kind}", status, True, rows, requests)
            added_this_page = 0
            for row in page_rows:
                event_id = _stable_hash("statistics", self.store_alias, kind, row)
                if event_id not in seen_events:
                    seen_events.add(event_id)
                    rows.append(row)
                    added_this_page += 1
            next_cursor = str(page_rows[-1].get("lastChangeDate") or "").strip()
            if next_cursor == cursor and added_this_page == 0 and len(page_rows) < 80_000:
                # flag=0 is inclusive of dateFrom, so the terminal request may
                # repeat only the boundary row instead of returning [].
                return SourceResult(f"statistics_{kind}", status, True, rows, requests)
            if not next_cursor or next_cursor == cursor:
                return SourceResult(
                    f"statistics_{kind}",
                    status,
                    False,
                    rows,
                    requests,
                    f"Statistics {kind} pagination did not advance lastChangeDate",
                )
            cursor = next_cursor
        return SourceResult(
            f"statistics_{kind}",
            status,
            False,
            rows,
            requests,
            f"Statistics {kind} exceeded max_pages={max_pages}",
        )

    def goods_returns(
        self,
        date_from: date | str,
        date_to: date | str,
        *,
        request_builder: GoodsReturnRequestBuilder | None = None,
    ) -> SourceResult:
        builder = request_builder or self.goods_return_request_builder
        return self._chunked_rows(
            "goods_returns",
            date_from,
            date_to,
            builder,
            row_paths=(
                ("report",),
                ("data",),
                ("returns",),
                ("items",),
                ("data", "report"),
                ("data", "returns"),
                ("data", "items"),
            ),
            throttle_seconds=self.goods_return_throttle_seconds,
        )

    def advertising_costs(
        self, date_from: date | str, date_to: date | str
    ) -> SourceResult:
        raw = self._chunked_rows(
            "advertising_costs",
            date_from,
            date_to,
            lambda start, end: RequestSpec(
                "GET",
                f"{ADVERTISING_BASE_URL}/adv/v1/upd?"
                + urlencode({"from": start.isoformat(), "to": end.isoformat()}),
            ),
        )
        raw.rows = normalize_advertising_cost_events(raw.rows, self.store_alias)
        return raw

    def advertising_payments(
        self, date_from: date | str, date_to: date | str
    ) -> SourceResult:
        raw = self._chunked_rows(
            "advertising_payments",
            date_from,
            date_to,
            lambda start, end: RequestSpec(
                "GET",
                f"{ADVERTISING_BASE_URL}/adv/v1/payments?"
                + urlencode({"from": start.isoformat(), "to": end.isoformat()}),
            ),
        )
        raw.rows = normalize_advertising_payment_events(raw.rows, self.store_alias)
        return raw

    def advertising_balance(self) -> SourceResult:
        return self._single_object_source(
            "advertising_balance", f"{ADVERTISING_BASE_URL}/adv/v1/balance"
        )

    def seller_balance(self) -> SourceResult:
        return self._single_object_source("seller_balance", SELLER_BALANCE_URL)

    def _single_object_source(self, source: str, url: str) -> SourceResult:
        response, error = self._call("GET", url)
        status = _status(response)
        request = {"status": status, "error": error}
        if error:
            return SourceResult(source, status, False, [], [request], error)
        if status == 204:
            return SourceResult(source, status, True, [], [request])
        if not _ok(response):
            return SourceResult(
                source,
                status,
                False,
                [],
                [request],
                f"{source} request failed with HTTP {status}",
            )
        data = _data(response)
        if not isinstance(data, Mapping):
            return SourceResult(
                source,
                status,
                False,
                [],
                [request],
                f"{source} response is not an object",
            )
        return SourceResult(source, status, True, [dict(data)], [request])

    def _chunked_rows(
        self,
        source: str,
        date_from: date | str,
        date_to: date | str,
        request_builder: GoodsReturnRequestBuilder,
        *,
        row_paths: Sequence[Sequence[str]] = (),
        throttle_seconds: float | None = None,
    ) -> SourceResult:
        start, end = _validated_period(date_from, date_to)
        rows: list[dict[str, Any]] = []
        requests: list[dict[str, Any]] = []
        status: int | None = None
        delay = self.chunk_throttle_seconds if throttle_seconds is None else max(
            0.0, throttle_seconds
        )
        for index, (chunk_start, chunk_end) in enumerate(date_windows(start, end)):
            if index:
                self.sleep(delay)
            spec = request_builder(chunk_start, chunk_end)
            response, error = self._call(spec.method, spec.url, body=spec.body)
            status = _status(response)
            requests.append(
                {
                    "status": status,
                    "dateFrom": chunk_start.isoformat(),
                    "dateTo": chunk_end.isoformat(),
                    "error": error,
                }
            )
            if error:
                return SourceResult(source, status, False, rows, requests, error)
            if status == 204:
                continue
            if not _ok(response):
                return SourceResult(
                    source,
                    status,
                    False,
                    rows,
                    requests,
                    f"{source} request failed with HTTP {status}",
                )
            page_rows = _list_rows(_data(response), paths=row_paths)
            if page_rows is None:
                return SourceResult(
                    source,
                    status,
                    False,
                    rows,
                    requests,
                    f"{source} response does not match the configured row contract",
                )
            rows.extend(page_rows)
        return SourceResult(source, status, True, rows, requests)

    def _call(
        self, method: str, url: str, *, body: Mapping[str, Any] | None = None
    ) -> tuple[Any | None, str | None]:
        try:
            response = self.requester(
                method,
                url,
                body=dict(body) if body is not None else None,
                retries=0,
            )
            return response, None
        except Exception as exc:  # transport failures have no HTTP status
            return None, f"{type(exc).__name__}: {exc}"


def date_windows(
    date_from: date | str, date_to: date | str, *, maximum_days: int = 31
) -> tuple[tuple[date, date], ...]:
    if maximum_days <= 0:
        raise FinanceSourceError("maximum_days must be positive")
    start, end = _validated_period(date_from, date_to)
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=maximum_days - 1))
        windows.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return tuple(windows)


def order_identity(row: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Return order identity keys in preferred order without inventing a key."""

    keys: list[tuple[str, str]] = []
    for name in ("srid", "orderId", "shkId"):
        value = _identifier(row.get(name))
        if value:
            keys.append((name, value))
    return tuple(keys)


def match_order_rows(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> OrderMatch:
    left_store = str(left.get("storeAlias") or left.get("store_alias") or "").strip()
    right_store = str(right.get("storeAlias") or right.get("store_alias") or "").strip()
    if left_store and right_store and left_store != right_store:
        return OrderMatch(False)
    left_keys = dict(order_identity(left))
    right_keys = dict(order_identity(right))
    for name in ("srid", "orderId", "shkId"):
        if left_keys.get(name) and left_keys[name] == right_keys.get(name):
            return OrderMatch(True, name, left_keys[name])
    return OrderMatch(False)


def classify_logistics_row(
    row: Mapping[str, Any], *, linked_order: Mapping[str, Any] | None = None
) -> LogisticsClassification:
    """Classify a finance logistics row without assuming a fixed return fee."""

    try:
        amount = _decimal(row.get("deliveryService")) + _decimal(
            row.get("rebillLogisticCost")
        )
    except FinanceSourceError as exc:
        return LogisticsClassification("unknown", Decimal("0"), str(exc))

    marker = " ".join(
        str(row.get(name) or "").strip().casefold()
        for name in ("sellerOperName", "docTypeName", "bonusTypeName")
    )
    forward_markers = (
        "销售时给客户",
        "к клиенту при продаже",
        "to customer on sale",
    )
    return_markers = (
        "取消",
        "退货",
        "从客户返回",
        "возврат",
        "отмена",
        "от клиент",
        "return",
        "cancel",
    )
    has_forward = any(value in marker for value in forward_markers)
    has_return = any(value in marker for value in return_markers)
    match = match_order_rows(row, linked_order) if linked_order is not None else None
    if has_return and not has_forward:
        return LogisticsClassification("return", amount, None, match)
    if has_forward and not has_return:
        return LogisticsClassification("forward", amount, None, match)
    try:
        return_count = _decimal(row.get("returnAmount"))
    except FinanceSourceError as exc:
        return LogisticsClassification("unknown", amount, str(exc), match)
    if return_count != 0:
        return LogisticsClassification("return", amount, None, match)
    return LogisticsClassification(
        "unknown",
        amount,
        "Logistics direction is not evidenced; actual fee is retained but not assigned",
        match,
    )


def normalize_advertising_cost_events(
    rows: Iterable[Mapping[str, Any]], store_alias: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in rows:
        amount = _decimal_text(row.get("updSum"))
        identity = {
            "storeAlias": store_alias,
            "advertId": row.get("advertId"),
            "updNum": row.get("updNum"),
            "updTime": row.get("updTime"),
            "updSum": amount,
            "paymentType": row.get("paymentType"),
            "campName": row.get("campName"),
        }
        events.append(
            {
                "eventId": _stable_hash("advertising_expense", identity),
                "storeAlias": store_alias,
                "eventType": "advertising_expense",
                "source": "adv/v1/upd",
                "pnl": True,
                "amount": amount,
                "advertId": row.get("advertId"),
                "occurredAt": row.get("updTime"),
                "paymentType": row.get("paymentType"),
                "raw": dict(row),
            }
        )
    return events


def normalize_advertising_payment_events(
    rows: Iterable[Mapping[str, Any]], store_alias: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in rows:
        identity = {"storeAlias": store_alias, **dict(row)}
        events.append(
            {
                "eventId": _stable_hash("advertising_funding", identity),
                "storeAlias": store_alias,
                "eventType": "advertising_funding",
                "source": "adv/v1/payments",
                "pnl": False,
                "raw": dict(row),
            }
        )
    return events


def advertising_pnl_events(
    *groups: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return unique actual-consumption events; funding is never a P&L cost."""

    selected: dict[str, dict[str, Any]] = {}
    for event in (item for group in groups for item in group):
        if event.get("pnl") is not True or event.get("eventType") != "advertising_expense":
            continue
        event_id = str(event.get("eventId") or "").strip()
        if not event_id:
            event_id = _stable_hash("advertising_expense_fallback", event)
        selected.setdefault(event_id, dict(event))
    return list(selected.values())


def deposit_campaign_budget(
    client: WbClient,
    *,
    campaign_id: int,
    amount: int,
    source: CampaignFundingSource | str | int,
    approved_by: str,
    confirm_live: bool,
    requester: RequestCallable | None = None,
) -> CampaignDepositResult:
    """Perform exactly one approved campaign-budget deposit with no retry.

    A transport error is deliberately reported as ``unknown`` because the API
    may have committed the transfer before the connection failed. The caller
    must read the campaign budget before deciding whether another request is
    safe.
    """

    if confirm_live is not True:
        raise ApprovalRequiredError("confirm_live=True is required for a campaign deposit")
    approver = str(approved_by or "").strip()
    if not approver:
        raise ApprovalRequiredError("approved_by is required for a campaign deposit")
    if isinstance(campaign_id, bool) or int(campaign_id) <= 0:
        raise FinanceSourceError("campaign_id must be a positive integer")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise FinanceSourceError("amount must be a positive integer")
    funding_source = _funding_source(source)
    request = requester or client.request
    source_name = funding_source.name.lower()
    body = {"sum": amount, "type": int(funding_source), "return": True}
    url = f"{ADVERTISING_BASE_URL}/adv/v1/budget/deposit?id={int(campaign_id)}"
    try:
        response = request("POST", url, body=body, retries=0)
    except Exception as exc:
        return CampaignDepositResult(
            campaign_id=int(campaign_id),
            amount=amount,
            source=source_name,
            approved_by=approver,
            status=None,
            state="unknown",
            requires_readback=True,
            error=f"{type(exc).__name__}: {exc}",
        )
    status = _status(response)
    if _ok(response):
        return CampaignDepositResult(
            campaign_id=int(campaign_id),
            amount=amount,
            source=source_name,
            approved_by=approver,
            status=status,
            state="succeeded",
            requires_readback=True,
            response=_data(response),
        )
    ambiguous = status is None or status >= 500
    return CampaignDepositResult(
        campaign_id=int(campaign_id),
        amount=amount,
        source=source_name,
        approved_by=approver,
        status=status,
        state="unknown" if ambiguous else "failed",
        requires_readback=ambiguous,
        response=_data(response),
        error=f"Campaign deposit returned HTTP {status}",
    )


def _default_goods_return_request(start: date, end: date) -> RequestSpec:
    return RequestSpec(
        "GET",
        f"{ANALYTICS_BASE_URL}/api/v1/analytics/goods-return?"
        + urlencode({"dateFrom": start.isoformat(), "dateTo": end.isoformat()}),
    )


def _read_scope(
    scope: Mapping[str, Any] | str | Path, *, repo_root: Path
) -> Mapping[str, Any]:
    if isinstance(scope, Mapping):
        return scope
    path = Path(scope)
    if not path.is_absolute():
        path = repo_root / path
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, Mapping):
        raise FinanceSourceError("Store scope JSON must contain an object")
    return data


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _validated_period(date_from: date | str, date_to: date | str) -> tuple[date, date]:
    start = date_from if isinstance(date_from, date) else date.fromisoformat(str(date_from))
    end = date_to if isinstance(date_to, date) else date.fromisoformat(str(date_to))
    if end < start:
        raise FinanceSourceError("date_to must be on or after date_from")
    return start, end


def _list_rows(
    data: Any, *, paths: Sequence[Sequence[str]] = ()
) -> list[dict[str, Any]] | None:
    if isinstance(data, list):
        return [dict(row) for row in data if isinstance(row, Mapping)]
    if isinstance(data, Mapping):
        for path in paths:
            value: Any = data
            for key in path:
                value = value.get(key) if isinstance(value, Mapping) else None
            if isinstance(value, list):
                return [dict(row) for row in value if isinstance(row, Mapping)]
    return None


def _status(response: Any | None) -> int | None:
    return getattr(response, "status", None) if response is not None else None


def _ok(response: Any | None) -> bool:
    return bool(getattr(response, "ok", False)) if response is not None else False


def _data(response: Any | None) -> Any:
    return getattr(response, "data", None) if response is not None else None


def _identifier(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FinanceSourceError(f"Invalid monetary value: {value!r}") from exc


def _decimal_text(value: Any) -> str:
    amount = _decimal(value)
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _stable_hash(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _funding_source(value: CampaignFundingSource | str | int) -> CampaignFundingSource:
    if isinstance(value, CampaignFundingSource):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        aliases = {
            "account": CampaignFundingSource.ACCOUNT,
            "balance": CampaignFundingSource.BALANCE,
            "bonus": CampaignFundingSource.BONUS,
            "bonuses": CampaignFundingSource.BONUS,
        }
        if normalized in aliases:
            return aliases[normalized]
    if not isinstance(value, bool):
        try:
            return CampaignFundingSource(int(value))
        except (TypeError, ValueError):
            pass
    raise FinanceSourceError("source must be account, balance or bonus")
