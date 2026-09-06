from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from wb_manager.client import WbClient
from wb_manager.finance_ledger import FinanceLedger
from wb_manager.finance_sources import (
    SourceResult,
    advertising_pnl_events,
    normalize_advertising_payment_events,
)
from wb_manager.finance_sync import _persist_source_rows, sync_enabled_stores


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _write_registry(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "registryId": "sync-integration",
                "status": "active",
                "reportingCurrency": "CNY",
                "financialTimezone": "Europe/Moscow",
                "officialHistoryStart": "2026-08-01",
                "stores": [
                    {
                        "alias": "owner-store",
                        "enabled": True,
                        "openedAt": "2026-08-01",
                        "tokenEnv": "SYNC_OWNER_TOKEN",
                        "tokenFile": ".env.missing",
                    },
                    {
                        "alias": "new-store",
                        "enabled": True,
                        "openedAt": "2026-08-01",
                        "tokenEnv": "SYNC_NEW_TOKEN",
                        "tokenFile": ".env.missing",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


class _Sources:
    def __init__(self, alias: str, probe: dict[str, int], lock: threading.Lock, barrier):
        self.alias = alias
        self.probe = probe
        self.lock = lock
        self.barrier = barrier

    def _empty(self, name: str) -> SourceResult:
        return SourceResult(name, 204, True, [])

    def finance_detailed(self, *_args, **_kwargs):
        with self.lock:
            self.probe["active"] += 1
            self.probe["maxActive"] = max(self.probe["maxActive"], self.probe["active"])
        self.barrier.wait(timeout=2)
        time.sleep(0.01)
        with self.lock:
            self.probe["active"] -= 1
        return self._empty("finance_detailed")

    def statistics_sales(self, *_args, **_kwargs):
        return self._empty("statistics_sales")

    def statistics_orders(self, *_args, **_kwargs):
        return self._empty("statistics_orders")

    def goods_returns(self, *_args, **_kwargs):
        return self._empty("goods_returns")

    def advertising_costs(self, *_args, **_kwargs):
        return self._empty("advertising_costs")

    def advertising_payments(self, *_args, **_kwargs):
        return self._empty("advertising_payments")

    def seller_balance(self):
        return SourceResult("seller_balance", 200, True, [{"balance": "100", "available": "90"}])

    def advertising_balance(self):
        return SourceResult("advertising_balance", 200, True, [{"balance": "5"}])


def test_sync_coordinator_preserves_incremental_windows_and_serial_persistence(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SYNC_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("SYNC_NEW_TOKEN", "new-token")
    registry = _write_registry(tmp_path / "stores.json")
    ledger = FinanceLedger(tmp_path / "finance.sqlite3")
    probe = {"active": 0, "maxActive": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def factory(client: WbClient, alias: str):
        return _Sources(alias, probe, lock, barrier)

    try:
        summaries = sync_enabled_stores(
            ledger,
            registry_path=registry,
            repo_root=tmp_path,
            through="2026-08-09",
            bootstrap=False,
            max_store_workers=2,
            source_factory=factory,
            now=datetime(2026, 8, 10, 9, 30, tzinfo=SHANGHAI),
        )
        assert [summary.store_alias for summary in summaries] == [
            "owner-store",
            "new-store",
        ]
        assert probe["maxActive"] == 2
        assert all(summary.primary_complete for summary in summaries)
        assert all(
            item["cursorMode"] == "watermark_overlap_1d"
            for summary in summaries
            for item in summary.sources
            if item["source"] in {"statistics_sales", "statistics_orders"}
        )
        statuses = ledger.connection.execute(
            "SELECT status, COUNT(*) FROM sync_runs GROUP BY status"
        ).fetchall()
        assert [(row[0], row[1]) for row in statuses] == [("complete", 16)]
    finally:
        ledger.close()


def test_rolling_window_boundary_does_not_trigger_report_refresh_without_row_changes(
    tmp_path, monkeypatch
):
    """A moving correction window is a cursor change, not financial evidence."""

    monkeypatch.setenv("SYNC_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("SYNC_NEW_TOKEN", "new-token")
    registry = _write_registry(tmp_path / "stores.json")
    ledger = FinanceLedger(tmp_path / "finance.sqlite3")
    probe = {"active": 0, "maxActive": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def factory(client: WbClient, alias: str):
        return _Sources(alias, probe, lock, barrier)

    try:
        first = sync_enabled_stores(
            ledger,
            registry_path=registry,
            repo_root=tmp_path,
            through="2026-08-09",
            bootstrap=False,
            max_store_workers=2,
            source_factory=factory,
            now=datetime(2026, 8, 10, 9, 30, tzinfo=SHANGHAI),
        )
        assert all(summary.report_relevant_changed for summary in first)

        # Advance far enough for the ordinary 30-day correction window to
        # move.  Every fake source still returns the same empty payload, so a
        # boundary shift alone must not request historical recalculation.
        second = sync_enabled_stores(
            ledger,
            registry_path=registry,
            repo_root=tmp_path,
            through="2026-09-10",
            bootstrap=False,
            max_store_workers=2,
            source_factory=factory,
            now=datetime(2026, 9, 11, 9, 30, tzinfo=SHANGHAI),
        )
        assert all(not summary.report_relevant_changed for summary in second)
        assert all(
            not item["contentChanged"]
            for summary in second
            for item in summary.sources
            if item["source"] in {
                "finance_detailed",
                "statistics_sales",
                "statistics_orders",
                "goods_returns",
                "advertising_costs",
                "advertising_payments",
            }
        )
    finally:
        ledger.close()


def test_payment_date_is_persisted_without_becoming_advertising_pnl(tmp_path):
    payment = {"id": 1, "sum": 500, "date": "2026-08-10", "type": 1}
    event = normalize_advertising_payment_events([payment], "new-store")[0]

    assert event["occurredAt"] == payment["date"]
    assert event["raw"] == payment
    assert event["pnl"] is False
    assert advertising_pnl_events([event]) == []

    with FinanceLedger(tmp_path / "finance.sqlite3") as ledger:
        ledger.register_store(
            "new-store",
            scope_id="sync-integration",
            at=datetime(2026, 8, 1, tzinfo=SHANGHAI),
        )
        inserted = _persist_source_rows(
            ledger,
            "new-store",
            "advertising_payments",
            [event],
            fetched_at=datetime(2026, 8, 11, 9, 30, tzinfo=SHANGHAI),
        )
        row = ledger.connection.execute(
            "SELECT occurred_at, spend_cny, payload_json FROM raw_ad"
        ).fetchone()

    assert inserted == 1
    assert row["occurred_at"] == "2026-08-09T21:00:00+00:00"
    assert row["spend_cny"] is None
    assert json.loads(row["payload_json"])["pnl"] is False
