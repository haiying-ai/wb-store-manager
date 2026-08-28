from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo


MOSCOW = ZoneInfo("Europe/Moscow")
_ISO_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


class FinancePeriodError(ValueError):
    pass


@dataclass(frozen=True)
class FinancePeriod:
    kind: str
    start: date
    end: date
    cumulative: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "timezone": "Europe/Moscow",
            "cumulative": self.cumulative,
        }


def previous_week(*, today: date | None = None) -> FinancePeriod:
    current = today or datetime.now(MOSCOW).date()
    this_monday = current - timedelta(days=current.weekday())
    end = this_monday - timedelta(days=1)
    return FinancePeriod("weekly", end - timedelta(days=6), end)


def previous_month(*, today: date | None = None) -> FinancePeriod:
    current = today or datetime.now(MOSCOW).date()
    first_this_month = current.replace(day=1)
    end = first_this_month - timedelta(days=1)
    return FinancePeriod("monthly", end.replace(day=1), end)


def earliest_registry_date(registry: Mapping[str, Any]) -> date:
    history = date.fromisoformat(str(registry.get("officialHistoryStart") or "2024-01-29"))
    opened = []
    for store in registry.get("stores", []):
        if not isinstance(store, Mapping) or store.get("enabled", True) is False:
            continue
        value = store.get("openedAt")
        if value:
            opened.append(max(history, date.fromisoformat(str(value)[:10])))
    return min(opened) if opened else history


def parse_report_period(
    *,
    kind: str,
    value: str,
    registry: Mapping[str, Any],
    today: date | None = None,
    first_monthly: bool = False,
) -> FinancePeriod:
    if kind not in {"bootstrap", "weekly", "monthly"}:
        raise FinancePeriodError(f"Unsupported report kind: {kind}")
    raw = str(value or "").strip()
    current = today or datetime.now(MOSCOW).date()
    if raw.lower() == "auto":
        if kind == "bootstrap":
            return FinancePeriod(
                kind,
                earliest_registry_date(registry),
                current - timedelta(days=1),
                cumulative=True,
            )
        selected = previous_week(today=current) if kind == "weekly" else previous_month(today=current)
    elif kind == "weekly" and (match := _ISO_WEEK.fullmatch(raw)):
        year, week = (int(match.group(1)), int(match.group(2)))
        try:
            start = date.fromisocalendar(year, week, 1)
        except ValueError as exc:
            raise FinancePeriodError(f"Invalid ISO week: {raw}") from exc
        selected = FinancePeriod(kind, start, start + timedelta(days=6))
    elif kind == "monthly" and (match := _MONTH.fullmatch(raw)):
        year, month = (int(match.group(1)), int(match.group(2)))
        try:
            start = date(year, month, 1)
        except ValueError as exc:
            raise FinancePeriodError(f"Invalid month: {raw}") from exc
        next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
        selected = FinancePeriod(kind, start, next_month - timedelta(days=1))
    elif ":" in raw:
        left, right = raw.split(":", 1)
        try:
            selected = FinancePeriod(kind, date.fromisoformat(left), date.fromisoformat(right))
        except ValueError as exc:
            raise FinancePeriodError(f"Invalid explicit finance period: {raw}") from exc
    else:
        raise FinancePeriodError(
            f"Invalid {kind} period {raw!r}; use auto, date range, ISO week, or month"
        )

    if selected.end < selected.start:
        raise FinancePeriodError("Finance period end precedes its start")
    if selected.end >= current:
        raise FinancePeriodError("Finance reports can include only complete days through yesterday")
    if kind == "weekly" and (
        selected.start.weekday() != 0
        or selected.end.weekday() != 6
        or (selected.end - selected.start).days != 6
    ):
        raise FinancePeriodError("Weekly reports must cover Monday through Sunday")
    if kind == "monthly" and (
        selected.start.day != 1
        or (selected.end + timedelta(days=1)).day != 1
    ):
        raise FinancePeriodError("Monthly reports must cover a full calendar month")
    if kind == "bootstrap":
        selected = FinancePeriod(kind, selected.start, selected.end, cumulative=True)
    elif kind == "monthly" and first_monthly:
        selected = FinancePeriod(
            kind,
            earliest_registry_date(registry),
            selected.end,
            cumulative=True,
        )
    return selected


def report_maturity_blocker(period: FinancePeriod, *, today: date | None = None) -> str | None:
    current = today or datetime.now(MOSCOW).date()
    if period.kind not in {"weekly", "monthly"}:
        return None
    # WB weekly settlement/corrections normally arrive after Monday. A report
    # is not eligible for FINAL until three complete days follow the period.
    eligible = period.end + timedelta(days=3)
    if current < eligible:
        return f"Official settlement stability window is open through {eligible.isoformat()}"
    return None
