from __future__ import annotations

import argparse
from datetime import date, datetime
import sys
from types import ModuleType, SimpleNamespace

import pytest

from wb_manager.finance_cli import add_finance_parser, dispatch_finance
from wb_manager.finance_service import FinanceService, FinanceServiceError
from wb_manager.finance_sync import StoreSyncSummary


def _bare_service(now: str = "2026-09-07T14:00:00+08:00") -> FinanceService:
    service = FinanceService.__new__(FinanceService)
    service.now = datetime.fromisoformat(now)
    return service


def _adapter(monkeypatch, *, refresh=None, report=None, published=None, archive=None, fallback=None):
    module = ModuleType("wb_manager.finance_running_adapter")
    module.refresh_running_accounts = refresh or (lambda *args, **kwargs: {"status": "READY"})
    module.build_running_account_report = report or (lambda *args, **kwargs: {"status": "READY"})
    module.running_report_published = published or (lambda *args, **kwargs: True)
    module.archive_running_account_report = archive or (lambda *args, **kwargs: {"status": "NOT_CONFIGURED"})
    module.latest_verified_owner_report = fallback or (lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, module.__name__, module)


def _daily_service(monkeypatch, *, now="2026-09-07T14:00:00+08:00", complete=False):
    service = _bare_service(now)
    service.periodic_standard = SimpleNamespace(first_monthly_scheduled_for="never")
    service.max_store_workers = 2
    service.registry_path = "stores.json"
    service.repo_root = "."
    service.source_factory = None
    service.ledger = SimpleNamespace(find_reports=lambda **kwargs: [{"report_id": "prior"}])
    service._new_report_run_cache = lambda: {"hits": {}}
    service._cache_entry_counts = lambda cache: {}
    service._reconcile_synced_advertising_payments = lambda: []
    service._refresh_finalized_reports = lambda **kwargs: ([], 0)
    service.report = lambda **kwargs: {"reportKey": kwargs["kind"]}
    summary = StoreSyncSummary(
        "owner-store", date(2026, 8, 1), date(2026, 9, 6),
        (
            {"source": "sales", "complete": True, "statusCode": 200},
            {"source": "finance", "complete": complete, "statusCode": 200 if complete else 400},
        ),
    )
    monkeypatch.setattr("wb_manager.finance_service.sync_enabled_stores", lambda *a, **kw: [summary])
    return service, summary


def test_refresh_passes_complete_day_and_partial_source_evidence_to_adapter(monkeypatch):
    calls = []
    _adapter(monkeypatch, refresh=lambda *a, **kw: calls.append((a, kw)) or {"status": "PENDING_DATA"})
    service = _bare_service()
    statuses = [{"storeAlias": "owner-store", "sources": [{"complete": False, "statusCode": 400}]}]
    result = service.account_refresh(through="2026-09-06", source_statuses=statuses)
    assert result["status"] == "PENDING_DATA"
    assert calls == [((service,), {"through": date(2026, 9, 6), "source_statuses": statuses})]


@pytest.mark.parametrize(
    "period,through,period_start,expected_start",
    [
        ("weekly", "2026-09-06", None, date(2026, 8, 31)),
        ("monthly", "2026-08-31", None, date(2026, 8, 1)),
        ("as-of", "2026-09-03", None, None),
        ("as-of", "2026-09-03", "2026-08-18", date(2026, 8, 18)),
    ],
)
def test_all_owner_periods_use_one_report_adapter(monkeypatch, period, through, period_start, expected_start):
    calls = []
    _adapter(monkeypatch, report=lambda *a, **kw: calls.append((a, kw)) or {"templateVersion": "fixed-v1"})
    service = _bare_service()
    result = service.account_report(period=period, through=through, period_start=period_start)
    assert result["templateVersion"] == "fixed-v1"
    assert calls == [((service,), {"period": period, "through": date.fromisoformat(through), "period_start": expected_start})]


@pytest.mark.parametrize(
    "period,through,start,match",
    [
        ("weekly", "2026-09-03", None, "Monday through Sunday"),
        ("monthly", "2026-08-30", None, "full calendar month"),
        ("monthly", "2026-08-31", "2026-07-01", "full calendar month"),
        ("as-of", "2026-09-07", None, "complete-day"),
        ("as-of", "2026-09-03", "2026-09-04", "precedes"),
    ],
)
def test_owner_report_rejects_ambiguous_or_incomplete_period(monkeypatch, period, through, start, match):
    _adapter(monkeypatch)
    with pytest.raises(FinanceServiceError, match=match):
        _bare_service().account_report(period=period, through=through, period_start=start)


def test_daily_refresh_precedes_reports_and_preserves_partial_source_failure(monkeypatch):
    service, summary = _daily_service(monkeypatch)
    order = []
    _adapter(
        monkeypatch,
        refresh=lambda *a, **kw: order.append(("refresh", kw)) or {"status": "PENDING_DATA"},
        report=lambda *a, **kw: order.append(("owner", kw)) or {"reportId": "owner-fixed"},
    )
    service.report = lambda **kw: order.append(("legacy", kw)) or {"reportKey": kw["kind"]}
    result = service.daily(through="2026-09-06")
    assert [entry[0] for entry in order] == ["refresh", "owner", "legacy", "legacy"]
    assert order[0][1]["source_statuses"] == [summary.as_dict()]
    assert order[1][1]["period"] == "as-of"
    assert order[1][1]["period_start"] is None
    assert order[1][1]["through"] == date(2026, 9, 6)
    assert result["ownerReports"] == [{"reportId": "owner-fixed"}]
    assert result["ownerLedger"] == result["ownerReports"][0]
    assert result["ownerLedgerCovers"] == [{
        "period": "weekly", "periodStart": "2026-08-31", "periodEnd": "2026-09-06",
        "coveredBy": "ownerLedger", "workbook": None, "templateVersion": None,
    }]
    assert result["status"] == "PENDING_DATA"
    assert result["stores"][0]["sources"][1]["statusCode"] == 400


def test_legacy_failure_does_not_erase_generated_owner_report(monkeypatch):
    service, _ = _daily_service(monkeypatch)
    _adapter(monkeypatch, report=lambda *a, **kw: {"reportId": "owner-fixed"})

    def failed_legacy(**kwargs):
        raise RuntimeError("legacy workbook unavailable")

    service.report = failed_legacy
    result = service.daily(through="2026-09-06")
    assert result["ownerReports"] == [{"reportId": "owner-fixed"}]
    assert result["status"] == "PARTIAL_FAILURE"
    assert len(result["phaseFailures"]) == 2
    assert result["phaseFailures"][0]["error"] == "legacy workbook unavailable"


def test_failed_refresh_prevents_stale_owner_publication_but_remains_visible(monkeypatch):
    service, _ = _daily_service(monkeypatch)

    def fail_refresh(*args, **kwargs):
        raise RuntimeError("running ledger lock failed")

    _adapter(monkeypatch, refresh=fail_refresh)
    result = service.daily(through="2026-09-06")
    assert result["ownerReports"] == []
    assert result["ownerLedger"] is None
    assert result["ownerLedgerCovers"] == []
    assert result["runningAccounts"] is None
    assert result["status"] == "PARTIAL_FAILURE"
    assert result["phaseFailures"][0]["phase"] == "runningAccountRefresh"


def test_auxiliary_canonical_workbook_failure_keeps_owner_delivery(monkeypatch):
    service, _ = _daily_service(monkeypatch)
    service.output_root = "reports"
    service.advertising_debt_status = lambda: {}
    _adapter(monkeypatch, report=lambda *a, **kw: {"reportId": "owner-fixed"})
    workbook = ModuleType("wb_manager.finance_workbook")

    def failed_workbook(**kwargs):
        raise RuntimeError("legacy canonical workbook locked")

    workbook.sync_canonical_workbook = failed_workbook
    monkeypatch.setitem(sys.modules, workbook.__name__, workbook)
    result = service.daily(through="2026-09-06", sync_workbook=True)
    assert result["ownerReports"] == [{"reportId": "owner-fixed"}]
    assert result["workbookSync"] is None
    assert result["status"] == "PARTIAL_FAILURE"
    assert result["phaseFailures"] == [{
        "phase": "workbookSync", "status": "FAILED", "error": "legacy canonical workbook locked",
    }]


def test_ordinary_day_refreshes_or_reuses_daily_dashboard_without_rebuilding_closed_workbooks(monkeypatch):
    service, _ = _daily_service(monkeypatch, now="2026-09-08T14:00:00+08:00", complete=True)
    calls = []
    _adapter(monkeypatch, report=lambda *a, **kw: calls.append(kw) or {"status": "NOOP", "workbook": "fixed-dashboard.xlsx"})
    result = service.daily(through="2026-09-07")
    assert result["runningAccounts"] == {"status": "READY"}
    assert result["ownerLedger"] == {"status": "NOOP", "workbook": "fixed-dashboard.xlsx"}
    assert result["ownerReports"] == [result["ownerLedger"]]
    assert result["ownerLedgerCovers"] == []
    assert calls == [{"period": "as-of", "period_start": None, "through": date(2026, 9, 7)}]
    assert result["status"] == "SUCCESS"


def test_missed_trigger_catchup_checks_only_latest_closed_week_and_month(monkeypatch):
    service, _ = _daily_service(monkeypatch, now="2026-09-08T14:00:00+08:00", complete=True)
    checks = []
    publications = []
    _adapter(
        monkeypatch,
        published=lambda *a, **kw: checks.append(kw) or False,
        report=lambda *a, **kw: publications.append(kw) or {"reportId": kw["period"]},
    )
    result = service.daily(through="2026-09-07")
    expected = [
        {"period": "weekly", "period_start": date(2026, 8, 31), "through": date(2026, 9, 6)},
        {"period": "monthly", "period_start": date(2026, 8, 1), "through": date(2026, 8, 31)},
    ]
    assert checks == expected
    assert publications == [{"period": "as-of", "period_start": None, "through": date(2026, 9, 7)}] + expected
    assert result["ownerReports"] == [{"reportId": "as-of"}, {"reportId": "weekly"}, {"reportId": "monthly"}]


def test_failed_publication_lookup_does_not_guess_or_reexport(monkeypatch):
    service, _ = _daily_service(monkeypatch, now="2026-09-08T14:00:00+08:00")
    calls = []

    def failed_lookup(*args, **kwargs):
        raise RuntimeError("publication manifest unreadable")

    _adapter(monkeypatch, published=failed_lookup, report=lambda *a, **kw: calls.append(kw) or {})
    result = service.daily(through="2026-09-07")
    assert calls == [{"period": "as-of", "period_start": None, "through": date(2026, 9, 7)}]
    assert result["ownerReports"] == [{}]
    assert result["ownerLedger"] == {}
    assert result["status"] == "PARTIAL_FAILURE"
    assert {row["phase"] for row in result["phaseFailures"]} == {
        "ownerWeeklyPublicationCheck", "ownerMonthlyPublicationCheck",
    }


def test_month_start_publishes_same_owner_template_for_previous_calendar_month(monkeypatch):
    service, _ = _daily_service(monkeypatch, now="2026-10-01T14:00:00+08:00")
    calls = []
    _adapter(monkeypatch, report=lambda *a, **kw: calls.append(kw) or {"reportId": "month"})
    result = service.daily(through="2026-09-30")
    assert calls == [{"period": "as-of", "period_start": None, "through": date(2026, 9, 30)}]
    assert result["ownerLedgerCovers"][0]["period"] == "monthly"
    assert result["ownerLedgerCovers"][0]["periodStart"] == "2026-09-01"


def test_historical_daily_cutoff_uses_cutoff_plus_one_for_closed_periods(monkeypatch):
    service, _ = _daily_service(monkeypatch, now="2026-10-12T14:00:00+08:00", complete=True)
    checks = []
    _adapter(monkeypatch, published=lambda *a, **kw: checks.append(kw) or True)
    result = service.daily(through="2026-09-08")
    assert checks == [
        {"period": "weekly", "period_start": date(2026, 8, 31), "through": date(2026, 9, 6)},
        {"period": "monthly", "period_start": date(2026, 8, 1), "through": date(2026, 8, 31)},
    ]
    assert result["ownerLedgerCovers"] == []


def test_failed_daily_dashboard_does_not_claim_to_cover_closed_week(monkeypatch):
    service, _ = _daily_service(monkeypatch, complete=True)
    calls = []

    def publish(*args, **kwargs):
        calls.append(kwargs["period"])
        if kwargs["period"] == "as-of":
            raise RuntimeError("dashboard build failed")
        return {"reportId": "fallback-closed-week"}

    _adapter(monkeypatch, report=publish)
    result = service.daily(through="2026-09-06")
    assert calls == ["as-of", "weekly"]
    assert result["ownerLedger"] is None
    assert result["ownerLedgerCovers"] == []
    assert result["ownerReports"] == [{"reportId": "fallback-closed-week"}]
    assert result["phaseFailures"] == [{"phase": "ownerLedgerReport", "status": "FAILED", "error": "dashboard build failed"}]
    assert result["status"] == "PARTIAL_FAILURE"


@pytest.mark.parametrize("now,expected", [
    ("2026-09-08T00:00:00+08:00", date(2026, 9, 6)),
    ("2026-09-08T04:59:59+08:00", date(2026, 9, 6)),
    ("2026-09-08T05:00:00+08:00", date(2026, 9, 7)),
    ("2026-09-08T09:30:00+08:00", date(2026, 9, 7)),
])
def test_daily_and_local_entrypoints_use_last_complete_moscow_day(monkeypatch, now, expected):
    service, summary = _daily_service(monkeypatch, now=now, complete=True)
    sync_calls, refresh_calls, report_calls = [], [], []
    monkeypatch.setattr("wb_manager.finance_service.sync_enabled_stores", lambda *a, **kw: sync_calls.append(kw) or [summary])
    _adapter(monkeypatch,
        refresh=lambda *a, **kw: refresh_calls.append(kw) or {"status": "READY"},
        report=lambda *a, **kw: report_calls.append(kw) or {"reportId": "daily"})
    result = service.daily(through="yesterday")
    assert result["through"] == expected.isoformat()
    assert sync_calls[0]["through"] == expected
    assert refresh_calls[0]["through"] == expected
    assert report_calls[0] == {"period": "as-of", "period_start": None, "through": expected}
    service.account_refresh(through="yesterday")
    service.account_report(period="as-of", through="yesterday")
    assert refresh_calls[-1]["through"] == report_calls[-1]["through"] == expected


def test_incomplete_financial_day_is_rejected_before_any_source_ingest(monkeypatch):
    service, _ = _daily_service(monkeypatch, now="2026-09-08T02:00:00+08:00")
    monkeypatch.setattr("wb_manager.finance_service.sync_enabled_stores", lambda *a, **kw: pytest.fail("must not fetch an incomplete day"))
    with pytest.raises(FinanceServiceError, match="complete-day"):
        service.daily(through="2026-09-07")


def test_legacy_debt_reconciliation_failure_does_not_discard_owner_delivery(monkeypatch):
    service, _ = _daily_service(monkeypatch, complete=True)
    _adapter(monkeypatch, report=lambda *a, **kw: {"reportId": "owner-fixed"})

    def fail_debt():
        raise RuntimeError("unrelated historical debt row malformed")

    service._reconcile_synced_advertising_payments = fail_debt
    result = service.daily(through="2026-09-06")
    assert result["ownerLedger"] == {"reportId": "owner-fixed"}
    assert result["advertisingDebtReconciliation"] is None
    assert result["phaseFailures"] == [{"phase": "advertisingDebtReconciliation", "status": "FAILED",
        "error": "unrelated historical debt row malformed"}]
    assert result["status"] == "PARTIAL_FAILURE"


def test_archive_failure_preserves_current_owner_ledger_and_explicit_failure(monkeypatch):
    service, _ = _daily_service(monkeypatch, complete=True)
    def fail_archive(*args, **kwargs):
        raise PermissionError("archive folder unavailable")
    _adapter(monkeypatch, report=lambda *a, **kw: {"workbook": "verified-current.xlsx"}, archive=fail_archive)
    result = service.daily(through="2026-09-06")
    assert result["ownerLedger"]["workbook"] == "verified-current.xlsx"
    assert result["ownerArchives"] == []
    assert result["phaseFailures"] == [{"phase": "ownerLedgerArchive", "status": "FAILED", "error": "archive folder unavailable"}]
    direct = service.account_report(period="as-of", through="2026-09-06")
    assert direct["workbook"] == "verified-current.xlsx"
    assert direct["status"] == "PARTIAL_FAILURE"


def test_fatal_sync_returns_labelled_verified_prior_report_without_new_cutoff_claim(monkeypatch):
    service, _ = _daily_service(monkeypatch, now="2026-09-09T09:30:00+08:00")
    prior = {"status": "STALE_VERIFIED_FALLBACK", "isCurrent": False, "periodEnd": "2026-09-06",
             "dataThrough": "2026-09-06", "requestedThrough": "2026-09-08", "workbook": "previous-verified.xlsx"}
    def fail_sync(*args, **kwargs):
        raise RuntimeError("all-source coordinator failed")
    monkeypatch.setattr("wb_manager.finance_service.sync_enabled_stores", fail_sync)
    _adapter(monkeypatch, refresh=lambda *a, **kw: pytest.fail("No new materialization after failed coordinator"),
        report=lambda *a, **kw: pytest.fail("No fresh dashboard after failed coordinator"), fallback=lambda *a, **kw: prior)
    result = service.daily(through="yesterday")
    assert result["through"] == "2026-09-08"
    assert result["ownerLedger"] == prior
    assert result["runningAccounts"] is None
    assert result["ownerLedgerCovers"] == []
    assert result["phaseFailures"] == [{"phase": "sync", "status": "FAILED", "error": "all-source coordinator failed"}]
    assert result["status"] == "PARTIAL_FAILURE"


def test_source_success_does_not_clear_remaining_account_completeness_gaps(monkeypatch):
    service, _ = _daily_service(monkeypatch, complete=True)
    _adapter(monkeypatch, refresh=lambda *a, **kw: {"status": "PENDING_DATA"},
        report=lambda *a, **kw: {"reportStatus": "PENDING_DATA"})
    assert service.daily(through="2026-09-06")["status"] == "PENDING_DATA"


def _parser():
    parser = argparse.ArgumentParser()
    add_finance_parser(parser.add_subparsers(dest="command", required=True))
    return parser


def test_cli_exposes_persistent_refresh_and_fixed_report_modes():
    args = _parser().parse_args(["finance", "account-refresh", "--through", "2026-09-06"])
    assert args.finance_command == "account-refresh"
    assert args.through == "2026-09-06"
    args = _parser().parse_args([
        "finance", "account-report", "--period", "as-of", "--from", "2026-08-01", "--through", "2026-09-03",
    ])
    assert (args.period, args.period_start, args.through) == ("as-of", "2026-08-01", "2026-09-03")


def test_cli_returns_nonzero_for_visible_partial_daily_failure(monkeypatch, capsys):
    service = SimpleNamespace(
        close=lambda: None,
        account_refresh=lambda **kw: {"status": "PARTIAL_FAILURE", "phaseFailures": [{"phase": "refresh"}]},
    )
    monkeypatch.setattr("wb_manager.finance_service.FinanceService", lambda **kw: service)
    args = _parser().parse_args(["finance", "account-refresh", "--through", "2026-09-06"])
    assert dispatch_finance(args) == 1
    assert "PARTIAL_FAILURE" in capsys.readouterr().out


def test_owner_example_refund_zero_agency_receipts_promo_and_later_withdrawal_settlement():
    """Synthetic owner example only; these are not private store rates or facts."""
    from wb_manager.finance_running_account import (
        METRICS, RunningAccount, cash_event, refund_event, sale_event,
    )

    with RunningAccount(":memory:") as account:
        for store in ("synthetic-a", "synthetic-b"):
            account.register_store(store, "2026-08-01")
            for metric in METRICS:
                account.set_coverage(store, "CNY", metric, "2026-08-01", "2026-09-30", True,
                                     evidence=("synthetic-complete-sources-including-zero-agency-receipts",))
        original_sales = [sale_event(
            "synthetic-a", f"sale-{index}", "2026-08-25", order_id=f"order-{index}",
            amounts={"gross_sales": "2000", "seller_proceeds": "1800", "product_cost": "1000",
                     "agency_accrued": "100", "promotion_accrued": "60", "shared_pool_accrued": "60", "return_reserve_accrued": "0"},
        ) for index in range(10)]
        for event in original_sales:
            account.upsert_event(event)
        before = account.report("2026-08-24", "2026-08-30", ["synthetic-a", "synthetic-b"])
        before_id = account.save_report_snapshot(before)["report_id"]
        later = [
            refund_event("synthetic-a", "refund-1", "2026-09-02", original_sale_id="sale-1", saleable_return=True),
            cash_event("synthetic-a", "cash-ad", "2026-09-03", metric="cash_ad_spend", amount="50"),
            cash_event("synthetic-a", "promo-ad", "2026-09-03", metric="promo_credit_spend", amount="100"),
            cash_event("synthetic-a", "receipt", "2026-09-03", metric="receipts_received", amount="1000"),
            cash_event("synthetic-a", "withdrawal-1:request", "2026-09-03", metric="withdrawal_requested", amount="200"),
            cash_event("synthetic-a", "withdrawal-1:settlement", "2026-09-08", metric="withdrawal_completed", amount="200"),
        ]
        for event in later:
            account.upsert_event(event)
        week = account.report("2026-08-31", "2026-09-06", ["synthetic-a", "synthetic-b"])
        current = week["stores"]["synthetic-a"]["period"]["metrics"]
        cumulative = week["stores"]["synthetic-a"]["cumulative"]["metrics"]
        assert current["agency_accrued"]["amount"] == "-100.00"
        assert current["promotion_accrued"]["amount"] == "-60.00"
        assert current["product_cost"]["amount"] == "-1000.00"
        assert current["profit"]["amount"] == "-640.00"
        assert current["operating_contribution"]["amount"] == "-750.00"
        assert cumulative["agency_accrued"]["amount"] == "900.00"
        assert cumulative["agency_paid"]["amount"] == "0.00"
        assert cumulative["agency_payable"]["amount"] == "900.00"
        assert cumulative["advertising_available"]["amount"] == "490.00"
        assert cumulative["profit"]["amount"] == "5760.00"
        assert cumulative["cash_ad_spend"]["amount"] == "50.00"
        assert cumulative["promo_credit_spend"]["amount"] == "100.00"
        assert cumulative["withdrawal_requested"]["amount"] == "200.00"
        assert cumulative["withdrawal_completed"]["amount"] == "0.00"
        assert cumulative["withdrawal_pending"]["amount"] == "200.00"
        assert cumulative["withdrawal_available"]["amount"] == "800.00"
        assert week["stores"]["synthetic-b"]["cumulative"]["metrics"]["agency_payable"]["amount"] == "0.00"
        assert week["stores"]["ALL"]["cumulative"]["metrics"]["agency_payable"]["amount"] == "900.00"
        settled = account.report("2026-09-07", "2026-09-08", ["synthetic-a", "synthetic-b"])
        settled_total = settled["stores"]["synthetic-a"]["cumulative"]["metrics"]
        assert settled_total["withdrawal_requested"]["amount"] == "200.00"
        assert settled_total["withdrawal_completed"]["amount"] == "200.00"
        assert settled_total["withdrawal_pending"]["amount"] == "0.00"
        assert settled_total["withdrawal_available"]["amount"] == "800.00"
        for event in [*original_sales, *later]:
            assert account.upsert_event(event)["changed"] is False
        assert account.read_report_snapshot(before_id) == before
        assert account.report("2026-08-31", "2026-09-06", ["synthetic-a", "synthetic-b"]) == week
