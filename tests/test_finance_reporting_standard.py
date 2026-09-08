from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wb_manager.finance_reporting_standard import (
    PeriodicReportingStandardError,
    load_active_wb_periodic_reporting_standard,
)


@pytest.fixture
def standard_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Explicit synthetic contract: never read a merchant's private settings."""
    monkeypatch.delenv("WB_FINANCE_CASH_EVENTS", raising=False)
    config_dir = tmp_path / "config" / "finance"
    config_dir.mkdir(parents=True)
    (config_dir / "stores.json").write_text("{}", encoding="utf-8")
    (config_dir / "historical_cash_events.example.json").write_text(
        json.dumps({
            "schemaVersion": 1, "status": "active", "currency": "CNY",
            "governance": {"appendOnly": True, "storeSpecific": True, "neverInferFromBalanceDelta": True},
            "events": [],
        }), encoding="utf-8",
    )
    raw = {
        "schemaVersion": 3, "standardId": "synthetic-periodic-standard-v1",
        "status": "active", "effectiveFrom": "2025-02-01T00:00:00+03:00",
        "canonicalWorkbook": "reports/示例财务台账.xlsx",
        "runningAccounts": {
            "enabled": True, "templateVersion": "synthetic-owner-dashboard-v1",
            "missingCashHistory": "unknown, never implicit zero", "immutableHistory": True,
        },
        "storeScope": {
            "registry": "config/finance/stores.json", "separateBeforeConsolidating": True,
            "fundingAndWithdrawalRemainStoreSpecific": True,
        },
        "historicalCashEventsRegistry": "config/finance/historical_cash_events.example.json",
        "historicalCashEventsLocalOverride": "config/finance/historical_cash_events.local.json",
        "periods": {
            "weekly": {"publishOn": "Monday"},
            "monthly": {"publishOn": "first day of each month"},
            "firstMonthlyBootstrap": {
                "scheduledFor": "2025-02-01", "periodEndInclusive": "2025-01-31",
                "perStoreStart": "stores.openedAt",
                "requiredViews": ["monthly activity", "opening-to-cutoff cumulative"],
            },
        },
        "publication": {
            "publishOnScheduleEvenWithOpenItems": True, "openItemsMustBeVisible": True,
            "settledRowsRemainUsableForAllocation": True, "unsettledRowsExcludedFromCashAllocation": True,
        },
        "orderTimeVersioning": {
            "selectionTimestamp": "orderCreatedAt",
            "lockedFields": [
                "purchaseCostCny", "salesAmountGCny", "costVersionId",
                "sellingPricePolicyVersionId", "financialReportingPolicyVersionId",
                "managedOperationsRate", "advertisingAndReturnPoolRate",
            ],
        },
        "fundingGovernance": {
            "promotionalCreditsIgnored": True, "noMoneyMovementWithoutExplicitAuthorization": True,
            "advertisingFundingSource": "balance", "platformBalanceField": "net", "platformRequestType": 1,
            "platformDeductionMode": "future_sales_settlement_netting",
            "separateWithdrawalRequired": False, "manualCashTopUpRequired": False,
            "campaignAllocationRequiresExactApprovalAndReadback": True,
            "platformDeductionIsReconciliationOnly": True, "preserveHistoricalWithdrawalAdvertisingDebt": True,
            "noNewWithdrawalLinkedAdvertisingDebtAfterCutover": True,
            "legacyWithdrawalAdvertisingDebtCutover": "2025-01-31",
        },
    }
    return raw, config_dir / "standard.json"


def _write_standard(fixture, raw=None) -> Path:
    default_raw, path = fixture
    path.write_text(json.dumps(default_raw if raw is None else raw, ensure_ascii=False), encoding="utf-8")
    return path


def test_explicit_periodic_reporting_standard_is_safe_and_resolvable(tmp_path: Path, standard_fixture):
    standard = load_active_wb_periodic_reporting_standard(_write_standard(standard_fixture), repo_root=tmp_path)

    assert standard.standard_id == "synthetic-periodic-standard-v1"
    running = standard.raw["runningAccounts"]
    assert running["enabled"] is True
    assert running["templateVersion"] == "synthetic-owner-dashboard-v1"
    assert running["missingCashHistory"] == "unknown, never implicit zero"
    assert running["immutableHistory"] is True
    assert standard.canonical_workbook.is_absolute()
    assert standard.canonical_workbook.suffix == ".xlsx"
    assert standard.weekly_publish_day == "Monday"
    assert standard.monthly_publish_day == "first day of each month"
    assert standard.first_monthly_scheduled_for == "2025-02-01"
    assert standard.first_monthly_period_end == "2025-01-31"


def test_periodic_standard_requires_two_first_monthly_views(tmp_path: Path, standard_fixture):
    changed = deepcopy(standard_fixture[0])
    changed["periods"]["firstMonthlyBootstrap"]["requiredViews"] = ["monthly only"]
    path = _write_standard(standard_fixture, changed)

    with pytest.raises(PeriodicReportingStandardError, match="both monthly and cumulative"):
        load_active_wb_periodic_reporting_standard(path, repo_root=tmp_path)


def test_periodic_standard_requires_explicit_funding_authorization(tmp_path: Path, standard_fixture):
    changed = deepcopy(standard_fixture[0])
    changed["fundingGovernance"]["noMoneyMovementWithoutExplicitAuthorization"] = False
    path = _write_standard(standard_fixture, changed)

    with pytest.raises(PeriodicReportingStandardError, match="explicit authorization"):
        load_active_wb_periodic_reporting_standard(path, repo_root=tmp_path)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("advertisingFundingSource", "account"),
        ("platformRequestType", 0),
        ("separateWithdrawalRequired", True),
        ("preserveHistoricalWithdrawalAdvertisingDebt", False),
    ],
)
def test_periodic_standard_enforces_settlement_balance_funding(
    tmp_path: Path, standard_fixture, key: str, value
):
    changed = deepcopy(standard_fixture[0])
    changed["fundingGovernance"][key] = value
    path = _write_standard(standard_fixture, changed)

    with pytest.raises(PeriodicReportingStandardError):
        load_active_wb_periodic_reporting_standard(path, repo_root=tmp_path)


def test_periodic_standard_uses_tracked_cash_example_when_local_override_is_missing(
    tmp_path: Path,
    standard_fixture,
):
    raw, path = standard_fixture
    _write_standard(standard_fixture)
    example = path.parent / "historical_cash_events.example.json"

    standard = load_active_wb_periodic_reporting_standard(path, repo_root=tmp_path)

    assert standard.canonical_workbook == (
        tmp_path / raw["canonicalWorkbook"]
    ).resolve()
    assert standard.historical_cash_events_registry == example.resolve()


PROMO_CANONICAL_KEY = "promotionalCreditsExcludedFromCashAccounting"
PROMO_LEGACY_KEY = "promotionalCreditsIgnored"


def _standard_with_promo_contract(standard_fixture, flags, mode="prefer_eligible"):
    changed = deepcopy(standard_fixture[0])
    governance = changed["fundingGovernance"]
    governance.pop(PROMO_CANONICAL_KEY, None)
    governance.pop(PROMO_LEGACY_KEY, None)
    governance.update(flags)
    if mode is not None:
        changed["runningAccounts"]["funding"] = {"cashbackMode": mode}
    return _write_standard(standard_fixture, changed)


def _runtime_plan_from_standard(standard):
    """Exercise the loaded rule, selector and immutable builder, without APIs."""
    from wb_manager.finance_funding_bridge import _promo_policy
    from wb_manager.finance_funds import build_advertising_deposit_plan, select_cashback_split

    enabled = _promo_policy(SimpleNamespace(periodic_standard=standard))
    buckets = [{"sum": 37, "percent": 100, "expiration_date": "2026-10-01T00:00:00Z"}]
    split = select_cashback_split(
        100, buckets if enabled else [], now="2026-09-08T01:00:00Z",
    )
    basis = {
        "schemaVersion": 1,
        "basisType": "daily_confirmed_advertising",
        "basisId": "daily-confirmed:synthetic-store:2026-09-07:" + "a" * 64,
        "fingerprint": "a" * 64,
        "store": "synthetic-store",
        "currency": "CNY",
        "dataThrough": "2026-09-07",
        "stateThrough": "2026-09-07",
        "cutoffView": "latest_complete_day",
        "availableCashCny": "100.00",
        "grossConfirmedAccrualCny": "150.00",
        "sharedReturnChargeCny": "10.00",
        "confirmedCashAllocatedCny": "40.00",
        "lockedCashCny": "0.00",
        "confirmedCashbackAllocatedCny": "0.00",
        "lockedCashbackCny": "0.00",
        "cashSpendCny": "30.00",
        "unspentBookBudgetCny": "110.00",
        "fundingAllowed": True,
        "blockers": [],
        "warnings": [],
        "completeness": {"sourceCoverageComplete": True},
        "evidence": {
            "events": [{"eventId": "synthetic-sale", "cashCny": "150.00"}],
            "fundingActions": [], "sourceCoverage": [], "excludedEvents": [],
            "policy": {"policyVersion": "synthetic-policy"},
            "activationSource": "synthetic-approved-opening",
        },
        "limitations": [],
    }
    plan = build_advertising_deposit_plan(
        report_id=None, store="synthetic-store", campaign=42, amount_cny=100,
        source="balance", currency="CNY", source_balance_cny="100.00",
        source_balance_evidence_at="2026-09-08T01:00:00Z",
        policy_cap_cny="100.00", available_advertising_reserve_cny="100.00",
        approved_by="synthetic-owner", approved_at="2026-09-08T01:00:00Z",
        expires_at="2026-09-08T01:30:00Z", funding_basis=basis,
        cashback_amount_cny=split["cashbackAmountCny"],
        cashback_percent=split["cashbackPercent"],
        cashback_available_cny=split["cashbackAvailableCny"],
        cashback_expires_at=split["cashbackExpiresAt"],
    )
    return enabled, plan.as_dict()


@pytest.mark.parametrize("flags", [
    {PROMO_CANONICAL_KEY: True},
    {PROMO_LEGACY_KEY: True},
    {PROMO_CANONICAL_KEY: True, PROMO_LEGACY_KEY: True},
], ids=["canonical", "legacy", "both"])
def test_cash_accounting_exclusion_allows_explicit_runtime_promo_funding(
    tmp_path: Path, standard_fixture, flags,
):
    standard = load_active_wb_periodic_reporting_standard(
        _standard_with_promo_contract(standard_fixture, flags), repo_root=tmp_path,
    )
    enabled, payload = _runtime_plan_from_standard(standard)
    assert enabled is True
    assert payload["schemaVersion"] == 3
    assert payload["amountCny"] == "100.00"
    assert payload["cashAmountCny"] == "63.00"
    assert payload["cashbackAmountCny"] == "37.00"
    assert payload["fundingBasis"]["availableCashCny"] == "100.00"
    assert payload["cashbackPercent"] == 100
    assert payload["withdrawalExecutionSupported"] is False


@pytest.mark.parametrize("key", [PROMO_CANONICAL_KEY, PROMO_LEGACY_KEY])
@pytest.mark.parametrize("invalid", [False, "true", 1, None], ids=["false", "string", "int", "null"])
@pytest.mark.parametrize("both", [False, True], ids=["single", "dual-conflict"])
def test_cash_accounting_exclusion_rejects_invalid_or_conflicting_flags(
    tmp_path: Path, standard_fixture, key, invalid, both,
):
    flags = {PROMO_CANONICAL_KEY: True, PROMO_LEGACY_KEY: True} if both else {}
    flags[key] = invalid
    path = _standard_with_promo_contract(standard_fixture, flags)
    with pytest.raises(PeriodicReportingStandardError):
        load_active_wb_periodic_reporting_standard(path, repo_root=tmp_path)


def test_cash_accounting_exclusion_requires_canonical_or_legacy_flag(tmp_path: Path, standard_fixture):
    path = _standard_with_promo_contract(standard_fixture, {})
    with pytest.raises(PeriodicReportingStandardError):
        load_active_wb_periodic_reporting_standard(path, repo_root=tmp_path)


@pytest.mark.parametrize("mode", [None, "disabled", "cash_only"])
@pytest.mark.parametrize("flags", [
    {PROMO_CANONICAL_KEY: True},
    {PROMO_LEGACY_KEY: True},
    {PROMO_CANONICAL_KEY: True, PROMO_LEGACY_KEY: True},
], ids=["canonical", "legacy", "both"])
def test_cash_accounting_flag_does_not_enable_runtime_promotion(
    tmp_path: Path, standard_fixture, flags, mode,
):
    standard = load_active_wb_periodic_reporting_standard(
        _standard_with_promo_contract(standard_fixture, flags, mode), repo_root=tmp_path,
    )
    enabled, payload = _runtime_plan_from_standard(standard)
    assert enabled is False
    assert payload["schemaVersion"] == 3
    assert payload["amountCny"] == payload["cashAmountCny"] == "100.00"
    assert payload["cashbackAmountCny"] == "0.00"
    assert payload["cashbackPercent"] is None
