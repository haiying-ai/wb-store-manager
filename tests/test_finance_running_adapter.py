from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import json
from types import SimpleNamespace
from threading import Barrier

import pytest

from wb_manager import finance_running_adapter as adapter
from wb_manager.finance_running_account import RunningAccount


def _setup(tmp_path, monkeypatch):
    policy = SimpleNamespace(reporting_policy_id="fixture-historical-policy",
        managed_operations_rate=Decimal("0.05"), promotion_reserve_rate=Decimal("0.03"),
        return_risk_reserve_rate=Decimal("0"), reserve_allocation_mode="LEGACY_SHARED_POOL")
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"agencyPaymentCoverage": [{"store": "test-store", "through": "2026-09-06", "receivedCny": "0", "evidence": "owner fixture confirmation"}],
        "returnDecisions": [{"store": "test-store", "orderId": "order-one", "saleable": True}]}), encoding="utf-8")
    service = SimpleNamespace(repo_root=tmp_path, ledger=SimpleNamespace(path=tmp_path / "source.sqlite3"),
        registry_path=tmp_path / "registry.json", store_aliases=["test-store"],
        policy_timeline=SimpleNamespace(select=lambda _: policy),
        periodic_standard=SimpleNamespace(raw={"runningAccounts": {"decisions": str(decisions)}}, historical_cash_events_registry=None))
    data = {"stores": [{"storeAlias": "test-store", "openedAt": "2026-08-01T00:00:00+03:00", "currency": "CNY"}],
        "settlementSales": [], "settlementCharges": [], "rawStatistics": [], "rawOrders": [], "rawReturns": [],
        "approvedCostVersions": [], "advertisingReconciliation": {"events": [], "duplicateReplacements": [], "byStore": {}},
        "withdrawals": [], "excludedWithdrawals": [], "balanceSnapshots": [], "sourceCoverage": [], "issues": []}
    monkeypatch.setattr(adapter, "extract_running_account_sources", lambda *args, **kwargs: deepcopy(data))
    return service, data


def _coverage(data, source, status="complete"):
    data["sourceCoverage"].append({"store_alias": "test-store", "source": source,
        "period_start": "2026-08-01T05:00:00+08:00", "period_end": "2026-09-07T04:59:59+08:00",
        "coverage_status": status, "captured_at": "2026-09-07T10:00:00+08:00", "coverage_id": "fixture-" + source})


def _statistics(data, *, converted=True):
    data["rawStatistics"].append({"storeAlias": "test-store", "raw": {"saleID": "S-one", "srid": "stable-srid", "date": "2026-08-20T14:00:00", "supplierArticle": "IPHONE 13-128GB-BLACK-V3", "priceWithDisc": 20000, "forPay": 18000}})
    order = {"id": "order-one", "srid": "stable-srid", "createdAt": "2026-08-18T14:00:00Z", "article": "IPHONE 13-128GB-BLACK-V3"}
    if converted:
        order.update(price=2000000, convertedPrice=200000, currencyCode=643, convertedCurrencyCode=156)
    data["rawOrders"].append({"storeAlias": "test-store", "raw": order})
    data["approvedCostVersions"].append({"cost_version_uid": "fixture-old-cost", "sku": "IPHONE 13-128GB-BLACK",
        "sku_aliases_json": "[]", "effective_from": "2026-08-01T00:00:00+03:00", "effective_to": "2026-09-01T00:00:00+03:00",
        "approved_by": "fixture-owner", "purchase_cost_cny": "1000"})


def _official_sale(data):
    data["settlementSales"].append({"storeAlias": "test-store", "eventId": "finance:test-store:rrd:1:sale", "eventType": "sale",
        "occurredAt": "2026-08-20T00:00:00+03:00", "orderId": "order-one", "srid": "stable-srid", "quantity": 1,
        "orderCreatedAt": "2026-08-18T00:00:00+03:00", "sku": "IPHONE 13-128GB-BLACK-V3",
        "salesAmountGCny": "2000", "forPayCny": "1800", "purchaseCostCny": "1000",
        "managedOperationsCny": "100", "promotionReserveCny": "60", "returnRiskReserveCny": "0",
        "costVersionId": "fixture-old-cost", "policyId": "fixture-historical-policy", "reserveAllocationMode": "LEGACY_SHARED_POOL",
        "evidenceIds": ["finance:rrdId:1"]})


def _report(service, cutoff="2026-09-06"):
    with RunningAccount(adapter._settings(service)["db"]) as account:
        return account.report("2026-08-31", cutoff, service.store_aliases)


def _known(report, metric, period="cumulative"):
    return report["stores"]["test-store"][period]["metrics"][metric]["known_subtotal"]


def test_refresh_noop_stable_timestamp_and_native_statistics_never_cash(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _statistics(data)
    _coverage(data, "finance_detailed", "missing")
    first = adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    state = adapter._read(adapter._state_path(service))
    second = adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    again = adapter._read(adapter._state_path(service))
    assert first["inserted"] == 1 and second["inserted"] == second["updated"] == 0
    assert state["refreshedAt"] == again["refreshedAt"]
    assert state["contentHash"] == again["contentHash"]
    actual = _report(service)
    assert _known(actual, "gross_sales") == "0.00"
    assert actual["stores"]["test-store"]["cumulative"]["metrics"]["gross_sales"]["amount"] is None
    assert _known(actual, "product_cost") == "1000.00"
    assert _known(actual, "receipts_received") == "0.00"
    assert actual["stores"]["test-store"]["cumulative"]["metrics"]["receipts_received"]["amount"] is None
    assert again["provisionalEvents"][0]["amounts"]["gross_sales"] == "2000.00"
    assert again["provisionalEvents"][0]["metadata"]["basis"] == "unapproved_same_order_estimate"
    assert actual["stores"]["test-store"]["cumulative"]["metrics"]["agency_paid"]["amount"] == "0.00"


def test_statistics_becomes_settlement_revision_without_double_cost(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _statistics(data)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    _official_sale(data)
    _coverage(data, "finance_detailed")
    result = adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    with RunningAccount(adapter._settings(service)["db"]) as account:
        events = account.current_events()
        assert len(events) == 1
        assert events[0].metadata["basis"] == "platform_settlement"
        assert len(account.event_history("test-store", events[0].event_id)) == 2
    assert result["updated"] == 1 and result["inserted"] == 0
    report = _report(service)
    assert _known(report, "product_cost") == "1000.00"
    assert _known(report, "agency_accrued") == "100.00"
    assert adapter._read(adapter._state_path(service))["provisionalEvents"] == []


def test_official_refund_replaces_statistical_reversal_and_preserves_actual_delta(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _statistics(data)
    _official_sale(data)
    data["rawStatistics"].append({"storeAlias": "test-store", "raw": {"saleID": "R-one", "srid": "stable-srid", "date": "2026-09-02T10:00:00", "priceWithDisc": 20000, "forPay": 18000}})
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    data["settlementSales"].append({"storeAlias": "test-store", "eventId": "finance:test-store:rrd:2:refund", "eventType": "refund",
        "occurredAt": "2026-09-03T00:00:00+03:00", "srid": "stable-srid", "orderId": "order-one", "quantity": -1,
        "salesAmountGCny": "-1950", "forPayCny": "-1700", "evidenceIds": ["finance:rrdId:2"]})
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    report = _report(service)
    assert _known(report, "agency_accrued", "period") == "-100.00"
    assert _known(report, "agency_accrued") == "0.00"
    assert _known(report, "seller_proceeds") == "100.00"
    assert _known(report, "gross_sales") == "50.00"
    assert _known(report, "product_cost") == "0.00"
    with RunningAccount(adapter._settings(service)["db"]) as account:
        refunds = [event for event in account.current_events() if event.kind == "refund"]
        assert len(refunds) == 1 and refunds[0].event_date == "2026-09-03"
        assert len(account.event_history("test-store", refunds[0].event_id)) == 2


def test_advertising_real_contract_promo_exclusion_and_superseded_expense(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _coverage(data, "advertising_costs")
    row = {"storeAlias": "test-store", "eventId": "temporary-doc", "eventType": "advertising_expense", "occurredAt": "2026-09-02T04:59:59+08:00",
           "currency": "CNY", "costFundingClass": "seller_cash", "sellerCashCostCny": "35", "platformPromoConsumptionCny": "0"}
    data["advertisingReconciliation"]["events"] = [row]
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    data["advertisingReconciliation"]["events"] = [{**row, "eventId": "final-doc"}, {**row, "eventId": "promo", "costFundingClass": "platform_promo", "sellerCashCostCny": "0", "platformPromoConsumptionCny": "500"}]
    data["advertisingReconciliation"]["duplicateReplacements"] = [{"storeAlias": "test-store", "retainedEventId": "final-doc", "supersededEventIds": ["temporary-doc"]}]
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    report = _report(service)
    assert _known(report, "cash_ad_spend") == "35.00"
    assert _known(report, "promo_credit_spend") == "500.00"
    assert report["stores"]["test-store"]["cumulative"]["metrics"]["cash_ad_spend"]["amount"] == "35.00"
    with RunningAccount(adapter._settings(service)["db"]) as account:
        assert next(event for event in account.current_events() if event.event_id == "ad:final-doc").event_date == "2026-09-01"


def test_charge_eventtype_routes_actual_return_pool_without_double_cost(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _official_sale(data)
    for index, kind, amount in ((1, "forward_logistics", "12"), (2, "return_logistics", "9"), (3, "other_platform_cost", "4")):
        data["settlementCharges"].append({"storeAlias": "test-store", "eventId": f"charge-{index}", "eventType": kind, "amountCny": amount,
            "occurredAt": "2026-09-01T00:00:00+03:00", "orderId": "order-one", "srid": "stable-srid", "evidenceIds": [f"fee-{index}"]})
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    report = _report(service)
    assert _known(report, "logistics_cost") == "12.00"
    assert _known(report, "shared_pool_return_cost") == "9.00"
    assert _known(report, "other_cost") == "4.00"
    assert _known(report, "operating_contribution") == "675.00"


def test_earlier_refresh_does_not_erase_latest_state_and_report_filters_future(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _statistics(data)
    data["rawStatistics"].append({"storeAlias": "test-store", "raw": {"saleID": "R-future", "srid": "not-yet-linked", "date": "2026-09-10T10:00:00", "priceWithDisc": 10000, "forPay": 9000}})
    adapter.refresh_running_accounts(service, through=date(2026, 9, 12))
    initial = adapter._read(adapter._state_path(service))
    result = adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    assert result["through"] == "2026-09-12"
    assert adapter._read(adapter._state_path(service))["refreshedAt"] == initial["refreshedAt"]
    payload, _ = adapter._running_account_payload(service, period="as-of", period_start=date(2026, 8, 1), through=date(2026, 8, 31))
    assert all(row["event_date"] <= "2026-08-31" for row in payload["postings"])
    assert all(row["date"] <= "2026-08-31" for row in payload["nativeStatistics"])
    assert not any(row.get("code") == "ORIGINAL_SALE_MISSING" for row in payload["issues"])


def test_no_conversion_evidence_no_estimate_current_cost_not_used_for_old_sale(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _statistics(data, converted=False)
    data["approvedCostVersions"] = [{**data["approvedCostVersions"][0], "effective_from": "2026-09-01T00:00:00+03:00", "effective_to": None}]
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    state = adapter._read(adapter._state_path(service))
    assert state["provisionalEvents"] == []
    with RunningAccount(adapter._settings(service)["db"]) as account:
        event = account.current_events()[0]
        assert event.amounts["product_cost"] is None
        assert event.amounts["gross_sales"] is None


def test_balance_and_report_coverage_never_prove_cash_receipts_or_missing_finance_zero(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _coverage(data, "seller_balance")
    _coverage(data, "report:finance_detailed")
    data["balanceSnapshots"] = [{"storeAlias": "test-store", "observedAt": "2026-09-06T10:00:00+03:00", "accountBalanceCny": "9999"}]
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    report = _report(service)
    for key in ("gross_sales", "logistics_cost", "receipts_received", "withdrawal_completed"):
        assert report["stores"]["test-store"]["cumulative"]["metrics"][key]["amount"] is None


def test_voided_withdrawal_request_and_release_cancel_without_duplicate_new_request(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    data["excludedWithdrawals"] = [{"storeAlias": "test-store", "eventId": "wrong-request", "recordId": "wrong-id", "amountCny": "300",
        "requestedAt": "2026-09-01T00:00:00+03:00", "cancelledAt": "2026-09-02T00:00:00+03:00", "latestStatus": "voided"}]
    data["withdrawals"] = [{"storeAlias": "test-store", "eventId": "correct-request", "amountCny": "300", "requestedAt": "2026-09-02T00:00:00+03:00", "settledByCutoff": False}]
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    report = _report(service)
    assert _known(report, "withdrawal_requested") == "600.00"
    assert _known(report, "withdrawal_cancelled") == "300.00"
    assert _known(report, "withdrawal_pending") == "300.00"


def test_custom_ledger_isolates_database_owner_decisions_imports_and_inherited_output(tmp_path, monkeypatch):
    service, _ = _setup(tmp_path, monkeypatch)
    service.periodic_standard.raw = {"runningAccounts": {
        "database": "data/finance/wb_running_accounts.sqlite3",
        "decisions": "config/finance/owner_accounting_decisions.local.json",
        "sourceImports": "config/finance/running_source_imports.local.json",
        "ownerReportRoot": "outputs/an-unrelated-owner-thread",
    }}
    settings = adapter._settings(service)
    assert settings["db"] == tmp_path / "source.running.sqlite3"
    assert settings["decisions"] == tmp_path / "source.owner_decisions.json"
    assert settings["imports"] == tmp_path / "source.source_imports.json"
    assert settings["output"] == tmp_path / "source.running_reports"


def test_repeated_noop_report_model_has_identical_content_address(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _statistics(data)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    first, first_id = adapter._running_account_payload(service, period="weekly", period_start=date(2026, 8, 31), through=date(2026, 9, 6))
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    second, second_id = adapter._running_account_payload(service, period="weekly", period_start=date(2026, 8, 31), through=date(2026, 9, 6))
    assert first_id == second_id
    assert first == second


def _published_fixture(service, *, legacy=False):
    model, _ = adapter._running_account_payload(service, period="weekly", period_start=date(2026, 8, 31),
        through=date(2026, 9, 6), persist_snapshot=False)
    paths = adapter._settings(service)
    folder = paths["output"] / "published-fixture"
    folder.mkdir(parents=True, exist_ok=True)
    workbook, verification, source = folder / "fixture.xlsx", folder / "verification.json", folder / "report.json"
    workbook.write_bytes(b"unit-test-fixture-not-a-deliverable")
    adapter._write(verification, {"formulaErrorCount": 0, "totalsVerified": True})
    adapter._write(source, model)
    record = {"workbook": str(workbook), "verification": str(verification), "reportJson": str(source)}
    record["artifactHashes"] = adapter._artifact_hashes(record)
    if not legacy:
        record["businessFingerprint"] = adapter._business_fingerprint(model)
    adapter._write(paths["db"].with_suffix(".publications.json"), {"weekly:2026-08-31:2026-09-06": record})
    return model


def _is_published(service):
    return adapter.running_report_published(service, period="weekly", period_start=date(2026, 8, 31), through=date(2026, 9, 6))


def _fake_owner_export(service, monkeypatch, *, through=date(2026, 9, 6)):
    fake_runtime = service.repo_root / "fake-runtime.txt"
    fake_runtime.write_text("Never executed; fixture only", encoding="utf-8")
    monkeypatch.setenv("WB_FINANCE_NODE", str(fake_runtime))

    def fake_builder(args, **kwargs):
        adapter.Path(args[args.index("--output") + 1]).write_bytes(b"unit-test-placeholder-not-a-workbook")
        adapter._write(adapter.Path(args[args.index("--verification") + 1]), {"formulaErrorCount": 0, "totalsVerified": True})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adapter.subprocess, "run", fake_builder)
    return adapter.build_running_account_report(service, period="as-of", period_start=None, through=through)


def test_closed_period_coverage_survives_next_run_without_duplicate_excel(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    weekly_dashboard = _fake_owner_export(service, monkeypatch)
    monthly_dashboard = _fake_owner_export(service, monkeypatch, through=date(2026, 8, 31))
    adapter.refresh_running_accounts(service, through=date(2026, 9, 7))
    assert _is_published(service)
    assert adapter.running_report_published(service, period="monthly", period_start=date(2026, 8, 1), through=date(2026, 8, 31))
    manifest = adapter._read(adapter._settings(service)["db"].with_suffix(".publications.json"))
    assert len(manifest) == 2
    assert all(row["period"] == "as-of" for row in manifest.values())
    assert weekly_dashboard["workbook"] != monthly_dashboard["workbook"]


def test_file_corruption_cannot_be_noop_and_repair_preserves_old_artifact(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    first = _fake_owner_export(service, monkeypatch)
    old_workbook = adapter.Path(first["workbook"])
    old_workbook.write_bytes(b"corrupt-or-manually-edited")
    assert not _is_published(service)
    repaired = _fake_owner_export(service, monkeypatch)
    assert repaired["workbook"] != first["workbook"]
    assert old_workbook.read_bytes() == b"corrupt-or-manually-edited"
    assert adapter._publication_files_valid(repaired)
    assert _fake_owner_export(service, monkeypatch)["status"] == "NOOP"


def test_archive_sha_replay_noop_and_conflicting_file_never_overwritten(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    service.periodic_standard.raw["runningAccounts"]["archiveRoot"] = str(tmp_path / "configured-archive")
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    report = _fake_owner_export(service, monkeypatch)
    first = adapter.archive_running_account_report(service, report)
    repeated = adapter.archive_running_account_report(service, report)
    assert first["status"] == "PASS"
    assert repeated["status"] == "NOOP"
    assert adapter._artifact_hashes(first) == report["artifactHashes"]
    assert adapter.Path(first["workbook"]).parent.name == report["businessFingerprint"]
    assert adapter.Path(first["workbook"]).parent.parent.name == "2026-09-06"
    archive_workbook = adapter.Path(first["workbook"])
    archive_workbook.write_bytes(b"different-archived-file")
    with pytest.raises(RuntimeError, match="Existing archive differs"):
        adapter.archive_running_account_report(service, report)
    assert archive_workbook.read_bytes() == b"different-archived-file"
    assert adapter._publication_files_valid(report)


def test_template_mismatch_blocks_only_publication_not_daily_account_refresh(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    service.periodic_standard.raw["runningAccounts"]["templateVersion"] = "unavailable-owner-v99"
    refreshed = adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    assert refreshed["through"] == "2026-09-06"
    with pytest.raises(RuntimeError, match="does not match available stable template"):
        adapter._running_account_payload(service, period="as-of", period_start=None, through=date(2026, 9, 6))


def test_fallback_keeps_real_cutoff_and_rejects_corrupt_saved_workbook(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    first = _fake_owner_export(service, monkeypatch)
    fallback = adapter.latest_verified_owner_report(service, through=date(2026, 9, 8))
    assert fallback["requestedThrough"] == "2026-09-08"
    assert fallback["periodEnd"] == fallback["dataThrough"] == "2026-09-06"
    assert fallback["isCurrent"] is False
    adapter.Path(first["workbook"]).write_bytes(b"damaged")
    assert adapter.latest_verified_owner_report(service, through=date(2026, 9, 8)) is None


def test_published_pending_week_invalidated_by_recovered_past_settlement(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _statistics(data)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    _published_fixture(service)
    assert _is_published(service)
    _official_sale(data)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 7))
    assert not _is_published(service)


def test_future_event_and_balance_do_not_republish_unchanged_prior_week(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _statistics(data)
    _coverage(data, "advertising_costs")
    data["balanceSnapshots"] = [{"storeAlias": "test-store", "source": "seller_balance", "observedAt": "2026-09-06T10:00:00+03:00", "accountBalanceCny": "500"}]
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    before = _published_fixture(service)
    data["balanceSnapshots"] = [{"storeAlias": "test-store", "source": "seller_balance", "observedAt": "2026-09-07T10:00:00+03:00", "accountBalanceCny": "900"}]
    data["advertisingReconciliation"]["events"] = [{"storeAlias": "test-store", "eventId": "future-unknown", "occurredAt": "2026-09-07T10:00:00+03:00",
        "costFundingClass": "unknown", "sellerCashCostCny": None, "platformPromoConsumptionCny": None}]
    data["advertisingReconciliation"]["byStore"] = {"test-store": {"classificationComplete": False}}
    adapter.refresh_running_accounts(service, through=date(2026, 9, 7))
    after, _ = adapter._running_account_payload(service, period="weekly", period_start=date(2026, 8, 31), through=date(2026, 9, 6), persist_snapshot=False)
    assert after["sourceCutoff"] != before["sourceCutoff"]
    assert after["sources"]["balanceSnapshots"][0]["accountBalanceCny"] == "500"
    assert adapter._business_fingerprint(after) == adapter._business_fingerprint(before)
    assert _is_published(service)
    monkeypatch.setattr(adapter.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("No workbook rebuild expected")))
    result = adapter.build_running_account_report(service, period="weekly", period_start=date(2026, 8, 31), through=date(2026, 9, 6))
    assert result["status"] == "NOOP"


def test_publication_check_legacy_compatible_and_does_not_append_snapshots(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _statistics(data)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    _published_fixture(service, legacy=True)
    with RunningAccount(adapter._settings(service)["db"]) as account:
        count_before = account.connection.execute("SELECT COUNT(*) FROM ra_snapshots").fetchone()[0]
    assert _is_published(service)
    assert _is_published(service)
    with RunningAccount(adapter._settings(service)["db"]) as account:
        assert account.connection.execute("SELECT COUNT(*) FROM ra_snapshots").fetchone()[0] == count_before


def test_past_source_completeness_change_invalidates_publication_even_same_known_amount(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _official_sale(data)
    _coverage(data, "finance_detailed", "missing")
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    _published_fixture(service)
    data["sourceCoverage"][0]["coverage_status"] = "complete"
    adapter.refresh_running_accounts(service, through=date(2026, 9, 7))
    assert not _is_published(service)


def test_fingerprint_ignores_capture_timestamps_and_unrelated_future_owner_payment(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _statistics(data)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    model = _published_fixture(service)
    changed = deepcopy(model)
    changed["sourceRefreshedAt"] = "2026-09-08T00:00:00+00:00"
    changed["sourceCutoff"] = "2026-09-07"
    changed["ownerDecisions"]["agencyPayments"] = [{"store": "test-store", "date": "2026-09-07", "amountCny": "10", "eventId": "future-payment"}]
    assert adapter._business_fingerprint(changed) == adapter._business_fingerprint(model)


def test_four_period_accounts_keep_each_columns_cumulative_closing_balance(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _official_sale(data)
    decisions_path = adapter._settings(service)["decisions"]
    decisions = adapter._read(decisions_path)
    decisions["agencyPaymentCoverage"][0]["through"] = "2026-08-31"
    decisions["agencyReceiptMode"] = "owner_notifies_each_receipt"
    decisions["agencyPayments"] = [{"store": "test-store", "eventId": "fixture-september-payment",
        "date": "2026-09-03", "amountCny": "30", "evidence": "synthetic owner receipt"}]
    adapter._write(decisions_path, decisions)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    model, _ = adapter._running_account_payload(service, period="as-of", period_start=None,
        through=date(2026, 9, 6), persist_snapshot=False)
    assert model["templateVersion"] == "wb-owner-dashboard-v2"
    windows = {row["key"]: row for row in model["ownerView"]["periods"]}
    accounts = model["ownerPeriodAccounts"]
    assert list(accounts) == ["cumulative", "last_month", "last_week", "yesterday"]
    assert (windows["last_month"]["start"], windows["last_month"]["end"]) == ("2026-08-01", "2026-08-31")
    assert (windows["last_week"]["start"], windows["last_week"]["end"]) == ("2026-08-31", "2026-09-06")
    assert windows["yesterday"]["start"] == windows["yesterday"]["end"] == "2026-09-06"
    for key, expected_payable in (("last_month", "100.00"), ("last_week", "70.00"), ("yesterday", "70.00"), ("cumulative", "70.00")):
        for basis in ("actual", "provisional"):
            account = accounts[key][basis]
            assert account["period_start"] == windows[key]["start"]
            assert account["through"] == windows[key]["end"]
            assert _known(account, "agency_payable") == expected_payable
    assert _known(accounts["last_week"]["actual"], "agency_accrued", "period") == "0.00"
    assert _known(accounts["last_week"]["actual"], "agency_paid", "period") == "30.00"
    assert _known(accounts["yesterday"]["actual"], "agency_payable", "period") == "0.00"


def test_owner_mapping_only_source_change_invalidates_publication(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _official_sale(data)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    before = _published_fixture(service)
    assert _is_published(service)
    data["rawOrders"].append({"storeAlias": "test-store", "source": "marketplace_orders", "raw": {
        "id": 123456789, "srid": "stable-srid", "createdAt": "2026-08-18T14:00:00Z",
        "article": "IPHONE 13-128GB-BLACK-V3"}})
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    after, _ = adapter._running_account_payload(service, period="weekly", period_start=date(2026, 8, 31),
        through=date(2026, 9, 6), persist_snapshot=False)
    assert after["actual"] == before["actual"]
    assert after["provisional"] == before["provisional"]
    assert after["ownerView"] != before["ownerView"]
    assert after["ownerView"]["stores"]["test-store"]["orderRows"][0]["orderId"] == "123456789"
    assert adapter._business_fingerprint(after) != adapter._business_fingerprint(before)
    assert not _is_published(service)


def test_dashboard_reference_amounts_do_not_sum_scenario_and_actual(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _statistics(data)
    decisions_path = adapter._settings(service)["decisions"]
    decisions = adapter._read(decisions_path)
    decisions["estimateApproval"] = "approved_reference_only"
    adapter._write(decisions_path, decisions)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    model, _ = adapter._running_account_payload(service, period="as-of", period_start=None,
        through=date(2026, 9, 6), persist_snapshot=False)
    rows = model["ownerView"]["stores"]["test-store"]["orderRows"]
    assert len(rows) == 1
    assert rows[0]["actualAmounts"]["gross_sales"] is None
    assert rows[0]["estimateAmounts"]["gross_sales"] == "2000.00"
    assert rows[0]["amounts"]["gross_sales"] == "2000.00"
    assert _known(model["ownerPeriodAccounts"]["last_month"]["actual"], "gross_sales") == "0.00"
    assert _known(model["ownerPeriodAccounts"]["last_month"]["provisional"], "gross_sales") == "2000.00"


def test_old_template_publication_cannot_be_delivered_as_dashboard_v2(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    model = _published_fixture(service)
    model["templateVersion"] = "wb-owner-running-v1"
    manifest_path = adapter._settings(service)["db"].with_suffix(".publications.json")
    manifest = adapter._read(manifest_path)
    record = manifest["weekly:2026-08-31:2026-09-06"]
    record["templateVersion"] = "wb-owner-running-v1"
    record["businessFingerprint"] = adapter._business_fingerprint(model)
    adapter._write(manifest_path, manifest)
    assert not _is_published(service)


def test_marketplace_archives_are_verified_store_scoped_identity_evidence_and_replay_noop(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _official_sale(data)
    live_order = {"id": 123456789, "rid": "stable-srid", "createdAt": "2026-08-18T14:00:00Z", "price": 2000000}
    data["rawOrders"].append({"storeAlias": "test-store", "source": "marketplace_orders", "raw": live_order})
    good, nested, failed, other_store = [tmp_path / name for name in ("good.json", "nested.json", "failed.json", "other.json")]
    adapter._write(good, {"storeAlias": "test-store", "status": 200, "ok": True,
        "data": {"orders": [live_order, live_order]}})
    adapter._write(nested, {"storeAlias": "test-store", "response": {"statusCode": 200,
        "data": {"orders": [live_order]}}})
    adapter._write(failed, {"storeAlias": "test-store", "status": 400, "data": {"orders": [live_order]}})
    adapter._write(other_store, {"storeAlias": "disabled-store", "status": 200, "data": {"orders": [live_order]}})
    adapter._write(adapter._settings(service)["imports"], {"marketplaceOrderHistory": [str(good), str(nested), str(failed), str(other_store)]})
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    repeated = adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    assert repeated["inserted"] == repeated["updated"] == 0
    model, _ = adapter._running_account_payload(service, period="as-of", period_start=None,
        through=date(2026, 9, 6), persist_snapshot=False)
    assert len(model["sources"]["rawOrders"]) == 1
    imported = model["sources"]["rawOrders"][0]
    assert imported["sourceType"] == "marketplace_orders"
    assert imported["sourceFile"] == str(good)
    assert len(imported["evidenceIds"]) == 2
    assert model["ownerView"]["stores"]["test-store"]["orderRows"][0]["orderId"] == "123456789"
    assert _known(model["actual"], "gross_sales") == "2000.00"
    reasons = {row.get("reason") for row in model["issues"]}
    assert "marketplace_order_archive_not_successful" in reasons
    assert "marketplace_order_archive_invalid_scope_or_shape" in reasons


def test_marketplace_archive_future_created_order_does_not_rewrite_past_mapping(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _official_sale(data)
    archive = tmp_path / "future-order.json"
    adapter._write(archive, {"storeAlias": "test-store", "status": 200, "data": {"orders": [{
        "id": 123456789, "rid": "stable-srid", "createdAt": "2026-09-08T14:00:00Z", "price": 100}]}})
    adapter._write(adapter._settings(service)["imports"], {"marketplaceOrderHistory": [str(archive)]})
    adapter.refresh_running_accounts(service, through=date(2026, 9, 8))
    model, _ = adapter._running_account_payload(service, period="as-of", period_start=None,
        through=date(2026, 9, 6), persist_snapshot=False)
    assert model["sources"]["rawOrders"] == []
    assert model["ownerView"]["stores"]["test-store"]["orderRows"][0]["orderId"] is None


def test_daily_dashboard_uses_short_chinese_filename_and_verified_noop(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    fake_runtime = tmp_path / "test-runtime-not-executable.txt"
    fake_runtime.write_text("Unit test only; never executed", encoding="utf-8")
    monkeypatch.setenv("WB_FINANCE_NODE", str(fake_runtime))
    calls = []

    def fake_builder(args, **kwargs):
        calls.append(args)
        # These are placeholders, not authored spreadsheets or deliverables.
        workbook = adapter.Path(args[args.index("--output") + 1])
        workbook.write_bytes(b"unit-test-fixture-not-a-deliverable")
        verification = adapter.Path(args[args.index("--verification") + 1])
        adapter._write(verification, {"formulaErrorCount": 0, "totalsVerified": True})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adapter.subprocess, "run", fake_builder)
    first = adapter.build_running_account_report(service, period="as-of", period_start=None, through=date(2026, 9, 6))
    repeated = adapter.build_running_account_report(service, period="as-of", period_start=None, through=date(2026, 9, 6))
    assert adapter.Path(first["workbook"]).name == "WB双店财务汇总_截至2026-09-06.xlsx"
    assert repeated["status"] == "NOOP"
    assert repeated["workbook"] == first["workbook"]
    assert repeated["templateVersion"] == "wb-owner-dashboard-v2"
    assert len(calls) == 1


def test_atomic_replace_retries_short_windows_lock_and_does_not_swallow_permanent_error(tmp_path, monkeypatch):
    destination = tmp_path / "retry.json"
    original = adapter.os.replace
    attempts = []

    def locked_once(source, target):
        attempts.append(target)
        if len(attempts) == 1:
            error = PermissionError("brief Windows sharing lock")
            error.winerror = 32
            raise error
        return original(source, target)

    monkeypatch.setattr(adapter.os, "replace", locked_once)
    adapter._write(destination, {"version": 1})
    assert len(attempts) == 2
    assert adapter._read(destination) == {"version": 1}

    def forbidden(source, target):
        raise PermissionError("permanent permission failure")

    monkeypatch.setattr(adapter.os, "replace", forbidden)
    with pytest.raises(PermissionError, match="permanent"):
        adapter._write(destination, {"version": 2})
    assert adapter._read(destination) == {"version": 1}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_owner_notifies_receipts_mode_extends_registered_zero_and_payment_reduces_once(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _official_sale(data)
    path = adapter._settings(service)["decisions"]
    decisions = adapter._read(path)
    decisions["agencyReceiptMode"] = "owner_notifies_each_receipt"
    adapter._write(path, decisions)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 8))
    metrics = _report(service, "2026-09-08")["stores"]["test-store"]["cumulative"]["metrics"]
    assert metrics["agency_paid"]["amount"] == "0.00"
    assert metrics["agency_payable"]["known_subtotal"] == "100.00"
    assert metrics["receipts_received"]["amount"] is None
    decisions["agencyPayments"] = [{"store": "test-store", "eventId": "owner-notified-one", "date": "2026-09-08", "amountCny": "30", "evidence": "owner confirmed exact receipt"}]
    adapter._write(path, decisions)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 8))
    repeat = adapter.refresh_running_accounts(service, through=date(2026, 9, 8))
    metrics = _report(service, "2026-09-08")["stores"]["test-store"]["cumulative"]["metrics"]
    assert metrics["agency_paid"]["amount"] == "30.00"
    assert metrics["agency_payable"]["known_subtotal"] == "70.00"
    assert repeat["updated"] == repeat["inserted"] == 0
    assert metrics["withdrawal_completed"]["amount"] is None


def test_default_receipt_coverage_does_not_silently_extend_after_confirmed_cutoff(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _official_sale(data)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 8))
    metrics = _report(service, "2026-09-08")["stores"]["test-store"]["cumulative"]["metrics"]
    assert metrics["agency_paid"]["amount"] is None


def test_concurrent_publication_merge_keeps_both_periods_and_existing_history(tmp_path):
    database = tmp_path / "isolated-running.sqlite3"
    manifest = database.with_suffix(".publications.json")
    adapter._write(manifest, {"earlier-month": {"workbook": "unchanged-history.xlsx"}})
    barrier = Barrier(2)

    def publish(key):
        barrier.wait(timeout=5)
        adapter._merge_publication(database, key, {"workbook": key + ".xlsx", "businessFingerprint": key})

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(publish, key) for key in ("weekly:fixture", "monthly:fixture")]
        for job in jobs:
            job.result(timeout=35)
    saved = adapter._read(manifest)
    assert set(saved) == {"earlier-month", "weekly:fixture", "monthly:fixture"}
    assert saved["earlier-month"]["workbook"] == "unchanged-history.xlsx"
    assert saved["weekly:fixture"]["businessFingerprint"] == "weekly:fixture"
    assert saved["monthly:fixture"]["businessFingerprint"] == "monthly:fixture"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_writes_use_unique_temporary_paths_and_leave_complete_json(tmp_path):
    destination = tmp_path / "shared.json"
    barrier = Barrier(2)

    def write(value):
        barrier.wait(timeout=5)
        adapter._write(destination, {"writer": value, "payload": [value] * 100})

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(write, value) for value in ("one", "two")]
        for job in jobs:
            job.result(timeout=10)
    value = adapter._read(destination)
    assert value["writer"] in {"one", "two"}
    assert value["payload"] == [value["writer"]] * 100
    assert not list(tmp_path.glob("*.tmp"))
