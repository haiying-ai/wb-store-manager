from __future__ import annotations

import hashlib
import inspect
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Mapping, Sequence

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
    purchase_cost_cny: Decimal | None
    actual_commission_cny: Decimal | None
    commission_used_cny: Decimal | None
    actual_payment_fee_cny: Decimal | None
    payment_fee_used_cny: Decimal | None
    actual_forward_logistics_cny: Decimal | None
    actual_storage_cny: Decimal | None
    actual_other_platform_fee_cny: Decimal | None
    forward_operations_used_cny: Decimal | None
    managed_operations_cny: Decimal | None
    combined_pool_cny: Decimal | None
    actual_return_logistics_cny: Decimal | None
    advertising_provision_cny: Decimal | None
    excess_return_loss_cny: Decimal | None
    actual_advertising_spend_cny: Decimal = ZERO
    advertising_reconciliation_variance_cny: Decimal | None = None
    owner_profit_cny: Decimal | None = None
    template_calculation: Mapping[str, Decimal | None] = field(default_factory=dict)
    cost_version_id: str | None = None
    component_sources: Mapping[str, str] = field(default_factory=dict)
    issues: tuple[ReconciliationIssue, ...] = ()
    evidence_ids: tuple[str, ...] = ()

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
            "netTurnoverCny": _money_text(self.net_turnover_cny),
            "purchaseCostCny": _money_text(self.purchase_cost_cny),
            "actualWbCommissionCny": _money_text(self.actual_commission_cny),
            "wbCommissionCny": _money_text(self.commission_used_cny),
            "actualPaymentFeeCny": _money_text(self.actual_payment_fee_cny),
            "paymentFeeCny": _money_text(self.payment_fee_used_cny),
            "forwardLogisticsCny": _money_text(self.actual_forward_logistics_cny),
            "storageAndOperationsCny": _money_text(self.actual_storage_cny),
            "otherPlatformCostCny": _money_text(self.actual_other_platform_fee_cny),
            "forwardOperationsUsedCny": _money_text(self.forward_operations_used_cny),
            "managedOperationsCny": _money_text(self.managed_operations_cny),
            "combinedPoolCny": _money_text(self.combined_pool_cny),
            "returnLogisticsCny": _money_text(self.actual_return_logistics_cny),
            "advertisingProvisionCny": _money_text(self.advertising_provision_cny),
            "excessReturnLossCny": _money_text(self.excess_return_loss_cny),
            "actualAdvertisingSpendCny": _money_text(self.actual_advertising_spend_cny),
            "advertisingReconciliationVarianceCny": _money_text(
                self.advertising_reconciliation_variance_cny
            ),
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
    actual_advertising_spend_cny: Decimal
    other_platform_cost_cny: Decimal | None
    owner_profit_cny: Decimal | None
    unresolved_order_count: int
    issue_count: int

    @property
    def status(self) -> str:
        return "PARTIAL" if self.unresolved_order_count or self.issue_count else "FINAL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "storeAlias": self.store_alias,
            "status": self.status,
            "orderCount": self.order_count,
            "completedCount": self.completed_count,
            "returnedCount": self.returned_count,
            "correctedCount": self.corrected_count,
            "netQuantity": _quantity_text(self.net_quantity),
            "netTurnoverCny": _money_text(self.net_turnover_cny),
            "purchaseCostCny": _money_text(self.purchase_cost_cny),
            "wbCommissionCny": _money_text(self.commission_cny),
            "paymentFeeCny": _money_text(self.payment_fee_cny),
            "forwardOperationsCny": _money_text(self.forward_operations_cny),
            "managedOperationsCny": _money_text(self.managed_operations_cny),
            "returnLogisticsCny": _money_text(self.return_logistics_cny),
            "advertisingProvisionCny": _money_text(self.advertising_provision_cny),
            "excessReturnLossCny": _money_text(self.excess_return_loss_cny),
            "actualAdvertisingSpendCny": _money_text(
                self.actual_advertising_spend_cny
            ),
            "otherPlatformCostCny": _money_text(self.other_platform_cost_cny),
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
                "managedOperationsRate": "0.02",
                "combinedPoolRate": "0.03",
            },
            "orders": [order.to_dict() for order in self.orders],
            "stores": [store.to_dict() for store in self.stores],
            "totals": totals.to_dict(),
            "blockers": [issue.to_dict() for issue in self.blockers],
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class _PolicyView:
    reporting_policy_id: str
    pricing_policy_id: str
    managed_rate: Decimal
    pool_rate: Decimal
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

    policy_view = _policy_view(policy)
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
        _calculate_group(group, cost_selector=cost_selector, policy=policy_view)
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
    policy: _PolicyView,
) -> OrderCalculation:
    issues: list[ReconciliationIssue] = []
    evidence_ids = tuple(record.evidence_id for record in group.records)
    created_at = _earliest_timestamp(group.records, _CREATED_AT_FIELDS, issues, group)
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
    payment, payment_present = _sum_first_alias(
        finance_records, _PAYMENT_FIELDS, issues, group, "payment fee"
    )
    storage, storage_present = _sum_first_alias(
        finance_records, _STORAGE_FIELDS, issues, group, "storage"
    )
    direct_forward, direct_forward_present = _sum_first_alias(
        finance_records, _DIRECT_FORWARD_FIELDS, issues, group, "forward logistics"
    )
    other, other_present = _sum_other_platform(finance_records, issues, group)
    if direct_forward_present:
        forward_logistics += direct_forward
        forward_present = True

    component_sources: dict[str, str] = {}
    positive_revenue = max(ZERO, revenue_value or ZERO)
    if commission_present:
        actual_commission = _money(commission)
        commission_used = actual_commission
        component_sources["commission"] = "financeActual"
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
        actual_payment = _money(payment)
        payment_used = actual_payment
        component_sources["paymentFee"] = "financeActual"
    else:
        actual_payment = None
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

    managed = _money(positive_revenue * policy.managed_rate) if revenue_value is not None else None
    combined_pool = _money(positive_revenue * policy.pool_rate) if revenue_value is not None else None
    if combined_pool is not None and actual_return_logistics is not None:
        advertising_provision = _money(max(ZERO, combined_pool - actual_return_logistics))
        excess_return = _money(max(ZERO, actual_return_logistics - combined_pool))
    else:
        advertising_provision = None
        excess_return = None

    template = _template_calculation(
        revenue_value,
        purchase_cost,
        net_quantity,
        policy,
    )
    blocking = any(issue.blocking for issue in issues)
    required_values = (
        revenue_value,
        purchase_cost,
        commission_used,
        payment_used,
        forward_operations_used,
        managed,
        actual_return_logistics,
        advertising_provision,
    )
    if blocking or any(value is None for value in required_values):
        owner_profit = None
    else:
        owner_profit = _money(
            revenue_value
            - purchase_cost
            - commission_used
            - payment_used
            - forward_operations_used
            - actual_storage
            - actual_other
            - managed
            - actual_return_logistics
            - advertising_provision
        )

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
        purchase_cost_cny=purchase_cost,
        actual_commission_cny=actual_commission,
        commission_used_cny=commission_used,
        actual_payment_fee_cny=actual_payment,
        payment_fee_used_cny=payment_used,
        actual_forward_logistics_cny=actual_forward,
        actual_storage_cny=actual_storage,
        actual_other_platform_fee_cny=actual_other,
        forward_operations_used_cny=forward_operations_used,
        managed_operations_cny=managed,
        combined_pool_cny=combined_pool,
        actual_return_logistics_cny=actual_return_logistics,
        advertising_provision_cny=advertising_provision,
        excess_return_loss_cny=excess_return,
        owner_profit_cny=owner_profit,
        template_calculation=template,
        cost_version_id=cost_selection.version_id if cost_selection else None,
        component_sources=component_sources,
        issues=tuple(_deduplicate_issues(issues)),
        evidence_ids=evidence_ids,
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
    def sum_field(name: str) -> Decimal | None:
        values = [getattr(order, name) for order in orders]
        if any(value is None for value in values):
            return None
        return _money(sum(values, ZERO))

    other_values: list[Decimal | None] = []
    for order in orders:
        if order.actual_storage_cny is None or order.actual_other_platform_fee_cny is None:
            other_values.append(None)
        else:
            other_values.append(order.actual_storage_cny + order.actual_other_platform_fee_cny)
    other_total = None if any(value is None for value in other_values) else _money(sum(other_values, ZERO))
    blocking_order_count = sum(order.evidence_status == "BLOCKED" for order in orders)
    blocking_issues = [issue for issue in issues if issue.blocking]
    return StoreSummary(
        store_alias=store_alias,
        order_count=len(orders),
        completed_count=sum(order.outcome == "completed" for order in orders),
        returned_count=sum(order.outcome == "returned" for order in orders),
        corrected_count=sum(order.outcome == "corrected" for order in orders),
        net_quantity=sum((order.net_quantity for order in orders), ZERO),
        net_turnover_cny=sum_field("net_turnover_cny"),
        purchase_cost_cny=sum_field("purchase_cost_cny"),
        commission_cny=sum_field("commission_used_cny"),
        payment_fee_cny=sum_field("payment_fee_used_cny"),
        forward_operations_cny=sum_field("forward_operations_used_cny"),
        managed_operations_cny=sum_field("managed_operations_cny"),
        return_logistics_cny=sum_field("actual_return_logistics_cny"),
        advertising_provision_cny=sum_field("advertising_provision_cny"),
        excess_return_loss_cny=sum_field("excess_return_loss_cny"),
        actual_advertising_spend_cny=_money(
            sum((order.actual_advertising_spend_cny for order in orders), ZERO)
        ),
        other_platform_cost_cny=other_total,
        owner_profit_cny=sum_field("owner_profit_cny"),
        unresolved_order_count=blocking_order_count,
        issue_count=len(blocking_issues),
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
            "managedOperationsCny": None,
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
    managed = _money(positive_revenue * policy.managed_rate)
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
    pool = _policy_decimal(
        policy,
        "combined_advertising_return_pool_rate",
        raw,
        ("costs", "advertisingAndReturns", "combinedPoolRate"),
    )
    if managed != Decimal("0.02") or pool != Decimal("0.03"):
        raise FinanceCalculationError(
            f"Financial calculation requires managed rate 0.02 and combined pool 0.03; got {managed}/{pool}"
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
        for field_name in fields:
            raw = record.row.get(field_name)
            if raw in (None, ""):
                continue
            try:
                values.append(_datetime(raw))
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
) -> tuple[Decimal, bool]:
    total = ZERO
    present = False
    for aliases in _OTHER_PLATFORM_FIELD_GROUPS:
        subtotal, seen = _sum_first_alias(
            records, aliases, issues, group, "other platform fee"
        )
        total += subtotal
        present = present or seen
    return total, present


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
    marker = _marker_text(row)
    return any(value in marker for value in _RETURN_MARKERS)


def _is_correction(row: Mapping[str, Any]) -> bool:
    marker = _marker_text(row)
    return any(value in marker for value in _CORRECTION_MARKERS)


def _is_completed_marker(row: Mapping[str, Any]) -> bool:
    marker = _marker_text(row)
    return any(value in marker for value in _COMPLETED_MARKERS)


def _has_logistics_marker(row: Mapping[str, Any]) -> bool:
    marker = _marker_text(row)
    return any(value in marker for value in ("物流", "логист", "delivery", "logistic"))


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


def _datetime(value: Any) -> datetime:
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
        result = result.replace(tzinfo=timezone.utc)
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
