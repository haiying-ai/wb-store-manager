from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from wb_manager.finance_reporting_standard import (
    ACTIVE_WB_PERIODIC_REPORTING_STANDARD,
    PeriodicReportingStandardError,
    load_active_wb_periodic_reporting_standard,
)


def test_active_periodic_reporting_standard_is_safe_and_resolvable():
    standard = load_active_wb_periodic_reporting_standard()

    assert standard.standard_id == "wb-periodic-finance-workbook-20260906-v3"
    assert standard.canonical_workbook.is_absolute()
    assert standard.canonical_workbook.suffix == ".xlsx"
    assert standard.weekly_publish_day == "Monday"
    assert standard.monthly_publish_day == "first day of each month"
    assert standard.first_monthly_scheduled_for == "2026-09-01"
    assert standard.first_monthly_period_end == "2026-08-31"


def test_periodic_standard_requires_two_first_monthly_views(tmp_path: Path):
    raw = json.loads(ACTIVE_WB_PERIODIC_REPORTING_STANDARD.read_text(encoding="utf-8"))
    changed = deepcopy(raw)
    changed["periods"]["firstMonthlyBootstrap"]["requiredViews"] = ["monthly only"]
    changed["canonicalWorkbook"] = str(
        Path(raw["canonicalWorkbook"]).resolve()
    )
    changed["storeScope"]["registry"] = str(
        (ACTIVE_WB_PERIODIC_REPORTING_STANDARD.parent / "stores.json").resolve()
    )
    changed["historicalCashEventsRegistry"] = str(
        (ACTIVE_WB_PERIODIC_REPORTING_STANDARD.parent / "historical_cash_events.example.json").resolve()
    )
    changed.pop("historicalCashEventsLocalOverride", None)
    path = tmp_path / "standard.json"
    path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(PeriodicReportingStandardError, match="both monthly and cumulative"):
        load_active_wb_periodic_reporting_standard(path, repo_root=tmp_path)


def test_periodic_standard_requires_explicit_funding_authorization(tmp_path: Path):
    raw = json.loads(ACTIVE_WB_PERIODIC_REPORTING_STANDARD.read_text(encoding="utf-8"))
    changed = deepcopy(raw)
    changed["fundingGovernance"]["noMoneyMovementWithoutExplicitAuthorization"] = False
    changed["canonicalWorkbook"] = str(Path(raw["canonicalWorkbook"]).resolve())
    changed["storeScope"]["registry"] = str(
        (ACTIVE_WB_PERIODIC_REPORTING_STANDARD.parent / "stores.json").resolve()
    )
    changed["historicalCashEventsRegistry"] = str(
        (ACTIVE_WB_PERIODIC_REPORTING_STANDARD.parent / "historical_cash_events.example.json").resolve()
    )
    changed.pop("historicalCashEventsLocalOverride", None)
    path = tmp_path / "standard.json"
    path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")

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
    tmp_path: Path, key: str, value
):
    raw = json.loads(ACTIVE_WB_PERIODIC_REPORTING_STANDARD.read_text(encoding="utf-8"))
    changed = deepcopy(raw)
    changed["fundingGovernance"][key] = value
    changed["canonicalWorkbook"] = str(Path(raw["canonicalWorkbook"]).resolve())
    changed["storeScope"]["registry"] = str(
        (ACTIVE_WB_PERIODIC_REPORTING_STANDARD.parent / "stores.json").resolve()
    )
    changed["historicalCashEventsRegistry"] = str(
        (ACTIVE_WB_PERIODIC_REPORTING_STANDARD.parent / "historical_cash_events.example.json").resolve()
    )
    changed.pop("historicalCashEventsLocalOverride", None)
    path = tmp_path / "standard.json"
    path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(PeriodicReportingStandardError):
        load_active_wb_periodic_reporting_standard(path, repo_root=tmp_path)


def test_periodic_standard_uses_tracked_cash_example_when_local_override_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("WB_FINANCE_CASH_EVENTS", raising=False)
    raw = json.loads(ACTIVE_WB_PERIODIC_REPORTING_STANDARD.read_text(encoding="utf-8"))
    config_dir = tmp_path / "config" / "finance"
    config_dir.mkdir(parents=True)
    (config_dir / "stores.json").write_text("{}", encoding="utf-8")
    example = config_dir / "historical_cash_events.example.json"
    example.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "status": "active",
                "currency": "CNY",
                "governance": {
                    "appendOnly": True,
                    "storeSpecific": True,
                    "neverInferFromBalanceDelta": True,
                },
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    path = config_dir / "standard.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    standard = load_active_wb_periodic_reporting_standard(path, repo_root=tmp_path)

    assert standard.canonical_workbook == (
        tmp_path / raw["canonicalWorkbook"]
    ).resolve()
    assert standard.historical_cash_events_registry == example.resolve()
