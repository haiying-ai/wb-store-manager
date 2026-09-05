from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from .client import WbClient
from .finance_ledger import FinanceLedger, canonical_json
from .finance_sources import (
    FinanceSources,
    SourceResult,
    classify_logistics_row,
    load_store_specs,
    resolve_store_credentials,
)


MOSCOW = ZoneInfo("Europe/Moscow")
SHANGHAI = ZoneInfo("Asia/Shanghai")
PRIMARY_SOURCES = ("finance_detailed", "advertising_costs", "seller_balance")
PERIOD_SOURCES = (
    "finance_detailed",
    "statistics_sales",
    "statistics_orders",
    "goods_returns",
    "advertising_costs",
    "advertising_payments",
)
BALANCE_SOURCES = ("seller_balance", "advertising_balance")


class FinanceSyncError(ValueError):
    pass


@dataclass(frozen=True)
class StoreSyncSummary:
    store_alias: str
    requested_from: date
    requested_through: date
    sources: tuple[dict[str, Any], ...]

    @property
    def primary_complete(self) -> bool:
        by_name = {str(item["source"]): bool(item["complete"]) for item in self.sources}
        return all(by_name.get(source, False) for source in PRIMARY_SOURCES)

    def as_dict(self) -> dict[str, Any]:
        return {
            "storeAlias": self.store_alias,
            "requestedFrom": self.requested_from.isoformat(),
            "requestedThrough": self.requested_through.isoformat(),
            "primaryComplete": self.primary_complete,
            "sources": list(self.sources),
        }


def read_registry(path: str | Path) -> dict[str, Any]:
    registry_path = Path(path)
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise FinanceSyncError(f"Finance store registry is missing: {registry_path}") from exc
    except json.JSONDecodeError as exc:
        raise FinanceSyncError(f"Finance store registry is invalid JSON: {registry_path}") from exc
    if not isinstance(data, dict) or data.get("status") != "active":
        raise FinanceSyncError("Finance store registry must be an active JSON object")
    if data.get("reportingCurrency") != "CNY":
        raise FinanceSyncError("Finance store registry reportingCurrency must be CNY")
    if data.get("financialTimezone") != "Europe/Moscow":
        raise FinanceSyncError("Finance periods must use Europe/Moscow")
    return data


def register_enabled_stores(
    ledger: FinanceLedger,
    registry: Mapping[str, Any],
    *,
    registry_path: str | Path,
    repo_root: str | Path,
) -> tuple[str, ...]:
    specs = load_store_specs(registry, repo_root=repo_root)
    raw_by_alias = {
        str(item.get("alias")): dict(item)
        for item in registry.get("stores", [])
        if isinstance(item, Mapping)
    }
    aliases: list[str] = []
    for spec in specs:
        metadata = raw_by_alias.get(spec.alias, {})
        metadata.pop("token", None)
        metadata.pop("apiKey", None)
        ledger.register_store(
            spec.alias,
            scope_id=str(registry.get("registryId") or Path(registry_path).stem),
            currency="CNY",
            active=True,
            metadata=metadata,
        )
        aliases.append(spec.alias)
    return tuple(aliases)


def resolve_through(value: str | date, *, now: datetime | None = None) -> date:
    if isinstance(value, date):
        return value
    raw = str(value).strip().lower()
    if raw == "yesterday":
        current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
        return current.date() - timedelta(days=1)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise FinanceSyncError(f"Invalid --through date: {value!r}") from exc


def determine_sync_start(
    ledger: FinanceLedger,
    *,
    store_alias: str,
    through: date,
    registry: Mapping[str, Any],
    bootstrap: bool,
) -> date:
    raw_store = next(
        (
            item
            for item in registry.get("stores", [])
            if isinstance(item, Mapping) and str(item.get("alias")) == store_alias
        ),
        {},
    )
    history_start = date.fromisoformat(str(registry.get("officialHistoryStart") or "2024-01-29"))
    opened = raw_store.get("openedAt")
    if opened:
        history_start = max(history_start, date.fromisoformat(str(opened)[:10]))
    trim_gaps = getattr(ledger, "trim_open_gaps_before", None)
    if trim_gaps is not None:
        trim_gaps(store_alias, _period_start(history_start))
    if bootstrap:
        return history_start

    candidates = [through]
    gaps = ledger.list_open_gaps(store_alias)
    for gap in gaps:
        raw = str(gap["gap_from"])
        candidates.append(date.fromisoformat(raw[:10]))
    watermark = ledger.get_watermark(store_alias, "finance_detailed")
    if watermark:
        watermark_date = date.fromisoformat(str(watermark)[:10])
        candidates.append(watermark_date + timedelta(days=1))
        # Re-read a rolling window so delayed refunds and WB corrections can
        # restate older orders without waiting for a manual backfill.
        candidates.append(through - timedelta(days=119))
    else:
        candidates.append(history_start)
    return max(history_start, min(candidates))


def sync_enabled_stores(
    ledger: FinanceLedger,
    *,
    registry_path: str | Path,
    repo_root: str | Path,
    through: str | date,
    bootstrap: bool,
    source_factory: Callable[[WbClient, str], FinanceSources] | None = None,
    now: datetime | None = None,
) -> tuple[StoreSyncSummary, ...]:
    root = Path(repo_root).resolve()
    registry = read_registry(registry_path)
    register_enabled_stores(
        ledger, registry, registry_path=registry_path, repo_root=root
    )
    end = resolve_through(through, now=now)
    if end >= (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI).date():
        raise FinanceSyncError("Finance synchronization can run only through yesterday")

    summaries: list[StoreSyncSummary] = []
    for spec in load_store_specs(registry, repo_root=root):
        start = determine_sync_start(
            ledger,
            store_alias=spec.alias,
            through=end,
            registry=registry,
            bootstrap=bootstrap,
        )
        if end < start:
            raise FinanceSyncError(
                f"Sync period for {spec.alias} ends before its history start: {start}"
            )
        credentials = resolve_store_credentials(spec)
        client = WbClient(credentials.token)
        sources = (
            source_factory(client, spec.alias)
            if source_factory is not None
            else FinanceSources(client, store_alias=spec.alias)
        )
        rows: list[dict[str, Any]] = []
        stats_start = max(start, end - timedelta(days=89))
        invocations: tuple[tuple[str, Callable[[], SourceResult]], ...] = (
            ("finance_detailed", lambda s=sources: s.finance_detailed(start, end)),
            (
                "statistics_sales",
                lambda s=sources: s.statistics_sales(f"{stats_start.isoformat()}T00:00:00"),
            ),
            (
                "statistics_orders",
                lambda s=sources: s.statistics_orders(f"{stats_start.isoformat()}T00:00:00"),
            ),
            ("goods_returns", lambda s=sources: s.goods_returns(start, end)),
            ("advertising_costs", lambda s=sources: s.advertising_costs(start, end)),
            ("advertising_payments", lambda s=sources: s.advertising_payments(start, end)),
            ("seller_balance", lambda s=sources: s.seller_balance()),
            ("advertising_balance", lambda s=sources: s.advertising_balance()),
        )
        for source_name, invoke in invocations:
            run_id = ledger.start_sync(
                spec.alias,
                source_name,
                _period_start(start),
                _period_end(end),
                started_at=now,
            )
            try:
                result = invoke()
            except Exception as exc:  # A source failure must not imply a zero value.
                result = SourceResult(
                    source=source_name,
                    status=None,
                    complete=False,
                    rows=[],
                    blocker=f"{type(exc).__name__}: {exc}",
                )
            inserted = _persist_source_rows(
                ledger, spec.alias, source_name, result.rows, fetched_at=now
            )
            complete = bool(result.complete)
            ledger.finish_sync(
                run_id,
                "complete" if complete else "failed",
                watermark_at=_period_end(end) if complete else None,
                rows_seen=len(result.rows),
                rows_inserted=inserted,
                rows_updated=max(0, len(result.rows) - inserted),
                error_text=result.blocker,
                completed_at=now,
            )
            snapshot_hash = hashlib.sha256(
                canonical_json(
                    {
                        "source": source_name,
                        "complete": complete,
                        "rows": result.rows,
                    }
                ).encode("utf-8")
            ).hexdigest()
            _record_coverage_if_supported(
                ledger,
                store_alias=spec.alias,
                source=source_name,
                period_start=_period_start(start),
                period_end=_period_end(end),
                coverage_status="complete" if complete else "missing",
                snapshot_hash=snapshot_hash,
                row_count=len(result.rows),
                sync_run_id=run_id,
                captured_at=now,
                metadata={"blocker": result.blocker, "httpStatus": result.status},
            )
            rows.append(
                {
                    "source": source_name,
                    "complete": complete,
                    "status": result.status,
                    "rows": len(result.rows),
                    "inserted": inserted,
                    "blocker": result.blocker,
                    "snapshotSha256": snapshot_hash,
                }
            )
        summaries.append(
            StoreSyncSummary(
                store_alias=spec.alias,
                requested_from=start,
                requested_through=end,
                sources=tuple(rows),
            )
        )
    return tuple(summaries)


def _persist_source_rows(
    ledger: FinanceLedger,
    store_alias: str,
    source: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    fetched_at: datetime | None,
) -> int:
    inserted = 0
    for index, row in enumerate(rows):
        payload = dict(row)
        if source == "finance_detailed":
            result = _upsert_finance_row(ledger, store_alias, payload, fetched_at=fetched_at)
        elif source in {"statistics_sales", "statistics_orders"}:
            identity = _event_identity(payload, fallback=f"{source}:{index}")
            occurred = _event_time(
                payload, "lastChangeDate", "sale_dt", "saleDt", "date", "orderDt"
            )
            result = ledger.upsert_sale(
                store_alias,
                identity,
                payload,
                occurred_at=occurred,
                sale_id=_first(payload, "saleID", "saleId", "odid"),
                order_id=_first(payload, "orderId", "odid"),
                srid=_first(payload, "srid"),
                buyer_paid_cny=_first(
                    payload, "finishedPrice", "priceWithDisc", "retailPriceWithDisc"
                ),
                for_pay_cny=_first(payload, "forPay", "totalPrice"),
                fetched_at=fetched_at,
            )
        elif source == "goods_returns":
            identity = _event_identity(payload, fallback=f"goods_returns:{index}")
            result = ledger.upsert_return(
                store_alias,
                identity,
                payload,
                occurred_at=_event_time(
                    payload,
                    "returnDate",
                    "lastChangeDate",
                    "date",
                    "saleDt",
                    "orderDt",
                ),
                return_id=_first(payload, "returnId", "id", "saleID", "saleId"),
                order_id=_first(payload, "orderId", "odid"),
                srid=_first(payload, "srid"),
                shk_id=_first(payload, "shkId", "stickerId"),
                actual_logistics_cny=None,
                refund_cny=_first(payload, "refund", "returnPrice", "finishedPrice"),
                fetched_at=fetched_at,
            )
        elif source in {"advertising_costs", "advertising_payments"}:
            identity = {"eventId": payload.get("eventId") or f"{source}:{index}"}
            result = ledger.upsert_ad(
                store_alias,
                identity,
                payload,
                occurred_at=_event_time(
                    payload,
                    "occurredAt",
                    "updTime",
                    "date",
                ),
                campaign_id=_first(payload, "advertId", "advert_id", "campaignId"),
                nm_id=_first(payload, "nmId", "nm_id"),
                spend_cny=payload.get("amount") if payload.get("pnl") is True else None,
                fetched_at=fetched_at,
            )
        elif source in BALANCE_SOURCES:
            observed = fetched_at or datetime.now(timezone.utc)
            account, withdrawable, ad_balance = _balance_amounts(payload, source)
            result = ledger.upsert_balance(
                store_alias,
                {
                    "source": source,
                    "observedDate": observed.astimezone(SHANGHAI).date().isoformat(),
                    "payloadSha256": hashlib.sha256(
                        canonical_json(payload).encode("utf-8")
                    ).hexdigest(),
                },
                {"source": source, **payload},
                observed_at=observed,
                account_balance_cny=account,
                withdrawable_balance_cny=withdrawable,
                ad_balance_cny=ad_balance,
                fetched_at=fetched_at,
            )
        else:
            continue
        inserted += int(result.inserted)
    return inserted


def _upsert_finance_row(
    ledger: FinanceLedger,
    store_alias: str,
    row: Mapping[str, Any],
    *,
    fetched_at: datetime | None,
):
    rrd_id = _first(row, "rrdId", "rrd_id")
    if rrd_id is None:
        raise FinanceSyncError("Finance detail row has no rrdId")
    buyer = _signed_buyer_paid(row)
    for_pay = _signed_amount(row.get("forPay"), row)
    acquiring = _money(row.get("acquiringFee"))
    commission_magnitude = max(
        Decimal("0"),
        abs(buyer) - abs(for_pay) - abs(acquiring),
    )
    commission = -commission_magnitude if buyer < 0 else commission_magnitude
    logistics = Decimal("0")
    return_logistics = Decimal("0")
    if _looks_like_logistics(row):
        classified = classify_logistics_row(row)
        if classified.blocker is None and classified.direction == "forward":
            logistics = classified.amount
        elif classified.blocker is None and classified.direction == "return":
            return_logistics = classified.amount
    other = sum(
        (
            _money(row.get(key))
            for key in ("paidStorage", "deduction", "paidAcceptance", "penalty")
        ),
        Decimal("0"),
    ) - _money(row.get("additionalPayment"))
    return ledger.upsert_finance(
        store_alias,
        rrd_id,
        row,
        order_id=_first(row, "orderId"),
        srid=_first(row, "srid"),
        shk_id=_first(row, "shkId", "stickerId"),
        occurred_at=_event_time(row, "rrDate", "saleDt", "createDate", "orderDt"),
        buyer_paid_cny=buyer,
        for_pay_cny=for_pay,
        commission_cny=commission,
        logistics_cny=logistics,
        return_logistics_cny=return_logistics,
        acquiring_fee_cny=acquiring,
        other_cost_cny=other,
        fetched_at=fetched_at,
    )


def _record_coverage_if_supported(ledger: FinanceLedger, **values: Any) -> None:
    method = getattr(ledger, "record_source_coverage", None)
    if method is None:
        return
    method(**values)


def _event_identity(row: Mapping[str, Any], *, fallback: str) -> dict[str, Any]:
    for key in ("srid", "orderId", "odid", "shkId", "saleID", "saleId", "id"):
        value = row.get(key)
        if value not in (None, ""):
            return {key: str(value), "lastChangeDate": row.get("lastChangeDate")}
    return {"fallback": fallback, "payload": dict(row)}


def _event_time(
    row: Mapping[str, Any],
    *keys: str,
    fallback: datetime | str | None = None,
) -> str:
    candidates = [row]
    nested_raw = row.get("raw")
    if isinstance(nested_raw, Mapping):
        candidates.append(nested_raw)
    for candidate in candidates:
        for key in keys:
            raw = candidate.get(key)
            parsed = _parse_event_time(raw)
            if parsed is not None:
                return parsed.isoformat()
    parsed_fallback = _parse_event_time(fallback)
    if parsed_fallback is not None:
        return parsed_fallback.isoformat()
    raise FinanceSyncError("Source row has no usable event timestamp")


def _parse_event_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(raw), time.min)
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW)
    return parsed


def _signed_buyer_paid(row: Mapping[str, Any]) -> Decimal:
    value = _money(
        _first(row, "retailPriceWithDisc", "retailAmount", "finishedPrice", "priceWithDisc")
    )
    return _signed_amount(value, row)


def _signed_amount(value: Any, row: Mapping[str, Any]) -> Decimal:
    amount = _money(value)
    marker = " ".join(
        str(row.get(key) or "").casefold()
        for key in ("docTypeName", "sellerOperName", "saleID", "saleId")
    )
    if any(token in marker for token in ("退货", "返还", "return", "возврат")):
        return -abs(amount)
    return amount


def _looks_like_logistics(row: Mapping[str, Any]) -> bool:
    marker = " ".join(
        str(row.get(key) or "").casefold()
        for key in ("sellerOperName", "bonusTypeName")
    )
    return any(
        (
            "物流" in marker,
            "логист" in marker,
            _money(row.get("deliveryService")) != 0,
            _money(row.get("rebillLogisticCost")) != 0,
        )
    )


def _balance_amounts(
    row: Mapping[str, Any], source: str
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if source == "advertising_balance":
        value = _money(_first(row, "balance", "net", "sum", "amount"))
        return None, None, value
    account_key, account_raw = _first_item(
        row, "balance", "current", "currentBalance", "accountBalance", "total"
    )
    withdrawable_key, withdrawable_raw = _first_item(
        row, "available", "withdrawable", "for_withdraw", "forWithdraw", "balance"
    )
    account = _seller_balance_money(row, account_key, account_raw)
    withdrawable = _seller_balance_money(row, withdrawable_key, withdrawable_raw)
    return account, withdrawable, None


def _seller_balance_money(
    row: Mapping[str, Any], field: str | None, value: Any
) -> Decimal | None:
    amount = _optional_money(value)
    if amount is None:
        return None
    # The seller-balance endpoint currently emits CNY ``current`` and
    # ``for_withdraw`` as integer fen even though finance-report line items are
    # decimal CNY.  This is observable in live evidence (for example 269163
    # corresponds to the 2691.63 settlement balance).  Limit the conversion to
    # these endpoint-specific fields so generic/test balance aliases and other
    # currencies retain their documented units.
    currency = str(row.get("currency") or "").strip().casefold()
    if currency == "cny" and field in {"current", "for_withdraw"}:
        return amount / Decimal("100")
    return amount


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _first_item(row: Mapping[str, Any], *keys: str) -> tuple[str | None, Any]:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return key, row.get(key)
    return None, None


def _money(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise FinanceSyncError(f"Invalid platform amount: {value!r}") from exc
    if not result.is_finite():
        raise FinanceSyncError(f"Invalid platform amount: {value!r}")
    return result


def _optional_money(value: Any) -> Decimal | None:
    return None if value in (None, "") else _money(value)


def _period_start(day: date) -> str:
    return datetime.combine(day, time.min, tzinfo=MOSCOW).isoformat()


def _period_end(day: date) -> str:
    return datetime.combine(day, time.max, tzinfo=MOSCOW).isoformat()
