from copy import deepcopy
from decimal import Decimal

import pytest

from wb_manager.finance_owner_views import build_owner_view, period_windows


def _event(event_id="sale:s:srid:sale.srid", *, store="s", day="2026-09-01", kind="sale", order_id="101", amounts=None, metadata=None, evidence=None, original=None):
    return {"store_id": store, "event_id": event_id, "event_date": day, "kind": kind, "currency": "CNY",
            "order_id": order_id, "amounts": amounts or {"gross_sales": "100", "product_cost": "60"},
            "metadata": metadata or {"sku": "PHONE", "srid": "sale.srid"}, "evidence": evidence or [],
            "original_sale_id": original}


def _payload(events=None, *, scenario=None, cutoff="2026-09-06", approved=True):
    rows = events if events is not None else [_event()]
    return {"periodEnd": cutoff, "periodStart": "2026-08-31", "postings": rows,
            "scenarioPostings": rows if scenario is None else scenario,
            "ownerDecisions": {"estimateApproval": "approved_reference_only" if approved else "pending"},
            "sources": {"stores": [{"storeAlias": "s", "openedAt": "2026-07-15"}], "settlementSales": [], "settlementCharges": [],
                        "rawOrders": [], "rawStatistics": [], "rawReturns": []}, "nativeStatistics": []}


def test_four_windows_use_cutoff_plus_one_as_run_day():
    p = _payload()
    windows = period_windows(p)
    assert list(windows) == ["yesterday", "last_week", "last_month", "cumulative"]
    assert windows["cumulative"]["start"] == "2026-07-15"
    assert (windows["last_month"]["start"], windows["last_month"]["end"]) == ("2026-08-01", "2026-08-31")
    assert (windows["last_week"]["start"], windows["last_week"]["end"]) == ("2026-08-31", "2026-09-06")
    assert windows["yesterday"]["start"] == windows["yesterday"]["end"] == "2026-09-06"
    midweek = period_windows(_payload(cutoff="2026-09-03"))
    assert midweek["last_week"]["end"] == "2026-08-30"
    year_boundary = period_windows(_payload(cutoff="2026-12-31"))
    assert year_boundary["last_month"]["start"] == "2026-12-01"


def test_scenario_replaces_actual_once_with_delta_not_second_full_row():
    a = _event(amounts={"gross_sales": None, "product_cost": "60", "agency_accrued": None})
    s = _event(amounts={"gross_sales": "100", "product_cost": "60", "agency_accrued": "2"})
    result = build_owner_view(_payload([a], scenario=[s]))["stores"]["s"]
    assert len(result["flowRows"]) == len(result["orderRows"]) == 1
    row = result["flowRows"][0]
    assert row["actualAmounts"]["gross_sales"] is None
    assert row["estimateAmounts"]["gross_sales"] == "100.00"
    assert row["estimateAmounts"]["product_cost"] == "0.00"
    assert row["amounts"]["product_cost"] == "60.00"
    assert row["amounts"]["gross_sales"] == "100.00"
    assert row["amountBasis"] == "含暂估"


def test_unapproved_estimate_does_not_enter_selected_amounts():
    a = _event(amounts={"gross_sales": None, "product_cost": "60"})
    s = _event(amounts={"gross_sales": "100", "product_cost": "60"})
    row = build_owner_view(_payload([a], scenario=[s], approved=False))["stores"]["s"]["flowRows"][0]
    assert row["amounts"]["gross_sales"] is None
    assert row["estimatedMetrics"] == []


def test_exact_srid_rrd_and_shk_links_recover_real_order_number():
    event = _event(event_id="finance:s:rrd:42:return_logistics", kind="cash", order_id=None,
                   amounts={"shared_pool_return_cost": "11"}, metadata={"sku": "PHONE"}, evidence=["finance:rrdId:42"])
    p = _payload([event])
    p["sources"]["settlementCharges"] = [{"storeAlias": "s", "rrdId": "42", "srid": "source.srid", "orderId": "0", "sku": "PHONE", "occurredAt": "2026-09-01"}]
    p["sources"]["rawOrders"] = [{"storeAlias": "s", "raw": {"srid": "source.srid", "sticker": "90001", "supplierArticle": "PHONE"}}]
    p["sources"]["rawReturns"] = [{"storeAlias": "s", "raw": {"orderId": 101, "shkId": "90001", "srid": "return.srid"}}]
    row = build_owner_view(p)["stores"]["s"]["orderRows"][0]
    assert row["orderId"] == "101"
    assert row["rrdId"] == "42"
    assert row["mappingStatus"] == "已关联"
    assert "source.srid" in row["srids"] and "return.srid" in row["srids"]


def test_srid_never_displayed_as_order_number_and_unmatched_fee_retained():
    event = _event(order_id="source.srid", metadata={"srid": "source.srid", "sku": "PHONE"})
    row = build_owner_view(_payload([event]))["stores"]["s"]["orderRows"][0]
    assert row["orderId"] is None
    assert row["mappingStatus"] == "待匹配"
    assert "source.srid" in row["srids"]


def test_marketplace_id_and_exact_rid_recover_finance_order_id_zero():
    fee = _event("finance:s:rrd:42:return_logistics", kind="cash", order_id="0", amounts={"shared_pool_return_cost": "11"},
                 metadata={"sku": "PHONE"}, evidence=["finance:rrdId:42"])
    p = _payload([fee])
    p["sources"]["settlementCharges"] = [{"storeAlias": "s", "rrdId": "42", "srid": "line.1.0", "orderId": "0", "sku": "PHONE"}]
    p["sources"]["rawOrders"] = [{"storeAlias": "s", "source": "marketplace-api/orders", "raw": {
        "id": 1234567890, "rid": "line.1.0", "orderUid": "line", "createdAt": "2026-08-25T09:00:14Z", "article": "PHONE", "price": 100}}]
    row = build_owner_view(p)["stores"]["s"]["orderRows"][0]
    assert row["orderId"] == "1234567890"
    assert row["mappingStatus"] == "已关联"
    # Another line of the same customer order is not this order.
    p["sources"]["rawOrders"][0]["raw"]["rid"] = "line.0.0"
    assert build_owner_view(p)["stores"]["s"]["orderRows"][0]["orderId"] is None


def test_marketplace_archive_provenance_preserved_and_future_created_at_filtered():
    event = _event(order_id=None)
    p = _payload([event])
    archived = {"storeAlias": "s", "source": "marketplace_orders", "sourceFile": "E:/audit/orders.json",
                "evidenceIds": ["marketplace-order-archive:E:/audit/orders.json"],
                "raw": {"id": 101, "rid": "sale.srid", "createdAt": "2026-09-01T09:00:00Z", "price": 100}}
    p["sources"]["rawOrders"] = [archived]
    row = build_owner_view(p)["stores"]["s"]["orderRows"][0]
    assert row["orderId"] == "101"
    assert "marketplace-order-archive:E:/audit/orders.json" in row["linkEvidence"]
    assert "sourceFile:E:/audit/orders.json" in row["linkEvidence"]
    archived["raw"]["createdAt"] = "2026-09-07T09:00:00Z"
    assert build_owner_view(p)["stores"]["s"]["orderRows"][0]["orderId"] is None


def test_advertising_and_withdrawal_are_store_flows_not_sku_allocations():
    sale = _event()
    ad = _event("ad:one", kind="cash", order_id=None, amounts={"cash_ad_spend": "9", "promo_credit_spend": "5"}, metadata={"sku": "PHONE"})
    withdrawal = _event("withdrawal:one", kind="cash", order_id=None, amounts={"withdrawal_requested": "20"}, metadata={"sku": "PHONE"})
    store = build_owner_view(_payload([sale, ad, withdrawal]))["stores"]["s"]
    assert len(store["orderRows"]) == 1
    assert len(store["flowRows"]) == 3
    assert all(row["orderId"] is None and row["mappingStatus"] == "店铺级" for row in store["flowRows"] if row["eventId"] != sale["event_id"])
    assert store["mappingSummary"]["partitionComplete"] is True

def test_completed_withdrawal_agency_fee_is_labeled_store_level_not_order_sale():
    fee = _event("withdrawal:one:agency-fee", kind="cash", order_id=None,
                 amounts={"agency_accrued": "2.00"},
                 metadata={"basis": "completed_withdrawal_agency_fee"})
    store = build_owner_view(_payload([fee]))["stores"]["s"]
    assert store["orderRows"] == []
    assert store["flowRows"][0]["kindLabel"] == "代运营费：提现到账×2%"
    assert store["flowRows"][0]["mappingStatus"] == "店铺级"
    assert store["flowRows"][0]["amounts"]["agency_accrued"] == "2.00"


@pytest.mark.parametrize("scope", ["store", "store_level"])
def test_explicit_store_scope_account_charge_not_forced_into_orders(scope):
    fee = _event("account:fee", kind="cash", order_id=None, amounts={"other_cost": "7"}, metadata={"scope": scope})
    store = build_owner_view(_payload([fee]))["stores"]["s"]
    assert store["orderRows"] == []
    assert store["flowRows"][0]["mappingStatus"] == "店铺级"
    assert store["flowRows"][0]["amounts"]["other_cost"] == "7.00"


def test_missing_order_id_alone_does_not_infer_store_scope():
    fee = _event("finance:unmatched-fee", kind="cash", order_id=None, amounts={"other_cost": "7"}, metadata={"scope": "unknown"})
    store = build_owner_view(_payload([fee]))["stores"]["s"]
    assert len(store["orderRows"]) == 1
    assert store["orderRows"][0]["mappingStatus"] == "待匹配"


def test_same_order_number_and_srid_in_another_store_do_not_link():
    p = _payload([_event(order_id=None)])
    p["sources"]["stores"].append({"storeAlias": "t", "openedAt": "2026-08-01"})
    p["sources"]["settlementSales"] = [{"storeAlias": "t", "orderId": "999", "srid": "sale.srid", "sku": "OTHER"}]
    assert build_owner_view(p)["stores"]["s"]["orderRows"][0]["orderId"] is None


def test_conflicting_order_identity_is_not_silently_assigned():
    p = _payload()
    p["sources"]["settlementSales"] = [{"storeAlias": "s", "orderId": "999", "srid": "sale.srid", "sku": "PHONE"}]
    result = build_owner_view(p)
    row = result["stores"]["s"]["orderRows"][0]
    assert row["orderId"] is None
    assert row["mappingStatus"] == "待匹配"
    assert result["issues"][0]["reason"] == "conflicting_order_identities"


def test_refund_is_separate_business_event_and_joins_original_sale():
    sale = _event(day="2026-08-30")
    refund = _event("refund:s:srid:sale.srid:part:1", day="2026-09-04", kind="refund", order_id=None,
                    amounts={"gross_sales": "-100", "product_cost": "-60"}, metadata={"sku": "PHONE"},
                    evidence=["WB statistics refund:R123456"], original=sale["event_id"])
    rows = build_owner_view(_payload([sale, refund]))["stores"]["s"]["orderRows"]
    assert len(rows) == 2
    assert [row["orderId"] for row in rows] == ["101", "101"]
    assert rows[1]["saleId"] == "R123456"
    assert rows[1]["srid"] == "sale.srid"
    assert "last_week" not in rows[0]["periodMembership"] and "last_week" in rows[1]["periodMembership"]


def test_every_metric_reconciles_order_partition_plus_nonorder_partition():
    sale = _event(amounts={"gross_sales": "100", "product_cost": "60"})
    ad = _event("ad:one", kind="cash", order_id=None, amounts={"cash_ad_spend": "9", "promo_credit_spend": "5"})
    store = build_owner_view(_payload([sale, ad]))["stores"]["s"]
    for metric in store["flowRows"][0]["amounts"]:
        all_flows = sum((Decimal(row["amounts"][metric] or "0") for row in store["flowRows"]), Decimal(0))
        partition = store["orderRows"] + [row for row in store["flowRows"] if not row["isOrderEvent"]]
        assert all_flows == sum((Decimal(row["amounts"][metric] or "0") for row in partition), Decimal(0))


def test_future_rows_are_excluded_and_payload_is_unchanged():
    p = _payload([_event(), _event("future", day="2026-09-07")])
    before = deepcopy(p)
    assert len(build_owner_view(p)["stores"]["s"]["flowRows"]) == 1
    assert p == before


def test_duplicate_id_is_idempotent_but_conflict_is_rejected():
    event = _event()
    assert len(build_owner_view(_payload([event, deepcopy(event)]))["stores"]["s"]["flowRows"]) == 1
    different = deepcopy(event)
    different["amounts"]["gross_sales"] = "101"
    with pytest.raises(ValueError, match="Conflicting"):
        build_owner_view(_payload([event, different]))


def test_scenario_cannot_move_original_event_to_another_day():
    with pytest.raises(ValueError, match="Scenario changes"):
        build_owner_view(_payload([_event()], scenario=[_event(day="2026-09-02")]))
