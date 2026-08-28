from datetime import date

import pytest

from wb_manager.finance_periods import (
    FinancePeriodError,
    parse_report_period,
    previous_month,
    previous_week,
    report_maturity_blocker,
)


REGISTRY = {
    "officialHistoryStart": "2024-01-29",
    "stores": [
        {"alias": "a", "enabled": True, "openedAt": "2026-07-04"},
        {"alias": "b", "enabled": True, "openedAt": "2026-08-01"},
    ],
}


def test_previous_natural_periods():
    assert previous_week(today=date(2026, 8, 28)).start == date(2026, 8, 17)
    assert previous_week(today=date(2026, 8, 28)).end == date(2026, 8, 23)
    assert previous_month(today=date(2026, 8, 28)).start == date(2026, 7, 1)
    assert previous_month(today=date(2026, 8, 28)).end == date(2026, 7, 31)


def test_bootstrap_and_first_monthly_are_cumulative():
    bootstrap = parse_report_period(
        kind="bootstrap", value="auto", registry=REGISTRY, today=date(2026, 8, 28)
    )
    first = parse_report_period(
        kind="monthly",
        value="auto",
        registry=REGISTRY,
        today=date(2026, 8, 28),
        first_monthly=True,
    )
    assert (bootstrap.start, bootstrap.end, bootstrap.cumulative) == (
        date(2026, 7, 4),
        date(2026, 8, 27),
        True,
    )
    assert (first.start, first.end, first.cumulative) == (
        date(2026, 7, 4),
        date(2026, 7, 31),
        True,
    )


def test_week_month_and_explicit_period_validation():
    week = parse_report_period(
        kind="weekly", value="2026-W34", registry=REGISTRY, today=date(2026, 8, 28)
    )
    month = parse_report_period(
        kind="monthly", value="2026-07", registry=REGISTRY, today=date(2026, 8, 28)
    )
    assert (week.start, week.end) == (date(2026, 8, 17), date(2026, 8, 23))
    assert (month.start, month.end) == (date(2026, 7, 1), date(2026, 7, 31))
    with pytest.raises(FinancePeriodError):
        parse_report_period(
            kind="weekly",
            value="2026-08-18:2026-08-24",
            registry=REGISTRY,
            today=date(2026, 8, 28),
        )


def test_maturity_window_is_fail_closed():
    period = parse_report_period(
        kind="weekly", value="2026-W34", registry=REGISTRY, today=date(2026, 8, 28)
    )
    assert report_maturity_blocker(period, today=date(2026, 8, 25)) is not None
    assert report_maturity_blocker(period, today=date(2026, 8, 26)) is None
