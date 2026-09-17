"""Evidence-labelled owner cash positions at each report column's endpoint.

This view combines a WB seller account *stock* with the known subtotal of
completed withdrawals. It does not manufacture account receipts or turn the
combination into a period cash flow. Pending withdrawals are shown separately:
the relationship between a processing request and WB's ``current`` balance is
not established by these sources.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


_MOSCOW = ZoneInfo("Europe/Moscow")
_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")


def _money(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number.quantize(_CENT) if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _cny(value: Decimal | None) -> str | None:
    return format(value, ".2f") if value is not None else None


def _instant(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def _opened_day(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        when = _instant(value)
        if when is None:
            raise ValueError(f"Invalid store opening date: {value!r}") from None
        return when.astimezone(_MOSCOW).date()


def _period_rows(periods: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    rows = ([{"key": key, **row} for key, row in periods.items()]
            if isinstance(periods, Mapping) else list(periods))
    result = []
    for row in rows:
        key, start, end = str(row["key"]), str(row["start"]), str(row["end"])
        if date.fromisoformat(start) > date.fromisoformat(end):
            raise ValueError(f"Period {key} ends before it starts")
        result.append({"key": key, "start": start, "end": end})
    if len({row["key"] for row in result}) != len(result):
        raise ValueError("Duplicate owner cash position period")
    return result


def _stores(sources: Mapping[str, Any], enabled_stores: Sequence[Any]) -> dict[str, str | None]:
    opened = {str(row["storeAlias"]): row.get("openedAt")
              for row in sources.get("stores", []) if row.get("storeAlias")}
    result = {}
    for item in enabled_stores:
        alias = str(item["storeAlias"] if isinstance(item, Mapping) else item)
        if alias in result:
            raise ValueError(f"Duplicate enabled store: {alias}")
        result[alias] = (item.get("openedAt", opened.get(alias))
                         if isinstance(item, Mapping) else opened.get(alias))
    return result


def _known_metric(accounts: Mapping[str, Any], period: str, alias: str, metric: str) -> tuple[Decimal | None, bool]:
    row = (accounts.get(period, {}).get("actual", {}).get("stores", {})
           .get(alias, {}).get("cumulative", {}).get("metrics", {}).get(metric, {}))
    return _money(row.get("known_subtotal", row.get("amount"))), row.get("complete") is True


def _snapshots(sources: Mapping[str, Any], enabled: set[str]) -> dict[str, list[tuple[datetime, Decimal, str | None]]]:
    result: dict[str, list[tuple[datetime, Decimal, str | None]]] = {alias: [] for alias in enabled}
    for row in sources.get("balanceSnapshots", []):
        alias = str(row.get("storeAlias") or "")
        if alias not in enabled or row.get("source") != "seller_balance" or row.get("currency") != "CNY":
            continue
        when = _instant(row.get("observedAt"))
        amount = _money(row.get("accountBalanceCny"))
        if when is None or amount is None:
            continue
        result[alias].append((when, amount, row.get("eventId")))
    for rows in result.values():
        rows.sort(key=lambda item: (item[0], str(item[2] or "")))
    return result


def build_owner_cash_positions(
    sources: Mapping[str, Any],
    owner_period_accounts: Mapping[str, Any],
    periods: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    enabled_stores: Sequence[Any],
) -> dict[str, Any]:
    """Return per-store and combined endpoint known subtotals.

    The latest eligible CNY ``seller_balance.accountBalanceCny`` snapshot may
    predate the period end but may never follow it. ``for_withdraw`` is not used.
    A missing snapshot leaves the combined position unknown even when completed
    withdrawal history has a known subtotal. This is not bank receipt coverage.
    """
    stores = _stores(sources, enabled_stores)
    snapshots = _snapshots(sources, set(stores))
    result: dict[str, Any] = {"schemaVersion": 1,
        "basis": "wb_account_balance_plus_known_completed_withdrawals",
        "isPeriodCashFlow": False, "isBankReceiptEvidence": False, "periods": {}}
    for period in _period_rows(periods):
        key, end = period["key"], date.fromisoformat(period["end"])
        store_rows: dict[str, Any] = {}
        completed_sum = pending_sum = _ZERO
        balance_sum = position_sum = _ZERO
        all_completed_known = all_pending_known = all_balances_known = True
        active_count = 0
        for alias, opened in stores.items():
            opened_day = _opened_day(opened)
            if opened_day and opened_day > end:
                store_rows[alias] = {"store": alias, "status": "NOT_APPLICABLE",
                    "completedWithdrawalsKnownSubtotalCny": "0.00", "accountBalanceCny": "0.00",
                    "snapshotObservedAt": None, "snapshotDay": None, "snapshotFreshness": None,
                    "snapshotEventId": None, "conflictingSnapshotEventIds": [],
                    "pendingWithdrawalKnownSubtotalCny": "0.00", "pendingExcludedFromSubtotal": True,
                    "pendingTreatment": "not_added_separately_balance_relation_unverified",
                    "pendingBalanceRelation": "none_recorded", "knownSubtotalCny": "0.00", "gaps": []}
                continue
            active_count += 1
            completed, completed_complete = _known_metric(owner_period_accounts, key, alias, "withdrawal_completed")
            pending, pending_complete = _known_metric(owner_period_accounts, key, alias, "withdrawal_pending")
            eligible = [row for row in snapshots[alias] if row[0].astimezone(_MOSCOW).date() <= end]
            latest_at = eligible[-1][0] if eligible else None
            latest = [row for row in eligible if row[0] == latest_at] if latest_at else []
            conflicting = len({row[1] for row in latest}) > 1
            snapshot = None if conflicting else eligible[-1] if eligible else None
            balance = snapshot[1] if snapshot else None
            observed_day = latest_at.astimezone(_MOSCOW).date() if latest_at else None
            gaps = []
            if completed is None:
                gaps.append("MISSING_COMPLETED_WITHDRAWAL_SUBTOTAL")
                all_completed_known = False
            elif not completed_complete:
                gaps.append("WITHDRAWAL_HISTORY_INCOMPLETE")
                completed_sum += completed
            else:
                completed_sum += completed
            if pending is None:
                gaps.append("MISSING_PENDING_WITHDRAWAL_SUBTOTAL")
                all_pending_known = False
            else:
                pending_sum += pending
                if pending != 0:
                    gaps.append("PENDING_BALANCE_RELATION_UNVERIFIED")
                if not pending_complete:
                    gaps.append("PENDING_WITHDRAWAL_HISTORY_INCOMPLETE")
            if balance is None:
                gaps.append("CONFLICTING_SELLER_BALANCE_SNAPSHOT" if conflicting
                            else "MISSING_ELIGIBLE_SELLER_BALANCE_SNAPSHOT")
                all_balances_known = False
            else:
                balance_sum += balance
            if observed_day is not None and observed_day < end:
                gaps.append("STALE_SELLER_BALANCE_SNAPSHOT")
            known_position = balance + completed if balance is not None and completed is not None else None
            if known_position is not None:
                position_sum += known_position
            store_rows[alias] = {"store": alias,
                "status": "KNOWN_SUBTOTAL" if known_position is not None else "INCOMPLETE",
                "completedWithdrawalsKnownSubtotalCny": _cny(completed),
                "accountBalanceCny": _cny(balance),
                "snapshotObservedAt": latest_at.isoformat() if latest_at else None,
                "snapshotDay": observed_day.isoformat() if observed_day else None,
                "snapshotFreshness": ("same_day" if observed_day == end else "stale") if observed_day else None,
                "snapshotEventId": snapshot[2] if snapshot else None,
                "conflictingSnapshotEventIds": sorted(str(row[2] or "") for row in latest) if conflicting else [],
                "pendingWithdrawalKnownSubtotalCny": _cny(pending),
                "pendingExcludedFromSubtotal": True,
                "pendingTreatment": "not_added_separately_balance_relation_unverified",
                "pendingBalanceRelation": "unverified" if pending else "none_recorded" if pending == 0 else "unknown",
                "knownSubtotalCny": _cny(known_position), "gaps": gaps}
        combined_known = all_balances_known and all_completed_known
        combined_gaps = [{"store": alias, "code": gap}
                         for alias, row in store_rows.items() for gap in row["gaps"]]
        result["periods"][key] = {"start": period["start"], "end": period["end"],
            "stores": store_rows,
            "ALL": {"status": "NOT_APPLICABLE" if not active_count else "KNOWN_SUBTOTAL" if combined_known else "INCOMPLETE",
                "completedWithdrawalsKnownSubtotalCny": _cny(completed_sum) if all_completed_known else None,
                "accountBalanceCny": _cny(balance_sum) if all_balances_known else None,
                "pendingWithdrawalKnownSubtotalCny": _cny(pending_sum) if all_pending_known else None,
                "pendingExcludedFromSubtotal": True,
                "pendingTreatment": "not_added_separately_balance_relation_unverified",
                "knownSubtotalCny": _cny(position_sum) if combined_known else None,
                "gaps": combined_gaps}}
    return result
