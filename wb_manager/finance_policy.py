from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_PHONE_FINANCIAL_POLICY = (
    ROOT / "config" / "finance" / "active_phone_financial_reporting_policy.json"
)
ACTIVE_PHONE_PRICING_POLICY = (
    ROOT / "config" / "pricing" / "active_phone_pricing_policy.json"
)
ADVERTISING_DEPOSIT_CAP_RULE = "ledger_available_advertising_reserve_exact"
ADVERTISING_FUNDING_SOURCE = "balance"
ADVERTISING_BALANCE_FIELD = "net"
ADVERTISING_REQUEST_TYPE = 1
ADVERTISING_DEDUCTION_MODE = "future_sales_settlement_netting"
WITHDRAWAL_ADVERTISING_DEBT_CUTOVER_MODE = "not_applicable_to_withdrawal"
LEGACY_SHARED_POOL = "LEGACY_SHARED_POOL"
SEPARATE_RETURN_AND_PROMOTION = "SEPARATE_RETURN_AND_PROMOTION"
MOSCOW = ZoneInfo("Europe/Moscow")


class FinancialPolicyError(RuntimeError):
    """Raised when the active financial-reporting contract is unsafe or inconsistent."""


@dataclass(frozen=True)
class PhoneFinancialPolicy:
    reporting_policy_id: str
    pricing_policy_id: str
    effective_from: str
    effective_to: str | None
    reporting_currency: str
    managed_operations_rate: Decimal
    combined_advertising_return_pool_rate: Decimal
    reserve_allocation_mode: str
    return_risk_reserve_rate: Decimal
    promotion_reserve_rate: Decimal
    total_reserve_rate: Decimal
    advertising_deposit_cap_rule: str
    advertising_funding_source: str
    advertising_balance_field: str
    advertising_request_type: int
    advertising_deduction_mode: str
    raw: dict[str, Any]

    def managed_operations_amount(self, signed_buyer_paid_cny: Decimal) -> Decimal:
        return max(Decimal("0"), signed_buyer_paid_cny) * self.managed_operations_rate

    def combined_pool_amount(self, signed_buyer_paid_cny: Decimal) -> Decimal:
        return max(Decimal("0"), signed_buyer_paid_cny) * self.combined_advertising_return_pool_rate

    def return_risk_reserve_amount(self, signed_buyer_paid_cny: Decimal) -> Decimal:
        return max(Decimal("0"), signed_buyer_paid_cny) * self.return_risk_reserve_rate

    def promotion_reserve_amount(self, signed_buyer_paid_cny: Decimal) -> Decimal:
        return max(Decimal("0"), signed_buyer_paid_cny) * self.promotion_reserve_rate


@dataclass(frozen=True)
class PhoneFinancialPolicyTimeline:
    """Select the immutable reporting policy in force when an order was created.

    The immediate predecessor is intentionally used as the carry-back policy.  It
    preserves the already-published three-percent treatment for every historical
    order without depending on incomplete policies from older schema generations.
    """

    active_policy: PhoneFinancialPolicy
    predecessor_policy: PhoneFinancialPolicy
    historical_policies: tuple[PhoneFinancialPolicy, ...] = ()

    def select(self, order_created_at: str | None) -> PhoneFinancialPolicy:
        if not order_created_at:
            return self.predecessor_policy
        try:
            created_at = _instant(order_created_at, "orderCreatedAt")
            boundary = _instant(self.active_policy.effective_from, "effectiveFrom")
        except FinancialPolicyError:
            return self.predecessor_policy
        policies = self.historical_policies or (self.predecessor_policy,)
        candidates = (*policies, self.active_policy)
        applicable = [
            item
            for item in candidates
            if created_at >= _instant(item.effective_from, "effectiveFrom")
        ]
        if applicable:
            return max(
                applicable,
                key=lambda item: _instant(item.effective_from, "effectiveFrom"),
            )
        return min(
            policies,
            key=lambda item: _instant(item.effective_from, "effectiveFrom"),
        )


def load_active_phone_financial_policy(
    path: Path = ACTIVE_PHONE_FINANCIAL_POLICY,
    *,
    pricing_policy_path: Path = ACTIVE_PHONE_PRICING_POLICY,
) -> PhoneFinancialPolicy:
    data = _read_json(path, label="financial reporting policy")
    pricing = _read_json(pricing_policy_path, label="pricing policy")

    if data.get("status") != "active":
        raise FinancialPolicyError(
            f"Financial reporting policy is not active: {data.get('policyId')}"
        )
    if pricing.get("status") != "active":
        raise FinancialPolicyError(
            f"Referenced pricing policy is not active: {pricing.get('policyId')}"
        )

    governance = data.get("governance") or {}
    if int(data.get("schemaVersion") or 0) >= 5:
        required_versioning = (
            "effectiveDatedReportingPolicies",
            "reportingPolicyVersionUsesOrderCreatedAt",
            "requireExplicitSourceCurrency",
            "rubCostImportsRequireExplicitExchangeRate",
        )
        missing_versioning = [
            key for key in required_versioning if governance.get(key) is not True
        ]
        if missing_versioning:
            raise FinancialPolicyError(
                "Financial-policy versioning governance is incomplete: "
                + ", ".join(missing_versioning)
            )
        if governance.get("latestTemplateForNonPurchaseCostRules") is not False:
            raise FinancialPolicyError(
                "Non-purchase-cost reporting rules must be selected by orderCreatedAt"
            )

    reporting_policy_id = _required_text(data, "policyId", "financial reporting policy")
    pricing_policy_id = _required_text(pricing, "policyId", "pricing policy")
    references = data.get("pricingReferences") or {}
    referenced_pricing_id = _required_text(
        references, "activePricingPolicyId", "pricingReferences"
    )
    if referenced_pricing_id != pricing_policy_id:
        raise FinancialPolicyError(
            "Financial reporting policy references pricing policy "
            f"{referenced_pricing_id}, but the active pricing policy is {pricing_policy_id}"
        )
    for key in ("reportingPolicyId", "financialReportingPolicyId"):
        declared = str(references.get(key) or "").strip()
        if declared and declared != reporting_policy_id:
            raise FinancialPolicyError(
                f"{key} does not match the active reporting policy ID: {reporting_policy_id}"
            )

    scope = data.get("scope") or {}
    currency = _required_text(scope, "reportingCurrency", "scope")
    if currency != "CNY":
        raise FinancialPolicyError(
            f"Phone financial reporting currency must be CNY, got {currency}"
        )

    costs = data.get("costs") or {}
    managed = costs.get("managedOperations") or {}
    advertising_returns = costs.get("advertisingAndReturns") or {}
    managed_rate = _decimal(managed.get("rate"), "managedOperations.rate")
    reserve_mode, return_risk_rate, promotion_rate, total_reserve_rate = (
        _reserve_policy(advertising_returns)
    )
    combined_rate = total_reserve_rate
    if managed_rate != Decimal("0.02"):
        raise FinancialPolicyError(
            f"Managed-operations rate must be 0.02, got {managed_rate}"
        )
    if reserve_mode == LEGACY_SHARED_POOL and combined_rate != Decimal("0.03"):
        raise FinancialPolicyError(
            f"Legacy combined advertising-and-return pool rate must be 0.03, got {combined_rate}"
        )
    if reserve_mode == SEPARATE_RETURN_AND_PROMOTION and (
        return_risk_rate != Decimal("0.02")
        or promotion_rate != Decimal("0.03")
        or combined_rate != Decimal("0.05")
    ):
        raise FinancialPolicyError(
            "Separate reserves must be return risk 0.02 + promotion 0.03 = total 0.05"
        )

    _validate_return_logistics_contract(data, advertising_returns)
    _validate_advertising_contract(data)
    funding = data.get("funding") or {}
    advertising_deposit = funding.get("advertisingDeposit") or {}
    cap_rule = _required_text(
        advertising_deposit,
        "perActionCapRule",
        "funding.advertisingDeposit",
    )
    if cap_rule != ADVERTISING_DEPOSIT_CAP_RULE:
        raise FinancialPolicyError(
            "Advertising-deposit per-action cap must equal the live ledger-derived "
            f"available reserve via rule {ADVERTISING_DEPOSIT_CAP_RULE}"
        )
    required_true = (
        "requireExactReserveEquality",
        "requirePriorDryRunProposal",
        "requireLiveSellerBalanceCurrencyReadback",
    )
    missing = [key for key in required_true if advertising_deposit.get(key) is not True]
    if missing:
        raise FinancialPolicyError(
            "Advertising-deposit funding governance is incomplete: " + ", ".join(missing)
        )
    required_funding_values = {
        "fundingSource": ADVERTISING_FUNDING_SOURCE,
        "platformBalanceField": ADVERTISING_BALANCE_FIELD,
        "platformRequestType": ADVERTISING_REQUEST_TYPE,
        "deductionMode": ADVERTISING_DEDUCTION_MODE,
    }
    invalid_funding_values = [
        key
        for key, expected in required_funding_values.items()
        if advertising_deposit.get(key) != expected
    ]
    if invalid_funding_values:
        raise FinancialPolicyError(
            "Advertising campaign allocation must use WB settlement balance/net/type=1: "
            + ", ".join(invalid_funding_values)
        )
    required_false = ("separateWithdrawalRequired", "manualCashTopUpRequired")
    invalid_false = [key for key in required_false if advertising_deposit.get(key) is not False]
    if invalid_false:
        raise FinancialPolicyError(
            "Advertising campaign allocation must not require withdrawal or cash top-up: "
            + ", ".join(invalid_false)
        )
    if advertising_deposit.get("actualDeductionEvidenceRequired") is not True:
        raise FinancialPolicyError(
            "Platform advertising settlement deductions require actual readback evidence"
        )

    withdrawal_debt = funding.get("withdrawalAdvertisingDebt") or {}
    cutover = _required_text(
        withdrawal_debt,
        "cutoverEffectiveFrom",
        "funding.withdrawalAdvertisingDebt",
    )
    effective_from = _required_text(data, "effectiveFrom", "financial reporting policy")
    if _instant(cutover, "funding.withdrawalAdvertisingDebt.cutoverEffectiveFrom") > _instant(
        effective_from, "effectiveFrom"
    ):
        raise FinancialPolicyError(
            "Withdrawal advertising-debt cutover cannot be later than policy effectiveFrom"
        )
    if withdrawal_debt.get("modeFromCutover") != WITHDRAWAL_ADVERTISING_DEBT_CUTOVER_MODE:
        raise FinancialPolicyError(
            "Withdrawals after the cutover must not create withdrawal-linked advertising debt"
        )
    for key in ("preserveHistoricalRecords", "noRetroactiveClearing"):
        if withdrawal_debt.get(key) is not True:
            raise FinancialPolicyError(
                f"Withdrawal advertising-debt governance requires {key}=true"
            )

    return PhoneFinancialPolicy(
        reporting_policy_id=reporting_policy_id,
        pricing_policy_id=pricing_policy_id,
        effective_from=effective_from,
        effective_to=str(data.get("effectiveTo") or "").strip() or None,
        reporting_currency=currency,
        managed_operations_rate=managed_rate,
        combined_advertising_return_pool_rate=combined_rate,
        reserve_allocation_mode=reserve_mode,
        return_risk_reserve_rate=return_risk_rate,
        promotion_reserve_rate=promotion_rate,
        total_reserve_rate=total_reserve_rate,
        advertising_deposit_cap_rule=cap_rule,
        advertising_funding_source=ADVERTISING_FUNDING_SOURCE,
        advertising_balance_field=ADVERTISING_BALANCE_FIELD,
        advertising_request_type=ADVERTISING_REQUEST_TYPE,
        advertising_deduction_mode=ADVERTISING_DEDUCTION_MODE,
        raw=data,
    )


def load_phone_financial_policy_timeline(
    path: Path = ACTIVE_PHONE_FINANCIAL_POLICY,
    *,
    pricing_policy_path: Path = ACTIVE_PHONE_PRICING_POLICY,
) -> PhoneFinancialPolicyTimeline:
    """Load the active policy and its archived immediate predecessor."""

    active = load_active_phone_financial_policy(
        path, pricing_policy_path=pricing_policy_path
    )
    predecessor_id = str(active.raw.get("supersedes") or "").strip()
    if not predecessor_id:
        raise FinancialPolicyError("Active financial policy must name its predecessor")
    predecessor_path = path.parent / "policies" / f"{predecessor_id}.json"
    predecessor_data = _read_json(
        predecessor_path, label="predecessor financial reporting policy"
    )
    if predecessor_data.get("status") != "historical":
        raise FinancialPolicyError(
            f"Predecessor financial policy must be historical: {predecessor_id}"
        )
    predecessor = _parse_historical_policy(predecessor_data)
    if predecessor.effective_to != active.effective_from:
        raise FinancialPolicyError(
            "Predecessor effectiveTo must equal active policy effectiveFrom"
        )
    history = [predecessor]
    next_id = str(predecessor.raw.get("supersedes") or "").strip()
    while next_id:
        next_path = path.parent / "policies" / f"{next_id}.json"
        if not next_path.is_file():
            break
        try:
            older = _parse_historical_policy(
                _read_json(next_path, label="historical financial reporting policy")
            )
        except FinancialPolicyError:
            # Older schema generations may not carry the calculation contract.
            # Stop rather than guessing; the earliest fully specified policy is
            # the safe carry-back for orders that predate it.
            break
        if older.effective_to != history[-1].effective_from:
            raise FinancialPolicyError(
                "Historical financial-policy intervals must be contiguous"
            )
        history.append(older)
        next_id = str(older.raw.get("supersedes") or "").strip()
    return PhoneFinancialPolicyTimeline(
        active, predecessor, tuple(reversed(history))
    )


def _parse_historical_policy(data: dict[str, Any]) -> PhoneFinancialPolicy:
    scope = data.get("scope") or {}
    costs = data.get("costs") or {}
    managed = costs.get("managedOperations") or {}
    advertising_returns = costs.get("advertisingAndReturns") or {}
    managed_rate = _decimal(managed.get("rate"), "managedOperations.rate")
    reserve_mode, return_rate, promotion_rate, total_rate = _reserve_policy(
        advertising_returns
    )
    references = data.get("pricingReferences") or {}
    funding = data.get("funding") or {}
    deposit = funding.get("advertisingDeposit") or {}
    return PhoneFinancialPolicy(
        reporting_policy_id=_required_text(data, "policyId", "historical policy"),
        pricing_policy_id=_required_text(
            references, "activePricingPolicyId", "pricingReferences"
        ),
        effective_from=_required_text(data, "effectiveFrom", "historical policy"),
        effective_to=_required_text(data, "effectiveTo", "historical policy"),
        reporting_currency=_required_text(scope, "reportingCurrency", "scope"),
        managed_operations_rate=managed_rate,
        combined_advertising_return_pool_rate=total_rate,
        reserve_allocation_mode=reserve_mode,
        return_risk_reserve_rate=return_rate,
        promotion_reserve_rate=promotion_rate,
        total_reserve_rate=total_rate,
        advertising_deposit_cap_rule=str(deposit.get("perActionCapRule") or ""),
        advertising_funding_source=str(deposit.get("fundingSource") or ""),
        advertising_balance_field=str(deposit.get("platformBalanceField") or ""),
        advertising_request_type=int(deposit.get("platformRequestType") or 0),
        advertising_deduction_mode=str(deposit.get("deductionMode") or ""),
        raw=data,
    )


def _reserve_policy(
    advertising_returns: dict[str, Any],
) -> tuple[str, Decimal, Decimal, Decimal]:
    mode = str(advertising_returns.get("reserveAllocationMode") or "").strip()
    has_separate_rates = any(
        key in advertising_returns
        for key in ("returnRiskReserveRate", "promotionReserveRate", "totalReserveRate")
    )
    if mode == SEPARATE_RETURN_AND_PROMOTION or has_separate_rates:
        return_rate = _decimal(
            advertising_returns.get("returnRiskReserveRate"),
            "advertisingAndReturns.returnRiskReserveRate",
        )
        promotion_rate = _decimal(
            advertising_returns.get("promotionReserveRate"),
            "advertisingAndReturns.promotionReserveRate",
        )
        total_rate = _decimal(
            advertising_returns.get("totalReserveRate"),
            "advertisingAndReturns.totalReserveRate",
        )
        if return_rate + promotion_rate != total_rate:
            raise FinancialPolicyError(
                "Return-risk and promotion reserve rates must add to totalReserveRate"
            )
        return SEPARATE_RETURN_AND_PROMOTION, return_rate, promotion_rate, total_rate
    combined_rate = _decimal(
        advertising_returns.get("combinedPoolRate"),
        "advertisingAndReturns.combinedPoolRate",
    )
    return LEGACY_SHARED_POOL, Decimal("0"), combined_rate, combined_rate


def _instant(value: str, label: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise FinancialPolicyError(f"Missing timestamp: {label}")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinancialPolicyError(f"Invalid timestamp for {label}: {text}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW)
    return parsed.astimezone(timezone.utc)


def _validate_return_logistics_contract(
    data: dict[str, Any], advertising_returns: dict[str, Any]
) -> None:
    governance = data.get("governance") or {}
    required_true = (
        "returnLogisticsMustComeFromPlatformFinance",
        "noReturnLogisticsFallback",
    )
    missing = [key for key in required_true if governance.get(key) is not True]
    if missing:
        raise FinancialPolicyError(
            "Return-logistics governance is incomplete: " + ", ".join(missing)
        )

    source = str(
        advertising_returns.get("actualReturnLogisticsSource")
        or advertising_returns.get("returnLogisticsActualSource")
        or ""
    ).strip().lower()
    if "finance" not in source or not any(
        marker in source for marker in ("transaction", "row", "流水", "明细")
    ):
        raise FinancialPolicyError(
            "Actual return logistics must identify platform finance transactions/rows as its source"
        )

    forbidden_keys = {
        "actualreturnlogisticsperunitcny",
        "returnlogisticsfallbackcnyperreturnedunit",
        "returnlogisticscnyperreturnedunit",
        "fallbackreturnlogisticsperunitcny",
        "fixedreturnlogisticscny",
    }
    found = sorted(
        path for path, _ in _walk(data) if _normalized_key(path.rsplit(".", 1)[-1]) in forbidden_keys
    )
    if found:
        raise FinancialPolicyError(
            "Fixed 8/16 CNY return-logistics assumptions are forbidden; use platform finance rows: "
            + ", ".join(found)
        )

    for path, value in _walk(data):
        normalized_path = _normalized_key(path)
        if (
            "return" in normalized_path
            and any(marker in normalized_path for marker in ("fallback", "perunit", "fixed"))
            and _is_fixed_return_amount(value)
        ):
            raise FinancialPolicyError(
                f"Fixed 8/16 CNY return-logistics fallback is forbidden at {path}"
            )
        if not isinstance(value, str):
            continue
        lowered = value.lower().replace(" ", "")
        if "return" not in lowered and "退货" not in lowered and "returnlogistics" not in path.lower():
            continue
        fixed_formula = any(
            marker in lowered
            for marker in (
                "8*returned",
                "8*return",
                "returnedunitcount*8",
                "returncount*8",
                "每件8",
                "每单8",
                "固定8",
                "固定16",
                "8cnyper",
                "16cnyper",
            )
        )
        if fixed_formula:
            raise FinancialPolicyError(
                f"Fixed return-logistics formula is forbidden at {path}"
            )


def _validate_advertising_contract(data: dict[str, Any]) -> None:
    governance = data.get("governance") or {}
    required_true = (
        "actualAdvertisingSpendReconciliationOnly",
        "noDoubleCountActualAdvertisingOutsideCombinedPool",
        "platformAdvertisingSettlementDeductionReconciliationOnly",
        "noNewWithdrawalLinkedAdvertisingDebtAfterCutover",
    )
    missing = [key for key in required_true if governance.get(key) is not True]
    if missing:
        raise FinancialPolicyError(
            "Advertising governance is incomplete: " + ", ".join(missing)
        )

    formulas = data.get("formulas") or {}
    profit_formula = " ".join(
        str(value).lower()
        for key, value in formulas.items()
        if "profit" in str(key).lower()
    )
    forbidden = (
        "actualadvertisingspendcny",
        "actualadspendcny",
        "advertisingactualcny",
    )
    if any(name in profit_formula.replace(" ", "") for name in forbidden):
        raise FinancialPolicyError(
            "Actual advertising spend is reconciliation-only and must not be deducted again in profit formulas"
        )


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    if not path.exists():
        raise FinancialPolicyError(f"Active {label} is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinancialPolicyError(f"Cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FinancialPolicyError(f"{label.title()} must be a JSON object: {path}")
    return value


def _required_text(parent: dict[str, Any], key: str, label: str) -> str:
    value = str(parent.get(key) or "").strip()
    if not value:
        raise FinancialPolicyError(f"{label} requires {key}")
    return value


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinancialPolicyError(f"{label} must be a decimal number") from exc
    if not result.is_finite():
        raise FinancialPolicyError(f"{label} must be finite")
    return result


def _walk(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path, child
            yield from _walk(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = f"{prefix}[{index}]"
            yield path, child
            yield from _walk(child, path)


def _normalized_key(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _is_fixed_return_amount(value: Any) -> bool:
    try:
        return Decimal(str(value)) in {Decimal("8"), Decimal("16")}
    except (InvalidOperation, TypeError, ValueError):
        return False
