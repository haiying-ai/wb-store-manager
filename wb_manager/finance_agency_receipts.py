"""Keep owner-confirmed combined agency receipts separate from store cash.

Only an explicit allocation creates a store-level agency payment.  The rest of
each confirmed batch remains dated, auditable cash with no invented store.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
import re
from typing import Any
from urllib.parse import quote


_CENTS = re.compile(r"(?:0|[1-9][0-9]*)\.[0-9]{2}\Z")
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_ZERO = Decimal("0.00")


class AgencyReceiptError(ValueError):
    """A combined receipt cannot be represented without losing cash truth."""


def _day(value: Any, field: str) -> str:
    if isinstance(value, datetime):
        raise AgencyReceiptError(f"{field} must be a calendar date")
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or not _ISO_DAY.fullmatch(value):
        raise AgencyReceiptError(f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise AgencyReceiptError(f"{field} is not a valid calendar date") from exc


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AgencyReceiptError(f"{field} must be nonempty text without edge spaces")
    return value


def _money(value: Any, field: str) -> Decimal:
    # JSON numbers and binary floats can conceal a fraction of a cent.  The
    # private receipt registry uses explicit decimal strings instead.
    if not isinstance(value, str) or not _CENTS.fullmatch(value):
        raise AgencyReceiptError(f"{field} must be a positive NN.NN string")
    amount = Decimal(value)
    if amount <= _ZERO:
        raise AgencyReceiptError(f"{field} must be positive")
    return amount


def _formatted(amount: Decimal) -> str:
    return f"{amount:.2f}"


def _allocation_event_id(batch_id: str, store: str) -> str:
    # Percent-encoding keeps the two identity components unambiguous even if
    # an owner ID or a configured store alias contains a colon or percent sign.
    return f"batch:{quote(batch_id, safe='')}:{quote(store, safe='')}"


def prepare_agency_receipt_batches(
    decisions: Mapping[str, Any],
    enabled_stores: Iterable[str],
    *,
    through: str | date,
    periods: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    report_stores: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate batches and expose allocated events plus unallocated cash.

    ``allocatedPayments`` has the same core fields as legacy ``agencyPayments``
    and may be passed to ``cash_event`` only for its named store.  Its event ID
    is stable across reruns and allocation revisions. ``unallocatedReceipts``
    has no store and must not be passed to ``cash_event``.  Both are cutoff
    filtered. Future entries are validated but excluded from the returned
    register so historical report fingerprints remain tied to their cutoff.
    A combined unallocated receipt enters report totals only when every store
    named by its batch is within ``report_stores``. Allocated store payments
    remain available to the adapter for normal store-scoped ingestion.

    Each ``unallocatedByPeriod`` entry reports cash dated within that period
    and the closing cumulative amount since the earliest supplied period start.
    """
    if not isinstance(decisions, Mapping):
        raise AgencyReceiptError("decisions must be a mapping")
    cutoff = _day(through, "through")
    enabled_rows = list(enabled_stores)
    if any(not isinstance(store, str) or not store for store in enabled_rows):
        raise AgencyReceiptError("enabled_stores contains an invalid store")
    enabled = set(enabled_rows)
    selected_rows = enabled_rows if report_stores is None else list(report_stores)
    if any(not isinstance(store, str) or store not in enabled for store in selected_rows):
        raise AgencyReceiptError("report_stores contains a disabled or invalid store")
    selected = set(selected_rows)

    raw_legacy = decisions.get("agencyPayments", [])
    raw_batches = decisions.get("agencyReceiptBatches", [])
    if not isinstance(raw_legacy, list) or not isinstance(raw_batches, list):
        raise AgencyReceiptError("agencyPayments and agencyReceiptBatches must be lists")
    legacy_ids = {
        row.get("eventId") for row in raw_legacy
        if isinstance(row, Mapping) and isinstance(row.get("eventId"), str)
    }
    windows: list[dict[str, str]] = []
    window_keys: set[str] = set()
    if isinstance(periods, Mapping):
        period_rows = []
        for period_key, period_value in periods.items():
            if not isinstance(period_value, Mapping):
                raise AgencyReceiptError(f"periods[{period_key}] must be a mapping")
            if period_value.get("key", period_key) != period_key:
                raise AgencyReceiptError(f"periods[{period_key}].key conflicts with its mapping key")
            period_rows.append({**period_value, "key": period_key})
    else:
        period_rows = periods
    for index, raw in enumerate(period_rows):
        if not isinstance(raw, Mapping):
            raise AgencyReceiptError(f"periods[{index}] must be a mapping")
        key = _text(raw.get("key"), f"periods[{index}].key")
        start = _day(raw.get("start"), f"periods[{index}].start")
        end = _day(raw.get("end"), f"periods[{index}].end")
        if key in window_keys or start > end or end > cutoff:
            raise AgencyReceiptError(f"periods[{index}] has duplicate key or invalid bounds")
        window_keys.add(key)
        windows.append({"key": key, "start": start, "end": end})
    cumulative_start = next((row["start"] for row in windows if row["key"] == "cumulative"),
                            min((row["start"] for row in windows), default=None))

    batches: list[dict[str, Any]] = []
    batch_ids: set[str] = set()
    allocation_ids: set[str] = set()
    for index, raw in enumerate(raw_batches):
        if not isinstance(raw, Mapping):
            raise AgencyReceiptError(f"agencyReceiptBatches[{index}] must be a mapping")
        prefix = f"agencyReceiptBatches[{index}]"
        batch_id = _text(raw.get("eventId"), f"{prefix}.eventId")
        if batch_id in batch_ids or batch_id in legacy_ids:
            raise AgencyReceiptError(f"{prefix}.eventId duplicates another receipt")
        batch_ids.add(batch_id)
        received = _day(raw.get("date"), f"{prefix}.date")
        amount = _money(raw.get("amountCny"), f"{prefix}.amountCny")
        evidence = _text(raw.get("evidence"), f"{prefix}.evidence")
        stores = raw.get("stores")
        if not isinstance(stores, list) or not stores:
            raise AgencyReceiptError(f"{prefix}.stores must be a nonempty list")
        if any(not isinstance(store, str) or store not in enabled for store in stores):
            raise AgencyReceiptError(f"{prefix}.stores contains a disabled or invalid store")
        if len(set(stores)) != len(stores):
            raise AgencyReceiptError(f"{prefix}.stores contains a duplicate store")
        allocations = raw.get("allocations", [])
        if not isinstance(allocations, list):
            raise AgencyReceiptError(f"{prefix}.allocations must be a list")
        allocated = _ZERO
        used_stores: set[str] = set()
        clean_allocations: list[dict[str, str]] = []
        for position, item in enumerate(allocations):
            field = f"{prefix}.allocations[{position}]"
            if not isinstance(item, Mapping):
                raise AgencyReceiptError(f"{field} must be a mapping")
            store = item.get("store")
            if not isinstance(store, str) or store not in stores or store in used_stores:
                raise AgencyReceiptError(f"{field}.store is outside batch or duplicated")
            used_stores.add(store)
            part = _money(item.get("amountCny"), f"{field}.amountCny")
            allocated += part
            clean_allocations.append({"store": store, "amountCny": _formatted(part)})
            derived_id = _allocation_event_id(batch_id, store)
            if derived_id in legacy_ids or derived_id in allocation_ids:
                raise AgencyReceiptError(f"{field} collides with an existing receipt event ID")
            allocation_ids.add(derived_id)
        if allocated > amount:
            raise AgencyReceiptError(f"{prefix}.allocations exceed confirmed batch cash")
        batches.append({
            "eventId": batch_id, "date": received, "amountCny": _formatted(amount),
            "evidence": evidence, "stores": sorted(stores),
            "allocations": sorted(clean_allocations, key=lambda row: row["store"]),
            "allocatedCny": _formatted(allocated),
            "unallocatedCny": _formatted(amount - allocated),
        })
    batches.sort(key=lambda row: (row["date"], row["eventId"]))

    allocated_payments: list[dict[str, Any]] = []
    unallocated_receipts: list[dict[str, Any]] = []
    visible_batches = [
        {**batch, "inReportScope": set(batch["stores"]).issubset(selected)}
        for batch in batches if batch["date"] <= cutoff
    ]
    excluded_group_receipts: list[dict[str, Any]] = []
    for batch in visible_batches:
        metadata = {
            "basis": "owner_confirmed_combined_agency_receipt",
            "batchEventId": batch["eventId"],
            "batchAmountCny": batch["amountCny"],
            "batchAllocatedCny": batch["allocatedCny"],
            "batchUnallocatedCny": batch["unallocatedCny"],
            "eligibleStores": list(batch["stores"]),
        }
        for part in batch["allocations"]:
            allocated_payments.append({
                "store": part["store"],
                "eventId": _allocation_event_id(batch["eventId"], part["store"]),
                "date": batch["date"], "amountCny": part["amountCny"],
                "evidence": batch["evidence"], "metadata": dict(metadata),
            })
        if Decimal(batch["unallocatedCny"]) > _ZERO:
            receipt = {
                "eventId": batch["eventId"], "date": batch["date"],
                "amountCny": batch["unallocatedCny"], "evidence": batch["evidence"],
                "stores": list(batch["stores"]), "metadata": dict(metadata),
            }
            if batch["inReportScope"]:
                unallocated_receipts.append(receipt)
            else:
                excluded_group_receipts.append({
                    **receipt, "excludedReason": "batch_stores_outside_report_scope",
                })
    allocated_payments.sort(key=lambda row: (row["date"], row["eventId"]))

    period_amounts: dict[str, dict[str, str]] = {}
    for window in windows:
        start, end = window["start"], window["end"]
        period_total = sum((Decimal(row["amountCny"]) for row in unallocated_receipts
                            if start <= row["date"] <= end), _ZERO)
        cumulative_total = sum((Decimal(row["amountCny"]) for row in unallocated_receipts
                                if cumulative_start <= row["date"] <= end), _ZERO)
        period_amounts[window["key"]] = {
            "start": start, "end": end, "cumulativeStart": cumulative_start,
            "periodCny": _formatted(period_total),
            "cumulativeCny": _formatted(cumulative_total),
        }
    return {
        "batches": visible_batches,
        "allocatedPayments": allocated_payments,
        "unallocatedReceipts": unallocated_receipts,
        "excludedGroupReceipts": excluded_group_receipts,
        "unallocatedByPeriod": period_amounts,
    }


def prepare_agency_settlement_adjustments(
    decisions: Mapping[str, Any],
    receipt_register: Mapping[str, Any],
    *,
    through: str | date,
    periods: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    report_stores: Iterable[str],
) -> dict[str, Any]:
    """Expose owner-approved one-cent, non-cash settlement adjustments.

    Adjustments reduce only the consolidated agency payable. They are never
    agency receipts or store-level events. A combined batch can be adjusted
    only in a report containing every store named by that batch. The linked
    receipt batch remains the sole cash fact.
    """
    if not isinstance(decisions, Mapping) or not isinstance(receipt_register, Mapping):
        raise AgencyReceiptError("adjustment inputs must be mappings")
    cutoff = _day(through, "through")
    raw_entries = decisions.get("agencySettlementAdjustments", [])
    if not isinstance(raw_entries, list):
        raise AgencyReceiptError("agencySettlementAdjustments must be a list")
    raw_batches = decisions.get("agencyReceiptBatches", [])
    if not isinstance(raw_batches, list):
        raise AgencyReceiptError("agencyReceiptBatches must be a list")
    batches = {row["eventId"]: row for row in raw_batches if isinstance(row, Mapping)}
    receipt_ids = {row.get("eventId") for row in raw_batches if isinstance(row, Mapping)}
    receipt_ids.update(row.get("eventId") for row in decisions.get("agencyPayments", [])
                       if isinstance(row, Mapping))
    receipt_ids.update(row.get("eventId") for row in receipt_register.get("allocatedPayments", []))
    selected = set(report_stores)

    if isinstance(periods, Mapping):
        period_rows = []
        for key, row in periods.items():
            if not isinstance(row, Mapping) or row.get("key", key) != key:
                raise AgencyReceiptError(f"periods[{key}] has an invalid key")
            period_rows.append({**row, "key": key})
    else:
        period_rows = list(periods)
    windows = []
    window_keys = set()
    for index, row in enumerate(period_rows):
        if not isinstance(row, Mapping):
            raise AgencyReceiptError(f"periods[{index}] must be a mapping")
        key = _text(row.get("key"), f"periods[{index}].key")
        start = _day(row.get("start"), f"periods[{index}].start")
        end = _day(row.get("end"), f"periods[{index}].end")
        if key in window_keys or start > end or end > cutoff:
            raise AgencyReceiptError(f"periods[{index}] has duplicate key or invalid bounds")
        window_keys.add(key)
        windows.append({"key": key, "start": start, "end": end})
    cumulative_start = next((row["start"] for row in windows if row["key"] == "cumulative"),
                            min((row["start"] for row in windows), default=None))

    entries = []
    excluded = []
    ids = set()
    adjusted_batches = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, Mapping):
            raise AgencyReceiptError(f"agencySettlementAdjustments[{index}] must be a mapping")
        prefix = f"agencySettlementAdjustments[{index}]"
        event_id = _text(raw.get("eventId"), f"{prefix}.eventId")
        if event_id in ids or event_id in receipt_ids:
            raise AgencyReceiptError(f"{prefix}.eventId duplicates an existing cash or adjustment identity")
        ids.add(event_id)
        day = _day(raw.get("date"), f"{prefix}.date")
        amount = _money(raw.get("amountCny"), f"{prefix}.amountCny")
        if amount != Decimal("0.01"):
            raise AgencyReceiptError(f"{prefix}.amountCny must be the owner-approved 0.01 tail difference")
        batch_id = _text(raw.get("batchEventId"), f"{prefix}.batchEventId")
        batch = batches.get(batch_id)
        if batch is None:
            raise AgencyReceiptError(f"{prefix}.batchEventId does not identify a receipt batch")
        if batch_id in adjusted_batches:
            raise AgencyReceiptError(f"{prefix}.batchEventId already has a settlement adjustment")
        adjusted_batches.add(batch_id)
        batch_day = _day(batch.get("date"), f"{prefix}.linkedBatch.date")
        if day < batch_day or amount > _money(batch.get("amountCny"), f"{prefix}.linkedBatch.amountCny"):
            raise AgencyReceiptError(f"{prefix} precedes or exceeds its linked receipt batch")
        evidence = _text(raw.get("evidence"), f"{prefix}.evidence")
        stores = batch.get("stores")
        if not isinstance(stores, list) or not stores or any(not isinstance(store, str) for store in stores):
            raise AgencyReceiptError(f"{prefix}.linkedBatch.stores is invalid")
        row = {"eventId": event_id, "date": day, "amountCny": _formatted(amount),
               "batchEventId": batch_id, "evidence": evidence, "stores": sorted(stores),
               "basis": "owner_confirmed_non_cash_agency_settlement_tail"}
        if day > cutoff:
            continue
        if set(stores).issubset(selected):
            entries.append(row)
        else:
            excluded.append({**row, "excludedReason": "batch_stores_outside_report_scope"})
    entries.sort(key=lambda row: (row["date"], row["eventId"]))
    excluded.sort(key=lambda row: (row["date"], row["eventId"]))
    by_period = {}
    for window in windows:
        start, end = window["start"], window["end"]
        period_amount = sum((Decimal(row["amountCny"]) for row in entries
                             if start <= row["date"] <= end), _ZERO)
        cumulative_amount = sum((Decimal(row["amountCny"]) for row in entries
                                 if cumulative_start <= row["date"] <= end), _ZERO)
        by_period[window["key"]] = {"start": start, "end": end, "cumulativeStart": cumulative_start,
                                     "periodCny": _formatted(period_amount),
                                     "cumulativeCny": _formatted(cumulative_amount)}
    return {"entries": entries, "excludedGroupAdjustments": excluded, "byPeriod": by_period}


__all__ = ["AgencyReceiptError", "prepare_agency_receipt_batches", "prepare_agency_settlement_adjustments"]
