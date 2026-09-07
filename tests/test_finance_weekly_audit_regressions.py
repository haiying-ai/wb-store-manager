from copy import deepcopy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from wb_manager.finance_calculations import calculate_financial_report
from wb_manager.finance_periods import FinancePeriod
from wb_manager.finance_service import FinanceService, _record_in_period, _with_advertising_reconciliation, _statistics_period_events
from wb_manager.finance_sources import normalize_advertising_cost_events, reconcile_advertising_cost_events
from wb_manager.finance_sync import _persist_source_rows


PERIOD = FinancePeriod("weekly", date(2026, 8, 31), date(2026, 9, 6))
POLICY = {
    "policyId": "audit-policy",
    "costs": {
        "wbCommission": {"fallbackRate": "0.17"},
        # Existing legacy adapter validates these supported contract rates.
        "managedOperations": {"rate": "0.02"},
        "advertisingAndReturns": {"combinedPoolRate": "0.03"},
        "paymentAndWithdrawal": {"fallbackRate": "0.006"},
        "fixedOperations": {"fallbackPerCompletedUnitCny": "7"},
    },
}


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"_source": "finance", "rrDate": "2026-08-30", "createDate": "2026-08-31"}, False),
        ({"_source": "finance", "rrDate": "2026-09-06", "createDate": "2026-09-07"}, True),
        ({"_source": "finance", "saleDt": "2026-08-30T23:00:00+03:00", "createDate": "2026-08-31"}, False),
        ({"_source": "order", "saleID": "S123", "date": "2026-08-30T12:00:00", "lastChangeDate": "2026-09-02"}, False),
        ({"_source": "order", "saleID": "R123", "date": "2026-09-04T16:41:17"}, True),
    ],
)
def test_period_uses_business_date_before_report_or_update_date(row, expected):
    assert _record_in_period(row, PERIOD) is expected


def _confirmed_order():
    return {
        "storeAlias": "new-store",
        "orderKey": "srid:old-order",
        "evidenceStatus": "COMPLETE",
        "cashReconciliationStatus": "PASS",
        "outcome": "completed",
        "netQuantity": "1",
        "salesAmountGCny": "1000.00",
        "netTurnoverCny": "1000.00",
        "forPayCny": "850.00",
        "strictOwnerProfitCny": "200.00",
        "ownerProfitCny": "200.00",
    }


def test_unchanged_prior_order_does_not_count_as_new_confirmed_order():
    order = _confirmed_order()
    result = FinanceService._period_delta_calculation(
        None,
        {"orders": [order], "issues": []},
        PERIOD,
        prior_calculation={"orders": [deepcopy(order)]},
    )
    assert result["orders"] == []
    assert result["totals"]["confirmedOrderCount"] == 0
    assert result["totals"]["orderCount"] == 0


def test_real_historical_financial_adjustment_remains_visible():
    prior = _confirmed_order()
    current = dict(prior, forPayCny="849.00", strictOwnerProfitCny="199.00")
    result = FinanceService._period_delta_calculation(
        None,
        {"orders": [current], "issues": []},
        PERIOD,
        prior_calculation={"orders": [prior]},
    )
    assert len(result["orders"]) == 1
    assert result["orders"][0]["forPayCny"] == "-1.00"
    assert result["orders"][0]["periodRecognition"] == "PRIOR_PERIOD_CORRECTION"


@pytest.mark.parametrize("source", ["statistics_sales", "statistics_orders"])
def test_statistics_rubles_never_populate_cny_or_fake_order_settlement(source):
    captured = []

    class Ledger:
        def upsert_sale(self, *args, **kwargs):
            captured.append((args, kwargs))
            return SimpleNamespace(inserted=True, changed=True)

    row = {
        "srid": "test-srid",
        "date": "2026-09-02T12:00:00",
        "lastChangeDate": "2026-09-02T12:10:00",
        "finishedPrice": 11110,
        "priceWithDisc": 12340,
        "totalPrice": 13000,
    }
    if source == "statistics_sales":
        row.update(saleID="S123", forPay=10489.25)
    _persist_source_rows(Ledger(), "new-store", source, [row], fetched_at=None)
    assert captured[0][1]["buyer_paid_cny"] is None
    assert captured[0][1]["for_pay_cny"] is None
    assert captured[0][0][2] == row


def _calculate_statistics_order(row):
    return calculate_financial_report(
        order_rows=[{
            "storeAlias": "new-store",
            "srid": "stats-order",
            "supplierArticle": "PHONE",
            "orderCreatedAt": "2026-08-31T12:00:00+03:00",
            **row,
        }],
        finance_rows=[],
        cost_selector=lambda *_args, **_kwargs: {
            "costVersionId": "cost-v1", "purchaseCostCny": "600"
        },
        policy=POLICY,
    ).to_dict()["orders"][0]


def test_statistics_sale_records_lifecycle_and_cost_without_inventing_cny_sales():
    order = _calculate_statistics_order({"saleID": "S1001", "date": "2026-09-06T10:00:00"})
    assert order["outcome"] == "completed"
    assert order["netQuantity"] == "1"
    assert order["completedAt"] == "2026-09-06T10:00:00+03:00"
    assert order["purchaseCostCny"] == "600.00"
    assert order["salesAmountGCny"] is None
    assert order["evidenceStatus"] == "BLOCKED"


def test_statistics_return_id_overrides_generic_completed_status():
    order = _calculate_statistics_order({
        "saleID": "R1002", "date": "2026-09-04T16:00:00", "status": "completed"
    })
    assert order["outcome"] == "returned"
    assert order["netQuantity"] == "0"
    assert order["salesAmountGCny"] is None
    assert order["completedAt"] is None


def test_explicit_completion_has_priority_over_statistics_date():
    order = _calculate_statistics_order({
        "saleID": "S123",
        "date": "2026-08-31T10:00:00",
        "completedAt": "2026-09-06T15:00:00+03:00",
    })
    assert order["completedAt"] == "2026-09-06T15:00:00+03:00"
    assert order["orderCreatedAt"] == "2026-08-31T12:00:00+03:00"


def test_store_advertising_cash_and_promo_remain_visible_without_orders():
    events = normalize_advertising_cost_events([
        {"updNum": 1, "advertId": 123, "updTime": "2026-09-02T12:00:00+03:00", "updSum": 275, "paymentType": "Balance"},
        {"updNum": 2, "advertId": 123, "updTime": "2026-09-02T13:00:00+03:00", "updSum": 600, "paymentType": "Bonus"},
    ], "new-store")
    result = _with_advertising_reconciliation(
        {"orders": [], "stores": [], "totals": {}},
        reconcile_advertising_cost_events(events),
    )
    assert result["totals"]["actualAdvertisingSpendCny"] == "275.00"
    assert result["totals"]["platformPromoAdvertisingSpendCny"] == "600.00"
    assert result["stores"][0]["actualAdvertisingSpendCny"] == "275.00"
    assert len(result["advertisingReconciliation"]["cashEventIds"]) == 1


def test_unknown_advertising_funding_is_not_reported_as_zero_cash_cost():
    events = normalize_advertising_cost_events([
        {"updNum": 1, "advertId": 123, "updTime": "2026-09-02T12:00:00+03:00", "updSum": 25, "paymentType": "new-unrecognized-source"},
    ], "new-store")
    result = _with_advertising_reconciliation(
        {"orders": [], "stores": [], "totals": {}},
        reconcile_advertising_cost_events(events),
    )
    assert result["totals"]["actualAdvertisingSpendCny"] is None
    assert result["totals"]["unclassifiedAdvertisingSpendCny"] == "25.00"


def test_statistics_return_is_visible_even_before_cny_refund_posts():
    report = calculate_financial_report(
        order_rows=[{
            "storeAlias": "new-store", "srid": "returned-order", "supplierArticle": "PHONE",
            "orderCreatedAt": "2026-08-27T12:00:00+03:00",
            "saleID": "R1002", "date": "2026-09-04T16:00:00", "forPay": -9123.45,
        }],
        finance_rows=[{
            "storeAlias": "new-store", "srid": "returned-order", "rrdId": "old-sale",
            "vendorCode": "PHONE", "orderDt": "2026-08-27T12:00:00+03:00",
            "saleDt": "2026-08-30T10:00:00+03:00", "rrDate": "2026-08-30",
            "retailPriceWithDisc": "1250.00", "forPayCny": "1062.50",
            "paymentFeeCny": "0", "quantity": 1, "sellerOperName": "sale",
        }],
        cost_selector=lambda *_args, **_kwargs: {"costVersionId": "cost-v1", "purchaseCostCny": "600"},
        policy=POLICY,
    ).to_dict()
    order = report["orders"][0]
    assert order["outcome"] == "returned"
    assert order["evidenceStatus"] == "BLOCKED"
    assert "incomplete_refund_evidence" in {issue["code"] for issue in order["issues"]}
    assert order["salesAmountGCny"] == "1250.00"
    assert report["totals"]["confirmedOrderCount"] == 0


def test_native_currency_sales_and_refunds_survive_missing_cny_settlements():
    raw = [{
        "storeAlias": "new-store", "saleID": "R1002", "date": "2026-09-04T16:00:00",
        "srid": "returned-order", "finishedPrice": -9200, "forPay": -9123.45,
    }]
    result = _statistics_period_events(raw, PERIOD)
    assert len(result) == 1
    assert result[0]["eventType"] == "return"
    assert result[0]["currency"] == "RUB"
    assert result[0]["sellerProceedsRub"] == -9123.45
    assert result[0]["occurredAt"] == "2026-09-04T16:00:00+03:00"
