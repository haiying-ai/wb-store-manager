from __future__ import annotations

import threading
import time
from datetime import date
from pathlib import Path

from wb_manager.client import WbClient
from wb_manager.finance_parallel import (
    StoreFetchPlan,
    fetch_store_sources_bounded,
)
from wb_manager.finance_sources import SourceResult, StoreCredentials, StoreSpec


def _plan(alias: str, token: str) -> StoreFetchPlan:
    spec = StoreSpec(alias, f"{alias}_TOKEN", Path("token"))
    return StoreFetchPlan(spec, StoreCredentials(spec, token), date(2026, 8, 1), date(2026, 8, 2))


class _ProbeSources:
    def __init__(
        self,
        alias: str,
        probe: dict[str, object],
        lock: threading.Lock,
        barrier: threading.Barrier | None = None,
    ):
        self.alias = alias
        self.probe = probe
        self.lock = lock
        self.barrier = barrier

    def _result(self, source: str) -> SourceResult:
        return SourceResult(source, 200, True, [])

    def finance_detailed(self, *_args, **_kwargs):
        with self.lock:
            self.probe["active"] += 1
            self.probe["max_active"] = max(self.probe["max_active"], self.probe["active"])
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        time.sleep(0.03)
        with self.lock:
            self.probe["active"] -= 1
        return self._result("finance_detailed")

    def statistics_sales(self, *_args, **_kwargs):
        if _args:
            self.probe["sales_start"] = _args[0]
        return self._result("statistics_sales")

    def statistics_orders(self, *_args, **_kwargs):
        if _args:
            self.probe["orders_start"] = _args[0]
        return self._result("statistics_orders")

    def goods_returns(self, *_args, **_kwargs):
        return self._result("goods_returns")

    def advertising_costs(self, *_args, **_kwargs):
        return self._result("advertising_costs")

    def advertising_payments(self, *_args, **_kwargs):
        return self._result("advertising_payments")

    def seller_balance(self, *_args, **_kwargs):
        return self._result("seller_balance")

    def advertising_balance(self, *_args, **_kwargs):
        return self._result("advertising_balance")


def test_distinct_tokens_run_stores_in_parallel():
    probe = {"active": 0, "max_active": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def factory(client: WbClient, alias: str):
        return _ProbeSources(alias, probe, lock, barrier)

    results = fetch_store_sources_bounded(
        (_plan("owner", "token-owner"), _plan("new", "token-new")),
        source_factory=factory,
        max_workers=2,
    )
    assert [item.store_alias for item in results] == ["owner", "new"]
    assert all(len(item.sources) == 8 for item in results)
    assert probe["max_active"] == 2


def test_shared_token_is_serialized():
    probe = {"active": 0, "max_active": 0}
    lock = threading.Lock()

    def factory(client: WbClient, alias: str):
        return _ProbeSources(alias, probe, lock)

    fetch_store_sources_bounded(
        (_plan("owner", "same-token"), _plan("new", "same-token")),
        source_factory=factory,
        max_workers=2,
    )
    assert probe["max_active"] == 1


def test_source_specific_start_is_used_for_statistics():
    probe = {}
    lock = threading.Lock()

    def factory(client: WbClient, alias: str):
        return _ProbeSources(alias, probe, lock)

    plan = StoreFetchPlan(
        _plan("owner", "token-owner").spec,
        StoreCredentials(_plan("owner", "token-owner").spec, "token-owner"),
        date(2026, 8, 1),
        date(2026, 8, 2),
        {"statistics_sales": date(2026, 7, 15), "statistics_orders": date(2026, 7, 16)},
    )
    fetch_store_sources_bounded((plan,), source_factory=factory)
    assert probe["sales_start"] == "2026-07-15T00:00:00"
    assert probe["orders_start"] == "2026-07-16T00:00:00"


def test_store_credential_failure_is_isolated_from_other_stores():
    probe = {"active": 0, "max_active": 0}
    lock = threading.Lock()

    def factory(client: WbClient, alias: str):
        return _ProbeSources(alias, probe, lock)

    broken_spec = StoreSpec("broken", "BROKEN_TOKEN", Path("token"))
    broken = StoreFetchPlan(
        broken_spec,
        None,
        date(2026, 8, 1),
        date(2026, 8, 2),
        configuration_error="FinanceSourceError: Token is unavailable for store broken",
    )
    results = fetch_store_sources_bounded(
        (broken, _plan("owner", "token-owner")),
        source_factory=factory,
        max_workers=2,
    )
    assert results[0].store_alias == "broken"
    assert all(not item.complete for _, item in results[0].sources)
    assert results[1].store_alias == "owner"
    assert all(item.complete for _, item in results[1].sources)


def test_mismatched_source_result_is_failed_without_cross_source_attribution():
    probe = {}
    lock = threading.Lock()

    class WrongSource(_ProbeSources):
        def statistics_sales(self, *_args, **_kwargs):
            return SourceResult("statistics_orders", 200, True, [])

    def factory(client: WbClient, alias: str):
        return WrongSource(alias, probe, lock)

    result = fetch_store_sources_bounded(
        (_plan("owner", "token-owner"),), source_factory=factory
    )[0]
    by_name = dict(result.sources)
    assert by_name["statistics_sales"].complete is False
    assert "mismatched source" in (by_name["statistics_sales"].blocker or "")
    assert by_name["statistics_orders"].complete is True
