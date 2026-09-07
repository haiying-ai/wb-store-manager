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
from .finance_ledger import FinanceLedger, canonical_json, normalize_iso
from .finance_parallel import build_store_fetch_plans, fetch_store_sources_bounded
from .finance_sources import (
    FinanceSources,
    SourceResult,
    classify_logistics_row,
    load_store_specs,
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
# Payment/readback rows settle the separate advertising-debt ledger.  They are
# not an order P&L input and therefore must not force every historical weekly
# or monthly calculation to run again.
REPORT_RELEVANT_SOURCES = (
    "finance_detailed",
    "statistics_sales",
    "statistics_orders",
    "goods_returns",
    "advertising_costs",
)
BALANCE_SOURCES = ("seller_balance", "advertising_balance")
SYNC_SOURCES = (
    "finance_detailed",
    "statistics_sales",
    "statistics_orders",
    "goods_returns",
    "advertising_costs",
    "advertising_payments",
    "seller_balance",
    "advertising_balance",
)


class FinanceSyncError(ValueError):
    pass


@dataclass(frozen=True)
class StoreSyncSummary:
    store_alias: str
    requested_from: date
    requested_through: date
    sources: tuple[dict[str, Any], ...]
    changed: bool = False
    report_relevant_changed: bool = False
    coverage_changed_sources: tuple[str, ...] = ()

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
            "changed": self.changed,
            "reportRelevantChanged": self.report_relevant_changed,
            "coverageChangedSources": list(self.coverage_changed_sources),
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
    correction_window_days: int = 120,
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
        # restate older orders without waiting for a manual backfill.  The
        # scheduled daily path supplies a shorter window on ordinary days;
        # Monday/month-start and explicit bootstrap runs retain the full
        # historical repair window.
        try:
            window = max(1, int(correction_window_days))
        except (TypeError, ValueError):
            window = 120
        candidates.append(through - timedelta(days=window - 1))
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
    correction_window_days: int = 120,
    max_store_workers: int = 2,
    source_factory: Callable[[WbClient, str], FinanceSources] | None = None,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[StoreSyncSummary, ...]:
    root = Path(repo_root).resolve()
    registry = read_registry(registry_path)
    register_enabled_stores(
        ledger, registry, registry_path=registry_path, repo_root=root
    )
    end = resolve_through(through, now=now)
    if end >= (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI).date():
        raise FinanceSyncError("Finance synchronization can run only through yesterday")

    # Phase-3 coordinator: perform API reads in bounded, token-isolated
    # workers and persist the returned snapshots below in one SQLite thread.
    return _sync_enabled_stores_bounded(
        ledger,
        registry=registry,
        specs=load_store_specs(registry, repo_root=root),
        end=end,
        bootstrap=bootstrap,
        correction_window_days=correction_window_days,
        max_store_workers=max_store_workers,
        source_factory=source_factory,
        now=now,
        environ=environ,
    )


def _sync_enabled_stores_bounded(*args: Any, **kwargs: Any) -> tuple[StoreSyncSummary, ...]:
    """Run the bounded coordinator and close any audit rows on fatal errors.

    Source-level failures are represented in their normal result contract.  A
    malformed result, alias mismatch, or ledger coverage exception can still
    escape the coordinator, though; this wrapper prevents those exceptional
    paths from leaving pre-created ``sync_runs`` stuck in ``running``.
    """

    before = _running_sync_run_ids(args[0] if args else kwargs.get("ledger"))
    try:
        return _sync_enabled_stores_bounded_impl(*args, **kwargs)
    except Exception as exc:
        ledger = args[0] if args else kwargs.get("ledger")
        after = _running_sync_run_ids(ledger)
        _fail_running_sync_runs(
            ledger,
            after - before,
            blocker=f"{type(exc).__name__}: {exc}",
            completed_at=kwargs.get("now"),
        )
        raise


def _sync_enabled_stores_bounded_impl(
    ledger: FinanceLedger,
    *,
    registry: Mapping[str, Any],
    specs: tuple[Any, ...],
    end: date,
    bootstrap: bool,
    correction_window_days: int,
    max_store_workers: int,
    source_factory: Callable[[WbClient, str], FinanceSources] | None,
    now: datetime | None,
    environ: Mapping[str, str] | None,
) -> tuple[StoreSyncSummary, ...]:
    """Fetch all stores concurrently, then commit source snapshots serially.

    The function deliberately separates the network phase from the ledger
    phase.  Each worker owns one ``WbClient``/``FinanceSources`` pair and the
    helper enforces a token-scoped lock; this means platform throttles remain
    local to a credential.  Source adapters disable per-request retries, so a
    HTTP 429 is persisted as incomplete evidence and retried by the next
    scheduled sync/replay.  Only this function's second half touches SQLite,
    preserving the existing idempotent write and watermark semantics.
    """

    starts: dict[str, date] = {}
    source_starts_by_alias: dict[str, dict[str, date]] = {}
    for spec in specs:
        start = determine_sync_start(
            ledger,
            store_alias=spec.alias,
            through=end,
            registry=registry,
            bootstrap=bootstrap,
            correction_window_days=correction_window_days,
        )
        if end < start:
            raise FinanceSyncError(
                f"Sync period for {spec.alias} ends before its history start: {start}"
            )
        stats_start = max(start, end - timedelta(days=89))
        starts[spec.alias] = start
        source_starts_by_alias[spec.alias] = {
            "finance_detailed": start,
            # Explicit bootstrap must remain a full history backfill even if a
            # previous incremental watermark exists.
            "statistics_sales": (
                stats_start
                if bootstrap
                else min(
                    end,
                    _incremental_source_start(
                        ledger, spec.alias, "statistics_sales", stats_start
                    ),
                )
            ),
            "statistics_orders": (
                stats_start
                if bootstrap
                else min(
                    end,
                    _incremental_source_start(
                        ledger, spec.alias, "statistics_orders", stats_start
                    ),
                )
            ),
            "goods_returns": start,
            "advertising_costs": start,
            "advertising_payments": start,
            "seller_balance": start,
            "advertising_balance": start,
        }

    governance = registry.get("governance")
    fail_closed_credentials = (
        bool(governance.get("failClosedOnMissingStoreCredential", True))
        if isinstance(governance, Mapping)
        else True
    )
    plans = build_store_fetch_plans(
        specs,
        starts=starts,
        end=end,
        environ=environ,
        source_starts=source_starts_by_alias,
        fail_closed_on_missing_credentials=fail_closed_credentials,
    )

    # Record every source as running before any worker starts.  If a scheduler
    # or process dies during the network phase, the durable rows still show
    # exactly which source windows were in flight and can be recovered next run.
    run_ids: dict[str, dict[str, str]] = {}
    for plan in plans:
        source_runs: dict[str, str] = {}
        for source_name in SYNC_SOURCES:
            source_start = plan.source_start(source_name, plan.start)
            source_runs[source_name] = ledger.start_sync(
                plan.spec.alias,
                source_name,
                _period_start(source_start),
                _period_end(end),
                started_at=now,
            )
        run_ids[plan.spec.alias] = source_runs

    try:
        fetched = fetch_store_sources_bounded(
            plans,
            source_factory=source_factory,
            max_workers=max_store_workers,
        )
    except Exception as exc:
        # A defensive boundary for executor/configuration failures outside the
        # per-source result contract.  Never leave pre-created ``running``
        # rows hanging if the worker pool itself aborts.
        blocker = f"{type(exc).__name__}: {exc}"
        for source_runs in run_ids.values():
            for run_id in source_runs.values():
                try:
                    ledger.finish_sync(
                        run_id,
                        "failed",
                        error_text=blocker,
                        completed_at=now,
                    )
                except Exception:
                    # Preserve the original worker exception; the gap/failed
                    # state is best-effort when the ledger itself is broken.
                    pass
        raise

    if len(fetched) != len(plans):
        blocker = (
            "Fetcher returned an unexpected number of store results: "
            f"expected {len(plans)}, got {len(fetched)}"
        )
        for source_runs in run_ids.values():
            for run_id in source_runs.values():
                try:
                    ledger.finish_sync(
                        run_id, "failed", error_text=blocker, completed_at=now
                    )
                except Exception:
                    pass
        raise FinanceSyncError(blocker)

    report_sources = set(REPORT_RELEVANT_SOURCES)
    summaries: list[StoreSyncSummary] = []
    for plan, fetched_store in zip(plans, fetched):
        if fetched_store.store_alias != plan.spec.alias:
            raise FinanceSyncError(
                f"Fetch result alias mismatch: expected {plan.spec.alias}, "
                f"got {fetched_store.store_alias}"
            )
        source_results = {
            name: result for name, result in fetched_store.sources
        }
        source_rows: list[dict[str, Any]] = []
        changed = False
        report_relevant_changed = False
        coverage_changed_sources: list[str] = []
        for source_name in SYNC_SOURCES:
            result = source_results.get(source_name)
            if result is None:
                result = SourceResult(
                    source=source_name,
                    status=None,
                    complete=False,
                    rows=[],
                    blocker="Fetcher returned no result for source",
                )
            source_start = plan.source_start(source_name, plan.start)
            run_id = run_ids[plan.spec.alias][source_name]
            content_change_counter = [0]
            try:
                inserted = _persist_source_rows(
                    ledger,
                    plan.spec.alias,
                    source_name,
                    result.rows,
                    fetched_at=now,
                    changed_counter=content_change_counter,
                )
            except Exception as exc:
                # A malformed row is source-local evidence failure, not a
                # reason to discard the other stores/sources in this run.
                # Preserve any rows already upserted, mark this source failed,
                # and let the report gate expose the blocker.
                inserted = 0
                result = SourceResult(
                    source=source_name,
                    status=result.status,
                    complete=False,
                    rows=[],
                    requests=list(result.requests),
                    blocker=(
                        f"PersistenceError: {type(exc).__name__}: {exc}"
                    ),
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
            snapshot_hash = _source_snapshot_hash(
                source_name, complete, result.rows
            )
            previous_coverage = _latest_coverage_if_supported(
                ledger, plan.spec.alias, source_name
            )
            coverage_changed = _coverage_changed(
                previous_coverage,
                complete,
                snapshot_hash,
                content_changed=bool(content_change_counter[0]),
                period_start=_period_start(source_start),
                period_end=_period_end(end),
            )
            if coverage_changed:
                coverage_changed_sources.append(source_name)
            # Persisted-row content/status is the authoritative change signal;
            # the response hash remains an audit fingerprint only.  Counting
            # payload changes avoids marking every rolling-window replay as a
            # correction merely because its boundary moved.
            source_changed = coverage_changed
            changed = changed or source_changed
            report_relevant_changed = report_relevant_changed or (
                source_name in report_sources and source_changed
            )
            _record_coverage_if_supported(
                ledger,
                store_alias=plan.spec.alias,
                source=source_name,
                period_start=_period_start(source_start),
                period_end=_period_end(end),
                coverage_status="complete" if complete else "missing",
                snapshot_hash=snapshot_hash,
                row_count=len(result.rows),
                sync_run_id=run_id,
                captured_at=now,
                metadata={
                    "blocker": result.blocker,
                    "httpStatus": result.status,
                    "cursorMode": _cursor_mode(source_name, bootstrap),
                    "contentChanged": bool(content_change_counter[0]),
                },
            )
            source_rows.append(
                {
                    "source": source_name,
                    "complete": complete,
                    "status": result.status,
                    "rows": len(result.rows),
                    "inserted": inserted,
                    "blocker": result.blocker,
                    "snapshotSha256": snapshot_hash,
                    "requestedFrom": source_start.isoformat(),
                    "cursorMode": _cursor_mode(source_name, bootstrap),
                    "changed": source_changed,
                    "coverageChanged": coverage_changed,
                    "contentChanged": bool(content_change_counter[0]),
                }
            )
        summaries.append(
            StoreSyncSummary(
                store_alias=plan.spec.alias,
                requested_from=plan.start,
                requested_through=end,
                sources=tuple(source_rows),
                changed=changed,
                report_relevant_changed=report_relevant_changed,
                coverage_changed_sources=tuple(coverage_changed_sources),
            )
        )
    return tuple(summaries)


def _running_sync_run_ids(ledger: Any) -> set[str]:
    """Return currently running sync IDs without making cleanup fatal."""

    connection = getattr(ledger, "connection", None)
    if connection is None:
        return set()
    try:
        rows = connection.execute(
            "SELECT run_id FROM sync_runs WHERE status='running'"
        ).fetchall()
    except Exception:
        return set()
    return {str(row[0]) for row in rows}


def _fail_running_sync_runs(
    ledger: Any,
    run_ids: Iterable[str],
    *,
    blocker: str,
    completed_at: datetime | None,
) -> None:
    """Best-effort finalization for exceptional coordinator exits."""

    finish = getattr(ledger, "finish_sync", None)
    if finish is None:
        return
    for run_id in run_ids:
        try:
            finish(
                run_id,
                "failed",
                error_text=blocker,
                completed_at=completed_at,
            )
        except Exception:
            # Preserve the original exception; a broken ledger is reported by
            # the caller on its next health check rather than masked here.
            pass


def _persist_source_rows(
    ledger: FinanceLedger,
    store_alias: str,
    source: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    fetched_at: datetime | None,
    changed_counter: list[int] | None = None,
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
                # Official statistics amounts are RUB and remain in the raw
                # payload.  They cannot populate CNY settlement columns, and
                # an order's totalPrice is not a seller settlement (forPay).
                buyer_paid_cny=None,
                for_pay_cny=None,
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
                occurred_at=_event_time(payload, "occurredAt", "updTime", "date"),
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
        if changed_counter is not None:
            # Older ledger/test doubles may return an UpsertResult without the
            # optional ``changed`` field.  Treat an insertion as a content
            # change in that compatibility case; the real ledger distinguishes
            # an updated payload from an idempotent replay.
            changed_counter[0] += int(
                bool(
                    getattr(
                        result,
                        "changed",
                        bool(getattr(result, "inserted", False)),
                    )
                )
            )
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


def _latest_coverage_if_supported(
    ledger: FinanceLedger, store_alias: str, source: str
) -> Mapping[str, Any] | None:
    method = getattr(ledger, "get_latest_source_coverage", None)
    if method is not None:
        return method(store_alias, source)
    # Older test doubles/ledgers may not expose the convenience method.  Do
    # not fail the sync in that case; row-level upserts and source status still
    # provide a safe fallback.
    return None


def _source_snapshot_hash(
    source: str, complete: bool, rows: Iterable[Mapping[str, Any]]
) -> str:
    """Hash source content independent of an API's row ordering.

    WB does not contractually guarantee ordering for every advertising and
    statistics response.  Sorting canonical row objects avoids a needless
    historical-report refresh when the same evidence arrives in a different
    order, while retaining the source name and completeness state in the
    digest.
    """

    # Hash each row once, then sort the fixed-width digests.  Sorting
    # ``canonical_json(row)`` directly repeatedly serializes rows during the
    # comparison phase and becomes a measurable CPU/memory hotspot when the
    # finance endpoint returns tens of thousands of entries.
    row_digests = sorted(
        hashlib.sha256(canonical_json(dict(row)).encode("utf-8")).hexdigest()
        for row in rows
    )
    return hashlib.sha256(
        canonical_json(
            {
                "source": source,
                "complete": bool(complete),
                "rowCount": len(row_digests),
                "rowDigests": row_digests,
            }
        ).encode("utf-8")
    ).hexdigest()


def _cursor_mode(source: str, bootstrap: bool) -> str:
    if source in {"statistics_sales", "statistics_orders"}:
        # WB retains these operational change-log rows for roughly 90 days;
        # even bootstrap therefore starts at the documented retention floor.
        return "bootstrap_90d" if bootstrap else "watermark_overlap_1d"
    if bootstrap:
        return "bootstrap_full"
    if source in PERIOD_SOURCES:
        return "rolling_correction_window"
    return "point_in_time"


def _coverage_changed(
    previous: Mapping[str, Any] | None,
    complete: bool,
    snapshot_hash: str,
    *,
    content_changed: bool | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
) -> bool:
    if previous is None:
        return True
    previous_status = str(previous.get("coverage_status") or "").lower()
    previous_hash = str(previous.get("snapshot_hash") or "")
    status_changed = previous_status != ("complete" if complete else "missing")
    # The API response hash is intentionally only a fallback for callers that
    # do not provide persisted-row change information.  A rolling/cursor
    # window naturally changes the response row set every day; comparing that
    # hash directly would make every scheduled run look like a correction.
    if content_changed is not None:
        if status_changed or bool(content_changed):
            return True
        # A complete re-read of the *same* window is still useful for detecting
        # an authoritative source retracting a previously persisted row.  Only
        # compare response hashes when both boundaries are identical; never
        # compare a rolling/cursor response against a different window.
        def _same_instant(left: Any, right: str | None) -> bool:
            if right is None or left in (None, ""):
                return False
            try:
                return normalize_iso(str(left)) == normalize_iso(str(right))
            except Exception:
                return str(left) == str(right)

        same_period = _same_instant(
            previous.get("period_start"), period_start
        ) and _same_instant(previous.get("period_end"), period_end)
        return same_period and previous_hash != snapshot_hash
    return status_changed or previous_hash != snapshot_hash


def _incremental_source_start(
    ledger: FinanceLedger,
    store_alias: str,
    source: str,
    floor: date,
) -> date:
    """Choose a narrow cursor for append/change-log statistics sources.

    Statistics endpoints are inclusive change logs keyed by lastChangeDate.
    Re-reading one day before the durable watermark catches boundary events
    while avoiding a full rolling-period scan on every run.  Open gaps remain
    handled by determine_sync_start(), which supplies the lower floor.
    """

    watermark = ledger.get_watermark(store_alias, source)
    if not watermark:
        return floor
    try:
        watermark_date = date.fromisoformat(str(watermark)[:10])
    except ValueError:
        return floor
    return max(floor, watermark_date - timedelta(days=1))


def _event_identity(row: Mapping[str, Any], *, fallback: str) -> dict[str, Any]:
    for key in ("srid", "orderId", "odid", "shkId", "saleID", "saleId", "id"):
        value = row.get(key)
        if value not in (None, ""):
            return {key: str(value), "lastChangeDate": row.get("lastChangeDate")}
    return {"fallback": fallback, "payload": dict(row)}


def _event_time(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        raw = row.get(key)
        if raw in (None, ""):
            continue
        value = str(raw).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(value[:10]), time.min)
            except ValueError:
                continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MOSCOW)
        return parsed.isoformat()
    raise FinanceSyncError("Source row has no usable event timestamp")


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
