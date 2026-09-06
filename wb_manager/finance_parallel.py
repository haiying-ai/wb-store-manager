"""Bounded, read-only concurrency helpers for finance source collection.

The synchronization coordinator owns the ledger and remains responsible for
serial persistence.  This module only performs API reads: one worker is used
per store, source calls within a store stay sequential (therefore preserving
each source adapter's throttle), and stores sharing a token are serialized
behind a token-scoped lock.  Source adapters disable per-request retries; a
429 remains an incomplete source result for the next scheduled sync/replay.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from threading import Lock
from typing import Callable, Iterable, Mapping

from .client import WbClient
from .finance_sources import (
    FinanceSources,
    SourceResult,
    StoreCredentials,
    StoreSpec,
    resolve_store_credentials,
)


_SOURCE_NAMES = (
    "finance_detailed",
    "statistics_sales",
    "statistics_orders",
    "goods_returns",
    "advertising_costs",
    "advertising_payments",
    "seller_balance",
    "advertising_balance",
)


@dataclass(frozen=True)
class StoreFetchPlan:
    """Credential-bearing API-read plan for one store.

    ``StoreCredentials`` deliberately redacts its token in ``repr``.  Keep
    this object internal to the synchronization process and never serialize
    it into reports or logs.
    """

    spec: StoreSpec
    credentials: StoreCredentials | None
    start: date
    end: date
    # Optional per-source cursors supplied by the incremental sync policy.
    # Sources absent from this mapping use the plan's ordinary period start.
    source_starts: Mapping[str, date] = field(default_factory=dict)
    # Configuration failures are kept at store scope so one missing token does
    # not prevent other enabled stores from being collected.  The coordinator
    # will persist failed ``sync_runs`` for this store and expose the blocker
    # in its per-store summary.
    configuration_error: str | None = None

    @property
    def stats_start(self) -> date:
        # WB operational statistics retain orders/sales for about 90 days.
        # Finance detail remains the historical source for older bootstrap
        # periods; querying these change logs earlier cannot recover rows that
        # the platform no longer keeps.
        return max(self.start, self.end - timedelta(days=89))

    def source_start(self, source: str, default: date | None = None) -> date:
        """Return a source-specific start date, falling back safely."""

        return self.source_starts.get(source, default or self.start)


@dataclass(frozen=True)
class StoreFetchResult:
    """API-only result; persistence is intentionally left to the caller."""

    store_alias: str
    requested_from: date
    requested_through: date
    sources: tuple[tuple[str, SourceResult], ...]


def build_store_fetch_plans(
    stores: Iterable[StoreSpec],
    *,
    starts: dict[str, date],
    end: date,
    environ: Mapping[str, str] | None = None,
    source_starts: Mapping[str, Mapping[str, date]] | None = None,
    fail_closed_on_missing_credentials: bool = True,
) -> tuple[StoreFetchPlan, ...]:
    """Resolve credentials once and construct immutable fetch plans.

    Resolving before worker launch keeps credential/configuration failures
    deterministic and avoids any secret handling in worker error logs.
    """

    plans: list[StoreFetchPlan] = []
    for spec in stores:
        start = starts[spec.alias]
        try:
            credentials = resolve_store_credentials(spec, environ=environ)
            configuration_error = None
        except Exception as exc:
            if fail_closed_on_missing_credentials:
                raise
            credentials = None
            configuration_error = f"{type(exc).__name__}: {exc}"
        plans.append(
            StoreFetchPlan(
                spec,
                credentials,
                start,
                end,
                dict((source_starts or {}).get(spec.alias, {})),
                configuration_error,
            )
        )
    return tuple(plans)


def fetch_store_sources_bounded(
    plans: Iterable[StoreFetchPlan],
    *,
    source_factory: Callable[[WbClient, str], FinanceSources] | None = None,
    max_workers: int = 2,
) -> tuple[StoreFetchResult, ...]:
    """Fetch source snapshots with bounded, token-isolated concurrency.

    A worker handles one store at a time and invokes that store's eight source
    adapters sequentially.  A lock keyed by SHA-256(token) serializes stores
    that accidentally share a token while allowing distinct store tokens to
    overlap.  Existing per-source throttling and error handling stay inside
    the adapters; this layer does not add an unsafe retry or duplicate request.
    No ledger object is touched here, so SQLite writes remain
    serialized by the caller after all reads complete.
    """

    plan_list = tuple(plans)
    if not plan_list:
        return ()
    try:
        workers = max(1, int(max_workers))
    except (TypeError, ValueError):
        workers = 1
    workers = min(workers, len(plan_list))

    token_locks: dict[str, Lock] = {}
    for plan in plan_list:
        if plan.configuration_error or plan.credentials is None:
            continue
        digest = _token_digest(plan.credentials.token)
        token_locks.setdefault(digest, Lock())

    def fetch_one(plan: StoreFetchPlan) -> StoreFetchResult:
        if plan.configuration_error or plan.credentials is None:
            blocker = plan.configuration_error or "Store credentials are unavailable"
            return _failed_store_result(plan, blocker)
        digest = _token_digest(plan.credentials.token)
        # The lock covers the complete source sequence for this token.  This
        # preserves the adapter's per-token request pacing when aliases are
        # configured with one shared credential while still allowing
        # independent stores to run in parallel.
        with token_locks[digest]:
            client = WbClient(plan.credentials.token)
            try:
                sources = (
                    source_factory(client, plan.spec.alias)
                    if source_factory is not None
                    else FinanceSources(client, store_alias=plan.spec.alias)
                )
            except Exception as exc:
                blocker = f"{type(exc).__name__}: {exc}"
                return StoreFetchResult(
                    plan.spec.alias,
                    plan.start,
                    plan.end,
                    tuple(
                        (
                            source,
                            SourceResult(
                                source=source,
                                status=None,
                                complete=False,
                                rows=[],
                                blocker=blocker,
                            ),
                        )
                        for source in _SOURCE_NAMES
                    ),
                )
            invocations = _source_invocations(
                sources,
                plan.start,
                plan.end,
                plan.stats_start,
                source_starts=plan.source_starts,
            )
            fetched: list[tuple[str, SourceResult]] = []
            for source_name, invoke in invocations:
                try:
                    result = invoke()
                    if not isinstance(result, SourceResult):
                        raise TypeError(
                            f"{source_name} adapter returned "
                            f"{type(result).__name__}, expected SourceResult"
                        )
                    if str(result.source or "").strip() != source_name:
                        raise ValueError(
                            f"{source_name} adapter returned mismatched source "
                            f"{result.source!r}"
                        )
                except Exception as exc:  # a failed source is not a zero value
                    result = SourceResult(
                        source=source_name,
                        status=None,
                        complete=False,
                        rows=[],
                        blocker=f"{type(exc).__name__}: {exc}",
                    )
                fetched.append((source_name, result))
            return StoreFetchResult(
                plan.spec.alias,
                plan.start,
                plan.end,
                tuple(fetched),
            )

    # executor.map preserves input order, making persistence/report output
    # deterministic even though API requests overlap across stores.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wb-finance") as pool:
        return tuple(pool.map(fetch_one, plan_list))


def _source_invocations(
    sources: FinanceSources,
    start: date,
    end: date,
    stats_start: date,
    *,
    source_starts: Mapping[str, date] | None = None,
) -> tuple[tuple[str, Callable[[], SourceResult]], ...]:
    """Build the canonical source call order used by the sync coordinator."""

    starts = source_starts or {}
    return (
        (
            "finance_detailed",
            lambda: sources.finance_detailed(starts.get("finance_detailed", start), end),
        ),
        (
            "statistics_sales",
            lambda: sources.statistics_sales(
                f"{starts.get('statistics_sales', stats_start).isoformat()}T00:00:00"
            ),
        ),
        (
            "statistics_orders",
            lambda: sources.statistics_orders(
                f"{starts.get('statistics_orders', stats_start).isoformat()}T00:00:00"
            ),
        ),
        (
            "goods_returns",
            lambda: sources.goods_returns(starts.get("goods_returns", start), end),
        ),
        (
            "advertising_costs",
            lambda: sources.advertising_costs(starts.get("advertising_costs", start), end),
        ),
        (
            "advertising_payments",
            lambda: sources.advertising_payments(
                starts.get("advertising_payments", start), end
            ),
        ),
        ("seller_balance", lambda: sources.seller_balance()),
        ("advertising_balance", lambda: sources.advertising_balance()),
    )


def _failed_store_result(plan: StoreFetchPlan, blocker: str) -> StoreFetchResult:
    """Build a deterministic failed result for a store-level setup error."""

    return StoreFetchResult(
        plan.spec.alias,
        plan.start,
        plan.end,
        tuple(
            (
                source,
                SourceResult(
                    source=source,
                    status=None,
                    complete=False,
                    rows=[],
                    blocker=blocker,
                ),
            )
            for source in _SOURCE_NAMES
        ),
    )


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
