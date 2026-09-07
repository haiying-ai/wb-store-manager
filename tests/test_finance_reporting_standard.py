from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

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
