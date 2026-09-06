from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal

import pytest

from wb_manager.finance_calculations import (
    aggregate_pool_for_orders,
    build_order_groups,
    calculate_financial_report,
)


POLICY = {
    "policyId": "finance-test-v1",
    "pricingReferences": {"activePricingPolicyId": "pricing-latest-v5"},
    "costs": {
        "wbCommission": {"fallbackRate": "0.15"},
        "managedOperations": {"rate": "0.02"},
        "advertisingAndReturns": {"combinedPoolRate": "0.03"},
        "paymentAndWithdrawal": {"fallbackRate": "0.008"},
        "fixedOperations": {"fallbackPerCompletedUnitCny": "12"},
    },
}


def cost_selector(cost: str = "600", version: str = "cost-v1"):
    def select(sku, created_at, *, order_completed_at=None):
        return {
            "costVersionId": version,
            "purchaseCostCny": cost,
            "effectiveFrom": "2026-01-01T00:00:00+00:00",
        }

    return select


def order_row(**overrides):
    row = {
        "storeAlias": "owner-store",
        "srid": "srid-1",
        "orderId": "order-1",
        "shkId": "shk-1",
        "sku": "PHONE-128-BLACK",
        "nmId": "10001",
        "orderCreatedAt": "2026-07-10T09:00:00+03:00",
        "completedAt": "2026-07-12T10:00:00+03:00",
        "status": "completed",
    }
    row.update(overrides)
    return row


def sale_finance(**overrides):
    row = {
        "storeAlias": "owner-store",
        "rrdId": "rrd-sale-1",
        "srid": "srid-1",
        "orderId": "order-1",
        "shkId": "shk-1",
        "sku": "PHONE-128-BLACK",
        "nmId": "10001",
        "orderDt": "2026-07-10T09:00:00+03:00",
        "retailPriceWithDisc": "1000",
        "retailAmount": "1",
        "returnAmount": "0",
        "commissionCny": "150",
        "paymentFeeCny": "8",
        "deliveryService": "8",
        "rebillLogisticCost": "0",
        "bonusTypeName": "销售时给客户",
        "storageFee": "2",
        "otherPlatformFeeCny": "1",
    }
    row.update(overrides)
    if "forPayCny" not in overrides:
        quantity = Decimal(str(row.get("quantity", row.get("retailAmount", "1"))))
        g = Decimal(str(row["retailPriceWithDisc"])) * quantity
        row["forPayCny"] = str(
            g - Decimal(str(row["commissionCny"])) - Decimal(str(row["paymentFeeCny"]))
        )
    return row


def return_finance(
    *,
    rrd_id="rrd-return-1",
    delivery="8",
    rebill="0",
    include_refund=True,
    **overrides,
):
    row = {
        "storeAlias": "owner-store",
        "rrdId": rrd_id,
        "srid": "srid-returned-by-wb",
        "orderId": "order-1",
        "shkId": "shk-1",
        "sku": "PHONE-128-BLACK",
        "nmId": "10001",
        "date": "2026-07-15T12:00:00+03:00",
        "retailPriceWithDisc": "1000" if include_refund else "0",
        "retailAmount": "0",
        "returnAmount": "1" if include_refund else "0",
        "commissionCny": "-150" if include_refund else "0",
        "paymentFeeCny": "-8" if include_refund else "0",
        "deliveryService": delivery,
        "rebillLogisticCost": rebill,
        "bonusTypeName": "从客户返回退货",
        "storageFee": "0",
        "otherPlatformFeeCny": "0",
    }
    row.update(overrides)
    if "forPayCny" not in overrides:
        if include_refund:
            g = -Decimal(str(row["retailPriceWithDisc"]))
            row["forPayCny"] = str(
                g
                - Decimal(str(row["commissionCny"]))
                - Decimal(str(row["paymentFeeCny"]))
            )
        else:
            row["forPayCny"] = "0"
    return row


def calculate(finance_rows, **kwargs):
    return calculate_financial_report(
        order_rows=kwargs.pop("order_rows", [order_row()]),
        finance_rows=finance_rows,
        cost_selector=kwargs.pop("cost_selector", cost_selector()),
        policy=kwargs.pop("policy", POLICY),
        **kwargs,
    )


def test_reporting_policy_is_locked_by_order_creation_timestamp():
    old_policy = deepcopy(POLICY)
    old_policy["policyId"] = "finance-old-3pct"
    new_policy = deepcopy(POLICY)
    new_policy["policyId"] = "finance-new-5pct"
    new_policy["costs"]["advertisingAndReturns"] = {
        "reserveAllocationMode": "SEPARATE_RETURN_AND_PROMOTION",
        "returnRiskReserveRate": "0.02",
        "promotionReserveRate": "0.03",
        "totalReserveRate": "0.05",
    }

    class Timeline:
        active_policy = new_policy

        @staticmethod
        def select(created_at):
            return new_policy if created_at >= "2026-09-06T13:08:38+03:00" else old_policy

    report = calculate_financial_report(
        order_rows=[
            order_row(orderCreatedAt="2026-09-06T13:08:37+03:00"),
            order_row(
                srid="srid-2",
                orderId="order-2",
                shkId="shk-2",
                orderCreatedAt="2026-09-06T13:08:38+03:00",
            ),
        ],
        finance_rows=[
            sale_finance(orderDt="2026-09-06T13:08:37+03:00", deliveryService="0"),
            sale_finance(
                rrdId="rrd-sale-2",
                srid="srid-2",
                orderId="order-2",
                shkId="shk-2",
                orderDt="2026-09-06T13:08:38+03:00",
                deliveryService="0",
            ),
        ],
        cost_selector=cost_selector(),
        policy=Timeline(),
    ).to_dict()

    old, new = sorted(report["orders"], key=lambda row: row["orderCreatedAt"])
    assert old["financialReportingPolicyVersionId"] == "finance-old-3pct"
    assert old["combinedPoolCny"] == "30.00"
    assert old["strictOwnerProfitCny"] == "189.00"
    assert new["financialReportingPolicyVersionId"] == "finance-new-5pct"
    assert new["returnRiskReserveCny"] == "20.00"
    assert new["promotionReserveCny"] == "30.00"
    assert new["totalPolicyReserveCny"] == "50.00"
    assert new["advertisingProvisionCny"] == "30.00"
    assert new["strictOwnerProfitCny"] == "169.00"


def test_separate_return_reserve_only_charges_excess_and_never_reduces_promotion():
    pool = aggregate_pool_for_orders(
        [
            {
                "reserveAllocationMode": "SEPARATE_RETURN_AND_PROMOTION",
                "combinedPoolCny": "50",
                "returnRiskReserveCny": "20",
                "promotionReserveCny": "30",
                "returnLogisticsCny": "25",
            }
        ]
    )

    assert pool.advertising_provision_cny == Decimal("30.00")
    assert pool.excess_return_loss_cny == Decimal("5.00")


def test_transitive_closure_uses_any_identity_but_never_crosses_stores():
    grouped = build_order_groups(
        [
            {"storeAlias": "a", "srid": "s-1", "orderId": "o-1"},
            {
                "storeAlias": "a",
                "srid": "s-2",
                "orderId": "o-1",
                "shkId": "h-9",
            },
            {"storeAlias": "a", "shkId": "h-9"},
            {"storeAlias": "b", "orderId": "o-1", "shkId": "h-9"},
        ]
    )

    assert len(grouped.groups) == 2
    group_a = next(group for group in grouped.groups if group.store_alias == "a")
    assert len(group_a.records) == 3
    assert group_a.identities["srid"] == ("s-1", "s-2")
    assert group_a.order_key == "srid:s-1"
    assert not grouped.issues


def test_finance_barcode_sku_does_not_conflict_with_vendor_article():
    selected_skus = []

    def select_cost(sku, created_at, *, order_completed_at=None):
        selected_skus.append(sku)
        return {
            "costVersionId": "cost-v1",
            "purchaseCostCny": "600",
            "effectiveFrom": "2026-01-01T00:00:00+00:00",
        }

    report = calculate_financial_report(
        order_rows=[
            order_row(
                sku=None,
                supplierArticle="IPHONE 13-128GB-BLACK",
            )
        ],
        finance_rows=[
            sale_finance(
                sku="2053238379859",
                vendorCode="IPHONE 13-128GB-BLACK",
            )
        ],
        cost_selector=select_cost,
        policy=POLICY,
    )

    assert selected_skus == ["IPHONE 13-128GB-BLACK"]
    assert not any(issue.code == "conflicting_sku" for issue in report.issues)


def test_unallocated_actual_ad_spend_is_store_reconciliation_not_finality_blocker():
    report = calculate_financial_report(
        order_rows=[],
        finance_rows=[],
        advertising_events=[
            {
                "eventId": "ad-without-settled-order",
                "storeAlias": "new-store",
                "amount": "100",
                "pnl": True,
            }
        ],
        cost_selector=cost_selector(),
        policy=POLICY,
    )

    issue = next(item for item in report.issues if item.code == "advertising_spend_unallocated")
    assert issue.unresolved_amount_cny == Decimal("100.00")
    assert issue.blocking is False


def test_completed_order_uses_actual_finance_components_and_decimal_contract():
    report = calculate(
        [sale_finance()],
        advertising_events=[
            {
                "eventId": "ad-1",
                "storeAlias": "owner-store",
                "eventType": "advertising_expense",
                "pnl": True,
                "nmId": "10001",
                "amount": "25",
            },
            {
                "eventId": "deposit-1",
                "storeAlias": "owner-store",
                "eventType": "advertising_funding",
                "pnl": False,
                "amount": "999",
            },
        ],
    )

    assert report.status == "FINAL"
    detail = report.orders[0]
    assert detail.outcome == "completed"
    assert detail.net_quantity == Decimal("1")
    assert detail.net_turnover_cny == Decimal("1000.00")
    assert detail.for_pay_cny == Decimal("842.00")
    assert detail.purchase_cost_cny == Decimal("600.00")
    assert detail.actual_commission_cny == Decimal("150.00")
    assert detail.commission_formula_cny == Decimal("150.00")
    assert detail.commission_reconciliation_difference_cny == Decimal("0.00")
    assert detail.actual_payment_fee_cny == Decimal("8.00")
    assert detail.account_increment_cny == Decimal("842.00")
    assert detail.for_pay_reconciliation_difference_cny == Decimal("0.00")
    assert detail.cash_reconciliation_status == "PASS"
    assert detail.actual_forward_logistics_cny == Decimal("8.00")
    assert detail.actual_storage_cny == Decimal("2.00")
    assert detail.actual_other_platform_fee_cny == Decimal("1.00")
    assert detail.managed_operations_cny == Decimal("20.00")
    assert detail.combined_pool_cny == Decimal("30.00")
    assert detail.rule_withdrawable_cny == Decimal("812.00")
    assert detail.advertising_provision_cny == Decimal("30.00")
    assert detail.actual_advertising_spend_cny == Decimal("25.00")
    assert detail.advertising_reconciliation_variance_cny == Decimal("5.00")
    assert detail.owner_profit_cny == Decimal("181.00")
    assert detail.strict_owner_profit_cny == Decimal("181.00")
    assert report.stores[0].owner_profit_cny == Decimal("181.00")
    assert report.stores[0].confirmed_order_count == 1
    assert report.stores[0].open_item_order_count == 0


@pytest.mark.parametrize(
    ("marker_field", "marker"),
    [
        ("operationName", "广告费用周期扣款"),
        ("sellerOperName", "Удержание за рекламу"),
        ("docTypeName", "Advertising promotion settlement deduction"),
    ],
)
def test_advertising_cycle_deduction_is_reconciliation_only_in_all_languages(
    marker_field, marker
):
    report = calculate(
        [
            sale_finance(
                deduction="30",
                otherPlatformFeeCny="0",
                **{marker_field: marker},
            )
        ]
    )

    detail = report.orders[0]
    assert detail.advertising_settlement_deduction_cny == Decimal("30.00")
    assert detail.actual_other_platform_fee_cny == Decimal("0.00")
    assert detail.strict_owner_profit_cny == Decimal("182.00")
    assert detail.component_sources["advertisingSettlementDeduction"] == (
        "financeActualReconciliationOnly"
    )
    payload = report.to_dict()
    assert payload["orders"][0]["advertisingSettlementDeductionCny"] == "30.00"
    assert payload["orders"][0]["otherPlatformCostCny"] == "0.00"
    assert payload["totals"]["advertisingSettlementDeductionCny"] == "30.00"


def test_unmarked_deduction_remains_an_other_platform_cost():
    report = calculate(
        [sale_finance(deduction="30", operationName="Generic service deduction")]
    )

    detail = report.orders[0]
    assert detail.advertising_settlement_deduction_cny == Decimal("0.00")
    assert detail.actual_other_platform_fee_cny == Decimal("31.00")
    assert detail.strict_owner_profit_cny == Decimal("151.00")


@pytest.mark.parametrize(
    ("return_rows", "expected"),
    [
        ([return_finance(delivery="8")], Decimal("8.00")),
        (
            [
                return_finance(delivery="8"),
                return_finance(
                    rrd_id="rrd-return-2",
                    delivery="8",
                    include_refund=False,
                ),
            ],
            Decimal("16.00"),
        ),
        ([return_finance(delivery="7.10", rebill="4.15")], Decimal("11.25")),
    ],
)
def test_return_logistics_is_actual_sum_for_8_16_and_other_amounts(
    return_rows, expected
):
    report = calculate([sale_finance(), *return_rows])

    assert report.status == "FINAL"
    detail = report.orders[0]
    assert detail.outcome == "returned"
    assert detail.net_quantity == Decimal("0")
    assert detail.net_turnover_cny == Decimal("0.00")
    assert detail.purchase_cost_cny == Decimal("0.00")
    assert detail.actual_return_logistics_cny == expected
    assert detail.advertising_provision_cny == Decimal("0.00")
    assert detail.excess_return_loss_cny == expected


def test_cancel_with_return_amount_zero_is_return_logistics_not_a_fixed_guess():
    cancellation = return_finance(
        delivery="6.75",
        include_refund=False,
        bonusTypeName="取消时给客户",
        buyerPaidCny="0",
    )
    report = calculate(
        [cancellation],
        order_rows=[order_row(status="cancelled", completedAt=None)],
    )

    assert report.status == "FINAL"
    detail = report.orders[0]
    assert detail.outcome == "returned"
    assert detail.net_quantity == Decimal("0")
    assert detail.actual_return_logistics_cny == Decimal("6.75")
    assert detail.owner_profit_cny == Decimal("-6.75")


def test_unknown_logistics_direction_blocks_and_never_defaults_to_8_or_16():
    unknown = sale_finance(bonusTypeName="物流服务方向待确认", deliveryService="13.40")
    report = calculate([unknown])

    assert report.status == "PARTIAL"
    detail = report.orders[0]
    assert detail.owner_profit_cny is None
    issue = next(issue for issue in report.blockers if issue.code == "unknown_logistics_direction")
    assert issue.unresolved_amount_cny == Decimal("13.40")
    assert detail.actual_return_logistics_cny == Decimal("0.00")


def test_return_event_without_settled_refund_and_return_charge_is_blocked():
    report = calculate(
        [sale_finance()],
        return_rows=[
            {
                "storeAlias": "owner-store",
                "eventId": "goods-return-1",
                "orderId": "order-1",
                "shkId": "shk-1",
                "status": "returned",
            }
        ],
    )

    codes = {issue.code for issue in report.blockers}
    assert "incomplete_refund_evidence" in codes
    assert "missing_return_logistics_evidence" in codes
    assert report.orders[0].owner_profit_cny is None


def test_correction_uses_signed_adjustments_and_earliest_creation_for_cost():
    calls = []

    def dated_cost(sku, created_at, *, order_completed_at=None):
        calls.append((sku, created_at, order_completed_at))
        amount = "600" if created_at < "2026-08-01" else "900"
        return {"costVersionId": "old-node", "purchaseCostCny": amount}

    correction = {
        "storeAlias": "owner-store",
        "rrdId": "rrd-correction-1",
        "srid": "new-srid-after-correction",
        "orderId": "order-1",
        "shkId": "shk-1",
        "sku": "PHONE-128-BLACK",
        "nmId": "10001",
        "date": "2026-08-20T00:00:00+03:00",
        "sellerOperName": "Корректировка цены",
        "buyerPaidAdjustmentCny": "-100",
        "forPayCny": "-84.20",
        "commissionCny": "-15",
        "paymentFeeCny": "-0.80",
        "storageFee": "0",
        "otherPlatformFeeCny": "0",
    }
    report = calculate(
        [sale_finance(), correction],
        cost_selector=dated_cost,
    )

    detail = report.orders[0]
    assert report.status == "FINAL"
    assert detail.order_key == "srid:srid-1"
    assert detail.outcome == "corrected"
    assert detail.net_quantity == Decimal("1")
    assert detail.net_turnover_cny == Decimal("900.00")
    assert detail.actual_commission_cny == Decimal("135.00")
    assert detail.actual_payment_fee_cny == Decimal("7.20")
    assert detail.purchase_cost_cny == Decimal("600.00")
    assert detail.cost_version_id == "old-node"
    assert calls[0][1] == "2026-07-10T09:00:00+03:00"


def test_latest_template_recalculation_changes_template_not_actual_settlement():
    changed_policy = deepcopy(POLICY)
    changed_policy["policyId"] = "finance-new-template"
    changed_policy["costs"]["wbCommission"]["fallbackRate"] = "0.20"
    changed_policy["costs"]["fixedOperations"]["fallbackPerCompletedUnitCny"] = "20"

    baseline = calculate([sale_finance()], policy=POLICY).orders[0]
    changed = calculate([sale_finance()], policy=changed_policy).orders[0]

    assert baseline.owner_profit_cny == changed.owner_profit_cny == Decimal("181.00")
    assert baseline.template_calculation["ownerProfitCny"] == Decimal("180.00")
    assert changed.template_calculation["ownerProfitCny"] == Decimal("122.00")


def test_partial_return_can_exceed_three_percent_pool_without_double_counting_ads():
    sale_two = sale_finance(
        retailPriceWithDisc="100",
        retailAmount="2",
        commissionCny="30",
        paymentFeeCny="1.60",
    )
    returned_one = return_finance(
        delivery="8",
        retailPriceWithDisc="100",
        returnAmount="1",
        commissionCny="-15",
        paymentFeeCny="-0.80",
    )
    report = calculate(
        [sale_two, returned_one],
        cost_selector=cost_selector("40"),
        advertising_events=[
            {
                "eventId": "ad-over-pool",
                "storeAlias": "owner-store",
                "eventType": "advertising_expense",
                "pnl": True,
                "amount": "50",
            }
        ],
    )

    detail = report.orders[0]
    assert detail.net_quantity == Decimal("1")
    assert detail.net_turnover_cny == Decimal("100.00")
    assert detail.combined_pool_cny == Decimal("3.00")
    assert detail.actual_return_logistics_cny == Decimal("8.00")
    assert detail.advertising_provision_cny == Decimal("0.00")
    assert detail.excess_return_loss_cny == Decimal("5.00")
    profit_before_reconciliation = detail.owner_profit_cny
    assert detail.strict_owner_profit_cny == Decimal("23.20")
    assert detail.actual_advertising_spend_cny == Decimal("50.00")
    assert detail.owner_profit_cny == profit_before_reconciliation


def test_store_period_pool_aggregates_before_consuming_return_logistics():
    """A return in one order consumes the other orders' unused 3% reserve."""

    sale_a = sale_finance(
        rrdId="rrd-sale-a",
        srid="srid-a",
        orderId="order-a",
        shkId="shk-a",
    )
    sale_b = sale_finance(
        rrdId="rrd-sale-b",
        srid="srid-b",
        orderId="order-b",
        shkId="shk-b",
    )
    return_b = return_finance(
        rrd_id="rrd-return-b",
        srid="srid-return-b",
        orderId="order-b",
        shkId="shk-b",
        delivery="40",
    )
    report = calculate(
        [sale_a, sale_b, return_b],
        order_rows=[
            order_row(srid="srid-a", orderId="order-a", shkId="shk-a"),
            order_row(srid="srid-b", orderId="order-b", shkId="shk-b"),
        ],
    )

    assert report.status == "FINAL"
    detail_a = next(row for row in report.orders if row.order_key == "srid:srid-a")
    detail_b = next(row for row in report.orders if row.order_key == "srid:srid-b")
    # Per-order values remain available for audit and explain why the store
    # level result differs from a sum of independent max() calculations.
    assert detail_a.combined_pool_cny == Decimal("30.00")
    assert detail_a.advertising_provision_cny == Decimal("30.00")
    assert detail_b.actual_return_logistics_cny == Decimal("40.00")
    assert detail_b.excess_return_loss_cny == Decimal("40.00")

    summary = report.stores[0]
    assert summary.combined_pool_cny == Decimal("30.00")
    assert summary.return_logistics_cny == Decimal("40.00")
    assert summary.advertising_provision_cny == Decimal("0.00")
    assert summary.excess_return_loss_cny == Decimal("10.00")
    # The excess return loss is consumed once at store/period scope, not once
    # for every order.
    assert summary.strict_owner_profit_cny == Decimal("160.00")
    assert report.to_dict()["totals"]["advertisingProvisionCny"] == "0.00"


def test_pool_aggregation_ignores_negative_normal_pool_contributions():
    aggregate = aggregate_pool_for_orders(
        [
            {"combinedPoolCny": "30.00", "returnLogisticsCny": "0.00"},
            {"combinedPoolCny": "-5.00", "returnLogisticsCny": "10.00"},
        ]
    )

    assert aggregate.combined_pool_cny == Decimal("30.00")
    assert aggregate.return_logistics_cny == Decimal("10.00")
    assert aggregate.advertising_provision_cny == Decimal("20.00")
    assert aggregate.excess_return_loss_cny == Decimal("0.00")


def test_all_totals_sum_store_pool_allocations_without_cross_store_netting():
    sale_owner = sale_finance(
        storeAlias="owner-store",
        rrdId="rrd-owner",
        srid="srid-owner",
        orderId="order-owner",
        shkId="shk-owner",
    )
    sale_new = sale_finance(
        storeAlias="new-store",
        rrdId="rrd-new",
        srid="srid-new",
        orderId="order-new",
        shkId="shk-new",
    )
    return_new = return_finance(
        rrd_id="rrd-return-new",
        storeAlias="new-store",
        srid="srid-return-new",
        orderId="order-new",
        shkId="shk-new",
        delivery="40",
    )
    report = calculate(
        [sale_owner, sale_new, return_new],
        order_rows=[
            order_row(
                storeAlias="owner-store",
                srid="srid-owner",
                orderId="order-owner",
                shkId="shk-owner",
            ),
            order_row(
                storeAlias="new-store",
                srid="srid-new",
                orderId="order-new",
                shkId="shk-new",
            ),
        ],
    )

    by_store = {summary.store_alias: summary for summary in report.stores}
    assert by_store["owner-store"].advertising_provision_cny == Decimal("30.00")
    assert by_store["new-store"].advertising_provision_cny == Decimal("0.00")
    # If ALL were calculated directly, the owner's 30 CNY pool could be
    # incorrectly netted against the new store's 40 CNY return charge.
    assert report.to_dict()["totals"]["advertisingProvisionCny"] == "30.00"
    assert report.to_dict()["totals"]["excessReturnLossCny"] == "40.00"


def test_open_order_does_not_null_confirmed_settled_totals():
    open_order = order_row(
        srid="srid-open",
        orderId="order-open",
        shkId="shk-open",
        status="in_delivery",
        completedAt=None,
    )
    report = calculate(
        [sale_finance()],
        order_rows=[order_row(), open_order],
    )

    assert report.status == "PARTIAL"
    summary = report.stores[0]
    assert summary.order_count == 2
    assert summary.confirmed_order_count == 1
    assert summary.open_item_order_count == 1
    assert summary.net_turnover_cny == Decimal("1000.00")
    assert summary.for_pay_cny == Decimal("842.00")
    assert summary.account_increment_cny == Decimal("842.00")
    assert summary.rule_withdrawable_cny == Decimal("812.00")
    assert summary.strict_owner_profit_cny == Decimal("181.00")


def test_cash_reconciliation_mismatch_is_open_and_not_confirmed_cash():
    report = calculate([sale_finance(forPayCny="800")])

    assert report.status == "PARTIAL"
    detail = report.orders[0]
    assert detail.cash_reconciliation_status == "FAIL"
    assert detail.commission_reconciliation_difference_cny == Decimal("-42.00")
    assert detail.for_pay_reconciliation_difference_cny == Decimal("42.00")
    assert detail.strict_owner_profit_cny is None
    assert report.stores[0].confirmed_order_count == 0
    assert report.stores[0].open_item_order_count == 1
    assert report.stores[0].account_increment_cny is None


def test_overlapping_collection_deduplicates_finance_rows_by_stable_id():
    repeated = sale_finance()
    report = calculate([repeated, dict(repeated)])

    assert report.status == "FINAL"
    assert report.orders[0].net_turnover_cny == Decimal("1000.00")
    assert report.orders[0].actual_commission_cny == Decimal("150.00")


def test_wb_retail_amount_is_money_and_quantity_controls_units():
    report = calculate(
        [
            sale_finance(
                quantity=1,
                retailAmount="3054.97",
                retailPriceWithDisc="3703.06",
                commissionCny="555.46",
            )
        ]
    )

    detail = report.orders[0]
    assert detail.net_quantity == Decimal("1")
    assert detail.net_turnover_cny == Decimal("3703.06")
    assert detail.purchase_cost_cny == Decimal("600.00")


def test_conflicting_payload_for_same_rrdid_is_blocked_not_double_counted():
    original = sale_finance()
    conflict = sale_finance(retailPriceWithDisc="1100")
    report = calculate([original, conflict])

    assert report.status == "PARTIAL"
    assert report.orders[0].net_turnover_cny in {
        Decimal("1000.00"),
        Decimal("1100.00"),
    }
    assert any(
        issue.code == "conflicting_duplicate_evidence" for issue in report.blockers
    )


def test_result_is_json_serializable_and_exposes_store_summary_and_anomalies():
    report = calculate([sale_finance()])
    payload = report.to_dict()

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert '"netTurnoverCny": "1000.00"' in encoded
    assert payload["orders"][0]["salesAmountGCny"] == "1000.00"
    assert payload["orders"][0]["actualCommissionCCny"] == "150.00"
    assert payload["orders"][0]["acquiringFeeCny"] == "8.00"
    assert payload["orders"][0]["forPayCny"] == "842.00"
    assert payload["orders"][0]["accountIncrementCny"] == "842.00"
    assert payload["orders"][0]["ruleWithdrawableCny"] == "812.00"
    assert payload["orders"][0]["strictOwnerProfitCny"] == "181.00"
    assert payload["orders"][0]["cashReconciliationStatus"] == "PASS"
    assert payload["stores"][0]["status"] == "FINAL"
    assert payload["stores"][0]["confirmedOrderCount"] == 1
    assert payload["stores"][0]["openItemOrderCount"] == 0
    assert payload["totals"]["ownerProfitCny"] == "181.00"
    assert payload["blockers"] == []
