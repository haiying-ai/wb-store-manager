from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from threading import Barrier
from xml.etree import ElementTree
from zipfile import ZipFile

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
    # The source still carries the old order-price fee, but a sale is not a payout.
    assert _known(report, "agency_accrued") == "0.00"
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
    assert _known(report, "agency_accrued", "period") == "0.00"
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
    assert _known(report, "operating_contribution") == "775.00"


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
    assert _known(report, "agency_accrued") == "0.00"


def test_agency_fee_starts_only_when_requested_withdrawal_is_confirmed_paid(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _official_sale(data)
    data["withdrawals"] = [{"storeAlias": "test-store", "eventId": "payout-one",
        "recordId": "record-one", "amountCny": "100.00",
        "requestedAt": "2026-09-01T00:00:00+03:00", "settledAt": None,
        "settledByCutoff": False}]
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    pending = _report(service)
    assert _known(pending, "gross_sales") == "2000.00"
    assert _known(pending, "withdrawal_requested") == "100.00"
    assert _known(pending, "withdrawal_completed") == "0.00"
    assert _known(pending, "agency_accrued") == "0.00"

    data["withdrawals"][0].update(settledAt="2026-09-03T00:00:00+03:00", settledByCutoff=True)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    paid = _report(service)
    assert _known(paid, "withdrawal_completed") == "100.00"
    assert _known(paid, "agency_accrued") == "2.00"
    assert _known(paid, "agency_payable") == "2.00"
    with RunningAccount(adapter._settings(service)["db"]) as account:
        agency_events = [event for event in account.current_events()
            if event.amounts.get("agency_accrued") not in (None, "0", "0.00")]
        assert len(agency_events) == 1
        assert agency_events[0].event_date == "2026-09-03"

def test_old_sales_coverage_cannot_prove_complete_withdrawal_fee_history(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    data["withdrawals"] = [{"storeAlias": "test-store", "eventId": "payout-one",
        "recordId": "record-one", "amountCny": "100.00",
        "requestedAt": "2026-09-01T00:00:00+03:00",
        "settledAt": "2026-09-03T00:00:00+03:00", "settledByCutoff": True}]
    with RunningAccount(adapter._settings(service)["db"]) as account:
        account.register_store("test-store", "2026-08-01")
        account.set_coverage("test-store", "CNY", "agency_accrued",
            "2026-08-01", "2026-09-06", True, ("obsolete-order-coverage",))
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    metrics = _report(service)["stores"]["test-store"]["cumulative"]["metrics"]
    assert metrics["agency_accrued"]["known_subtotal"] == "2.00"
    assert metrics["agency_accrued"]["amount"] is None
    assert metrics["agency_payable"]["amount"] is None


def test_agency_fee_rounds_each_completed_payout_and_refresh_is_idempotent(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    data["withdrawals"] = [{"storeAlias": "test-store", "eventId": f"payout-{index}",
        "recordId": f"record-{index}", "amountCny": "100.25",
        "requestedAt": "2026-09-01T00:00:00+03:00",
        "settledAt": "2026-09-03T00:00:00+03:00", "settledByCutoff": True}
        for index in (1, 2)]
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    assert _known(_report(service), "withdrawal_completed") == "200.50"
    # 100.25 × 2% = 2.005, rounded to 2.01 per payout, not 4.01 in aggregate.
    assert _known(_report(service), "agency_accrued") == "4.02"
    repeat = adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    assert repeat["inserted"] == repeat["updated"] == 0
    with RunningAccount(adapter._settings(service)["db"]) as account:
        agency_events = [event for event in account.current_events()
            if event.amounts.get("agency_accrued") not in (None, "0", "0.00")]
        assert len(agency_events) == 2
        assert all(len(account.event_history("test-store", event.event_id)) == 1 for event in agency_events)


def test_combined_agency_receipt_preserves_periods_and_unallocated_store_truth(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    service.store_aliases = ["test-store", "other-store"]
    data["stores"].append({"storeAlias": "other-store", "openedAt": "2026-08-01T00:00:00+03:00", "currency": "CNY"})
    data["withdrawals"] = [
        {"storeAlias": store, "eventId": identity, "amountCny": amount,
         "requestedAt": settled, "settledAt": settled, "settledByCutoff": True}
        for store, identity, amount, settled in (
            ("test-store", "aug-credit", "100.00", "2026-08-28T00:00:00+03:00"),
            ("test-store", "sep-credit", "200.00", "2026-09-10T00:00:00+03:00"),
            ("other-store", "other-credit", "300.00", "2026-09-10T00:00:00+03:00"),
        )
    ]
    decisions_path = adapter._settings(service)["decisions"]
    decisions = adapter._read(decisions_path)
    decisions["agencyReceiptMode"] = "owner_notifies_each_receipt"
    decisions["agencyPaymentCoverage"].append({"store": "other-store", "through": "2026-09-06", "receivedCny": "0", "evidence": "fixture baseline"})
    decisions["agencyReceiptBatches"] = [{"eventId": "joint-receipt", "date": "2026-09-16",
        "amountCny": "11.99", "stores": service.store_aliases, "evidence": "owner fixture receipt", "allocations": []}]
    adapter._write(decisions_path, decisions)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 16))
    model, _ = adapter._running_account_payload(service, period="as-of", period_start=None, through=date(2026, 9, 16), persist_snapshot=False)
    for key, accrued, paid, payable in (
        ("cumulative", "12.00", "11.99", "0.01"),
        ("last_month", "2.00", "0.00", "2.00"),
        ("last_week", "10.00", "0.00", "12.00"),
        ("yesterday", "0.00", "11.99", "0.01"),
    ):
        summary = model["ownerPeriodAccounts"][key]["actual"]["stores"]["ALL"]
        assert summary["period"]["metrics"]["agency_accrued"]["known_subtotal"] == accrued
        assert summary["period"]["metrics"]["agency_paid"]["amount"] == paid
        assert summary["cumulative"]["metrics"]["agency_payable"]["known_subtotal"] == payable
    assert model["actual"]["stores"]["test-store"]["cumulative"]["metrics"]["agency_paid"]["amount"] == "0.00"
    assert model["ownerDecisions"]["agencyReceiptBatches"] == decisions["agencyReceiptBatches"]
    earlier, _ = adapter._running_account_payload(service, period="as-of", period_start=None, through=date(2026, 9, 13), persist_snapshot=False)
    assert earlier["agencyReceiptRegister"]["batches"] == []
    assert earlier["ownerDecisions"]["agencyReceiptBatches"] == []
    assert earlier["actual"]["stores"]["ALL"]["cumulative"]["metrics"]["agency_paid"]["amount"] == "0.00"
    for period, start, end in (("weekly", date(2026, 9, 7), date(2026, 9, 13)),
                               ("monthly", date(2026, 8, 1), date(2026, 8, 31))):
        historical, _ = adapter._running_account_payload(
            service, period=period, period_start=start, through=end, persist_snapshot=False)
        assert historical["ownerDecisions"]["agencyReceiptBatches"] == []
        assert historical["agencyReceiptRegister"]["batches"] == []
    assert adapter._read(decisions_path)["agencyReceiptBatches"] == decisions["agencyReceiptBatches"]

    # The owner separately waives the one-cent settlement tail. It changes
    # only the combined payable, never the 11.99 cash receipt or store fees.
    decisions["agencySettlementAdjustments"] = [{
        "eventId": "joint-receipt-tail-cent", "date": "2026-09-16",
        "amountCny": "0.01", "batchEventId": "joint-receipt",
        "evidence": "owner confirmed one-cent non-cash settlement tail",
    }]
    adapter._write(decisions_path, decisions)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 16))
    settled, _ = adapter._running_account_payload(
        service, period="as-of", period_start=None, through=date(2026, 9, 16), persist_snapshot=False)
    combined = settled["ownerPeriodAccounts"]["cumulative"]["actual"]["stores"]["ALL"]["cumulative"]["metrics"]
    assert combined["agency_accrued"]["known_subtotal"] == "12.00"
    assert combined["agency_paid"]["amount"] == "11.99"
    assert combined["agency_payable"]["known_subtotal"] == "0.00"
    assert settled["agencySettlementAdjustments"]["byPeriod"]["yesterday"]["periodCny"] == "0.01"
    assert settled["actual"]["stores"]["test-store"]["cumulative"]["metrics"]["agency_payable"]["known_subtotal"] == "6.00"
    repeated_settled, _ = adapter._running_account_payload(
        service, period="as-of", period_start=None, through=date(2026, 9, 16), persist_snapshot=False)
    assert repeated_settled["actual"]["stores"]["ALL"]["cumulative"]["metrics"]["agency_payable"]["known_subtotal"] == "0.00"
    earlier_adjustment, _ = adapter._running_account_payload(
        service, period="weekly", period_start=date(2026, 9, 7), through=date(2026, 9, 13), persist_snapshot=False)
    assert earlier_adjustment["agencySettlementAdjustments"]["entries"] == []
    assert earlier_adjustment["ownerDecisions"]["agencySettlementAdjustments"] == []
    assert earlier_adjustment["actual"]["stores"]["ALL"]["cumulative"]["metrics"]["agency_payable"]["known_subtotal"] == "12.00"

    # Later explicit splitting reallocates existing cash, never creates a second receipt.
    decisions["agencyReceiptBatches"][0]["allocations"] = [
        {"store": "test-store", "amountCny": "5.99"}, {"store": "other-store", "amountCny": "6.00"}]
    adapter._write(decisions_path, decisions)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 16))
    split, _ = adapter._running_account_payload(service, period="as-of", period_start=None, through=date(2026, 9, 16), persist_snapshot=False)
    assert split["actual"]["stores"]["ALL"]["cumulative"]["metrics"]["agency_paid"]["amount"] == "11.99"
    assert split["agencyReceiptRegister"]["unallocatedReceipts"] == []
    # Withdrawing a mistaken allocation restores group cash without stale store events.
    decisions["agencyReceiptBatches"][0]["allocations"] = []
    adapter._write(decisions_path, decisions)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 16))
    restored, _ = adapter._running_account_payload(service, period="as-of", period_start=None, through=date(2026, 9, 16), persist_snapshot=False)
    assert restored["actual"]["stores"]["ALL"]["cumulative"]["metrics"]["agency_paid"]["amount"] == "11.99"
    assert restored["actual"]["stores"]["test-store"]["cumulative"]["metrics"]["agency_paid"]["amount"] == "0.00"
    repeated = adapter.refresh_running_accounts(service, through=date(2026, 9, 16))
    assert repeated["inserted"] == repeated["updated"] == 0

    # A later real prepayment may produce a negative closing payable. The
    # earlier non-cash one-cent settlement remains applied exactly once.
    decisions["agencyReceiptBatches"].append({
        "eventId": "later-agency-prepayment", "date": "2026-09-17",
        "amountCny": "0.02", "stores": service.store_aliases,
        "evidence": "owner fixture later cash prepayment", "allocations": [],
    })
    adapter._write(decisions_path, decisions)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 17))
    later, _ = adapter._running_account_payload(
        service, period="as-of", period_start=None, through=date(2026, 9, 17), persist_snapshot=False)
    later_metrics = later["actual"]["stores"]["ALL"]["cumulative"]["metrics"]
    assert later_metrics["agency_accrued"]["known_subtotal"] == "12.00"
    assert later_metrics["agency_paid"]["amount"] == "12.01"
    assert later_metrics["agency_payable"]["known_subtotal"] == "-0.02"
    assert later["agencySettlementAdjustments"]["byPeriod"]["yesterday"]["periodCny"] == "0.00"
    assert later["agencySettlementAdjustments"]["byPeriod"]["cumulative"]["cumulativeCny"] == "0.01"


def test_real_owner_workbook_shows_completed_payout_fee_and_store_level_flow(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    builder = repo_root / "wb_manager" / "assets" / "build_running_finance_report.mjs"
    node = shutil.which("node")
    if not node or not (repo_root / "node_modules" / "@oai" / "artifact-tool" / "package.json").is_file():
        pytest.skip("offline Node spreadsheet runtime is not installed")

    service, data = _setup(tmp_path, monkeypatch)
    _official_sale(data)
    data["withdrawals"] = [{"storeAlias": "test-store", "eventId": "payout-100",
        "recordId": "record-100", "amountCny": "100.00",
        "requestedAt": "2026-09-01T00:00:00+03:00",
        "settledAt": "2026-09-03T00:00:00+03:00", "settledByCutoff": True}]
    # The builder's recalc check perturbs a positive cash-ad row and restores it.
    data["advertisingReconciliation"]["events"] = [{"storeAlias": "test-store",
        "eventId": "fixture-cash-ad", "eventType": "advertising_expense",
        "occurredAt": "2026-09-02T00:00:00+03:00", "currency": "CNY",
        "costFundingClass": "seller_cash", "sellerCashCostCny": "1.00",
        "platformPromoConsumptionCny": "0"}]
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    model, _ = adapter._running_account_payload(service, period="as-of", period_start=None,
        through=date(2026, 9, 6), persist_snapshot=False)
    assert model["templateVersion"] == "wb-owner-dashboard-v2.4"
    fee_rows = [row for row in model["ownerView"]["stores"]["test-store"]["flowRows"]
        if row.get("sourceBasis") == "completed_withdrawal_agency_fee"]
    assert len(fee_rows) == 1
    assert fee_rows[0]["isOrderEvent"] is False
    assert Decimal(fee_rows[0]["amounts"]["agency_accrued"]) == Decimal("2.00")

    input_path = tmp_path / "report.json"
    workbook_path = tmp_path / "report.xlsx"
    verification_path = tmp_path / "verification.json"
    input_path.write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run([node, str(builder), "--input", str(input_path),
        "--output", str(workbook_path), "--verification", str(verification_path)],
        cwd=repo_root, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    verification = json.loads(verification_path.read_text(encoding="utf-8"))
    assert verification["formulaErrorCount"] == 0
    assert verification["totalsVerified"] is True
    assert verification["recalcVerified"] is True
    assert verification["layoutContract"]["passed"] is True

    # The real builder emits the only store's summary as sheet2 and flow as sheet4.
    # Check saved Excel cells, not only the input model or in-memory verifier.
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(workbook_path) as workbook:
        summary = ElementTree.fromstring(workbook.read("xl/worksheets/sheet2.xml"))
        flow = ElementTree.fromstring(workbook.read("xl/worksheets/sheet4.xml"))
        shared = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            strings = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
            shared = ["".join(item.itertext()) for item in strings.findall("x:si", namespace)]

    def cell_value(sheet, address):
        cell = sheet.find(f".//x:c[@r='{address}']", namespace)
        assert cell is not None, address
        if cell.get("t") == "inlineStr":
            return "".join(cell.find("x:is", namespace).itertext())
        value = cell.find("x:v", namespace)
        assert value is not None, address
        return shared[int(value.text)] if cell.get("t") == "s" else value.text

    assert Decimal(cell_value(summary, "E16")) == Decimal("2.00")
    assert Decimal(cell_value(summary, "E20")) == Decimal("100.00")
    flow_rows = sorted(model["ownerView"]["stores"]["test-store"]["flowRows"],
        key=lambda row: (-int(row["eventDate"].replace("-", "")), row["eventId"]))
    fee_excel_row = 7 + next(index for index, row in enumerate(flow_rows)
        if row["eventId"] == fee_rows[0]["eventId"])
    assert cell_value(flow, f"C{fee_excel_row}") == "店铺级"
    assert Decimal(cell_value(flow, f"E{fee_excel_row}")) == Decimal("2.00")
    assert Decimal(cell_value(flow, f"I{fee_excel_row}")) == Decimal("2.00")


def test_old_order_price_agency_accrual_is_revised_to_zero_without_erasing_history(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    _official_sale(data)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    with RunningAccount(adapter._settings(service)["db"]) as account:
        current = next(event for event in account.current_events() if event.kind == "sale")
        account.upsert_event(replace(current, amounts={**current.amounts, "agency_accrued": "100.00"}),
            expected_revision=1, correction_reason="fixture for old order-price policy")
        assert _known(account.report("2026-08-31", "2026-09-06", service.store_aliases), "agency_accrued") == "100.00"
    result = adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    assert result["updated"] >= 1
    assert _known(_report(service), "agency_accrued") == "0.00"
    with RunningAccount(adapter._settings(service)["db"]) as account:
        history = account.event_history("test-store", current.event_id)
        assert [Decimal(item["payload"]["amounts"]["agency_accrued"]) for item in history] == [
            Decimal("0"), Decimal("100"), Decimal("0")]


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
    adapter._write(verification, {
        "templateVersion": adapter.TEMPLATE_VERSION,
        "formulaErrorCount": 0,
        "totalsVerified": True,
        "layoutContract": {
            "version": adapter.LAYOUT_CONTRACT_VERSION,
            "passed": True,
        },
    })
    adapter._write(source, model)
    record = {
        "workbook": str(workbook),
        "verification": str(verification),
        "reportJson": str(source),
        "templateVersion": adapter.TEMPLATE_VERSION,
    }
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
        adapter._write(adapter.Path(args[args.index("--verification") + 1]), {
            "templateVersion": adapter.TEMPLATE_VERSION,
            "formulaErrorCount": 0,
            "totalsVerified": True,
            "layoutContract": {
                "version": adapter.LAYOUT_CONTRACT_VERSION,
                "passed": True,
            },
        })
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


def test_template_hash_lock_accepts_approved_builder_and_keeps_legacy_fixtures_compatible(tmp_path, monkeypatch):
    service, _ = _setup(tmp_path, monkeypatch)
    legacy = adapter.validate_running_report_template(service)
    assert legacy["status"] == "LEGACY_UNLOCKED"
    assert legacy["builderSha256"] is None

    builder = tmp_path / "approved-builder.mjs"
    builder.write_text("// approved unit-test builder\n", encoding="utf-8")
    approved = adapter.hashlib.sha256(builder.read_bytes()).hexdigest()
    service.periodic_standard.raw["runningAccounts"].update({
        "builder": str(builder),
        "approvedBuilderSha256": approved.upper(),
    })
    locked = adapter.validate_running_report_template(service)
    assert locked == {
        "status": "LOCKED",
        "templateVersion": adapter.TEMPLATE_VERSION,
        "builder": str(builder),
        "builderSha256": approved,
        "approvedBuilderSha256": approved,
    }


def test_versioned_release_manifest_locks_the_default_builder(tmp_path, monkeypatch):
    service, _ = _setup(tmp_path, monkeypatch)
    builder = tmp_path / "wb_manager" / "assets" / "build_running_finance_report.mjs"
    builder.parent.mkdir(parents=True)
    builder.write_text("// released unit-test builder\n", encoding="utf-8")
    approved = adapter.hashlib.sha256(builder.read_bytes()).hexdigest()
    release_path = tmp_path / adapter.TEMPLATE_RELEASE_MANIFEST
    adapter._write(release_path, {
        "schemaVersion": 1,
        "templateVersion": adapter.TEMPLATE_VERSION,
        "builder": "wb_manager/assets/build_running_finance_report.mjs",
        "approvedBuilderSha256": approved,
        "layoutContractVersion": adapter.LAYOUT_CONTRACT_VERSION,
    })

    locked = adapter.validate_running_report_template(service)
    assert locked["status"] == "LOCKED"
    assert locked["builderSha256"] == locked["approvedBuilderSha256"] == approved

    builder.write_text("// changed after release\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not match the approved lock"):
        adapter.validate_running_report_template(service)


def test_template_hash_mismatch_fails_before_report_build_and_does_not_publish(tmp_path, monkeypatch):
    service, _ = _setup(tmp_path, monkeypatch)
    builder = tmp_path / "unapproved-builder.mjs"
    builder.write_text("// unapproved unit-test builder\n", encoding="utf-8")
    service.periodic_standard.raw["runningAccounts"].update({
        "builder": str(builder),
        "approvedBuilderSha256": "0" * 64,
    })
    monkeypatch.setattr(adapter.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("builder process must not run after a failed hash preflight")
    ))

    with pytest.raises(RuntimeError, match="does not match the approved lock"):
        adapter.build_running_account_report(
            service, period="as-of", period_start=None, through=date(2026, 9, 6)
        )
    assert not adapter._settings(service)["db"].with_suffix(".publications.json").exists()


def test_template_hash_is_rechecked_after_build_before_publication(tmp_path, monkeypatch):
    service, _ = _setup(tmp_path, monkeypatch)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    builder = tmp_path / "approved-builder.mjs"
    builder.write_text("// approved unit-test builder\n", encoding="utf-8")
    approved = adapter.hashlib.sha256(builder.read_bytes()).hexdigest()
    service.periodic_standard.raw["runningAccounts"].update({
        "builder": str(builder),
        "approvedBuilderSha256": approved,
    })
    fake_runtime = tmp_path / "fake-runtime.txt"
    fake_runtime.write_text("Never executed; fixture only", encoding="utf-8")
    monkeypatch.setenv("WB_FINANCE_NODE", str(fake_runtime))

    def mutating_builder(args, **kwargs):
        adapter.Path(args[args.index("--output") + 1]).write_bytes(b"unit-test-placeholder")
        adapter._write(adapter.Path(args[args.index("--verification") + 1]), {
            "formulaErrorCount": 0,
            "totalsVerified": True,
        })
        builder.write_text("// changed during report build\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adapter.subprocess, "run", mutating_builder)
    with pytest.raises(RuntimeError, match="does not match the approved lock"):
        adapter.build_running_account_report(
            service, period="as-of", period_start=None, through=date(2026, 9, 6)
        )
    assert not adapter._settings(service)["db"].with_suffix(".publications.json").exists()


def test_current_publication_requires_the_versioned_layout_contract(tmp_path, monkeypatch):
    service, _ = _setup(tmp_path, monkeypatch)
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    report = _fake_owner_export(service, monkeypatch)
    verification_path = adapter.Path(report["verification"])
    verification = adapter._read(verification_path)
    verification["layoutContract"].pop("version")
    adapter._write(verification_path, verification)
    report["artifactHashes"] = adapter._artifact_hashes(report)

    manifest_path = adapter._settings(service)["db"].with_suffix(".publications.json")
    manifest = adapter._read(manifest_path)
    manifest["as-of:2026-08-01:2026-09-06"] = report
    adapter._write(manifest_path, manifest)

    assert not adapter._publication_files_valid(report)
    assert adapter.latest_verified_owner_report(service, through=date(2026, 9, 8)) is None


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


def test_later_same_day_equal_balance_snapshot_keeps_publication_noop(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    data["balanceSnapshots"] = [{
        "storeAlias": "test-store", "source": "seller_balance", "currency": "CNY",
        "observedAt": "2026-09-06T10:00:00+03:00", "eventId": "first-observation",
        "accountBalanceCny": "500.00",
    }]
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    before = _published_fixture(service)

    data["balanceSnapshots"][0].update({
        "observedAt": "2026-09-06T12:00:00+03:00", "eventId": "later-observation",
    })
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    after, _ = adapter._running_account_payload(
        service, period="weekly", period_start=date(2026, 8, 31),
        through=date(2026, 9, 6), persist_snapshot=False,
    )
    assert after["ownerCashPositions"]["periods"]["yesterday"]["stores"]["test-store"]["snapshotEventId"] == "later-observation"
    assert adapter._business_fingerprint(after) == adapter._business_fingerprint(before)
    monkeypatch.setattr(adapter.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("No workbook rebuild expected")))
    assert adapter.build_running_account_report(
        service, period="weekly", period_start=date(2026, 8, 31),
        through=date(2026, 9, 6),
    )["status"] == "NOOP"


def test_cash_position_fingerprint_keeps_day_amount_and_conflict_state(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    data["balanceSnapshots"] = [{
        "storeAlias": "test-store", "source": "seller_balance", "currency": "CNY",
        "observedAt": "2026-09-06T10:00:00+03:00", "eventId": "first-observation",
        "accountBalanceCny": "500.00",
    }]
    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    model, _ = adapter._running_account_payload(
        service, period="weekly", period_start=date(2026, 8, 31),
        through=date(2026, 9, 6), persist_snapshot=False,
    )
    original = adapter._business_fingerprint(model)
    row = model["ownerCashPositions"]["periods"]["yesterday"]["stores"]["test-store"]

    changed_day = deepcopy(model)
    day_row = changed_day["ownerCashPositions"]["periods"]["yesterday"]["stores"]["test-store"]
    day_row.update(snapshotDay="2026-09-05", snapshotFreshness="stale")
    assert adapter._business_fingerprint(changed_day) != original

    changed_amount = deepcopy(model)
    changed_amount["ownerCashPositions"]["periods"]["yesterday"]["stores"]["test-store"]["accountBalanceCny"] = "501.00"
    assert adapter._business_fingerprint(changed_amount) != original

    conflict = deepcopy(model)
    conflict_row = conflict["ownerCashPositions"]["periods"]["yesterday"]["stores"]["test-store"]
    conflict_row["conflictingSnapshotEventIds"] = ["source-a", "source-b"]
    conflict_fingerprint = adapter._business_fingerprint(conflict)
    assert conflict_fingerprint != original
    conflict_row["conflictingSnapshotEventIds"] = ["source-c", "source-d"]
    assert adapter._business_fingerprint(conflict) == conflict_fingerprint
    assert row["conflictingSnapshotEventIds"] == []


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
    data["withdrawals"] = [{"storeAlias": "test-store", "eventId": "period-payout",
        "recordId": "period-payout-record", "amountCny": "5000.00",
        "requestedAt": "2026-08-27T00:00:00+03:00",
        "settledAt": "2026-08-28T00:00:00+03:00", "settledByCutoff": True}]
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
    assert model["templateVersion"] == "wb-owner-dashboard-v2.4"
    windows = {row["key"]: row for row in model["ownerView"]["periods"]}
    accounts = model["ownerPeriodAccounts"]
    assert list(accounts) == ["yesterday", "last_week", "last_month", "cumulative"]
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


def test_old_template_publication_cannot_be_delivered_as_dashboard_v21(tmp_path, monkeypatch):
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
    payload_calls = []
    original_payload = adapter._running_account_payload

    def counted_payload(*args, **kwargs):
        payload_calls.append((args, kwargs))
        return original_payload(*args, **kwargs)

    monkeypatch.setattr(adapter, "_running_account_payload", counted_payload)
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
        adapter._write(verification, {
            "templateVersion": adapter.TEMPLATE_VERSION,
            "formulaErrorCount": 0,
            "totalsVerified": True,
            "layoutContract": {
                "version": adapter.LAYOUT_CONTRACT_VERSION,
                "passed": True,
            },
        })
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adapter.subprocess, "run", fake_builder)
    first = adapter.build_running_account_report(service, period="as-of", period_start=None, through=date(2026, 9, 6))
    repeated = adapter.build_running_account_report(service, period="as-of", period_start=None, through=date(2026, 9, 6))
    assert adapter.Path(first["workbook"]).name == "WB双店财务汇总_截至2026-09-06.xlsx"
    assert repeated["status"] == "NOOP"
    assert repeated["workbook"] == first["workbook"]
    assert repeated["templateVersion"] == "wb-owner-dashboard-v2.4"
    assert len(calls) == 1
    assert len(payload_calls) == 2


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
    data["withdrawals"] = [{"storeAlias": "test-store", "eventId": "receipt-mode-payout",
        "recordId": "receipt-mode-payout-record", "amountCny": "5000.00",
        "requestedAt": "2026-09-01T00:00:00+03:00",
        "settledAt": "2026-09-02T00:00:00+03:00", "settledByCutoff": True}]
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


def test_evidenced_receipt_supports_partial_coverage_without_claiming_opening_history(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    data["cashReceipts"] = [{
        "eventId": "portal-payment-1", "storeAlias": "test-store",
        "receivedAt": "2026-09-01T09:00:00+03:00", "amountCny": "900.00",
        "sourceType": "wb_settlement_account_credit_history",
        "evidenceReference": "evidence/payment-1", "evidenceSource": "cash.json",
    }]
    data["cashReceiptCoverage"] = [{
        "coverageId": "test-store-partial", "storeAlias": "test-store",
        "from": "2026-08-15", "through": "2026-09-06",
        "complete": True, "evidence": "complete export range",
    }]

    adapter.refresh_running_accounts(service, through=date(2026, 9, 6))

    with RunningAccount(adapter._settings(service)["db"]) as account:
        partial = account.report("2026-08-15", "2026-09-06", service.store_aliases)
        cumulative = account.report("2026-08-01", "2026-09-06", service.store_aliases)
        events = account.current_events("test-store")
    assert partial["stores"]["test-store"]["period"]["metrics"]["receipts_received"]["amount"] == "900.00"
    cumulative_metric = cumulative["stores"]["test-store"]["period"]["metrics"]["receipts_received"]
    assert cumulative_metric["amount"] is None
    assert cumulative_metric["known_subtotal"] == "900.00"
    receipt = next(event for event in events if event.event_id == "cash-received:portal-payment-1")
    assert receipt.metadata["basis"] == "independently_evidenced_account_receipt"


def test_refresh_rejects_receipt_identity_present_in_both_sources(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    data["cashReceipts"] = [{
        "eventId": "same-receipt", "storeAlias": "test-store",
        "receivedAt": "2026-09-01T09:00:00+03:00", "amountCny": "100.00",
        "sourceType": "wb_settlement_account_credit_history",
        "evidenceReference": "evidence/payment-1",
    }]
    path = adapter._settings(service)["decisions"]
    decisions = adapter._read(path)
    decisions["cashReceipts"] = [{
        "store": "test-store", "eventId": "same-receipt",
        "date": "2026-09-01", "amountCny": "100.00",
        "evidence": "owner confirmed payment",
    }]
    adapter._write(path, decisions)

    with pytest.raises(ValueError, match="both the historical cash registry"):
        adapter.refresh_running_accounts(service, through=date(2026, 9, 6))
    assert not adapter._settings(service)["db"].exists()


def test_refresh_rejects_same_receipt_reference_with_different_id(tmp_path, monkeypatch):
    service, data = _setup(tmp_path, monkeypatch)
    data["cashReceipts"] = [{
        "eventId": "source-receipt", "storeAlias": "test-store",
        "receivedAt": "2026-09-01T09:00:00+03:00", "amountCny": "100.00",
        "evidenceReference": "shared-payment-reference",
    }]
    path = adapter._settings(service)["decisions"]
    decisions = adapter._read(path)
    decisions["cashReceipts"] = [{
        "store": "test-store", "eventId": "owner-receipt",
        "date": "2026-09-01", "amountCny": "100.00",
        "evidence": "shared-payment-reference",
    }]
    adapter._write(path, decisions)

    with pytest.raises(ValueError, match="identity or evidence"):
        adapter.refresh_running_accounts(service, through=date(2026, 9, 6))


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
