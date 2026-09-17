from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_WB_PERIODIC_REPORTING_STANDARD = (
    ROOT / "config" / "finance" / "active_wb_periodic_reporting_standard.json"
)
ACCOUNT_RECEIPT_SOURCE_TYPES = frozenset({
    "wb_settlement_account_credit_history",
    "verified_settlement_account_credit_statement",
})


class PeriodicReportingStandardError(RuntimeError):
    """Raised when the canonical periodic-reporting contract is unsafe."""


@dataclass(frozen=True)
class PeriodicReportingStandard:
    standard_id: str
    canonical_workbook: Path
    store_registry: Path
    historical_cash_events_registry: Path
    weekly_publish_day: str
    monthly_publish_day: str
    first_monthly_scheduled_for: str
    first_monthly_period_end: str
    raw: dict[str, Any]


def load_active_wb_periodic_reporting_standard(
    path: Path = ACTIVE_WB_PERIODIC_REPORTING_STANDARD,
    *,
    repo_root: Path = ROOT,
) -> PeriodicReportingStandard:
    raw = _read_object(path, "periodic reporting standard")
    if raw.get("status") != "active":
        raise PeriodicReportingStandardError("Periodic reporting standard is not active")

    standard_id = _text(raw, "standardId", "periodic reporting standard")
    configured_canonical = Path(
        _text(raw, "canonicalWorkbook", "periodic reporting standard")
    ).expanduser()
    if configured_canonical.suffix.lower() != ".xlsx":
        raise PeriodicReportingStandardError(
            "canonicalWorkbook must be an .xlsx path"
        )
    canonical = (
        configured_canonical.resolve()
        if configured_canonical.is_absolute()
        else (repo_root / configured_canonical).resolve()
    )

    store_scope = _mapping(raw.get("storeScope"), "storeScope")
    store_registry = _repo_path(
        _text(store_scope, "registry", "storeScope"), repo_root, "storeScope.registry"
    )
    if store_scope.get("separateBeforeConsolidating") is not True:
        raise PeriodicReportingStandardError(
            "Every enabled store must be calculated separately before consolidation"
        )
    if store_scope.get("fundingAndWithdrawalRemainStoreSpecific") is not True:
        raise PeriodicReportingStandardError(
            "Funding and withdrawal must remain store-specific"
        )

    default_history_registry = _repo_path(
        _text(raw, "historicalCashEventsRegistry", "periodic reporting standard"),
        repo_root,
        "historicalCashEventsRegistry",
    )
    environment_history_override = os.getenv("WB_FINANCE_CASH_EVENTS", "").strip()
    configured_history_override = str(
        raw.get("historicalCashEventsLocalOverride") or ""
    ).strip()
    if environment_history_override:
        history_registry = _repo_path(
            environment_history_override,
            repo_root,
            "WB_FINANCE_CASH_EVENTS",
        )
    elif configured_history_override:
        optional_override = _resolve_repo_path(configured_history_override, repo_root)
        history_registry = (
            optional_override.resolve()
            if optional_override.exists()
            else default_history_registry
        )
    else:
        history_registry = default_history_registry
    validate_historical_cash_events(
        _read_object(history_registry, "historical cash events")
    )

    periods = _mapping(raw.get("periods"), "periods")
    weekly = _mapping(periods.get("weekly"), "periods.weekly")
    monthly = _mapping(periods.get("monthly"), "periods.monthly")
    bootstrap = _mapping(
        periods.get("firstMonthlyBootstrap"), "periods.firstMonthlyBootstrap"
    )
    if weekly.get("publishOn") != "Monday":
        raise PeriodicReportingStandardError("Weekly report must publish on Monday")
    if monthly.get("publishOn") != "first day of each month":
        raise PeriodicReportingStandardError(
            "Monthly report must publish on the first day of each month"
        )
    views = bootstrap.get("requiredViews")
    if not isinstance(views, list) or len(views) != 2 or any(not str(v).strip() for v in views):
        raise PeriodicReportingStandardError(
            "First monthly bootstrap must define both monthly and cumulative views"
        )
    if bootstrap.get("perStoreStart") != "stores.openedAt":
        raise PeriodicReportingStandardError(
            "First monthly bootstrap must start from each store's openedAt"
        )

    publication = _mapping(raw.get("publication"), "publication")
    required_publication_true = (
        "publishOnScheduleEvenWithOpenItems",
        "openItemsMustBeVisible",
        "settledRowsRemainUsableForAllocation",
        "unsettledRowsExcludedFromCashAllocation",
    )
    missing = [key for key in required_publication_true if publication.get(key) is not True]
    if missing:
        raise PeriodicReportingStandardError(
            "Periodic publication governance is incomplete: " + ", ".join(missing)
        )

    versioning = _mapping(raw.get("orderTimeVersioning"), "orderTimeVersioning")
    if versioning.get("selectionTimestamp") != "orderCreatedAt":
        raise PeriodicReportingStandardError(
            "Order-time versions must be selected by orderCreatedAt"
        )
    locked = {str(value) for value in versioning.get("lockedFields") or []}
    required_locked = {
        "purchaseCostCny",
        "salesAmountGCny",
        "costVersionId",
        "sellingPricePolicyVersionId",
        "financialReportingPolicyVersionId",
        "managedOperationsRate",
        "advertisingAndReturnPoolRate",
    }
    if not required_locked.issubset(locked):
        raise PeriodicReportingStandardError(
            "Order-time lockedFields is missing required price/cost/policy snapshots"
        )

    funding = _mapping(raw.get("fundingGovernance"), "fundingGovernance")
    # Accounting exclusion is separate from permission to use an eligible
    # promotional bucket in an explicitly approved funding plan. Retain the
    # old spelling as a compatibility alias, not as a promotional-funding ban.
    accounting_keys = (
        "promotionalCreditsExcludedFromCashAccounting", "promotionalCreditsIgnored",
    )
    supplied_accounting_keys = [key for key in accounting_keys if key in funding]
    if not supplied_accounting_keys or any(
        funding[key] is not True for key in supplied_accounting_keys
    ):
        raise PeriodicReportingStandardError(
            "Promotional credits must be excluded from seller-funded cash advertising "
            "costs and confirmed cash reserves: set "
            "fundingGovernance.promotionalCreditsExcludedFromCashAccounting=true "
            "(promotionalCreditsIgnored is a legacy accounting alias; if both are "
            "present both must be true). This does not disable promotional funding, "
            "which is controlled separately by runningAccounts.funding.cashbackMode."
        )
    if funding.get("noMoneyMovementWithoutExplicitAuthorization") is not True:
        raise PeriodicReportingStandardError(
            "Money movement must require explicit authorization"
        )
    expected_funding_values = {
        "advertisingFundingSource": "balance",
        "platformBalanceField": "net",
        "platformRequestType": 1,
        "platformDeductionMode": "future_sales_settlement_netting",
    }
    invalid_funding_values = [
        key for key, expected in expected_funding_values.items()
        if funding.get(key) != expected
    ]
    if invalid_funding_values:
        raise PeriodicReportingStandardError(
            "Advertising funding must use WB settlement balance/net/type=1: "
            + ", ".join(invalid_funding_values)
        )
    for key in ("separateWithdrawalRequired", "manualCashTopUpRequired"):
        if funding.get(key) is not False:
            raise PeriodicReportingStandardError(
                f"Advertising campaign allocation requires {key}=false"
            )
    required_funding_true = (
        "campaignAllocationRequiresExactApprovalAndReadback",
        "platformDeductionIsReconciliationOnly",
        "preserveHistoricalWithdrawalAdvertisingDebt",
        "noNewWithdrawalLinkedAdvertisingDebtAfterCutover",
    )
    missing_funding = [
        key for key in required_funding_true if funding.get(key) is not True
    ]
    if missing_funding:
        raise PeriodicReportingStandardError(
            "Settlement-balance advertising governance is incomplete: "
            + ", ".join(missing_funding)
        )
    debt_cutover = _instant(
        _text(
            funding,
            "legacyWithdrawalAdvertisingDebtCutover",
            "fundingGovernance",
        ),
        "legacyWithdrawalAdvertisingDebtCutover",
    )
    standard_effective = _instant(
        _text(raw, "effectiveFrom", "periodic reporting standard"),
        "effectiveFrom",
    )
    if debt_cutover > standard_effective:
        raise PeriodicReportingStandardError(
            "Legacy withdrawal advertising-debt cutover cannot be later than effectiveFrom"
        )

    return PeriodicReportingStandard(
        standard_id=standard_id,
        canonical_workbook=canonical,
        store_registry=store_registry,
        historical_cash_events_registry=history_registry,
        weekly_publish_day=str(weekly["publishOn"]),
        monthly_publish_day=str(monthly["publishOn"]),
        first_monthly_scheduled_for=_text(
            bootstrap, "scheduledFor", "periods.firstMonthlyBootstrap"
        ),
        first_monthly_period_end=_text(
            bootstrap, "periodEndInclusive", "periods.firstMonthlyBootstrap"
        ),
        raw=raw,
    )


def validate_historical_cash_events(raw: Mapping[str, Any]) -> None:
    if raw.get("status") != "active":
        raise PeriodicReportingStandardError("Historical cash-event registry is not active")
    governance = _mapping(raw.get("governance"), "historical cash-event governance")
    for key in ("appendOnly", "storeSpecific", "neverInferFromBalanceDelta"):
        if governance.get(key) is not True:
            raise PeriodicReportingStandardError(
                f"Historical cash-event governance requires {key}=true"
            )
    events = raw.get("events")
    if not isinstance(events, list):
        raise PeriodicReportingStandardError("Historical cash events must be a list")
    if str(raw.get("currency") or "").upper() != "CNY":
        raise PeriodicReportingStandardError(
            "Historical cash-event registry currency must be CNY"
        )
    seen: set[str] = set()
    for index, event in enumerate(events):
        item = _mapping(event, f"historical cash event {index}")
        event_id = _text(item, "eventId", f"historical cash event {index}")
        if event_id in seen:
            raise PeriodicReportingStandardError(f"Duplicate historical eventId: {event_id}")
        seen.add(event_id)
        event_type = item.get("eventType")
        if event_type not in {"withdrawal", "account_receipt"}:
            raise PeriodicReportingStandardError(
                f"Unsupported historical cash event type: {event_type}"
            )
        if not str(item.get("store") or "").strip():
            raise PeriodicReportingStandardError("Historical cash event requires store")
        try:
            amount = Decimal(str(item.get("amountCny")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PeriodicReportingStandardError(
                "Historical cash event amountCny must be numeric"
            ) from exc
        if not amount.is_finite() or amount <= 0:
            raise PeriodicReportingStandardError(
                "Historical cash event amountCny must be finite and positive"
            )
        if item.get("status") != "readback_confirmed":
            raise PeriodicReportingStandardError(
                "Historical cash event requires readback_confirmed status"
            )
        if event_type == "account_receipt":
            _instant(
                _text(item, "receivedAt", f"historical cash event {index}"),
                f"historical cash event {index}.receivedAt",
            )
            source_type = _text(item, "sourceType", f"historical cash event {index}")
            if source_type not in ACCOUNT_RECEIPT_SOURCE_TYPES:
                raise PeriodicReportingStandardError(
                    f"Unsupported account receipt sourceType: {source_type}"
                )
            _text(item, "evidenceReference", f"historical cash event {index}")

    receipt_coverage = raw.get("receiptCoverage", [])
    if not isinstance(receipt_coverage, list):
        raise PeriodicReportingStandardError(
            "Historical receiptCoverage must be a list"
        )
    seen_coverage: set[str] = set()
    for index, coverage in enumerate(receipt_coverage):
        item = _mapping(coverage, f"historical receipt coverage {index}")
        coverage_id = _text(
            item, "coverageId", f"historical receipt coverage {index}"
        )
        if coverage_id in seen_coverage:
            raise PeriodicReportingStandardError(
                f"Duplicate historical receipt coverageId: {coverage_id}"
            )
        seen_coverage.add(coverage_id)
        _text(item, "store", f"historical receipt coverage {index}")
        start = _instant(
            _text(item, "from", f"historical receipt coverage {index}"),
            f"historical receipt coverage {index}.from",
        )
        through = _instant(
            _text(item, "through", f"historical receipt coverage {index}"),
            f"historical receipt coverage {index}.through",
        )
        if through < start:
            raise PeriodicReportingStandardError(
                "Historical receipt coverage through precedes from"
            )
        if item.get("complete") is not True:
            raise PeriodicReportingStandardError(
                "Historical receipt coverage requires complete=true"
            )
        _text(item, "evidence", f"historical receipt coverage {index}")


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PeriodicReportingStandardError(f"Cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PeriodicReportingStandardError(f"{label.title()} must be a JSON object")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PeriodicReportingStandardError(f"{label} must be an object")
    return value


def _text(parent: Mapping[str, Any], key: str, label: str) -> str:
    value = str(parent.get(key) or "").strip()
    if not value:
        raise PeriodicReportingStandardError(f"{label} requires {key}")
    return value


def _instant(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PeriodicReportingStandardError(
            f"{label} must be an ISO date or timestamp"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _repo_path(value: str, repo_root: Path, label: str) -> Path:
    resolved = _resolve_repo_path(value, repo_root)
    if not resolved.exists():
        raise PeriodicReportingStandardError(f"{label} does not exist: {resolved}")
    return resolved.resolve()


def _resolve_repo_path(value: str, repo_root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (repo_root / path)
