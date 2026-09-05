from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


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
WITHDRAWAL_ADVERTISING_DEBT_CUTOVER = "2026-09-05"
WITHDRAWAL_ADVERTISING_DEBT_CUTOVER_MODE = "not_applicable_to_withdrawal"


class FinancialPolicyError(RuntimeError):
    """Raised when the active financial-reporting contract is unsafe or inconsistent."""


@dataclass(frozen=True)
class PhoneFinancialPolicy:
    reporting_policy_id: str
    pricing_policy_id: str
    effective_from: str
    reporting_currency: str
    managed_operations_rate: Decimal
    combined_advertising_return_pool_rate: Decimal
    advertising_deposit_cap_rule: str
    settlement_balance_contract_enabled: bool
    advertising_funding_source: str | None
    advertising_balance_field: str | None
    advertising_request_type: int | None
    advertising_deduction_mode: str | None
    withdrawal_advertising_debt_cutover: date | None
    raw: dict[str, Any]

    @property
    def settlement_balance_enabled(self) -> bool:
        """Compatibility alias for the explicit modern-contract capability flag."""

        return self.settlement_balance_contract_enabled

    def managed_operations_amount(self, signed_buyer_paid_cny: Decimal) -> Decimal:
        return max(Decimal("0"), signed_buyer_paid_cny) * self.managed_operations_rate

    def combined_pool_amount(self, signed_buyer_paid_cny: Decimal) -> Decimal:
        return max(Decimal("0"), signed_buyer_paid_cny) * self.combined_advertising_return_pool_rate


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
    combined_rate = _decimal(
        advertising_returns.get("combinedPoolRate"),
        "advertisingAndReturns.combinedPoolRate",
    )
    if managed_rate != Decimal("0.02"):
        raise FinancialPolicyError(
            f"Managed-operations rate must be 0.02, got {managed_rate}"
        )
    if combined_rate != Decimal("0.03"):
        raise FinancialPolicyError(
            f"Combined advertising-and-return pool rate must be 0.03, got {combined_rate}"
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

    # Legacy private policy versions remain readable during a staged rollout,
    # but they do not authorize settlement-balance writes. Once any modern
    # field is declared, the complete contract is required so a partial upgrade
    # cannot silently choose another money source or withdrawal cutover.
    withdrawal_debt_value = funding.get("withdrawalAdvertisingDebt")
    withdrawal_debt = (
        withdrawal_debt_value if isinstance(withdrawal_debt_value, dict) else {}
    )
    settlement_keys = {
        "fundingSource",
        "platformBalanceField",
        "platformRequestType",
        "deductionMode",
        "separateWithdrawalRequired",
        "manualCashTopUpRequired",
        "actualDeductionEvidenceRequired",
    }
    modern_contract = bool(set(advertising_deposit) & settlement_keys) or (
        "withdrawalAdvertisingDebt" in funding
    )
    funding_source: str | None = None
    balance_field: str | None = None
    request_type: int | None = None
    deduction_mode: str | None = None
    withdrawal_cutover: date | None = None
    if modern_contract:
        if not isinstance(withdrawal_debt_value, dict):
            raise FinancialPolicyError(
                "funding.withdrawalAdvertisingDebt must be an object for the "
                "settlement-balance contract"
            )
        expected = {
            "fundingSource": ADVERTISING_FUNDING_SOURCE,
            "platformBalanceField": ADVERTISING_BALANCE_FIELD,
            "platformRequestType": ADVERTISING_REQUEST_TYPE,
            "deductionMode": ADVERTISING_DEDUCTION_MODE,
            "separateWithdrawalRequired": False,
            "manualCashTopUpRequired": False,
            "actualDeductionEvidenceRequired": True,
        }
        invalid = [
            key
            for key, value in expected.items()
            if not _contract_value_matches(advertising_deposit.get(key), value)
        ]
        if invalid:
            raise FinancialPolicyError(
                "Advertising campaign allocation must use WB settlement "
                "balance/net/type=1 without withdrawal or cash top-up: "
                + ", ".join(invalid)
            )
        cutover_text = _required_text(
            withdrawal_debt,
            "cutoverEffectiveFrom",
            "funding.withdrawalAdvertisingDebt",
        )
        effective_from = _required_text(
            data,
            "effectiveFrom",
            "financial reporting policy",
        )
        try:
            withdrawal_cutover = date.fromisoformat(cutover_text)
            policy_effective_from = date.fromisoformat(effective_from)
        except ValueError as exc:
            raise FinancialPolicyError(
                "Settlement-balance cutoverEffectiveFrom and policy effectiveFrom "
                "must be ISO dates"
            ) from exc
        if withdrawal_cutover != policy_effective_from:
            raise FinancialPolicyError(
                "funding.withdrawalAdvertisingDebt.cutoverEffectiveFrom must equal "
                "the financial reporting policy effectiveFrom"
            )
        if (
            withdrawal_debt.get("modeFromCutover")
            != WITHDRAWAL_ADVERTISING_DEBT_CUTOVER_MODE
        ):
            raise FinancialPolicyError(
                "Post-cutover withdrawals must not create advertising debt"
            )
        for key in ("preserveHistoricalRecords", "noRetroactiveClearing"):
            if withdrawal_debt.get(key) is not True:
                raise FinancialPolicyError(
                    f"Withdrawal advertising-debt governance requires {key}=true"
                )
        funding_source = str(advertising_deposit["fundingSource"])
        balance_field = str(advertising_deposit["platformBalanceField"])
        request_type = int(advertising_deposit["platformRequestType"])
        deduction_mode = str(advertising_deposit["deductionMode"])

    return PhoneFinancialPolicy(
        reporting_policy_id=reporting_policy_id,
        pricing_policy_id=pricing_policy_id,
        effective_from=str(data.get("effectiveFrom") or "").strip(),
        reporting_currency=currency,
        managed_operations_rate=managed_rate,
        combined_advertising_return_pool_rate=combined_rate,
        advertising_deposit_cap_rule=cap_rule,
        settlement_balance_contract_enabled=modern_contract,
        advertising_funding_source=funding_source,
        advertising_balance_field=balance_field,
        advertising_request_type=request_type,
        advertising_deduction_mode=deduction_mode,
        withdrawal_advertising_debt_cutover=withdrawal_cutover,
        raw=data,
    )


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


def _contract_value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, int):
        return type(actual) is int and actual == expected
    return actual == expected


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
