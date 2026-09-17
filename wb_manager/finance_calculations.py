from __future__ import annotations

import hashlib
import inspect
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .finance_sources import classify_logistics_row, order_identity


MONEY_QUANTUM = Decimal("0.01")
ZERO = Decimal("0")


class FinanceCalculationError(RuntimeError):
    """Raised when the calculation contract itself is invalid."""


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    message: str
    blocking: bool = True
    store_alias: str | None = None
    order_key: str | None = None
    evidence_ids: tuple[str, ...] = ()
    unresolved_amount_cny: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "blocking": self.blocking,
            "storeAlias": self.store_alias,
            "orderKey": self.order_key,
            "evidenceIds": list(self.evidence_ids),
            "unresolvedAmountCny": _money_text(self.unresolved_amount_cny),
        }


@dataclass(frozen=True)
class EvidenceRecord:
    source: str
    store_alias: str
    row: Mapping[str, Any]
    identities: tuple[tuple[str, str], ...]
    evidence_id: str


@dataclass(frozen=True)
class OrderEvidenceGroup:
    store_alias: str
    order_key: str
    identities: Mapping[str, tuple[str, ...]]
    records: tuple[EvidenceRecord, ...]


@dataclass(frozen=True)
class GroupingResult:
    groups: tuple[OrderEvidenceGroup, ...]
    issues: tuple[ReconciliationIssue, ...]


@dataclass(frozen=True)
class CostSelection:
    version_id: str
    purchase_cost_per_unit_cny: Decimal
    effective_from: str | None = None
    effective_to: str | None = None


@dataclass
class OrderCalculation:
    store_alias: str
    order_key: str
    identities: Mapping[str, tuple[str, ...]]
    order_created_at: str | None
    completed_at: str | None
    sku: str | None
    nm_id: str | None
    outcome: str
    net_quantity: Decimal
    net_turnover_cny: Decimal | None
    for_pay_cny: Decimal | None
    purchase_cost_cny: Decimal | None
    actual_commission_cny: Decimal | None
    commission_formula_cny: Decimal | None
    commission_reconciliation_difference_cny: Decimal | None
    commission_used_cny: Decimal | None
    actual_payment_fee_cny: Decimal | None
    payment_fee_used_cny: Decimal | None
    account_increment_cny: Decimal | None
    for_pay_reconciliation_difference_cny: Decimal | None
    cash_reconciliation_status: str
    actual_forward_logistics_cny: Decimal | None
    actual_storage_cny: Decimal | None
    actual_other_platform_fee_cny: Decimal | None
    forward_operations_used_cny: Decimal | None
    managed_operations_cny: Decimal | None
    combined_pool_cny: Decimal | None
    actual_return_logistics_cny: Decimal | None
    advertising_provision_cny: Decimal | None
    excess_return_loss_cny: Decimal | None
    rule_withdrawable_cny: Decimal | None
    strict_owner_profit_cny: Decimal | None
    advertising_settlement_deduction_cny: Decimal = ZERO
    actual_advertising_spend_cny: Decimal = ZERO
    advertising_reconciliation_variance_cny: Decimal | None = None
    owner_profit_cny: Decimal | None = None
    template_calculation: Mapping[str, Decimal | None] = field(default_factory=dict)
    cost_version_id: str | None = None
    component_sources: Mapping[str, str] = field(default_factory=dict)
    issues: tuple[ReconciliationIssue, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    financial_reporting_policy_id: str | None = None
    selling_price_policy_id: str | None = None
    reserve_allocation_mode: str = "LEGACY_SHARED_POOL"
    managed_operations_rate: Decimal = Decimal("0.02")
    return_risk_reserve_rate: Decimal = ZERO
    promotion_reserve_rate: Decimal = Decimal("0.03")
    total_reserve_rate: Decimal = Decimal("0.03")
    return_risk_reserve_cny: Decimal | None = None
    promotion_reserve_cny: Decimal | None = None

    @property
    def evidence_status(self) -> str:
        return "BLOCKED" if any(issue.blocking for issue in self.issues) else "COMPLETE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "storeAlias": self.store_alias,
            "orderKey": self.order_key,
            "identities": {key: list(values) for key, values in self.identities.items()},
            "orderCreatedAt": self.order_created_at,
            "completedAt": self.completed_at,
            "sku": self.sku,
            "nmId": self.nm_id,
            "outcome": self.outcome,
            "netQuantity": _quantity_text(self.net_quantity),
            "salesAmountGCny": _money_text(self.net_turnover_cny),
            "netTurnoverCny": _money_text(self.net_turnover_cny),
            "forPayCny": _money_text(self.for_pay_cny),
            "purchaseCostCny": _money_text(self.purchase_cost_cny),
            "actualCommissionCCny": _money_text(self.actual_commission_cny),
            "actualWbCommissionCny": _money_text(self.actual_commission_cny),
            "commissionFormulaCny": _money_text(self.commission_formula_cny),
            "commissionReconciliationDifferenceCny": _money_text(
                self.commission_reconciliation_difference_cny
            ),
            "wbCommissionCny": _money_text(self.commission_used_cny),
            "acquiringFeeCny": _money_text(self.actual_payment_fee_cny),
            "actualPaymentFeeCny": _money_text(self.actual_payment_fee_cny),
            "paymentFeeCny": _money_text(self.payment_fee_used_cny),
            "accountIncrementCny": _money_text(self.account_increment_cny),
            "forPayReconciliationDifferenceCny": _money_text(
                self.for_pay_reconciliation_difference_cny
            ),
            "cashReconciliationStatus": self.cash_reconciliation_status,
            "forwardLogisticsCny": _money_text(self.actual_forward_logistics_cny),
            "storageAndOperationsCny": _money_text(self.actual_storage_cny),
            "otherPlatformCostCny": _money_text(self.actual_other_platform_fee_cny),
            "forwardOperationsUsedCny": _money_text(self.forward_operations_used_cny),
            "managedOperationsCny": _money_text(self.managed_operations_cny),
            "financialReportingPolicyVersionId": self.financial_reporting_policy_id,
            "sellingPricePolicyVersionId": self.selling_price_policy_id,
            "reserveAllocationMode": self.reserve_allocation_mode,
            "managedOperationsRate": str(self.managed_operations_rate),
            "returnRiskReserveRate": str(self.return_risk_reserve_rate),
            "promotionReserveRate": str(self.promotion_reserve_rate),
            "totalReserveRate": str(self.total_reserve_rate),
            "returnRiskReserveCny": _money_text(self.return_risk_reserve_cny),
            "promotionReserveCny": _money_text(self.promotion_reserve_cny),
            "totalPolicyReserveCny": _money_text(self.combined_pool_cny),
            "combinedAdvertisingAndReturnPoolCny": _money_text(
                self.combined_pool_cny
            ),
            "combinedPoolCny": _money_text(self.combined_pool_cny),
            "returnLogisticsCny": _money_text(self.actual_return_logistics_cny),
            "advertisingProvisionCny": _money_text(self.advertising_provision_cny),
            "excessReturnLossCny": _money_text(self.excess_return_loss_cny),
            "ruleWithdrawableCny": _money_text(self.rule_withdrawable_cny),
            "advertisingSettlementDeductionCny": _money_text(
                self.advertising_settlement_deduction_cny
            ),
            "actualAdvertisingSpendCny": _money_text(self.actual_advertising_spend_cny),
            "advertisingReconciliationVarianceCny": _money_text(
                self.advertising_reconciliation_variance_cny
            ),
            "strictOwnerProfitCny": _money_text(self.strict_owner_profit_cny),
            "ownerProfitCny": _money_text(self.owner_profit_cny),
            "template": {
                key: _money_text(value) for key, value in self.template_calculation.items()
            },
            "costVersionId": self.cost_version_id,
            "componentSources": dict(self.component_sources),
            "evidenceStatus": self.evidence_status,
            "issues": [issue.to_dict() for issue in self.issues],
            "evidenceIds": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class StoreSummary:
    store_alias: str
    order_count: int
    completed_count: int
    returned_count: int
    corrected_count: int
    net_quantity: Decimal
    net_turnover_cny: Decimal | None
    purchase_cost_cny: Decimal | None
    commission_cny: Decimal | None
    payment_fee_cny: Decimal | None
    forward_operations_cny: Decimal | None
    managed_operations_cny: Decimal | None
    return_logistics_cny: Decimal | None
    advertising_provision_cny: Decimal | None
    excess_return_loss_cny: Decimal | None
    advertising_settlement_deduction_cny: Decimal | None
    actual_advertising_spend_cny: Decimal | None
    other_platform_cost_cny: Decimal | None
    owner_profit_cny: Decimal | None
    unresolved_order_count: int
    issue_count: int
    confirmed_order_count: int
    open_item_order_count: int
    for_pay_cny: Decimal | None
    account_increment_cny: Decimal | None
    combined_pool_cny: Decimal | None
    rule_withdrawable_cny: Decimal | None
    strict_owner_profit_cny: Decimal | None
    return_risk_reserve_cny: Decimal | None = None
    promotion_reserve_cny: Decimal | None = None

    @property
    def status(self) -> str:
        return "PARTIAL" if self.unresolved_order_count or self.issue_count else "FINAL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "storeAlias": self.store_alias,
            "status": self.status,
            "orderCount": self.order_count,
            "confirmedOrderCount": self.confirmed_order_count,
            "openItemOrderCount": self.open_item_order_count,
            "completedCount": self.completed_count,
            "returnedCount": self.returned_count,
            "correctedCount": self.corrected_count,
            "netQuantity": _quantity_text(self.net_quantity),
            "salesAmountGCny": _money_text(self.net_turnover_cny),
            "netTurnoverCny": _money_text(self.net_turnover_cny),
            "forPayCny": _money_text(self.for_pay_cny),
            "purchaseCostCny": _money_text(self.purchase_cost_cny),
            "actualCommissionCCny": _money_text(self.commission_cny),
            "wbCommissionCny": _money_text(self.commission_cny),
            "acquiringFeeCny": _money_text(self.payment_fee_cny),
            "paymentFeeCny": _money_text(self.payment_fee_cny),
            "accountIncrementCny": _money_text(self.account_increment_cny),
            "forwardOperationsCny": _money_text(self.forward_operations_cny),
            "managedOperationsCny": _money_text(self.managed_operations_cny),
            "returnRiskReserveCny": _money_text(self.return_risk_reserve_cny),
            "promotionReserveCny": _money_text(self.promotion_reserve_cny),
            "totalPolicyReserveCny": _money_text(self.combined_pool_cny),
            "combinedAdvertisingAndReturnPoolCny": _money_text(
                self.combined_pool_cny
            ),
            "combinedPoolCny": _money_text(self.combined_pool_cny),
            "returnLogisticsCny": _money_text(self.return_logistics_cny),
            "advertisingProvisionCny": _money_text(self.advertising_provision_cny),
            "excessReturnLossCny": _money_text(self.excess_return_loss_cny),
            "ruleWithdrawableCny": _money_text(self.rule_withdrawable_cny),
            "advertisingSettlementDeductionCny": _money_text(
                self.advertising_settlement_deduction_cny
            ),
            "actualAdvertisingSpendCny": _money_text(
                self.actual_advertising_spend_cny
            ),
            "otherPlatformCostCny": _money_text(self.other_platform_cost_cny),
            "strictOwnerProfitCny": _money_text(self.strict_owner_profit_cny),
            "ownerProfitCny": _money_text(self.owner_profit_cny),
            "unresolvedOrderCount": self.unresolved_order_count,
            "issueCount": self.issue_count,
        }


@dataclass(frozen=True)
class FinanceCalculationResult:
    reporting_policy_id: str
    pricing_policy_id: str
    status: str
    orders: tuple[OrderCalculation, ...]
    stores: tuple[StoreSummary, ...]
    issues: tuple[ReconciliationIssue, ...]
    managed_operations_rate: Decimal = Decimal("0.02")
    return_risk_reserve_rate: Decimal = ZERO
    promotion_reserve_rate: Decimal = Decimal("0.03")
    total_reserve_rate: Decimal = Decimal("0.03")
    reserve_allocation_mode: str = "LEGACY_SHARED_POOL"
    reporting_policy_versions: tuple[Mapping[str, Any], ...] = ()

    @property
    def blockers(self) -> tuple[ReconciliationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    def to_dict(self) -> dict[str, Any]:
        totals = _summary_for_orders("ALL", self.orders, self.issues)
        return {
            "status": self.status,
            "policy": {
                "reportingPolicyId": self.reporting_policy_id,
                "pricingPolicyId": self.pricing_policy_id,
                "managedOperationsRate": str(self.managed_operations_rate),
                "returnRiskReserveRate": str(self.return_risk_reserve_rate),
                "promotionReserveRate": str(self.promotion_reserve_rate),
                "totalReserveRate": str(self.total_reserve_rate),
                "combinedPoolRate": str(self.total_reserve_rate),
                "reserveAllocationMode": self.reserve_allocation_mode,
                "reportingPolicyIdsUsed": sorted(
                    {
                        order.financial_reporting_policy_id
                        for order in self.orders
                        if order.financial_reporting_policy_id
                    }
                ),
                "reportingPolicyVersions": [
                    dict(item) for item in self.reporting_policy_versions
                ],
            },
            "orders": [order.to_dict() for order in self.orders],
            "stores": [store.to_dict() for store in self.stores],
            "totals": totals.to_dict(),
            "blockers": [issue.to_dict() for issue in self.blockers],
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class PoolAggregation:
    """Store/period-level allocation of the combined three-percent pool.

    Order rows intentionally retain their own ``combinedPoolCny`` and
    ``returnLogisticsCny`` values for auditability.  The funding decision,
    however, is made at store + reporting-period scope: all positive-G pool
    amounts are added first, then the actual return-logistics total is
    deducted once.  Keeping this result as a small value object makes that
    rule reusable by report and funding code without mutating order rows.
    """

    combined_pool_cny: Decimal | None
    return_logistics_cny: Decimal | None
    advertising_provision_cny: Decimal | None
    excess_return_loss_cny: Decimal | None
    return_risk_reserve_cny: Decimal | None = None
    promotion_reserve_cny: Decimal | None = None

    def to_dict(self) -> dict[str, Decimal | None]:
        return {
            "combinedPoolCny": self.combined_pool_cny,
            "returnLogisticsCny": self.return_logistics_cny,
            "advertisingProvisionCny": self.advertising_provision_cny,
            "excessReturnLossCny": self.excess_return_loss_cny,
            "returnRiskReserveCny": self.return_risk_reserve_cny,
            "promotionReserveCny": self.promotion_reserve_cny,
            "totalPolicyReserveCny": self.combined_pool_cny,
        }


def aggregate_pool_for_orders(
    orders: Iterable[Mapping[str, Any] | OrderCalculation],
) -> PoolAggregation:
    """Aggregate the advertising/return pool for one store and one period.

    ``orders`` must already be restricted to the desired store/period and to
    the rows eligible for cash allocation (normally COMPLETE + cash PASS).
    The helper deliberately does not infer a missing amount as zero.  A
    missing pool or return-logistics value therefore propagates as ``None``
    so callers can keep the report open rather than silently underfunding the
    advertising reserve.
    """

    rows = tuple(orders)
    if not rows:
        return PoolAggregation(ZERO, ZERO, ZERO, ZERO, ZERO, ZERO)

    raw_pool_values = [
        _pool_order_value(row, "combined_pool_cny", "combinedPoolCny") for row in rows
    ]
    return_values = [
        _pool_order_value(row, "actual_return_logistics_cny", "returnLogisticsCny")
        for row in rows
    ]
    risk_values = [_reserve_component(row, "returnRiskReserveCny") for row in rows]
    promotion_values = [_reserve_component(row, "promotionReserveCny") for row in rows]
    if any(
        value is None
        for value in raw_pool_values + return_values + risk_values + promotion_values
    ):
        return PoolAggregation(None, None, None, None, None, None)

    correction_indexes = {
        index
        for index, row in enumerate(rows)
        if isinstance(row, Mapping)
        and str(row.get("periodRecognition") or "") == "PRIOR_PERIOD_CORRECTION"
    }
    base_indexes = [index for index in range(len(rows)) if index not in correction_indexes]
    correction_rows = [rows[index] for index in sorted(correction_indexes)]
    combined_pool = _money(
        sum(
            (
                raw_pool_values[index]
                if index in correction_indexes
                else max(ZERO, raw_pool_values[index])
            )
            for index in range(len(rows))
        )
    )
    return_logistics = _money(sum(return_values, ZERO))
    return_risk = _money(
        sum(
            (
                risk_values[index]
                if index in correction_indexes
                else max(ZERO, risk_values[index])
            )
            for index in range(len(rows))
        )
    )
    promotion = _money(
        sum(
            (
                promotion_values[index]
                if index in correction_indexes
                else max(ZERO, promotion_values[index])
            )
            for index in range(len(rows))
        )
    )

    legacy_indexes = [
        index
        for index in base_indexes
        if _reserve_mode(rows[index]) != "SEPARATE_RETURN_AND_PROMOTION"
    ]
    separate_indexes = [index for index in base_indexes if index not in legacy_indexes]
    legacy_pool = sum((max(ZERO, raw_pool_values[index]) for index in legacy_indexes), ZERO)
    legacy_return = sum((return_values[index] for index in legacy_indexes), ZERO)
    separate_return = sum((return_values[index] for index in separate_indexes), ZERO)
    separate_risk = sum((max(ZERO, risk_values[index]) for index in separate_indexes), ZERO)
    separate_promotion = sum(
        (max(ZERO, promotion_values[index]) for index in separate_indexes), ZERO
    )
    advertising_provision = _money(
        max(ZERO, legacy_pool - legacy_return) + separate_promotion
    )
    excess_return_loss = _money(
        max(ZERO, legacy_return - legacy_pool)
        + max(ZERO, separate_return - separate_risk)
    )
    if correction_rows:
        correction_ad_values = [
            _pool_order_value(row, "advertising_provision_cny", "advertisingProvisionCny")
            for row in correction_rows
        ]
        correction_excess_values = [
            _pool_order_value(row, "excess_return_loss_cny", "excessReturnLossCny")
            for row in correction_rows
        ]
        if any(value is None for value in correction_ad_values + correction_excess_values):
            return PoolAggregation(None, None, None, None, None, None)
        advertising_provision = _money(
            advertising_provision + sum(correction_ad_values, ZERO)
        )
        excess_return_loss = _money(
            excess_return_loss + sum(correction_excess_values, ZERO)
        )
    return PoolAggregation(
        combined_pool,
        return_logistics,
        advertising_provision,
        excess_return_loss,
        return_risk,
        promotion,
    )


def _reserve_mode(order: Mapping[str, Any] | OrderCalculation) -> str:
    if isinstance(order, Mapping):
        return str(
            order.get("reserveAllocationMode")
            or order.get("reserve_allocation_mode")
            or "LEGACY_SHARED_POOL"
        )
    return str(order.reserve_allocation_mode or "LEGACY_SHARED_POOL")


def _reserve_component(
    order: Mapping[str, Any] | OrderCalculation, key: str
) -> Decimal | None:
    attribute = (
        "return_risk_reserve_cny"
        if key == "returnRiskReserveCny"
        else "promotion_reserve_cny"
    )
    value = _pool_order_value(order, attribute, key)
    if value is not None:
        return value
    # Serialized snapshots created before the split only contain the shared
    # three-percent pool.  Treat it as the legacy promotional/shared reserve.
    if key == "returnRiskReserveCny":
        return ZERO
    return _pool_order_value(order, "combined_pool_cny", "combinedPoolCny")


def _pool_order_value(
    order: Mapping[str, Any] | OrderCalculation,
    attribute: str,
    key: str,
) -> Decimal | None:
    """Read a pool component from either a dataclass row or serialized row."""

    if isinstance(order, Mapping):
        value = order.get(key)
        if value is None:
            # Accept the snake_case names used by in-process ledger rows as
            # well as the camelCase names emitted in report snapshots.
            value = order.get(attribute)
        if value is None and key == "combinedPoolCny":
            value = order.get("combinedAdvertisingAndReturnPoolCny")
        if value is None and key == "returnLogisticsCny":
            value = order.get("actualReturnLogisticsCny")
    else:
        value = getattr(order, attribute, None)
    if value is None:
        return None
    return _money(_decimal(value, key))


def _sum_store_pool_aggregates(
    orders: Sequence[OrderCalculation],
) -> PoolAggregation:
    """Aggregate independently per store, then add the store results.

    This is used for the display-only ALL row.  It prevents return logistics
    in one store from consuming another store's advertising reserve.
    """

    by_store: dict[str, list[OrderCalculation]] = defaultdict(list)
    for order in orders:
        by_store[order.store_alias].append(order)
    aggregates = [aggregate_pool_for_orders(rows) for rows in by_store.values()]
    if not aggregates:
        return PoolAggregation(ZERO, ZERO, ZERO, ZERO, ZERO, ZERO)
    if any(
        value is None
        for aggregate in aggregates
        for value in (
            aggregate.combined_pool_cny,
            aggregate.return_logistics_cny,
            aggregate.advertising_provision_cny,
            aggregate.excess_return_loss_cny,
            aggregate.return_risk_reserve_cny,
            aggregate.promotion_reserve_cny,
        )
    ):
        return PoolAggregation(None, None, None, None, None, None)
    return PoolAggregation(
        _money(sum((item.combined_pool_cny for item in aggregates), ZERO)),
        _money(sum((item.return_logistics_cny for item in aggregates), ZERO)),
        _money(sum((item.advertising_provision_cny for item in aggregates), ZERO)),
        _money(sum((item.excess_return_loss_cny for item in aggregates), ZERO)),
        _money(sum((item.return_risk_reserve_cny for item in aggregates), ZERO)),
        _money(sum((item.promotion_reserve_cny for item in aggregates), ZERO)),
    )


@dataclass(frozen=True)
class _PolicyView:
    reporting_policy_id: str
    pricing_policy_id: str
    managed_rate: Decimal
    pool_rate: Decimal
    reserve_allocation_mode: str
    return_risk_rate: Decimal
    promotion_rate: Decimal
    commission_fallback_rate: Decimal | None
    payment_fallback_rate: Decimal | None
    fixed_operations_fallback_cny: Decimal | None


def build_order_groups(
    rows: Iterable[Mapping[str, Any]],
    *,
    default_store_alias: str | None = None,
    source: str = "evidence",
) -> GroupingResult:
    """Build store-isolated transitive order groups over srid/orderId/shkId.

    A row may bridge two otherwise different identities.  For example, a finance row
    carrying a new srid and an old orderId joins both srid branches.  Identity values
    never join across stores and key types are not conflated.
    """

    records = tuple(
        _record(row, source=source, default_store_alias=default_store_alias)
        for row in rows
    )
    return _group_records(records)


def calculate_financial_report(
    *,
    order_rows: Iterable[Mapping[str, Any]],
    finance_rows: Iterable[Mapping[str, Any]],
    cost_selector: Callable[..., Any],
    policy: Any,
    sale_rows: Iterable[Mapping[str, Any]] = (),
    return_rows: Iterable[Mapping[str, Any]] = (),
    advertising_events: Iterable[Mapping[str, Any]] = (),
    default_store_alias: str | None = None,
) -> FinanceCalculationResult:
    """Reconcile order evidence into actual and latest-template financial results.

    The function is calculation-only: it performs no I/O and mutates no caller data.
    Actual advertising spend is allocated for reconciliation but is never subtracted
    from owner profit after the contractual three-percent pool has been charged.
    """

    policy_view = _policy_view(getattr(policy, "active_policy", policy))
    records: list[EvidenceRecord] = []
    for source, collection in (
        ("order", order_rows),
        ("sale", sale_rows),
        ("finance", finance_rows),
        ("return", return_rows),
    ):
        records.extend(
            _record(row, source=source, default_store_alias=default_store_alias)
            for row in collection
        )
    grouping = _group_records(tuple(records))
    orders = [
        _calculate_group(group, cost_selector=cost_selector, policy=policy)
        for group in grouping.groups
    ]
    ad_issues = _allocate_actual_advertising(
        orders,
        advertising_events,
        default_store_alias=default_store_alias,
    )
    issues = list(grouping.issues)
    for order in orders:
        issues.extend(order.issues)
    issues.extend(ad_issues)
    issues = _deduplicate_issues(issues)
    store_aliases = sorted(
        {order.store_alias for order in orders}
        | {issue.store_alias for issue in issues if issue.store_alias}
    )
    stores = tuple(
        _summary_for_orders(
            alias,
            tuple(order for order in orders if order.store_alias == alias),
            tuple(issue for issue in issues if issue.store_alias == alias),
        )
        for alias in store_aliases
    )
    status = "PARTIAL" if any(issue.blocking for issue in issues) else "FINAL"
    return FinanceCalculationResult(
        reporting_policy_id=policy_view.reporting_policy_id,
        pricing_policy_id=policy_view.pricing_policy_id,
        status=status,
        orders=tuple(sorted(orders, key=lambda item: (item.store_alias, item.order_key))),
        stores=stores,
        issues=tuple(issues),
        managed_operations_rate=policy_view.managed_rate,
        return_risk_reserve_rate=policy_view.return_risk_rate,
        promotion_reserve_rate=policy_view.promotion_rate,
        total_reserve_rate=policy_view.pool_rate,
        reserve_allocation_mode=policy_view.reserve_allocation_mode,
        reporting_policy_versions=_reporting_policy_versions(policy),
    )


def calculate_finances(**kwargs: Any) -> FinanceCalculationResult:
    """Short alias for :func:`calculate_financial_report`."""

    return calculate_financial_report(**kwargs)


def reconcile_finances(**kwargs: Any) -> FinanceCalculationResult:
    """Domain alias retained for report orchestration code."""

    return calculate_financial_report(**kwargs)


def _group_records(records: Sequence[EvidenceRecord]) -> GroupingResult:
    records, duplicate_issues = _deduplicate_records(records)
    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    identity_owner: dict[tuple[str, str, str], int] = {}
    issues: list[ReconciliationIssue] = list(duplicate_issues)
    grouped_indexes: list[int] = []
    for index, record in enumerate(records):
        if not record.store_alias:
            issues.append(
                ReconciliationIssue(
                    "missing_store_identity",
                    "Evidence row has no store identity and cannot enter a financial group",
                    evidence_ids=(record.evidence_id,),
                )
            )
            continue
        if not record.identities:
            issues.append(
                ReconciliationIssue(
                    "missing_order_identity",
                    "Evidence row has none of srid/orderId/shkId",
                    store_alias=record.store_alias,
                    evidence_ids=(record.evidence_id,),
                )
            )
            continue
        grouped_indexes.append(index)
        for key_type, value in record.identities:
            typed_key = (record.store_alias, key_type, value)
            owner = identity_owner.setdefault(typed_key, index)
            union(index, owner)

    components: dict[int, list[EvidenceRecord]] = defaultdict(list)
    for index in grouped_indexes:
        components[find(index)].append(records[index])

    groups: list[OrderEvidenceGroup] = []
    for component in components.values():
        identities: dict[str, set[str]] = defaultdict(set)
        for record in component:
            for key_type, value in record.identities:
                identities[key_type].add(value)
        normalized_identities = {
            key: tuple(sorted(values))
            for key, values in identities.items()
            if values
        }
        order_key = _preferred_group_order_key(component, normalized_identities)
        groups.append(
            OrderEvidenceGroup(
                store_alias=component[0].store_alias,
                order_key=order_key,
                identities=normalized_identities,
                records=tuple(sorted(component, key=lambda item: item.evidence_id)),
            )
        )
    groups.sort(key=lambda item: (item.store_alias, item.order_key))
    return GroupingResult(tuple(groups), tuple(issues))


def _deduplicate_records(
    records: Sequence[EvidenceRecord],
) -> tuple[tuple[EvidenceRecord, ...], tuple[ReconciliationIssue, ...]]:
    unique: dict[tuple[str, str, str], EvidenceRecord] = {}
    issues: list[ReconciliationIssue] = []
    for record in records:
        key = (record.store_alias, record.source, record.evidence_id)
        existing = unique.get(key)
        if existing is None:
            unique[key] = record
            continue
        if _stable_hash(existing.row) == _stable_hash(record.row):
            continue
        identities = dict(existing.identities)
        identities.update(record.identities)
        order_key = None
        if identities:
            normalized = {name: (value,) for name, value in identities.items()}
            order_key = _preferred_order_key(normalized)
        issues.append(
            ReconciliationIssue(
                "conflicting_duplicate_evidence",
                "The same stable evidence ID has different payloads; one version cannot be selected silently",
                store_alias=record.store_alias or None,
                order_key=order_key,
                evidence_ids=(record.evidence_id,),
            )
        )
    return tuple(unique.values()), tuple(issues)


def _calculate_group(
    group: OrderEvidenceGroup,
    *,
    cost_selector: Callable[..., Any],
    policy: Any,
) -> OrderCalculation:
    issues: list[ReconciliationIssue] = []
    evidence_ids = tuple(record.evidence_id for record in group.records)
    created_at = _earliest_timestamp(group.records, _CREATED_AT_FIELDS, issues, group)
    policy = _policy_view(_policy_for_timestamp(policy, created_at))
    completed_at = _earliest_timestamp(
        group.records, _COMPLETED_AT_FIELDS, issues, group, required=False
    )
    sku = _single_text(group.records, _SKU_FIELDS, "conflicting_sku", issues, group)
    nm_id = _single_text(group.records, _NM_ID_FIELDS, "conflicting_nmid", issues, group)

    finance_records = tuple(record for record in group.records if record.source == "finance")
    net_quantity, retail_count, return_count = _net_quantity(group.records, finance_records)
    if net_quantity < 0:
        issues.append(
            _group_issue(
                group,
                "negative_net_quantity",
                f"Net quantity is negative ({_quantity_text(net_quantity)})",
                evidence_ids,
            )
        )

    correction_present = any(_is_correction(record.row) for record in group.records)
    explicit_return = any(
        record.source == "return" or _is_return_marker(record.row)
        for record in group.records
    ) or return_count > 0

    revenue, revenue_present, refund_amount_present = _net_buyer_payment(
        finance_records, issues, group
    )
    if not finance_records:
        issues.append(
            _group_issue(
                group,
                "missing_finance_rows",
                "Order group has no platform finance rows",
                evidence_ids,
            )
        )
    if not revenue_present:
        issues.append(
            _group_issue(
                group,
                "missing_buyer_payment_evidence",
                "Actual net buyer payment cannot be derived from finance rows",
                evidence_ids,
            )
        )
        revenue_value: Decimal | None = None
    else:
        revenue_value = _money(revenue)

    logistics = _logistics_components(finance_records, group)
    issues.extend(logistics[4])
    forward_logistics, return_logistics, forward_present, return_present, _ = logistics
    explicit_return = explicit_return or return_present
    sale_or_payment_present = retail_count > 0 or (revenue_value or ZERO) > 0
    if explicit_return and sale_or_payment_present and not refund_amount_present:
        issues.append(
            _group_issue(
                group,
                "incomplete_refund_evidence",
                "Return evidence exists but no linked negative buyer-payment/refund amount is settled",
                evidence_ids,
            )
        )
    if explicit_return and not return_present:
        issues.append(
            _group_issue(
                group,
                "missing_return_logistics_evidence",
                "Return/cancellation exists but no linked finance row proves its actual logistics charge",
                evidence_ids,
            )
        )
        actual_return_logistics: Decimal | None = None
    else:
        actual_return_logistics = _money(return_logistics)

    if correction_present:
        outcome = "corrected"
    elif any(_statistics_sale_kind(record.row) == "return" for record in group.records):
        # The platform's R sale event establishes the lifecycle even while
        # finance has not posted the CNY refund.  Finance quantities remain
        # evidence-based; the missing refund/logistics issues above exclude
        # the unsettled order from confirmed profit and cash allocation.
        outcome = "returned"
    elif explicit_return and net_quantity <= 0:
        outcome = "returned"
    elif net_quantity > 0:
        outcome = "completed"
    else:
        outcome = "returned" if explicit_return else "completed"
        if not explicit_return:
            issues.append(
                _group_issue(
                    group,
                    "unresolved_lifecycle",
                    "Order lifecycle is not evidenced as completed, returned, or corrected",
                    evidence_ids,
                )
            )

    commission, commission_present = _sum_first_alias(
        finance_records, _COMMISSION_FIELDS, issues, group, "commission"
    )
    for_pay, for_pay_present = _sum_first_alias(
        finance_records, _FOR_PAY_FIELDS, issues, group, "forPay"
    )
    payment, payment_present = _sum_first_alias(
        finance_records, _PAYMENT_FIELDS, issues, group, "payment fee"
    )
    storage, storage_present = _sum_first_alias(
        finance_records, _STORAGE_FIELDS, issues, group, "storage"
    )
    direct_forward, direct_forward_present = _sum_first_alias(
        finance_records, _DIRECT_FORWARD_FIELDS, issues, group, "forward logistics"
    )
    (
        other,
        other_present,
        advertising_settlement_deduction,
        advertising_settlement_present,
    ) = _sum_other_platform(finance_records, issues, group)
    if direct_forward_present:
        forward_logistics += direct_forward
        forward_present = True

    component_sources: dict[str, str] = {}
    if advertising_settlement_present:
        # A platform advertising/promotion cycle charge is evidence that the
        # already-reserved 3% pool was consumed.  It is intentionally exposed
        # for reconciliation but never booked again as an "other" P&L cost.
        component_sources["advertisingSettlementDeduction"] = (
            "financeActualReconciliationOnly"
        )
    positive_revenue = max(ZERO, revenue_value or ZERO)
    actual_for_pay = _money(for_pay) if for_pay_present else None
    actual_payment = _money(payment) if payment_present else None
    if actual_for_pay is not None:
        component_sources["forPay"] = "financeActual"
    if actual_payment is not None:
        component_sources["acquiringFee"] = "financeActual"
    commission_formula: Decimal | None = None
    if revenue_value is not None and actual_for_pay is not None and actual_payment is not None:
        commission_magnitude = max(
            ZERO,
            abs(revenue_value) - abs(actual_for_pay) - abs(actual_payment),
        )
        commission_formula = _money(
            -commission_magnitude if revenue_value < ZERO else commission_magnitude
        )

    if commission_present:
        actual_commission = _money(commission)
        commission_used = actual_commission
        component_sources["commission"] = "financeActual"
    elif commission_formula is not None:
        # C is a platform-derived amount: G - forPay - acquiring, with the
        # transaction sign preserved.  This is stronger evidence than a
        # pricing-template percentage and remains auditable through the two
        # source amounts and the reconciliation difference below.
        actual_commission = commission_formula
        commission_used = commission_formula
        component_sources["commission"] = "platformForPayDerived"
    else:
        actual_commission = None
        commission_used = _rate_fallback(positive_revenue, policy.commission_fallback_rate)
        component_sources["commission"] = "policyFallback"
        if positive_revenue:
            issues.append(
                _group_issue(
                    group,
                    "commission_fallback_used",
                    "Actual commission is absent; latest-policy fallback is shown separately",
                    evidence_ids,
                )
            )
    if payment_present:
        payment_used = actual_payment
        component_sources["paymentFee"] = "financeActual"
    else:
        payment_used = _rate_fallback(positive_revenue, policy.payment_fallback_rate)
        component_sources["paymentFee"] = "policyFallback"
        if positive_revenue:
            issues.append(
                _group_issue(
                    group,
                    "payment_fee_fallback_used",
                    "Actual payment/acquiring fee is absent; latest-policy fallback is shown separately",
                    evidence_ids,
                )
            )

    if not for_pay_present and finance_records:
        issues.append(
            _group_issue(
                group,
                "missing_for_pay_evidence",
                "Platform forPay is absent; G/C/acquiring cash reconciliation is pending",
                evidence_ids,
            )
        )

    commission_reconciliation_difference: Decimal | None = None
    if actual_commission is not None and commission_formula is not None:
        commission_reconciliation_difference = _money(
            actual_commission - commission_formula
        )
        if commission_reconciliation_difference != ZERO:
            issues.append(
                _group_issue(
                    group,
                    "commission_reconciliation_mismatch",
                    "Actual C differs from abs(G)-abs(forPay)-abs(acquiring) after cent rounding",
                    evidence_ids,
                    unresolved_amount=commission_reconciliation_difference,
                )
            )

    account_increment: Decimal | None = None
    if (
        revenue_value is not None
        and commission_used is not None
        and payment_used is not None
    ):
        account_increment = _money(
            revenue_value - commission_used - payment_used
        )
        component_sources["accountIncrement"] = "formula:G-C-acquiring"

    for_pay_reconciliation_difference: Decimal | None = None
    if account_increment is not None and actual_for_pay is not None:
        for_pay_reconciliation_difference = _money(
            account_increment - actual_for_pay
        )
        if for_pay_reconciliation_difference != ZERO:
            issues.append(
                _group_issue(
                    group,
                    "for_pay_reconciliation_mismatch",
                    "G-C-acquiring does not reconcile exactly to platform forPay after cent rounding",
                    evidence_ids,
                    unresolved_amount=for_pay_reconciliation_difference,
                )
            )

    if commission_reconciliation_difference is None or for_pay_reconciliation_difference is None:
        cash_reconciliation_status = "PENDING"
    elif (
        commission_reconciliation_difference != ZERO
        or for_pay_reconciliation_difference != ZERO
    ):
        cash_reconciliation_status = "FAIL"
    else:
        cash_reconciliation_status = "PASS"

    actual_forward = _money(forward_logistics) if forward_present else None
    actual_storage = _money(storage) if storage_present else ZERO
    actual_other = _money(other) if other_present else ZERO
    actual_operations_present = forward_present or storage_present or other_present
    if actual_operations_present:
        forward_operations_used = _money(actual_forward or ZERO)
        component_sources["forwardOperations"] = "financeActual"
    else:
        units = max(ZERO, net_quantity)
        forward_operations_used = (
            _money(policy.fixed_operations_fallback_cny * units)
            if policy.fixed_operations_fallback_cny is not None
            else None
        )
        component_sources["forwardOperations"] = "policyFallback"
        if units:
            issues.append(
                _group_issue(
                    group,
                    "forward_operations_fallback_used",
                    "Actual forward/storage/other operations are absent; latest-policy fallback is shown separately",
                    evidence_ids,
                )
            )

    cost_selection: CostSelection | None = None
    if not created_at:
        issues.append(
            _group_issue(
                group,
                "missing_order_creation_time",
                "Historical purchase cost cannot be selected without order creation time",
                evidence_ids,
            )
        )
    if not sku:
        issues.append(
            _group_issue(
                group,
                "missing_sku",
                "Historical purchase cost cannot be selected without SKU",
                evidence_ids,
            )
        )
    if created_at and sku:
        try:
            selected = _call_cost_selector(cost_selector, sku, created_at, completed_at)
            cost_selection = _normalize_cost_selection(selected)
        except Exception as exc:  # The selector owns domain-specific missing/overlap errors.
            issues.append(
                _group_issue(
                    group,
                    "historical_cost_unresolved",
                    f"Historical purchase cost selection failed: {exc}",
                    evidence_ids,
                )
            )
    purchase_cost = (
        _money(cost_selection.purchase_cost_per_unit_cny * max(ZERO, net_quantity))
        if cost_selection is not None
        else None
    )

    # The management fee is incurred on confirmed cash withdrawals, not on an
    # order's sale price.  Order calculations must not pre-accrue it; the
    # store-level withdrawal ledger carries the independently evidenced fee.
    managed = ZERO
    return_risk_reserve = (
        _money(positive_revenue * policy.return_risk_rate)
        if revenue_value is not None
        else None
    )
    promotion_reserve = (
        _money(positive_revenue * policy.promotion_rate)
        if revenue_value is not None
        else None
    )
    combined_pool = _money(positive_revenue * policy.pool_rate) if revenue_value is not None else None
    if combined_pool is not None and actual_return_logistics is not None:
        if policy.reserve_allocation_mode == "SEPARATE_RETURN_AND_PROMOTION":
            advertising_provision = promotion_reserve
            excess_return = _money(
                max(ZERO, actual_return_logistics - (return_risk_reserve or ZERO))
            )
        else:
            advertising_provision = _money(max(ZERO, combined_pool - actual_return_logistics))
            excess_return = _money(max(ZERO, actual_return_logistics - combined_pool))
    else:
        advertising_provision = None
        excess_return = None

    rule_withdrawable = (
        _money(account_increment - combined_pool)
        if account_increment is not None and combined_pool is not None
        else None
    )

    template = _template_calculation(
        revenue_value,
        purchase_cost,
        net_quantity,
        policy,
    )
    blocking = any(issue.blocking for issue in issues)
    required_values = (
        account_increment,
        purchase_cost,
        forward_operations_used,
        managed,
        combined_pool,
        excess_return,
    )
    if blocking or any(value is None for value in required_values):
        strict_owner_profit = None
    else:
        # Return/cancellation logistics consumes the 3% pool first.  Only the
        # amount above that pool is an additional loss; subtracting the full
        # return charge again would double count it.
        strict_owner_profit = _money(
            account_increment
            - purchase_cost
            - forward_operations_used
            - actual_storage
            - actual_other
            - managed
            - combined_pool
            - excess_return
        )
    owner_profit = strict_owner_profit

    return OrderCalculation(
        store_alias=group.store_alias,
        order_key=group.order_key,
        identities=group.identities,
        order_created_at=created_at,
        completed_at=completed_at,
        sku=sku,
        nm_id=nm_id,
        outcome=outcome,
        net_quantity=net_quantity,
        net_turnover_cny=revenue_value,
        for_pay_cny=actual_for_pay,
        purchase_cost_cny=purchase_cost,
        actual_commission_cny=actual_commission,
        commission_formula_cny=commission_formula,
        commission_reconciliation_difference_cny=commission_reconciliation_difference,
        commission_used_cny=commission_used,
        actual_payment_fee_cny=actual_payment,
        payment_fee_used_cny=payment_used,
        account_increment_cny=account_increment,
        for_pay_reconciliation_difference_cny=for_pay_reconciliation_difference,
        cash_reconciliation_status=cash_reconciliation_status,
        actual_forward_logistics_cny=actual_forward,
        actual_storage_cny=actual_storage,
        actual_other_platform_fee_cny=actual_other,
        forward_operations_used_cny=forward_operations_used,
        managed_operations_cny=managed,
        combined_pool_cny=combined_pool,
        actual_return_logistics_cny=actual_return_logistics,
        advertising_provision_cny=advertising_provision,
        excess_return_loss_cny=excess_return,
        rule_withdrawable_cny=rule_withdrawable,
        strict_owner_profit_cny=strict_owner_profit,
        advertising_settlement_deduction_cny=(
            _money(advertising_settlement_deduction)
            if advertising_settlement_present
            else ZERO
        ),
        owner_profit_cny=owner_profit,
        template_calculation=template,
        cost_version_id=cost_selection.version_id if cost_selection else None,
        component_sources=component_sources,
        issues=tuple(_deduplicate_issues(issues)),
        evidence_ids=evidence_ids,
        financial_reporting_policy_id=policy.reporting_policy_id,
        selling_price_policy_id=policy.pricing_policy_id,
        reserve_allocation_mode=policy.reserve_allocation_mode,
        managed_operations_rate=ZERO,
        return_risk_reserve_rate=policy.return_risk_rate,
        promotion_reserve_rate=policy.promotion_rate,
        total_reserve_rate=policy.pool_rate,
        return_risk_reserve_cny=return_risk_reserve,
        promotion_reserve_cny=promotion_reserve,
    )


def _allocate_actual_advertising(
    orders: list[OrderCalculation],
    events: Iterable[Mapping[str, Any]],
    *,
    default_store_alias: str | None,
) -> list[ReconciliationIssue]:
    issues: list[ReconciliationIssue] = []
    unique_events: dict[str, Mapping[str, Any]] = {}
    for event in events:
        if event.get("pnl") is False:
            continue
        event_type = str(event.get("eventType") or event.get("event_type") or "").casefold()
        if event.get("pnl") is not True and event_type not in {
            "advertising_expense",
            "advertising cost",
            "advertising_cost",
        }:
            continue
        event_id = str(event.get("eventId") or event.get("event_id") or "").strip()
        if not event_id:
            event_id = _stable_hash(event)
        unique_events.setdefault(event_id, event)

    for event_id, event in sorted(unique_events.items()):
        store = _store_alias(event, default_store_alias)
        amount_value = _first_value(event, ("amount", "amountCny", "spendCny", "updSum"))
        try:
            amount = _money(_decimal(amount_value, "advertising amount"))
        except FinanceCalculationError as exc:
            issues.append(
                ReconciliationIssue(
                    "invalid_advertising_amount",
                    str(exc),
                    store_alias=store or None,
                    evidence_ids=(event_id,),
                )
            )
            continue
        if not store:
            issues.append(
                ReconciliationIssue(
                    "advertising_store_unresolved",
                    "Advertising expense has no store identity",
                    evidence_ids=(event_id,),
                    unresolved_amount_cny=amount,
                )
            )
            continue
        candidates = [order for order in orders if order.store_alias == store]
        event_nm_id = _identifier(_first_value(event, _NM_ID_FIELDS))
        if event_nm_id:
            matching = [order for order in candidates if order.nm_id == event_nm_id]
            if matching:
                candidates = matching
        if not candidates:
            issues.append(
                ReconciliationIssue(
                    "advertising_spend_unallocated",
                    "Actual advertising spend has no in-scope order for reconciliation",
                    store_alias=store,
                    evidence_ids=(event_id,),
                    unresolved_amount_cny=amount,
                    blocking=False,
                )
            )
            continue
        weights = [max(ZERO, order.advertising_provision_cny or ZERO) for order in candidates]
        if sum(weights, ZERO) == 0:
            weights = [max(ZERO, order.net_turnover_cny or ZERO) for order in candidates]
        if sum(weights, ZERO) == 0:
            issues.append(
                ReconciliationIssue(
                    "advertising_spend_unallocated",
                    "Actual advertising spend cannot be allocated because all in-scope weights are zero",
                    store_alias=store,
                    evidence_ids=(event_id,),
                    unresolved_amount_cny=amount,
                    blocking=False,
                )
            )
            continue
        allocations = _proportional_allocation(amount, weights)
        for order, allocation in zip(candidates, allocations):
            order.actual_advertising_spend_cny = _money(
                order.actual_advertising_spend_cny + allocation
            )

    for order in orders:
        if order.advertising_provision_cny is not None:
            order.advertising_reconciliation_variance_cny = _money(
                order.advertising_provision_cny - order.actual_advertising_spend_cny
            )
    return issues


def _summary_for_orders(
    store_alias: str,
    orders: Sequence[OrderCalculation],
    issues: Sequence[ReconciliationIssue],
) -> StoreSummary:
    # The ALL row is display-only.  Build it from independent store summaries
    # so that a return charge in one store can never consume another store's
    # advertising reserve.
    if store_alias == "ALL":
        return _summary_for_all_stores(orders, issues)

    confirmed_orders = [
        order
        for order in orders
        if order.evidence_status == "COMPLETE"
        and order.strict_owner_profit_cny is not None
        and order.cash_reconciliation_status == "PASS"
    ]

    def sum_field(name: str) -> Decimal | None:
        values = [getattr(order, name) for order in confirmed_orders]
        if not values:
            return None if orders else ZERO
        if any(value is None for value in values):
            return None
        return _money(sum(values, ZERO))

    other_values: list[Decimal | None] = []
    for order in confirmed_orders:
        if order.actual_storage_cny is None or order.actual_other_platform_fee_cny is None:
            other_values.append(None)
        else:
            other_values.append(order.actual_storage_cny + order.actual_other_platform_fee_cny)
    other_total = (
        None
        if not confirmed_orders or any(value is None for value in other_values)
        else _money(sum(other_values, ZERO))
    )
    if not orders:
        other_total = ZERO
    if confirmed_orders:
        pool = aggregate_pool_for_orders(confirmed_orders)
    elif orders:
        # Preserve the existing missing-data contract: an open order does not
        # turn the store's financial amounts into zero.
        pool = PoolAggregation(None, None, None, None, None, None)
    else:
        pool = PoolAggregation(ZERO, ZERO, ZERO, ZERO, ZERO, ZERO)

    account_increment = sum_field("account_increment_cny")
    purchase_cost = sum_field("purchase_cost_cny")
    forward_operations = sum_field("forward_operations_used_cny")
    managed_operations = sum_field("managed_operations_cny")
    actual_storage = sum_field("actual_storage_cny")
    actual_other = sum_field("actual_other_platform_fee_cny")
    if not confirmed_orders:
        strict_owner_profit = None if orders else ZERO
    elif any(
        value is None
        for value in (
            account_increment,
            purchase_cost,
            forward_operations,
            actual_storage,
            actual_other,
            managed_operations,
            pool.combined_pool_cny,
            pool.excess_return_loss_cny,
        )
    ):
        strict_owner_profit = None
    else:
        # The three-percent pool is consumed by return logistics only after
        # all confirmed orders in this store/period have been summed.
        strict_owner_profit = _money(
            account_increment
            - purchase_cost
            - forward_operations
            - actual_storage
            - actual_other
            - managed_operations
            - pool.combined_pool_cny
            - pool.excess_return_loss_cny
        )
    blocking_order_count = sum(order.evidence_status == "BLOCKED" for order in orders)
    blocking_issues = [issue for issue in issues if issue.blocking]
    return StoreSummary(
        store_alias=store_alias,
        order_count=len(orders),
        completed_count=sum(order.outcome == "completed" for order in orders),
        returned_count=sum(order.outcome == "returned" for order in orders),
        corrected_count=sum(order.outcome == "corrected" for order in orders),
        net_quantity=sum((order.net_quantity for order in confirmed_orders), ZERO),
        net_turnover_cny=sum_field("net_turnover_cny"),
        for_pay_cny=sum_field("for_pay_cny"),
        purchase_cost_cny=sum_field("purchase_cost_cny"),
        commission_cny=sum_field("commission_used_cny"),
        payment_fee_cny=sum_field("payment_fee_used_cny"),
        account_increment_cny=account_increment,
        forward_operations_cny=forward_operations,
        managed_operations_cny=managed_operations,
        combined_pool_cny=pool.combined_pool_cny,
        return_risk_reserve_cny=pool.return_risk_reserve_cny,
        promotion_reserve_cny=pool.promotion_reserve_cny,
        return_logistics_cny=pool.return_logistics_cny,
        advertising_provision_cny=pool.advertising_provision_cny,
        excess_return_loss_cny=pool.excess_return_loss_cny,
        rule_withdrawable_cny=sum_field("rule_withdrawable_cny"),
        advertising_settlement_deduction_cny=sum_field(
            "advertising_settlement_deduction_cny"
        ),
        actual_advertising_spend_cny=sum_field("actual_advertising_spend_cny"),
        other_platform_cost_cny=other_total,
        strict_owner_profit_cny=strict_owner_profit,
        owner_profit_cny=strict_owner_profit,
        unresolved_order_count=blocking_order_count,
        issue_count=len(blocking_issues),
        confirmed_order_count=len(confirmed_orders),
        open_item_order_count=len(orders) - len(confirmed_orders),
    )


def _summary_for_all_stores(
    orders: Sequence[OrderCalculation],
    issues: Sequence[ReconciliationIssue],
) -> StoreSummary:
    """Combine store summaries without cross-store pool netting."""

    aliases = sorted(
        {order.store_alias for order in orders}
        | {issue.store_alias for issue in issues if issue.store_alias}
    )
    summaries = [
        _summary_for_orders(
            alias,
            tuple(order for order in orders if order.store_alias == alias),
            tuple(issue for issue in issues if issue.store_alias == alias),
        )
        for alias in aliases
    ]
    global_blocking_count = sum(
        not issue.store_alias and issue.blocking for issue in issues
    )
    if not summaries:
        return StoreSummary(
            store_alias="ALL",
            order_count=0,
            completed_count=0,
            returned_count=0,
            corrected_count=0,
            net_quantity=ZERO,
            net_turnover_cny=ZERO,
            purchase_cost_cny=ZERO,
            commission_cny=ZERO,
            payment_fee_cny=ZERO,
            forward_operations_cny=ZERO,
            managed_operations_cny=ZERO,
            return_logistics_cny=ZERO,
            advertising_provision_cny=ZERO,
            excess_return_loss_cny=ZERO,
            advertising_settlement_deduction_cny=ZERO,
            actual_advertising_spend_cny=ZERO,
            other_platform_cost_cny=ZERO,
            owner_profit_cny=ZERO,
            unresolved_order_count=0,
            issue_count=global_blocking_count,
            confirmed_order_count=0,
            open_item_order_count=0,
            for_pay_cny=ZERO,
            account_increment_cny=ZERO,
            combined_pool_cny=ZERO,
            rule_withdrawable_cny=ZERO,
            strict_owner_profit_cny=ZERO,
            return_risk_reserve_cny=ZERO,
            promotion_reserve_cny=ZERO,
        )

    def sum_int(name: str) -> int:
        return sum(int(getattr(summary, name)) for summary in summaries)

    def sum_decimal(name: str) -> Decimal | None:
        values = [getattr(summary, name) for summary in summaries]
        if any(value is None for value in values):
            return None
        return _money(sum(values, ZERO))

    # Every money field is combined from the already store-scoped summaries;
    # in particular advertisingProvision/excessReturnLoss are not recomputed
    # from ALL-store totals.
    return StoreSummary(
        store_alias="ALL",
        order_count=sum_int("order_count"),
        completed_count=sum_int("completed_count"),
        returned_count=sum_int("returned_count"),
        corrected_count=sum_int("corrected_count"),
        net_quantity=sum((summary.net_quantity for summary in summaries), ZERO),
        net_turnover_cny=sum_decimal("net_turnover_cny"),
        purchase_cost_cny=sum_decimal("purchase_cost_cny"),
        commission_cny=sum_decimal("commission_cny"),
        payment_fee_cny=sum_decimal("payment_fee_cny"),
        forward_operations_cny=sum_decimal("forward_operations_cny"),
        managed_operations_cny=sum_decimal("managed_operations_cny"),
        return_logistics_cny=sum_decimal("return_logistics_cny"),
        advertising_provision_cny=sum_decimal("advertising_provision_cny"),
        excess_return_loss_cny=sum_decimal("excess_return_loss_cny"),
        advertising_settlement_deduction_cny=sum_decimal(
            "advertising_settlement_deduction_cny"
        ),
        actual_advertising_spend_cny=sum_decimal("actual_advertising_spend_cny"),
        other_platform_cost_cny=sum_decimal("other_platform_cost_cny"),
        owner_profit_cny=sum_decimal("owner_profit_cny"),
        unresolved_order_count=sum_int("unresolved_order_count"),
        issue_count=sum_int("issue_count") + global_blocking_count,
        confirmed_order_count=sum_int("confirmed_order_count"),
        open_item_order_count=sum_int("open_item_order_count"),
        for_pay_cny=sum_decimal("for_pay_cny"),
        account_increment_cny=sum_decimal("account_increment_cny"),
        combined_pool_cny=sum_decimal("combined_pool_cny"),
        rule_withdrawable_cny=sum_decimal("rule_withdrawable_cny"),
        strict_owner_profit_cny=sum_decimal("strict_owner_profit_cny"),
        return_risk_reserve_cny=sum_decimal("return_risk_reserve_cny"),
        promotion_reserve_cny=sum_decimal("promotion_reserve_cny"),
    )


def _logistics_components(
    records: Sequence[EvidenceRecord], group: OrderEvidenceGroup
) -> tuple[Decimal, Decimal, bool, bool, list[ReconciliationIssue]]:
    forward = ZERO
    returned = ZERO
    forward_present = False
    return_present = False
    issues: list[ReconciliationIssue] = []
    for record in records:
        row = record.row
        has_raw_fields = any(
            key in row
            for key in (
                "deliveryService",
                "delivery_service",
                "rebillLogisticCost",
                "rebill_logistic_cost",
            )
        )
        if not has_raw_fields:
            if any(key in row for key in ("return_logistics_cny", "returnLogisticsCny")):
                issues.append(
                    _group_issue(
                        group,
                        "unclassified_return_logistics_shortcut",
                        "Return logistics must be reclassified from its raw finance row, not trusted from a pre-labelled amount",
                        (record.evidence_id,),
                    )
                )
            continue
        normalized_row = dict(row)
        if "deliveryService" not in normalized_row and "delivery_service" in normalized_row:
            normalized_row["deliveryService"] = normalized_row["delivery_service"]
        if "rebillLogisticCost" not in normalized_row and "rebill_logistic_cost" in normalized_row:
            normalized_row["rebillLogisticCost"] = normalized_row["rebill_logistic_cost"]
        classification = classify_logistics_row(normalized_row)
        if classification.direction == "forward":
            forward += classification.amount
            forward_present = True
        elif classification.direction == "return":
            returned += classification.amount
            return_present = True
        elif classification.amount != 0 or _has_logistics_marker(row):
            issues.append(
                _group_issue(
                    group,
                    "unknown_logistics_direction",
                    classification.blocker
                    or "Finance logistics amount cannot be assigned to forward or return",
                    (record.evidence_id,),
                    unresolved_amount=classification.amount,
                )
            )
    return forward, returned, forward_present, return_present, issues


def _net_buyer_payment(
    records: Sequence[EvidenceRecord],
    issues: list[ReconciliationIssue],
    group: OrderEvidenceGroup,
) -> tuple[Decimal, bool, bool]:
    total = ZERO
    present = False
    refund_present = False
    for record in records:
        row = record.row
        direct_key, direct_value = _first_item(row, _BUYER_PAID_FIELDS)
        if direct_key is not None:
            try:
                amount = _decimal(direct_value, direct_key)
            except FinanceCalculationError as exc:
                issues.append(
                    _group_issue(group, "invalid_buyer_payment", str(exc), (record.evidence_id,))
                )
                continue
            total += amount
            present = True
            refund_present = refund_present or amount < 0
            continue
        adjustment_key, adjustment_value = _first_item(row, _BUYER_ADJUSTMENT_FIELDS)
        if adjustment_key is not None:
            try:
                amount = _decimal(adjustment_value, adjustment_key)
            except FinanceCalculationError as exc:
                issues.append(
                    _group_issue(group, "invalid_buyer_adjustment", str(exc), (record.evidence_id,))
                )
                continue
            total += amount
            present = True
            refund_present = refund_present or amount < 0
            continue
        price_key, price_value = _first_item(row, _BUYER_PRICE_FIELDS)
        quantity = _retail_return_quantity(row)
        if price_key is None or quantity is None:
            continue
        try:
            price = _decimal(price_value, price_key)
        except FinanceCalculationError as exc:
            issues.append(
                _group_issue(group, "invalid_buyer_price", str(exc), (record.evidence_id,))
            )
            continue
        sale_count, return_count = quantity
        contribution = price * (sale_count - return_count)
        total += contribution
        present = True
        refund_present = refund_present or (price * return_count) > 0 or contribution < 0
    return total, present, refund_present


def _net_quantity(
    all_records: Sequence[EvidenceRecord], finance_records: Sequence[EvidenceRecord]
) -> tuple[Decimal, Decimal, Decimal]:
    finance_quantities = [
        _retail_return_quantity(record.row) for record in finance_records
    ]
    finance_quantities = [value for value in finance_quantities if value is not None]
    if finance_quantities:
        retail = sum((value[0] for value in finance_quantities), ZERO)
        returned = sum((value[1] for value in finance_quantities), ZERO)
        return retail - returned, retail, returned
    explicit_deltas: list[Decimal] = []
    for record in all_records:
        key, value = _first_item(record.row, _QUANTITY_DELTA_FIELDS)
        if key is not None:
            explicit_deltas.append(_decimal(value, key))
    if explicit_deltas:
        delta = sum(explicit_deltas, ZERO)
        return delta, max(ZERO, delta), max(ZERO, -delta)
    completed = any(_is_completed_marker(record.row) for record in all_records)
    returned = any(record.source == "return" or _is_return_marker(record.row) for record in all_records)
    retail_count = Decimal("1") if completed else ZERO
    return_count = Decimal("1") if returned and completed else ZERO
    return retail_count - return_count, retail_count, return_count


def _template_calculation(
    revenue: Decimal | None,
    purchase_cost: Decimal | None,
    net_quantity: Decimal,
    policy: _PolicyView,
) -> dict[str, Decimal | None]:
    if revenue is None:
        return {
            "commissionCny": None,
            "paymentFeeCny": None,
            "fixedOperationsCny": None,
            "managedOperationsCny": ZERO,
            "returnRiskReserveCny": None,
            "promotionReserveCny": None,
            "totalPolicyReserveCny": None,
            "combinedPoolCny": None,
            "ownerProfitCny": None,
        }
    positive_revenue = max(ZERO, revenue)
    commission = _rate_fallback(positive_revenue, policy.commission_fallback_rate)
    payment = _rate_fallback(positive_revenue, policy.payment_fallback_rate)
    fixed = (
        _money(policy.fixed_operations_fallback_cny * max(ZERO, net_quantity))
        if policy.fixed_operations_fallback_cny is not None
        else None
    )
    # Template/order estimates do not include a cash-withdrawal fee.
    managed = ZERO
    return_risk = _money(positive_revenue * policy.return_risk_rate)
    promotion = _money(positive_revenue * policy.promotion_rate)
    pool = _money(positive_revenue * policy.pool_rate)
    required = (purchase_cost, commission, payment, fixed)
    owner_profit = (
        _money(revenue - purchase_cost - commission - payment - fixed - managed - pool)
        if all(value is not None for value in required)
        else None
    )
    return {
        "commissionCny": commission,
        "paymentFeeCny": payment,
        "fixedOperationsCny": fixed,
        "managedOperationsCny": managed,
        "returnRiskReserveCny": return_risk,
        "promotionReserveCny": promotion,
        "totalPolicyReserveCny": pool,
        "combinedPoolCny": pool,
        "ownerProfitCny": owner_profit,
    }


def _policy_view(policy: Any) -> _PolicyView:
    raw = policy if isinstance(policy, Mapping) else getattr(policy, "raw", {})
    if not isinstance(raw, Mapping):
        raise FinanceCalculationError("policy.raw must be a mapping")
    managed = _policy_decimal(
        policy,
        "managed_operations_rate",
        raw,
        ("costs", "managedOperations", "rate"),
    )
    advertising_returns = _nested(raw, ("costs", "advertisingAndReturns")) or {}
    mode = str(
        getattr(policy, "reserve_allocation_mode", None)
        or (advertising_returns.get("reserveAllocationMode") if isinstance(advertising_returns, Mapping) else None)
        or "LEGACY_SHARED_POOL"
    ).strip()
    if mode == "SEPARATE_RETURN_AND_PROMOTION":
        return_risk = _policy_decimal(
            policy,
            "return_risk_reserve_rate",
            raw,
            ("costs", "advertisingAndReturns", "returnRiskReserveRate"),
        )
        promotion = _policy_decimal(
            policy,
            "promotion_reserve_rate",
            raw,
            ("costs", "advertisingAndReturns", "promotionReserveRate"),
        )
        pool = _policy_decimal(
            policy,
            "total_reserve_rate",
            raw,
            ("costs", "advertisingAndReturns", "totalReserveRate"),
        )
    else:
        pool = _policy_decimal(
            policy,
            "combined_advertising_return_pool_rate",
            raw,
            ("costs", "advertisingAndReturns", "combinedPoolRate"),
        )
        return_risk = ZERO
        promotion = pool
        mode = "LEGACY_SHARED_POOL"
    if managed != Decimal("0.02"):
        raise FinanceCalculationError(
            f"Financial calculation requires managed rate 0.02; got {managed}"
        )
    if mode == "LEGACY_SHARED_POOL" and pool != Decimal("0.03"):
        raise FinanceCalculationError(f"Legacy shared pool must be 0.03; got {pool}")
    if mode == "SEPARATE_RETURN_AND_PROMOTION" and (
        return_risk != Decimal("0.02")
        or promotion != Decimal("0.03")
        or pool != Decimal("0.05")
        or return_risk + promotion != pool
    ):
        raise FinanceCalculationError(
            "Separate reserve policy must be return 0.02 + promotion 0.03 = total 0.05"
        )
    return _PolicyView(
        reporting_policy_id=str(
            getattr(policy, "reporting_policy_id", None) or raw.get("policyId") or ""
        ).strip(),
        pricing_policy_id=str(
            getattr(policy, "pricing_policy_id", None)
            or _nested(raw, ("pricingReferences", "activePricingPolicyId"))
            or ""
        ).strip(),
        managed_rate=managed,
        pool_rate=pool,
        reserve_allocation_mode=mode,
        return_risk_rate=return_risk,
        promotion_rate=promotion,
        commission_fallback_rate=_optional_policy_decimal(
            raw, ("costs", "wbCommission", "fallbackRate")
        ),
        payment_fallback_rate=_optional_policy_decimal(
            raw, ("costs", "paymentAndWithdrawal", "fallbackRate")
        ),
        fixed_operations_fallback_cny=_optional_policy_decimal(
            raw, ("costs", "fixedOperations", "fallbackPerCompletedUnitCny")
        ),
    )


def _policy_for_timestamp(policy: Any, order_created_at: str | None) -> Any:
    selector = getattr(policy, "select", None)
    if callable(selector):
        return selector(order_created_at)
    return policy


def _reporting_policy_versions(policy: Any) -> tuple[Mapping[str, Any], ...]:
    active = getattr(policy, "active_policy", policy)
    historical = tuple(getattr(policy, "historical_policies", ()) or ())
    versions = (*historical, active)
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in versions:
        view = _policy_view(item)
        if view.reporting_policy_id in seen:
            continue
        seen.add(view.reporting_policy_id)
        raw = item if isinstance(item, Mapping) else getattr(item, "raw", {})
        result.append(
            {
                "reportingPolicyId": view.reporting_policy_id,
                "pricingPolicyId": view.pricing_policy_id,
                "reserveAllocationMode": view.reserve_allocation_mode,
                "managedOperationsRate": str(view.managed_rate),
                "returnRiskReserveRate": str(view.return_risk_rate),
                "promotionReserveRate": str(view.promotion_rate),
                "totalReserveRate": str(view.pool_rate),
                "effectiveFrom": raw.get("effectiveFrom"),
                "effectiveTo": raw.get("effectiveTo"),
            }
        )
    return tuple(result)


def _record(
    row: Mapping[str, Any], *, source: str, default_store_alias: str | None
) -> EvidenceRecord:
    if not isinstance(row, Mapping):
        raise FinanceCalculationError("Every evidence row must be a mapping")
    actual_source = str(row.get("sourceKind") or row.get("_sourceKind") or source).strip()
    store = _store_alias(row, default_store_alias)
    identities = order_identity(_identity_alias_view(row))
    return EvidenceRecord(
        source=actual_source,
        store_alias=store,
        row=dict(row),
        identities=identities,
        evidence_id=_evidence_id(row, actual_source),
    )


def _identity_alias_view(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = dict(row)
    aliases = {
        "srid": ("srid", "srId"),
        "orderId": ("orderId", "order_id", "odid"),
        "shkId": ("shkId", "shk_id"),
    }
    for target, names in aliases.items():
        if target not in value:
            candidate = _first_value(row, names)
            if candidate is not None:
                value[target] = candidate
    return value


def _store_alias(row: Mapping[str, Any], default: str | None) -> str:
    return str(
        row.get("storeAlias")
        or row.get("store_alias")
        or row.get("storeId")
        or row.get("store_id")
        or default
        or ""
    ).strip()


def _preferred_order_key(identities: Mapping[str, tuple[str, ...]]) -> str:
    for key_type in ("srid", "orderId", "shkId"):
        values = identities.get(key_type) or ()
        if values:
            return f"{key_type}:{values[0]}"
    raise FinanceCalculationError("Cannot build order key without identity")


def _preferred_group_order_key(
    records: Sequence[EvidenceRecord], identities: Mapping[str, tuple[str, ...]]
) -> str:
    # An order/statistics srid is more stable than a later correction-row srid.
    for key_type in ("srid", "orderId", "shkId"):
        for source in ("order", "sale"):
            values = sorted(
                value
                for record in records
                if record.source == source
                for name, value in record.identities
                if name == key_type
            )
            if values:
                return f"{key_type}:{values[0]}"
    # Finance-only groups can gain a different srid when WB posts a later
    # return/correction. Prefer the srid from the original non-return row;
    # otherwise a shared orderId (or sticker) keeps the ledger key stable.
    if any(record.source in {"finance", "return"} for record in records):
        sale_srids = sorted(
            value
            for record in records
            if record.source == "finance"
            and not _is_return_marker(record.row)
            and not _is_correction(record.row)
            for key_type, value in record.identities
            if key_type == "srid"
        )
        if sale_srids:
            return f"srid:{sale_srids[0]}"
        for key_type in ("orderId", "shkId", "srid"):
            values = identities.get(key_type) or ()
            if values:
                return f"{key_type}:{values[0]}"
    return _preferred_order_key(identities)


def _evidence_id(row: Mapping[str, Any], source: str) -> str:
    for key in (
        "rrdId",
        "rrd_id",
        "eventId",
        "event_id",
        "saleID",
        "saleId",
        "returnId",
        "return_id",
    ):
        value = _identifier(row.get(key))
        if value:
            return f"{source}:{key}:{value}"
    return f"{source}:sha256:{_stable_hash(row)}"


def _call_cost_selector(
    selector: Callable[..., Any], sku: str, created_at: str, completed_at: str | None
) -> Any:
    try:
        signature = inspect.signature(selector)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        parameters = signature.parameters.values()
        accepts_keyword = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            or parameter.name == "order_completed_at"
            for parameter in parameters
        )
        if accepts_keyword:
            return selector(sku, created_at, order_completed_at=completed_at)
    return selector(sku, created_at)


def _normalize_cost_selection(value: Any) -> CostSelection:
    if isinstance(value, Mapping):
        getter = value.get
    else:
        getter = lambda name, default=None: getattr(value, name, default)
    cost = _first_not_none(
        getter("purchase_cost_cny"),
        getter("purchaseCostCny"),
        getter("cost_cny"),
        getter("costCny"),
    )
    if cost is None:
        raise FinanceCalculationError("Cost selector returned no purchase cost")
    version = _first_not_none(
        getter("cost_version_id"),
        getter("costVersionId"),
        getter("version_id"),
        getter("versionId"),
    )
    if version is None:
        raise FinanceCalculationError("Cost selector returned no version ID")
    amount = _decimal(cost, "purchase cost")
    if amount < 0:
        raise FinanceCalculationError("Purchase cost cannot be negative")
    return CostSelection(
        version_id=str(version),
        purchase_cost_per_unit_cny=amount,
        effective_from=getter("effective_from") or getter("effectiveFrom"),
        effective_to=getter("effective_to") or getter("effectiveTo"),
    )


def _earliest_timestamp(
    records: Sequence[EvidenceRecord],
    fields: Sequence[str],
    issues: list[ReconciliationIssue],
    group: OrderEvidenceGroup,
    *,
    required: bool = True,
) -> str | None:
    values: list[datetime] = []
    for record in records:
        record_fields = fields
        if fields == _COMPLETED_AT_FIELDS and _statistics_sale_kind(record.row) == "sale":
            # Statistics /sales uses date for the sale event, while /orders
            # uses date for order creation.  Only a verified S-prefixed sale
            # event may supply this completion fallback.
            record_fields = (*fields, "date")
        for field_name in record_fields:
            raw = record.row.get(field_name)
            if raw in (None, ""):
                continue
            try:
                is_statistics = (
                    _statistics_sale_kind(record.row) is not None
                    or "isCancel" in record.row
                )
                values.append(
                    _datetime(
                        raw,
                        default_timezone=(
                            ZoneInfo("Europe/Moscow") if is_statistics else timezone.utc
                        ),
                    )
                )
                # Fields are ordered by authority.  A generic date must not
                # override an explicit orderCreatedAt/completedAt on one row.
                break
            except FinanceCalculationError as exc:
                issues.append(
                    _group_issue(
                        group,
                        "invalid_timestamp",
                        f"{field_name}: {exc}",
                        (record.evidence_id,),
                        blocking=required and not values,
                    )
                )
    if not values:
        return None
    return min(values).isoformat()


def _single_text(
    records: Sequence[EvidenceRecord],
    fields: Sequence[str],
    conflict_code: str,
    issues: list[ReconciliationIssue],
    group: OrderEvidenceGroup,
) -> str | None:
    values = {
        str(value).strip()
        for record in records
        for value in (_first_value(record.row, fields),)
        if value not in (None, "") and str(value).strip()
    }
    if not values:
        return None
    if len(values) > 1:
        issues.append(
            _group_issue(
                group,
                conflict_code,
                "Conflicting values in one transitive order group: " + ", ".join(sorted(values)),
                tuple(record.evidence_id for record in records),
            )
        )
    return sorted(values)[0]


def _sum_first_alias(
    records: Sequence[EvidenceRecord],
    fields: Sequence[str],
    issues: list[ReconciliationIssue],
    group: OrderEvidenceGroup,
    label: str,
) -> tuple[Decimal, bool]:
    total = ZERO
    present = False
    for record in records:
        key, value = _first_item(record.row, fields)
        if key is None:
            continue
        present = True
        try:
            total += _decimal(value, key)
        except FinanceCalculationError as exc:
            issues.append(
                _group_issue(
                    group,
                    f"invalid_{label.replace(' ', '_')}",
                    str(exc),
                    (record.evidence_id,),
                )
            )
    return total, present


def _sum_other_platform(
    records: Sequence[EvidenceRecord],
    issues: list[ReconciliationIssue],
    group: OrderEvidenceGroup,
) -> tuple[Decimal, bool, Decimal, bool]:
    other_records = [
        record for record in records if not _is_advertising_settlement_row(record.row)
    ]
    advertising_records = [
        record for record in records if _is_advertising_settlement_row(record.row)
    ]
    total = ZERO
    present = False
    advertising_total = ZERO
    advertising_present = False
    for aliases in _OTHER_PLATFORM_FIELD_GROUPS:
        subtotal, seen = _sum_first_alias(
            other_records, aliases, issues, group, "other platform fee"
        )
        total += subtotal
        present = present or seen
        advertising_subtotal, advertising_seen = _sum_first_alias(
            advertising_records,
            aliases,
            issues,
            group,
            "advertising settlement deduction",
        )
        advertising_total += advertising_subtotal
        advertising_present = advertising_present or advertising_seen
    return total, present, advertising_total, advertising_present


def _retail_return_quantity(row: Mapping[str, Any]) -> tuple[Decimal, Decimal] | None:
    quantity_key, quantity_value = _first_item(
        row, ("quantity", "retailQuantity", "retail_quantity")
    )
    retail_key, retail_value = _first_item(row, _RETAIL_AMOUNT_FIELDS)
    return_key, return_value = _first_item(row, _RETURN_AMOUNT_FIELDS)
    if quantity_key is not None:
        quantity = abs(_decimal(quantity_value, quantity_key))
        returned = (
            abs(_decimal(return_value, return_key)) if return_key is not None else ZERO
        )
        if _is_return_marker(row):
            return ZERO, max(quantity, returned)
        return quantity, returned
    if retail_key is None and return_key is None:
        delta_key, delta_value = _first_item(row, _QUANTITY_DELTA_FIELDS)
        if delta_key is None:
            return None
        delta = _decimal(delta_value, delta_key)
        return max(ZERO, delta), max(ZERO, -delta)
    retail = _decimal(retail_value, retail_key) if retail_key is not None else ZERO
    returned = _decimal(return_value, return_key) if return_key is not None else ZERO
    return retail, returned


def _is_return_marker(row: Mapping[str, Any]) -> bool:
    sale_kind = _statistics_sale_kind(row)
    if sale_kind is not None:
        return sale_kind == "return"
    marker = _marker_text(row)
    return any(value in marker for value in _RETURN_MARKERS)


def _is_correction(row: Mapping[str, Any]) -> bool:
    marker = _marker_text(row)
    return any(value in marker for value in _CORRECTION_MARKERS)


def _is_completed_marker(row: Mapping[str, Any]) -> bool:
    sale_kind = _statistics_sale_kind(row)
    if sale_kind is not None:
        return sale_kind == "sale"
    marker = _marker_text(row)
    return any(value in marker for value in _COMPLETED_MARKERS)


def _statistics_sale_kind(row: Mapping[str, Any]) -> str | None:
    value = str(row.get("saleID") or row.get("saleId") or "").strip().upper()
    if len(value) > 1 and value[1:].isdigit():
        if value[0] == "S":
            return "sale"
        if value[0] == "R":
            return "return"
    return None


def _has_logistics_marker(row: Mapping[str, Any]) -> bool:
    marker = _marker_text(row)
    return any(value in marker for value in ("物流", "логист", "delivery", "logistic"))


def _is_advertising_settlement_row(row: Mapping[str, Any]) -> bool:
    """Identify explicit advertising/promotion settlement deductions.

    WB finance rows are multilingual and the monetary value commonly arrives
    through the generic ``deduction`` field.  Classification therefore uses
    only explicit operation markers; an unmarked deduction remains an ordinary
    platform cost.
    """

    marker = _marker_text(row)
    return any(value in marker for value in _ADVERTISING_SETTLEMENT_MARKERS)


def _marker_text(row: Mapping[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "").strip().casefold()
        for key in (
            "sellerOperName",
            "seller_oper_name",
            "docTypeName",
            "doc_type_name",
            "bonusTypeName",
            "bonus_type_name",
            "operationType",
            "operation_type",
            "operationName",
            "operation_name",
            "serviceName",
            "service_name",
            "status",
            "eventType",
            "event_type",
        )
    )


def _policy_decimal(
    policy: Any,
    attribute: str,
    raw: Mapping[str, Any],
    path: Sequence[str],
) -> Decimal:
    value = getattr(policy, attribute, None) if not isinstance(policy, Mapping) else None
    if value is None:
        value = _nested(raw, path)
    return _decimal(value, ".".join(path))


def _optional_policy_decimal(
    raw: Mapping[str, Any], path: Sequence[str]
) -> Decimal | None:
    value = _nested(raw, path)
    return None if value is None else _decimal(value, ".".join(path))


def _nested(value: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _group_issue(
    group: OrderEvidenceGroup,
    code: str,
    message: str,
    evidence_ids: Sequence[str],
    *,
    blocking: bool = True,
    unresolved_amount: Decimal | None = None,
) -> ReconciliationIssue:
    return ReconciliationIssue(
        code=code,
        message=message,
        blocking=blocking,
        store_alias=group.store_alias,
        order_key=group.order_key,
        evidence_ids=tuple(evidence_ids),
        unresolved_amount_cny=_money(unresolved_amount) if unresolved_amount is not None else None,
    )


def _deduplicate_issues(
    issues: Iterable[ReconciliationIssue],
) -> list[ReconciliationIssue]:
    unique: dict[tuple[Any, ...], ReconciliationIssue] = {}
    for issue in issues:
        key = (
            issue.code,
            issue.store_alias,
            issue.order_key,
            issue.evidence_ids,
            issue.message,
        )
        unique.setdefault(key, issue)
    return list(unique.values())


def _proportional_allocation(amount: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    total_weight = sum(weights, ZERO)
    if total_weight <= 0:
        raise FinanceCalculationError("Allocation weights must total more than zero")
    allocations: list[Decimal] = []
    remaining = _money(amount)
    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            allocation = remaining
        else:
            allocation = _money(amount * weight / total_weight)
            remaining -= allocation
        allocations.append(_money(allocation))
    return allocations


def _rate_fallback(amount: Decimal, rate: Decimal | None) -> Decimal | None:
    return _money(amount * rate) if rate is not None else None


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _money_text(value: Decimal | None) -> str | None:
    return None if value is None else format(_money(value), "f")


def _quantity_text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def _decimal(value: Any, label: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise FinanceCalculationError(f"{label} is not a decimal amount")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinanceCalculationError(f"{label} is not a decimal amount") from exc
    if not result.is_finite():
        raise FinanceCalculationError(f"{label} must be finite")
    return result


def _datetime(value: Any, *, default_timezone=timezone.utc) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip()
        if not text:
            raise FinanceCalculationError("timestamp is empty")
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FinanceCalculationError(f"invalid ISO timestamp {text!r}") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=default_timezone)
    return result


def _first_item(
    row: Mapping[str, Any], fields: Sequence[str]
) -> tuple[str | None, Any]:
    for key in fields:
        if key in row:
            return key, row.get(key)
    return None, None


def _first_value(row: Mapping[str, Any], fields: Sequence[str]) -> Any:
    return _first_item(row, fields)[1]


def _first_not_none(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _identifier(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _stable_hash(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


_CREATED_AT_FIELDS = (
    "orderCreatedAt",
    "order_created_at",
    "orderDate",
    "order_date",
    "dateCreated",
    "createdAt",
    "created_at",
    "orderDt",
    "order_dt",
    "date",
)
_COMPLETED_AT_FIELDS = (
    "completedAt",
    "completed_at",
    "saleDt",
    "sale_dt",
    "soldAt",
    "sold_at",
)
_SKU_FIELDS = (
    "supplierArticle",
    "supplier_article",
    "vendorCode",
    "vendor_code",
    # In the current finance API, ``sku`` may contain a numeric barcode while
    # ``vendorCode`` carries the seller article used by the cost catalogue.
    # Keep the ambiguous field last so the stable seller article wins within
    # each evidence row.
    "sku",
)
_NM_ID_FIELDS = ("nmId", "nmID", "nm_id")
_BUYER_PAID_FIELDS = (
    "buyerPaidCny",
    "buyer_paid_cny",
    "netBuyerPaidCny",
    "net_buyer_paid_cny",
)
_BUYER_ADJUSTMENT_FIELDS = (
    "buyerPaidAdjustmentCny",
    "buyer_paid_adjustment_cny",
    "priceAdjustmentCny",
    "price_adjustment_cny",
)
_BUYER_PRICE_FIELDS = (
    "retailPriceWithDisc",
    "retail_price_withdisc_rub",
    "retail_price_withdisc",
    "finishedPrice",
    "priceWithDisc",
)
_RETAIL_AMOUNT_FIELDS = ("retailAmount", "retail_amount", "saleQuantity")
_RETURN_AMOUNT_FIELDS = ("returnAmount", "return_amount", "returnQuantity")
_QUANTITY_DELTA_FIELDS = ("quantityDelta", "quantity_delta", "netQuantityDelta")
_COMMISSION_FIELDS = (
    "commissionCny",
    "commission_cny",
    "wbCommissionCny",
    "wb_commission_cny",
    "ppvzReward",
    "ppvz_reward",
)
_FOR_PAY_FIELDS = (
    "forPayCny",
    "for_pay_cny",
    "forPay",
    "for_pay",
)
_PAYMENT_FIELDS = (
    "paymentFeeCny",
    "payment_fee_cny",
    "acquiringFeeCny",
    "acquiring_fee_cny",
    "acquiringFee",
    "acquiring_fee",
)
_STORAGE_FIELDS = (
    "storageCny",
    "storage_cny",
    "storageFee",
    "storage_fee",
)
_DIRECT_FORWARD_FIELDS = (
    "forwardLogisticsCny",
    "forward_logistics_cny",
    "logistics_cny",
)
_OTHER_PLATFORM_FIELD_GROUPS = (
    ("otherPlatformFeeCny", "other_platform_fee_cny", "otherCostCny", "other_cost_cny"),
    ("penaltyCny", "penalty_cny", "penalty"),
    ("deductionCny", "deduction_cny", "deduction"),
    ("acceptanceFeeCny", "acceptance_fee_cny", "acceptanceFee", "acceptance_fee"),
)
_ADVERTISING_SETTLEMENT_MARKERS = (
    "广告",
    "推广",
    "реклам",
    "продвижен",
    "advertising",
    "advertisement",
    "promotion",
    "ad fee",
    "ad deduction",
)
_RETURN_MARKERS = (
    "退货",
    "取消",
    "从客户返回",
    "return",
    "refund",
    "cancel",
    "возврат",
    "отмена",
    "от клиент",
)
_CORRECTION_MARKERS = (
    "更正",
    "调整",
    "校正",
    "correct",
    "adjust",
    "restate",
    "коррект",
    "исправ",
)
_COMPLETED_MARKERS = (
    "completed",
    "complete",
    "sold",
    "sale",
    "已完成",
    "продажа",
)


__all__ = [
    "CostSelection",
    "EvidenceRecord",
    "FinanceCalculationError",
    "FinanceCalculationResult",
    "GroupingResult",
    "OrderCalculation",
    "OrderEvidenceGroup",
    "ReconciliationIssue",
    "StoreSummary",
    "build_order_groups",
    "calculate_finances",
    "calculate_financial_report",
    "reconcile_finances",
]
