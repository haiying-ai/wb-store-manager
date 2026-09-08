from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


MOSCOW = ZoneInfo("Europe/Moscow")
_CENT = Decimal("0.01")
_DEPOSIT_SOURCES = {"account", "balance", "bonus"}
_DEPOSIT_CURRENCY = "CNY"
_MAX_DEPOSIT_BALANCE_EVIDENCE_AGE = timedelta(minutes=15)
_MAX_DEPOSIT_PLAN_LIFETIME = timedelta(hours=1)
_LOCKING_DEPOSIT_STATUSES = {
    "approved",
    "executing",
    "executed",
    "succeeded",
    "unknown",
    "readback_pending",
}
_KNOWN_DEPOSIT_STATUSES = _LOCKING_DEPOSIT_STATUSES | {
    "proposed",
    "failed",
    "cancelled",
    "readback_confirmed",
}


class FundingPlanError(ValueError):
    """Raised when a funding plan would require guessing or unsafe execution."""


@dataclass(frozen=True)
class AdvertisingAccrual:
    accrual_id: str
    amount_cny: Decimal
    allocated_cny: Decimal
    accrued_at: datetime
    historical_bootstrap: bool = False


@dataclass(frozen=True)
class DepositCommitment:
    action_id: str
    amount_cny: Decimal
    status: str
    readback_at: datetime | None = None


@dataclass(frozen=True)
class StoreFundingPlan:
    report_id: str
    store: str
    balance_evidence_at: datetime
    formal_activation_at: datetime
    historical_bootstrap: bool
    platform_available_balance_cny: Decimal
    unallocated_advertising_accrual_cny: Decimal
    excluded_historical_advertising_accrual_cny: Decimal
    unread_advertising_deposits_cny: Decimal
    other_locked_funds_cny: Decimal
    suggested_withdrawal_cny: Decimal
    eligible_accrual_ids: tuple[str, ...]
    excluded_accrual_ids: tuple[str, ...]
    locked_deposit_action_ids: tuple[str, ...]
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        payload = _funding_payload(
            report_id=self.report_id,
            store=self.store,
            balance_evidence_at=self.balance_evidence_at,
            formal_activation_at=self.formal_activation_at,
            historical_bootstrap=self.historical_bootstrap,
            platform_available_balance_cny=self.platform_available_balance_cny,
            unallocated_advertising_accrual_cny=self.unallocated_advertising_accrual_cny,
            excluded_historical_advertising_accrual_cny=(
                self.excluded_historical_advertising_accrual_cny
            ),
            unread_advertising_deposits_cny=self.unread_advertising_deposits_cny,
            other_locked_funds_cny=self.other_locked_funds_cny,
            suggested_withdrawal_cny=self.suggested_withdrawal_cny,
            eligible_accrual_ids=self.eligible_accrual_ids,
            excluded_accrual_ids=self.excluded_accrual_ids,
            locked_deposit_action_ids=self.locked_deposit_action_ids,
        )
        payload["fingerprint"] = self.fingerprint
        return payload


@dataclass(frozen=True)
class AdvertisingDepositPlan:
    report_id: str | None
    store: str
    campaign: int
    amount_cny: Decimal
    source: str
    currency: str
    source_balance_cny: Decimal
    source_balance_evidence_at: datetime
    policy_cap_cny: Decimal
    available_advertising_reserve_cny: Decimal
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    fingerprint: str
    funding_basis: Mapping[str, Any] | None = None
    cash_amount_cny: Decimal | None = None
    cashback_amount_cny: Decimal = Decimal("0")
    cashback_percent: int | None = None
    cashback_available_cny: Decimal | None = None
    cashback_expires_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = _deposit_payload(
            report_id=self.report_id,
            store=self.store,
            campaign=self.campaign,
            amount_cny=self.amount_cny,
            source=self.source,
            currency=self.currency,
            source_balance_cny=self.source_balance_cny,
            source_balance_evidence_at=self.source_balance_evidence_at,
            policy_cap_cny=self.policy_cap_cny,
            available_advertising_reserve_cny=self.available_advertising_reserve_cny,
            approved_by=self.approved_by,
            approved_at=self.approved_at,
            expires_at=self.expires_at,
            funding_basis=self.funding_basis,
            cash_amount_cny=self.cash_amount_cny,
            cashback_amount_cny=self.cashback_amount_cny,
            cashback_percent=self.cashback_percent,
            cashback_available_cny=self.cashback_available_cny,
            cashback_expires_at=self.cashback_expires_at,
        )
        payload["fingerprint"] = self.fingerprint
        return payload


@dataclass(frozen=True)
class DepositExecutionPreflight:
    allowed: bool
    reason: str
    requires_readback: bool
    automatic_retry_allowed: bool
    plan_fingerprint: str


def build_funding_plan(
    *,
    report_id: str,
    store: str,
    platform_available_balance_cny: Any,
    balance_evidence_at: datetime | str,
    formal_activation_at: date | datetime | str,
    advertising_accruals: Iterable[AdvertisingAccrual | Mapping[str, Any]] = (),
    deposit_commitments: Iterable[DepositCommitment | Mapping[str, Any]] = (),
    other_locked_funds_cny: Any = Decimal("0"),
    historical_bootstrap: bool = False,
) -> StoreFundingPlan:
    """Build a plan-only store balance allocation with no withdrawal executor."""

    report = _required_text(report_id, "report_id")
    store_name = _required_text(store, "store")
    balance = _non_negative_money(
        platform_available_balance_cny, "platform_available_balance_cny"
    )
    evidence_at = _aware_datetime(balance_evidence_at, "balance_evidence_at")
    activation_at = _activation_datetime(formal_activation_at)
    other_locked = _non_negative_money(other_locked_funds_cny, "other_locked_funds_cny")
    if not isinstance(historical_bootstrap, bool):
        raise FundingPlanError("historical_bootstrap must be boolean")

    eligible_amount = Decimal("0.00")
    excluded_amount = Decimal("0.00")
    eligible_ids: list[str] = []
    excluded_ids: list[str] = []
    seen_accruals: set[str] = set()
    for value in advertising_accruals:
        accrual = _advertising_accrual(value)
        if accrual.accrual_id in seen_accruals:
            raise FundingPlanError(f"Duplicate advertising accrual: {accrual.accrual_id}")
        seen_accruals.add(accrual.accrual_id)
        if accrual.allocated_cny > accrual.amount_cny:
            raise FundingPlanError(
                f"Advertising accrual allocation exceeds amount: {accrual.accrual_id}"
            )
        unallocated = accrual.amount_cny - accrual.allocated_cny
        excluded = (
            historical_bootstrap
            or accrual.historical_bootstrap
            or accrual.accrued_at < activation_at
        )
        if excluded:
            excluded_amount += unallocated
            excluded_ids.append(accrual.accrual_id)
        else:
            eligible_amount += unallocated
            eligible_ids.append(accrual.accrual_id)

    unread_deposits = Decimal("0.00")
    locked_deposit_ids: list[str] = []
    seen_deposits: set[str] = set()
    for value in deposit_commitments:
        commitment = _deposit_commitment(value)
        if commitment.action_id in seen_deposits:
            raise FundingPlanError(f"Duplicate deposit action: {commitment.action_id}")
        seen_deposits.add(commitment.action_id)
        if (
            commitment.readback_at is None
            and commitment.status in _LOCKING_DEPOSIT_STATUSES
        ):
            unread_deposits += commitment.amount_cny
            locked_deposit_ids.append(commitment.action_id)

    eligible_amount = eligible_amount.quantize(_CENT)
    excluded_amount = excluded_amount.quantize(_CENT)
    unread_deposits = unread_deposits.quantize(_CENT)
    total_locked = eligible_amount + unread_deposits + other_locked
    suggested = max(Decimal("0.00"), balance - total_locked).quantize(_CENT)
    payload = _funding_payload(
        report_id=report,
        store=store_name,
        balance_evidence_at=evidence_at,
        formal_activation_at=activation_at,
        historical_bootstrap=historical_bootstrap,
        platform_available_balance_cny=balance,
        unallocated_advertising_accrual_cny=eligible_amount,
        excluded_historical_advertising_accrual_cny=excluded_amount,
        unread_advertising_deposits_cny=unread_deposits,
        other_locked_funds_cny=other_locked,
        suggested_withdrawal_cny=suggested,
        eligible_accrual_ids=tuple(sorted(eligible_ids)),
        excluded_accrual_ids=tuple(sorted(excluded_ids)),
        locked_deposit_action_ids=tuple(sorted(locked_deposit_ids)),
    )
    fingerprint = _fingerprint(payload)
    return StoreFundingPlan(
        report_id=report,
        store=store_name,
        balance_evidence_at=evidence_at,
        formal_activation_at=activation_at,
        historical_bootstrap=historical_bootstrap,
        platform_available_balance_cny=balance,
        unallocated_advertising_accrual_cny=eligible_amount,
        excluded_historical_advertising_accrual_cny=excluded_amount,
        unread_advertising_deposits_cny=unread_deposits,
        other_locked_funds_cny=other_locked,
        suggested_withdrawal_cny=suggested,
        eligible_accrual_ids=tuple(sorted(eligible_ids)),
        excluded_accrual_ids=tuple(sorted(excluded_ids)),
        locked_deposit_action_ids=tuple(sorted(locked_deposit_ids)),
        fingerprint=fingerprint,
    )


def validate_funding_plan(plan: StoreFundingPlan | Mapping[str, Any]) -> dict[str, Any]:
    payload = plan.as_dict() if isinstance(plan, StoreFundingPlan) else dict(plan)
    required = {
        "schemaVersion",
        "planType",
        "reportId",
        "store",
        "balanceEvidenceAt",
        "formalActivationAt",
        "historicalBootstrap",
        "components",
        "suggestedWithdrawalCny",
        "evidence",
        "withdrawalExecutionSupported",
        "fingerprint",
    }
    if set(payload) != required:
        raise FundingPlanError(
            f"Funding plan fields differ from the contract: {sorted(set(payload) ^ required)}"
        )
    if payload.get("schemaVersion") != 1 or payload.get("planType") != "store_funding":
        raise FundingPlanError("Funding plan schema or type is invalid")
    _required_text(payload.get("reportId"), "reportId")
    _required_text(payload.get("store"), "store")
    _aware_datetime(payload.get("balanceEvidenceAt"), "balanceEvidenceAt")
    _aware_datetime(payload.get("formalActivationAt"), "formalActivationAt")
    if not isinstance(payload.get("historicalBootstrap"), bool):
        raise FundingPlanError("historicalBootstrap must be boolean")
    if payload.get("withdrawalExecutionSupported") is not False:
        raise FundingPlanError("Withdrawal execution is not supported")

    components = payload.get("components")
    if not isinstance(components, Mapping):
        raise FundingPlanError("Funding plan components are required")
    component_keys = {
        "platformAvailableBalanceCny",
        "postActivationUnallocatedAdvertisingAccrualCny",
        "historicalAdvertisingAccrualExcludedCny",
        "approvedOrExecutingUnreadAdvertisingDepositsCny",
        "otherLockedFundsCny",
        "totalLockedFundsCny",
    }
    if set(components) != component_keys:
        raise FundingPlanError("Funding plan components are incomplete or unknown")
    amounts = {
        key: _non_negative_money(value, key) for key, value in components.items()
    }
    expected_locked = (
        amounts["postActivationUnallocatedAdvertisingAccrualCny"]
        + amounts["approvedOrExecutingUnreadAdvertisingDepositsCny"]
        + amounts["otherLockedFundsCny"]
    ).quantize(_CENT)
    if amounts["totalLockedFundsCny"] != expected_locked:
        raise FundingPlanError("totalLockedFundsCny does not equal its components")
    suggested = _non_negative_money(
        payload.get("suggestedWithdrawalCny"), "suggestedWithdrawalCny"
    )
    expected_suggested = max(
        Decimal("0.00"),
        amounts["platformAvailableBalanceCny"] - expected_locked,
    ).quantize(_CENT)
    if suggested != expected_suggested:
        raise FundingPlanError("suggestedWithdrawalCny does not match the funding formula")

    evidence = payload.get("evidence")
    evidence_keys = {
        "eligibleAccrualIds",
        "excludedAccrualIds",
        "lockedDepositActionIds",
    }
    if not isinstance(evidence, Mapping) or set(evidence) != evidence_keys:
        raise FundingPlanError("Funding plan evidence is missing or unknown")
    for key in evidence_keys:
        values = evidence.get(key)
        if not isinstance(values, list) or any(not str(value).strip() for value in values):
            raise FundingPlanError(f"{key} must be a list of non-empty evidence IDs")
        if values != sorted(set(values)):
            raise FundingPlanError(f"{key} must be sorted and unique")

    actual_fingerprint = str(payload.get("fingerprint") or "").strip().lower()
    expected_fingerprint = _fingerprint(
        {key: value for key, value in payload.items() if key != "fingerprint"}
    )
    if actual_fingerprint != expected_fingerprint:
        raise FundingPlanError("Funding plan fingerprint does not match its content")
    return payload


def build_advertising_deposit_plan(
    *,
    report_id: str | None = None,
    store: str,
    campaign: int,
    amount_cny: Any,
    source: str,
    currency: str,
    source_balance_cny: Any,
    source_balance_evidence_at: datetime | str,
    policy_cap_cny: Any,
    available_advertising_reserve_cny: Any,
    approved_by: str,
    approved_at: datetime | str,
    expires_at: datetime | str,
    funding_basis: Mapping[str, Any] | None = None,
    cashback_amount_cny: Any = 0,
    cashback_percent: int | None = None,
    cashback_available_cny: Any | None = None,
    cashback_expires_at: datetime | str | None = None,
) -> AdvertisingDepositPlan:
    """Freeze a v2 weekly cash plan or a v3 daily cash/promo funding plan.

    ``amount_cny`` is the total campaign credit. Only its seller-cash portion
    consumes the daily confirmed reserve or live source balance. The service
    must verify the complete daily-basis fingerprint and recheck the selected
    live cashback bucket before execution; this pure builder grants no money
    movement authorization.
    """
    daily = funding_basis is not None
    if daily and report_id not in (None, ""):
        raise FundingPlanError("Daily funding plans require report_id=None")
    report = None if daily else _required_text(report_id, "report_id")
    store_name = _required_text(store, "store")
    if isinstance(campaign, bool):
        raise FundingPlanError("campaign must be a positive integer")
    try:
        campaign_id = int(campaign)
    except (TypeError, ValueError) as exc:
        raise FundingPlanError("campaign must be a positive integer") from exc
    if campaign_id <= 0 or str(campaign_id) != str(campaign).strip():
        raise FundingPlanError("campaign must be a positive integer")
    amount = _whole_money(amount_cny, "amount_cny", positive=True)
    source_name = _required_text(source, "source").lower()
    if source_name not in _DEPOSIT_SOURCES:
        raise FundingPlanError(f"Unsupported advertising deposit source: {source!r}")
    if daily and source_name != "balance":
        raise FundingPlanError("Daily funding must use WB balance (net/type=1)")
    currency_name = _required_text(currency, "currency").upper()
    if currency_name != _DEPOSIT_CURRENCY:
        raise FundingPlanError(
            f"Advertising deposit currency must be {_DEPOSIT_CURRENCY}"
        )
    source_balance = (_exact_non_negative_money if daily else _non_negative_money)(
        source_balance_cny, "source_balance_cny"
    )
    source_evidence_at = _aware_datetime(
        source_balance_evidence_at, "source_balance_evidence_at"
    )
    policy_cap = (_exact_non_negative_money if daily else _positive_money)(
        policy_cap_cny, "policy_cap_cny"
    )
    advertising_reserve = (_exact_non_negative_money if daily else _non_negative_money)(
        available_advertising_reserve_cny,
        "available_advertising_reserve_cny",
    )
    approver = _required_text(approved_by, "approved_by")
    approval_time = _aware_datetime(approved_at, "approved_at")
    expiry = _aware_datetime(expires_at, "expires_at")
    basis = _validated_funding_basis(funding_basis, store_name, currency_name) if daily else None
    cashback = _whole_money(cashback_amount_cny, "cashback_amount_cny")
    cashback_available = None
    cashback_expiry = None
    percent = None
    if not daily and (
        cashback or any(value is not None for value in (
            cashback_percent, cashback_available_cny, cashback_expires_at
        ))
    ):
        raise FundingPlanError("Cashback funding requires a daily funding basis (schema v3)")
    if cashback:
        percent = _cashback_percentage(cashback_percent)
        cashback_available = _exact_non_negative_money(cashback_available_cny, "cashback_available_cny")
        cashback_expiry = _aware_datetime(cashback_expires_at, "cashback_expires_at")
        if cashback > amount or cashback * 100 > amount * percent:
            raise FundingPlanError("cashback amount exceeds the bucket percentage of total deposit")
        if cashback > cashback_available:
            raise FundingPlanError("cashback amount exceeds the selected bucket balance")
        if cashback_expiry <= approval_time or expiry > cashback_expiry:
            raise FundingPlanError("cashback bucket expires before the approved plan expires")
    elif daily and any(value is not None for value in (
        cashback_percent, cashback_available_cny, cashback_expires_at
    )):
        raise FundingPlanError("Cash-only plans must not retain unused cashback bucket fields")
    cash = amount - cashback
    if source_evidence_at > approval_time:
        raise FundingPlanError("source balance evidence cannot postdate approval")
    if approval_time - source_evidence_at > _MAX_DEPOSIT_BALANCE_EVIDENCE_AGE:
        raise FundingPlanError("source balance evidence is stale at approval")
    if expiry <= approval_time:
        raise FundingPlanError("expires_at must be after approved_at")
    if expiry - approval_time > _MAX_DEPOSIT_PLAN_LIFETIME:
        raise FundingPlanError("advertising deposit approval cannot remain valid over one hour")
    if cash > source_balance:
        raise FundingPlanError("advertising deposit exceeds the frozen source balance")
    if policy_cap != advertising_reserve:
        raise FundingPlanError(
            "policy_cap_cny must equal the governed available advertising reserve"
        )
    if basis is not None and _non_negative_money(
        basis["availableCashCny"], "fundingBasis.availableCashCny"
    ) != advertising_reserve:
        raise FundingPlanError("Daily funding basis differs from the frozen cash reserve")
    if cash > policy_cap:
        raise FundingPlanError("advertising deposit exceeds the governed policy cap")
    if cash > advertising_reserve:
        raise FundingPlanError("advertising deposit exceeds the frozen advertising reserve")
    payload = _deposit_payload(
        report_id=report,
        store=store_name,
        campaign=campaign_id,
        amount_cny=amount,
        source=source_name,
        currency=currency_name,
        source_balance_cny=source_balance,
        source_balance_evidence_at=source_evidence_at,
        policy_cap_cny=policy_cap,
        available_advertising_reserve_cny=advertising_reserve,
        approved_by=approver,
        approved_at=approval_time,
        expires_at=expiry,
        funding_basis=basis,
        cash_amount_cny=cash if daily else None,
        cashback_amount_cny=cashback,
        cashback_percent=percent,
        cashback_available_cny=cashback_available,
        cashback_expires_at=cashback_expiry,
    )
    return AdvertisingDepositPlan(
        report_id=report,
        store=store_name,
        campaign=campaign_id,
        amount_cny=amount,
        source=source_name,
        currency=currency_name,
        source_balance_cny=source_balance,
        source_balance_evidence_at=source_evidence_at,
        policy_cap_cny=policy_cap,
        available_advertising_reserve_cny=advertising_reserve,
        approved_by=approver,
        approved_at=approval_time,
        expires_at=expiry,
        fingerprint=_fingerprint(payload),
        funding_basis=basis,
        cash_amount_cny=cash if daily else None,
        cashback_amount_cny=cashback,
        cashback_percent=percent,
        cashback_available_cny=cashback_available,
        cashback_expires_at=cashback_expiry,
    )


def validate_advertising_deposit_plan(
    plan: AdvertisingDepositPlan | Mapping[str, Any],
) -> dict[str, Any]:
    payload = plan.as_dict() if isinstance(plan, AdvertisingDepositPlan) else dict(plan)
    required = {
        "schemaVersion",
        "planType",
        "actionType",
        "reportId",
        "store",
        "campaign",
        "amountCny",
        "source",
        "currency",
        "sourceBalanceCny",
        "sourceBalanceEvidenceAt",
        "policyCapCny",
        "availableAdvertisingReserveCny",
        "approvedBy",
        "approvedAt",
        "expiresAt",
        "withdrawalExecutionSupported",
        "fingerprint",
    }
    version = payload.get("schemaVersion")
    if version == 3:
        required.update({
            "fundingBasis", "cashAmountCny", "cashbackAmountCny",
            "cashbackPercent", "cashbackAvailableCny", "cashbackExpiresAt",
        })
    if set(payload) != required:
        raise FundingPlanError(
            f"Deposit plan fields differ from the contract: {sorted(set(payload) ^ required)}"
        )
    if (
        version not in (2, 3)
        or payload.get("planType") != "advertising_deposit"
        or payload.get("actionType") != "advertising_top_up"
    ):
        raise FundingPlanError("Deposit plan schema or action type is invalid")
    if payload.get("withdrawalExecutionSupported") is not False:
        raise FundingPlanError("Advertising deposit plan cannot execute withdrawals")
    rebuilt = build_advertising_deposit_plan(
        report_id=payload.get("reportId"),
        store=payload.get("store"),
        campaign=payload.get("campaign"),
        amount_cny=payload.get("amountCny"),
        source=payload.get("source"),
        currency=payload.get("currency"),
        source_balance_cny=payload.get("sourceBalanceCny"),
        source_balance_evidence_at=payload.get("sourceBalanceEvidenceAt"),
        policy_cap_cny=payload.get("policyCapCny"),
        available_advertising_reserve_cny=payload.get(
            "availableAdvertisingReserveCny"
        ),
        approved_by=payload.get("approvedBy"),
        approved_at=payload.get("approvedAt"),
        expires_at=payload.get("expiresAt"),
        funding_basis=payload.get("fundingBasis") if version == 3 else None,
        cashback_amount_cny=payload.get("cashbackAmountCny", 0),
        cashback_percent=payload.get("cashbackPercent"),
        cashback_available_cny=payload.get("cashbackAvailableCny"),
        cashback_expires_at=payload.get("cashbackExpiresAt"),
    ).as_dict()
    if payload != rebuilt:
        if payload.get("fingerprint") != rebuilt.get("fingerprint"):
            raise FundingPlanError("Deposit plan fingerprint does not match its content")
        raise FundingPlanError("Deposit plan content is not canonical")
    return payload


def preflight_deposit_execution(
    plan: AdvertisingDepositPlan | Mapping[str, Any],
    *,
    current_status: str,
    readback_complete: bool = False,
    now: datetime | str | None = None,
) -> DepositExecutionPreflight:
    payload = validate_advertising_deposit_plan(plan)
    status = str(current_status or "").strip().lower()
    fingerprint = str(payload["fingerprint"])
    current = _aware_datetime(now or datetime.now(timezone.utc), "now")
    approved_at = _aware_datetime(payload["approvedAt"], "approvedAt")
    expires_at = _aware_datetime(payload["expiresAt"], "expiresAt")
    if readback_complete:
        return DepositExecutionPreflight(
            False,
            "deposit already has completed readback",
            False,
            False,
            fingerprint,
        )
    if status in {"unknown", "executing", "executed", "succeeded", "readback_pending"}:
        return DepositExecutionPreflight(
            False,
            "deposit outcome requires balance readback before any new attempt",
            True,
            False,
            fingerprint,
        )
    if status == "approved":
        if approved_at > current:
            return DepositExecutionPreflight(
                False,
                "approval time is in the future",
                False,
                False,
                fingerprint,
            )
        if current >= expires_at:
            return DepositExecutionPreflight(
                False,
                "advertising deposit approval has expired",
                False,
                False,
                fingerprint,
            )
        return DepositExecutionPreflight(
            True,
            "exact approved advertising deposit may execute once",
            True,
            False,
            fingerprint,
        )
    if status not in _KNOWN_DEPOSIT_STATUSES:
        return DepositExecutionPreflight(
            False,
            "unrecognized deposit status; automatic retry is forbidden",
            True,
            False,
            fingerprint,
        )
    return DepositExecutionPreflight(
        False,
        f"deposit status {status!r} requires a new explicit approval",
        False,
        False,
        fingerprint,
    )


def validate_deposit_plan(
    plan: AdvertisingDepositPlan | Mapping[str, Any],
) -> dict[str, Any]:
    return validate_advertising_deposit_plan(plan)


def deposit_execution_preflight(
    plan: AdvertisingDepositPlan | Mapping[str, Any],
    *,
    current_status: str,
    readback_complete: bool = False,
    now: datetime | str | None = None,
) -> DepositExecutionPreflight:
    return preflight_deposit_execution(
        plan,
        current_status=current_status,
        readback_complete=readback_complete,
        now=now,
    )


def _funding_payload(
    *,
    report_id: str,
    store: str,
    balance_evidence_at: datetime,
    formal_activation_at: datetime,
    historical_bootstrap: bool,
    platform_available_balance_cny: Decimal,
    unallocated_advertising_accrual_cny: Decimal,
    excluded_historical_advertising_accrual_cny: Decimal,
    unread_advertising_deposits_cny: Decimal,
    other_locked_funds_cny: Decimal,
    suggested_withdrawal_cny: Decimal,
    eligible_accrual_ids: tuple[str, ...],
    excluded_accrual_ids: tuple[str, ...],
    locked_deposit_action_ids: tuple[str, ...],
) -> dict[str, Any]:
    total_locked = (
        unallocated_advertising_accrual_cny
        + unread_advertising_deposits_cny
        + other_locked_funds_cny
    ).quantize(_CENT)
    return {
        "schemaVersion": 1,
        "planType": "store_funding",
        "reportId": report_id,
        "store": store,
        "balanceEvidenceAt": balance_evidence_at.isoformat(timespec="seconds"),
        "formalActivationAt": formal_activation_at.isoformat(timespec="seconds"),
        "historicalBootstrap": historical_bootstrap,
        "components": {
            "platformAvailableBalanceCny": _money_text(platform_available_balance_cny),
            "postActivationUnallocatedAdvertisingAccrualCny": _money_text(
                unallocated_advertising_accrual_cny
            ),
            "historicalAdvertisingAccrualExcludedCny": _money_text(
                excluded_historical_advertising_accrual_cny
            ),
            "approvedOrExecutingUnreadAdvertisingDepositsCny": _money_text(
                unread_advertising_deposits_cny
            ),
            "otherLockedFundsCny": _money_text(other_locked_funds_cny),
            "totalLockedFundsCny": _money_text(total_locked),
        },
        "suggestedWithdrawalCny": _money_text(suggested_withdrawal_cny),
        "evidence": {
            "eligibleAccrualIds": list(eligible_accrual_ids),
            "excludedAccrualIds": list(excluded_accrual_ids),
            "lockedDepositActionIds": list(locked_deposit_action_ids),
        },
        "withdrawalExecutionSupported": False,
    }


def _deposit_payload(
    *,
    report_id: str | None,
    store: str,
    campaign: int,
    amount_cny: Decimal,
    source: str,
    currency: str,
    source_balance_cny: Decimal,
    source_balance_evidence_at: datetime,
    policy_cap_cny: Decimal,
    available_advertising_reserve_cny: Decimal,
    approved_by: str,
    approved_at: datetime,
    expires_at: datetime,
    funding_basis: Mapping[str, Any] | None = None,
    cash_amount_cny: Decimal | None = None,
    cashback_amount_cny: Decimal = Decimal("0"),
    cashback_percent: int | None = None,
    cashback_available_cny: Decimal | None = None,
    cashback_expires_at: datetime | None = None,
) -> dict[str, Any]:
    payload = {
        "schemaVersion": 3 if funding_basis is not None else 2,
        "planType": "advertising_deposit",
        "actionType": "advertising_top_up",
        "reportId": report_id,
        "store": store,
        "campaign": campaign,
        "amountCny": _money_text(amount_cny),
        "source": source,
        "currency": currency,
        "sourceBalanceCny": _money_text(source_balance_cny),
        "sourceBalanceEvidenceAt": source_balance_evidence_at.isoformat(
            timespec="seconds"
        ),
        "policyCapCny": _money_text(policy_cap_cny),
        "availableAdvertisingReserveCny": _money_text(
            available_advertising_reserve_cny
        ),
        "approvedBy": approved_by,
        "approvedAt": approved_at.isoformat(timespec="seconds"),
        "expiresAt": expires_at.isoformat(timespec="seconds"),
        "withdrawalExecutionSupported": False,
    }
    if funding_basis is not None:
        payload.update({
            "fundingBasis": json.loads(json.dumps(funding_basis, allow_nan=False)),
            "cashAmountCny": _money_text(cash_amount_cny if cash_amount_cny is not None else amount_cny),
            "cashbackAmountCny": _money_text(cashback_amount_cny),
            "cashbackPercent": cashback_percent,
            "cashbackAvailableCny": (
                _money_text(cashback_available_cny) if cashback_available_cny is not None else None
            ),
            "cashbackExpiresAt": cashback_expires_at.isoformat() if cashback_expires_at else None,
        })
    return payload


def _whole_money(value: Any, label: str, *, positive: bool = False) -> Decimal:
    amount = _decimal(value, label)
    if amount < 0 or (positive and amount == 0):
        raise FundingPlanError(f"{label} must be {'positive' if positive else 'non-negative'}")
    if amount != amount.to_integral_value():
        raise FundingPlanError(f"{label} must be whole CNY")
    return amount.quantize(_CENT)


def _exact_non_negative_money(value: Any, label: str) -> Decimal:
    amount = _non_negative_money(value, label)
    if amount != _decimal(value, label):
        raise FundingPlanError(f"{label} must not contain fractional cents")
    return amount


def _cashback_percentage(value: Any) -> int:
    percent = _decimal(value, "cashback_percent")
    if percent != percent.to_integral_value() or not 0 < percent <= 100:
        raise FundingPlanError("cashback_percent must be an integer from 1 to 100")
    return int(percent)


def _validated_funding_basis(value: Any, store: str, currency: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise FundingPlanError("funding_basis must contain the complete daily basis object")
    try:
        basis = json.loads(json.dumps(dict(value), allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise FundingPlanError("funding_basis must contain finite JSON evidence") from exc
    if (
        type(basis.get("schemaVersion")) is not int
        or basis.get("schemaVersion") != 1
        or basis.get("basisType") != "daily_confirmed_advertising"
    ):
        raise FundingPlanError("funding_basis schema or type is invalid")
    if basis.get("store") != store:
        raise FundingPlanError("funding_basis store differs from the deposit store")
    if basis.get("currency", currency) != currency:
        raise FundingPlanError("funding_basis currency differs from the deposit currency")
    if not re.fullmatch(r"[0-9a-f]{64}", str(basis.get("fingerprint") or "")):
        raise FundingPlanError("funding_basis fingerprint must be a SHA-256 digest")
    through = basis.get("dataThrough")
    try:
        if not isinstance(through, str) or date.fromisoformat(through).isoformat() != through:
            raise ValueError("non-canonical date")
    except ValueError as exc:
        raise FundingPlanError("funding_basis dataThrough must be YYYY-MM-DD") from exc
    _exact_non_negative_money(basis.get("availableCashCny"), "funding_basis availableCashCny")
    return basis


def select_cashback_split(
    total_amount_cny: Any,
    cashbacks: Iterable[Mapping[str, Any]],
    *,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Recommend a bounded split from one valid, earliest-expiring WB bucket.

    WB accepts total ``sum`` plus integer ``cashback_sum`` and the selected
    bucket's ``percent``. No bucket balances are combined. The result is only
    a recommendation: it grants no approval and never retries a failed promo
    request as cash. A caller requiring promo must reject NO_USABLE_CASHBACK.
    """
    total = _whole_money(total_amount_cny, "total_amount_cny", positive=True)
    current = _aware_datetime(now or datetime.now(timezone.utc), "now")
    if isinstance(cashbacks, (str, bytes, Mapping)) or cashbacks is None:
        raise FundingPlanError("cashbacks must be an array of current balance buckets")
    try:
        buckets = iter(cashbacks)
    except TypeError as exc:
        raise FundingPlanError("cashbacks must be an array of current balance buckets") from exc
    candidates = []
    for bucket in buckets:
        if not isinstance(bucket, Mapping):
            continue
        try:
            available = _non_negative_money(bucket.get("sum"), "cashback bucket sum")
            if available != _decimal(bucket.get("sum"), "cashback bucket sum"):
                continue
            percent = _cashback_percentage(bucket.get("percent"))
            expires = _aware_datetime(bucket.get("expiration_date"), "cashback bucket expiration_date")
        except FundingPlanError:
            continue
        if expires <= current:
            continue
        promo = min(available, total * percent / 100).to_integral_value(rounding=ROUND_FLOOR)
        if promo <= 0:
            continue
        candidates.append((expires, percent, available, promo))
    selected = min(candidates, key=lambda item: (item[0], -item[3], item[1], item[2])) if candidates else None
    promo = selected[3] if selected else Decimal("0")
    return {
        "amountCny": _money_text(total),
        "cashAmountCny": _money_text(total - promo),
        "cashbackAmountCny": _money_text(promo),
        "cashbackPercent": selected[1] if selected else None,
        "cashbackAvailableCny": _money_text(selected[2]) if selected else None,
        "cashbackExpiresAt": selected[0].isoformat() if selected else None,
        "selectionStatus": "SELECTED" if selected else "NO_USABLE_CASHBACK",
    }


def _advertising_accrual(
    value: AdvertisingAccrual | Mapping[str, Any],
) -> AdvertisingAccrual:
    if isinstance(value, AdvertisingAccrual):
        raw = {
            "accrual_id": value.accrual_id,
            "amount_cny": value.amount_cny,
            "allocated_cny": value.allocated_cny,
            "accrued_at": value.accrued_at,
            "historical_bootstrap": value.historical_bootstrap,
        }
    elif isinstance(value, Mapping):
        raw = value
    else:
        raise FundingPlanError("advertising_accruals must contain objects")
    historical = raw.get("historical_bootstrap", raw.get("historicalBootstrap", False))
    if not isinstance(historical, bool):
        raise FundingPlanError("Advertising accrual historical flag must be boolean")
    return AdvertisingAccrual(
        accrual_id=_required_text(
            raw.get("accrual_id", raw.get("accrualId")), "accrual_id"
        ),
        amount_cny=_non_negative_money(
            raw.get("amount_cny", raw.get("amountCny")), "accrual amount"
        ),
        allocated_cny=_non_negative_money(
            raw.get("allocated_cny", raw.get("allocatedCny", 0)),
            "allocated accrual amount",
        ),
        accrued_at=_aware_datetime(
            raw.get("accrued_at", raw.get("accruedAt")), "accrued_at"
        ),
        historical_bootstrap=historical,
    )


def _deposit_commitment(
    value: DepositCommitment | Mapping[str, Any],
) -> DepositCommitment:
    if isinstance(value, DepositCommitment):
        raw = {
            "action_id": value.action_id,
            "amount_cny": value.amount_cny,
            "status": value.status,
            "readback_at": value.readback_at,
        }
    elif isinstance(value, Mapping):
        raw = value
    else:
        raise FundingPlanError("deposit_commitments must contain objects")
    status = _required_text(raw.get("status"), "deposit status").lower()
    if status not in _KNOWN_DEPOSIT_STATUSES:
        raise FundingPlanError(f"Unknown deposit commitment status: {status!r}")
    readback_value = raw.get("readback_at", raw.get("readbackAt"))
    readback = (
        _aware_datetime(readback_value, "readback_at")
        if readback_value is not None
        else None
    )
    return DepositCommitment(
        action_id=_required_text(
            raw.get("action_id", raw.get("actionId")), "action_id"
        ),
        amount_cny=_positive_money(
            raw.get("amount_cny", raw.get("amountCny")), "deposit amount"
        ),
        status=status,
        readback_at=readback,
    )


def _activation_datetime(value: date | datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise FundingPlanError("formal_activation_at is required")
        try:
            if len(raw) == 10:
                parsed = datetime.combine(date.fromisoformat(raw), time.min)
            else:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FundingPlanError("formal_activation_at is invalid") from exc
    else:
        raise FundingPlanError("formal_activation_at is invalid")
    return parsed.replace(tzinfo=MOSCOW) if parsed.tzinfo is None else parsed.astimezone(MOSCOW)


def _aware_datetime(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise FundingPlanError(f"{label} is required")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FundingPlanError(f"{label} must be an ISO timestamp") from exc
    else:
        raise FundingPlanError(f"{label} must be an ISO timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FundingPlanError(f"{label} must include a timezone offset")
    return parsed


def _required_text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise FundingPlanError(f"{label} is required")
    return result


def _non_negative_money(value: Any, label: str) -> Decimal:
    result = _decimal(value, label)
    if result < 0:
        raise FundingPlanError(f"{label} cannot be negative")
    return result.quantize(_CENT, rounding=ROUND_HALF_UP)


def _positive_money(value: Any, label: str) -> Decimal:
    result = _non_negative_money(value, label)
    if result <= 0:
        raise FundingPlanError(f"{label} must be positive")
    return result


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise FundingPlanError(f"{label} must be a decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FundingPlanError(f"{label} must be a decimal") from exc
    if not result.is_finite():
        raise FundingPlanError(f"{label} must be finite")
    return result


def _money_text(value: Decimal) -> str:
    return format(value.quantize(_CENT), "f")


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "AdvertisingAccrual",
    "AdvertisingDepositPlan",
    "DepositCommitment",
    "DepositExecutionPreflight",
    "FundingPlanError",
    "StoreFundingPlan",
    "build_advertising_deposit_plan",
    "build_funding_plan",
    "deposit_execution_preflight",
    "preflight_deposit_execution",
    "select_cashback_split",
    "validate_advertising_deposit_plan",
    "validate_deposit_plan",
    "validate_funding_plan",
]
