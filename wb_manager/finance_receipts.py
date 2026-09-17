"""Append independently evidenced account receipts to the private cash registry.

This importer is deliberately narrower than a bank-statement parser.  It accepts
only a normalized JSON manifest, never infers receipts from settlement proceeds,
withdrawals, or balance deltas, and is idempotent by stable source identity.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from .finance_reporting_standard import (
    ACCOUNT_RECEIPT_SOURCE_TYPES,
    validate_historical_cash_events,
)


CENT = Decimal("0.01")
MOSCOW = ZoneInfo("Europe/Moscow")


class ReceiptImportError(ValueError):
    """The receipt manifest cannot be safely added to the cash registry."""


def import_receipt_manifest(
    registry_path: Path,
    manifest_path: Path,
    *,
    enabled_stores: Iterable[str],
    store_openings: Mapping[str, str | date],
    legacy_receipts: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    registry_path = Path(registry_path).resolve()
    manifest_path = Path(manifest_path).resolve()
    if registry_path.name.endswith(".example.json"):
        raise ReceiptImportError(
            "Refusing to write account receipts to an example registry; "
            "configure a private local cash-event registry first"
        )
    registry = _read_object(registry_path, "historical cash-event registry")
    manifest = _read_object(manifest_path, "receipt import manifest")
    stores = set(enabled_stores)
    if str(manifest.get("currency") or "").upper() != "CNY":
        raise ReceiptImportError("Receipt import manifest currency must be CNY")
    raw_events = manifest.get("events", [])
    raw_coverage = manifest.get("receiptCoverage", [])
    if not isinstance(raw_events, list) or not isinstance(raw_coverage, list):
        raise ReceiptImportError("events and receiptCoverage must be lists")
    if not raw_events and not raw_coverage:
        raise ReceiptImportError("Receipt import manifest contains no evidence")

    events = [
        _normalize_event(item, index, stores, store_openings)
        for index, item in enumerate(raw_events)
    ]
    coverage = [
        _normalize_coverage(item, index, stores, store_openings)
        for index, item in enumerate(raw_coverage)
    ]
    _reject_duplicate_import_ids(events, "eventId", "receipt event")
    _reject_duplicate_import_ids(coverage, "coverageId", "receipt coverage")
    _reject_legacy_receipt_conflicts(events, legacy_receipts)

    candidate = deepcopy(registry)
    candidate_events = candidate.setdefault("events", [])
    candidate_coverage = candidate.setdefault("receiptCoverage", [])
    if not isinstance(candidate_events, list) or not isinstance(candidate_coverage, list):
        raise ReceiptImportError(
            "Historical cash registry events and receiptCoverage must be lists"
        )

    inserted_events, unchanged_events = _merge_append_only(
        candidate_events, events, "eventId", "receipt event"
    )
    inserted_coverage, unchanged_coverage = _merge_append_only(
        candidate_coverage, coverage, "coverageId", "receipt coverage"
    )
    validate_historical_cash_events(candidate)
    changed = bool(inserted_events or inserted_coverage)
    if changed:
        _atomic_write(registry_path, candidate)
    return {
        "status": "IMPORTED" if changed else "NOOP",
        "registry": str(registry_path),
        "manifest": str(manifest_path),
        "insertedEvents": inserted_events,
        "unchangedEvents": unchanged_events,
        "insertedCoverage": inserted_coverage,
        "unchangedCoverage": unchanged_coverage,
        "nextStep": "Run finance account-refresh, then regenerate the required owner report.",
    }


def _normalize_event(
    value: Any,
    index: int,
    stores: set[str],
    openings: Mapping[str, str | date],
) -> dict[str, Any]:
    item = _mapping(value, f"receipt event {index}")
    if item.get("eventType") != "account_receipt":
        raise ReceiptImportError(
            f"receipt event {index} requires eventType=account_receipt"
        )
    event_id = _text(item, "eventId", f"receipt event {index}")
    store = _store(item, index, stores)
    received_at = _text(item, "receivedAt", f"receipt event {index}")
    received_day = _iso_day(received_at, f"receipt event {index}.receivedAt")
    _not_before_opening(store, received_day, openings, f"receipt event {index}")
    if item.get("status") != "readback_confirmed":
        raise ReceiptImportError(
            f"receipt event {index} requires status=readback_confirmed"
        )
    amount = _positive_money(item.get("amountCny"), f"receipt event {index}")
    source_type = _text(item, "sourceType", f"receipt event {index}")
    if source_type not in ACCOUNT_RECEIPT_SOURCE_TYPES:
        raise ReceiptImportError(
            f"receipt event {index} has unsupported sourceType: {source_type}"
        )
    evidence = _text(item, "evidenceReference", f"receipt event {index}")
    result = {
        "eventId": event_id,
        "store": store,
        "eventType": "account_receipt",
        "amountCny": amount,
        "receivedAt": received_at,
        "status": "readback_confirmed",
        "sourceType": source_type,
        "evidenceReference": evidence,
    }
    external = str(item.get("externalReference") or "").strip()
    if external:
        result["externalReference"] = external
    return result


def _normalize_coverage(
    value: Any,
    index: int,
    stores: set[str],
    openings: Mapping[str, str | date],
) -> dict[str, Any]:
    item = _mapping(value, f"receipt coverage {index}")
    coverage_id = _text(item, "coverageId", f"receipt coverage {index}")
    store = _store(item, index, stores, label="receipt coverage")
    start = _iso_day(
        _text(item, "from", f"receipt coverage {index}"),
        f"receipt coverage {index}.from",
    )
    through = _iso_day(
        _text(item, "through", f"receipt coverage {index}"),
        f"receipt coverage {index}.through",
    )
    if through < start:
        raise ReceiptImportError(f"receipt coverage {index} through precedes from")
    _not_before_opening(store, start, openings, f"receipt coverage {index}")
    if item.get("complete") is not True:
        raise ReceiptImportError(
            f"receipt coverage {index} requires complete=true"
        )
    return {
        "coverageId": coverage_id,
        "store": store,
        "from": start.isoformat(),
        "through": through.isoformat(),
        "complete": True,
        "evidence": _text(item, "evidence", f"receipt coverage {index}"),
    }


def _merge_append_only(
    target: list[Any],
    incoming: list[dict[str, Any]],
    identity_key: str,
    label: str,
) -> tuple[int, int]:
    existing: dict[str, Any] = {}
    for index, value in enumerate(target):
        item = _mapping(value, f"existing {label} {index}")
        identity = _text(item, identity_key, f"existing {label} {index}")
        existing[identity] = item
    inserted = unchanged = 0
    for item in incoming:
        identity = item[identity_key]
        old = existing.get(identity)
        if old is None:
            target.append(item)
            existing[identity] = item
            inserted += 1
        elif _canonical(old) == _canonical(item):
            unchanged += 1
        else:
            raise ReceiptImportError(
                f"Conflicting {label} identity {identity}; append a new stable identity"
            )
    return inserted, unchanged


def _reject_duplicate_import_ids(
    rows: list[dict[str, Any]], identity_key: str, label: str
) -> None:
    identities = [row[identity_key] for row in rows]
    if len(identities) != len(set(identities)):
        raise ReceiptImportError(f"Receipt manifest contains duplicate {label} identities")


def _reject_legacy_receipt_conflicts(
    events: list[dict[str, Any]], legacy_receipts: Iterable[Mapping[str, Any]]
) -> None:
    for old in legacy_receipts:
        if not isinstance(old, Mapping):
            raise ReceiptImportError("Legacy cashReceipts entries must be objects")
        store = str(old.get("store") or "").strip()
        identity = str(old.get("eventId") or "").strip()
        reference = str(old.get("evidence") or "").strip()
        for event in events:
            if store == event["store"] and (
                identity == event["eventId"]
                or reference and reference in {
                    event["evidenceReference"], event.get("externalReference")
                }
            ):
                raise ReceiptImportError(
                    "Receipt also exists in legacy owner decisions; "
                    "reconcile the duplicate before importing"
                )


def _store(
    item: Mapping[str, Any],
    index: int,
    stores: set[str],
    *,
    label: str = "receipt event",
) -> str:
    store = _text(item, "store", f"{label} {index}")
    if store not in stores:
        raise ReceiptImportError(f"{label} {index} store is not enabled: {store}")
    return store


def _not_before_opening(
    store: str,
    event_day: date,
    openings: Mapping[str, str | date],
    label: str,
) -> None:
    raw = openings.get(store)
    if raw is None:
        raise ReceiptImportError(f"Missing opening date for enabled store {store}")
    opening = _iso_day(str(raw), f"{store} opening date")
    if event_day < opening:
        raise ReceiptImportError(f"{label} precedes store opening date {opening}")


def _positive_money(value: Any, label: str) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReceiptImportError(f"{label} amountCny must be numeric") from exc
    if not amount.is_finite() or amount <= 0:
        raise ReceiptImportError(f"{label} amountCny must be finite and positive")
    try:
        rounded = amount.quantize(CENT)
    except InvalidOperation as exc:
        raise ReceiptImportError(f"{label} amountCny is out of range") from exc
    if amount != rounded:
        raise ReceiptImportError(f"{label} amountCny cannot have fractional cents")
    return format(rounded, "f")


def _iso_day(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReceiptImportError(f"{label} must be an ISO date or timestamp") from exc
        if parsed.tzinfo is None:
            raise ReceiptImportError(f"{label} timestamp must include a timezone")
        return parsed.astimezone(MOSCOW).date()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReceiptImportError(f"{label} must be an object")
    return value


def _text(parent: Mapping[str, Any], key: str, label: str) -> str:
    value = str(parent.get(key) or "").strip()
    if not value:
        raise ReceiptImportError(f"{label} requires {key}")
    return value


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptImportError(f"Cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReceiptImportError(f"{label} must be a JSON object")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
