"""Read-only reconciliation for withdrawal-linked historical advertising debt.

Current campaign budgets use the same store's WB settlement balance, so
``settlement_balance`` withdrawals create no new advertising debt.  This module
keeps the legacy FIFO path available only for immutable pre-cutover evidence.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .finance_ledger import (
    FinanceLedger,
    FinanceLedgerError,
    canonical_json,
    normalize_iso,
    sha256_text,
)


_AMOUNT_KEYS = (
    "amount",
    "sum",
    "paymentSum",
    "paymentAmount",
    "depositAmount",
    "replenishmentAmount",
    "value",
)
_REFERENCE_KEYS = ("paymentId", "transactionId", "operationId", "id", "eventId")
_TIME_KEYS = ("paidAt", "paymentDate", "createdAt", "date", "time", "updTime")
_MOSCOW = ZoneInfo("Europe/Moscow")
_LEGACY_DEBT_PAYMENT_MARKERS = {
    "advertising_debt_payment",
    "legacy_advertising_debt_payment",
    "legacy_debt_payment",
    "withdrawal_advertising_debt_payment",
}
_LEGACY_MARKER_FIELDS = (
    "eventType",
    "event_type",
    "paymentPurpose",
    "payment_purpose",
    "purpose",
    "operationType",
    "operation_type",
    "debtType",
    "debt_type",
)
_SUCCESS_CARD_STATUSES = {"completed", "confirmed", "paid", "success", "succeeded"}


class AdvertisingDebtImportError(FinanceLedgerError):
    pass


def record_withdrawal_from_user_record(
    ledger: FinanceLedger,
    *,
    store_alias: str,
    record_key: str,
    amount_cny: Any,
    withdrawn_at: datetime | str,
    advertising_fee_due_cny: Any | None = None,
    advertising_fee_withheld_cny: Any | None = None,
    advertising_fee_status: str | None = None,
    settled_at: datetime | str | None = None,
    external_reference: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    advertising_fee_cutover: date | str | None = None,
    financial_timezone: str | ZoneInfo | None = None,
) -> dict[str, Any]:
    """Record an externally executed withdrawal without moving money."""

    record_id = ledger.record_withdrawal_record(
        record_key,
        store_alias,
        amount_cny=amount_cny,
        withdrawn_at=withdrawn_at,
        advertising_fee_due_cny=advertising_fee_due_cny,
        advertising_fee_withheld_cny=advertising_fee_withheld_cny,
        advertising_fee_status=advertising_fee_status,
        status="readback_confirmed" if settled_at is not None else "reported",
        settled_at=settled_at,
        external_reference=external_reference,
        source_type="user_provided",
        evidence=evidence,
        advertising_fee_cutover=advertising_fee_cutover,
        financial_timezone=financial_timezone,
    )
    return {
        "recordId": record_id,
        "storeAlias": store_alias,
        "debt": ledger.advertising_debt_summary(store_alias=store_alias),
    }


def extract_advertising_payment(
    row: Mapping[str, Any],
    *,
    fallback_occurred_at: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Extract an explicitly identified legacy-debt payment.

    Ordinary ``/adv/v1/payments`` campaign top-ups, including settlement-balance
    ``type=1`` rows, are deliberately excluded.  They are campaign-allocation
    evidence and must never clear pre-cutover withdrawal debt.
    """

    normalized = dict(row)
    db_occurred_at = normalized.get("occurred_at") or normalized.get("observed_at")
    if str(normalized.get("eventType") or "").strip() not in {
        "advertising_funding",
        "advertising_payment",
        *_LEGACY_DEBT_PAYMENT_MARKERS,
    }:
        payload_json = normalized.get("payload_json")
        if payload_json:
            try:
                parsed = json.loads(str(payload_json))
            except json.JSONDecodeError:
                return None
            if isinstance(parsed, Mapping):
                normalized = dict(parsed)
    candidates = [normalized]
    if isinstance(normalized.get("raw"), Mapping):
        candidates.append(normalized["raw"])
    if not _is_explicit_legacy_debt_payment(candidates):
        return None

    amount: Decimal | None = None
    amount_key: str | None = None
    for candidate in candidates:
        for key in _AMOUNT_KEYS:
            if candidate.get(key) in (None, ""):
                continue
            try:
                value = Decimal(str(candidate[key]))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if value.is_finite() and value > 0:
                amount = value.quantize(Decimal("0.01"))
                amount_key = key
                break
        if amount is not None:
            break
    if amount is None:
        return None

    paid_at: str | None = None
    for candidate in candidates:
        for key in _TIME_KEYS:
            if candidate.get(key) not in (None, ""):
                paid_at = _normalize_payment_time(candidate[key])
                if paid_at is not None:
                    break
        if paid_at is not None:
            break
    if paid_at is None:
        for fallback in (
            normalized.get("occurred_at"),
            db_occurred_at,
            fallback_occurred_at,
        ):
            paid_at = _normalize_payment_time(fallback)
            if paid_at is not None:
                break
    if paid_at is None:
        return None

    reference: str | None = None
    for candidate in candidates:
        for key in _REFERENCE_KEYS:
            if candidate.get(key) not in (None, ""):
                reference = str(candidate[key]).strip() or None
                break
        if reference:
            break
    return {
        "amountCny": format(amount, "f"),
        "paidAt": paid_at,
        "platformReference": reference,
        "eventId": str(normalized.get("eventId") or "").strip() or None,
        "amountField": amount_key,
        "raw": normalized,
    }


def import_advertising_payment_rows(
    ledger: FinanceLedger,
    store_alias: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    source_type: str = "platform_api",
    fallback_occurred_at: datetime | str | None = None,
) -> dict[str, Any]:
    alias = str(store_alias or "").strip()
    if not alias:
        raise AdvertisingDebtImportError("store_alias is required")
    imported: list[str] = []
    skipped: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            skipped.append({"index": index, "reason": "row is not an object"})
            continue
        extracted = extract_advertising_payment(
            row, fallback_occurred_at=fallback_occurred_at
        )
        if extracted is None:
            skipped.append({"index": index, "reason": "ambiguous advertising payment"})
            continue
        raw_hash = sha256_text(canonical_json(extracted["raw"]))
        key = f"platform-ad-payment:{alias}:{extracted['eventId'] or raw_hash}"
        imported.append(
            ledger.record_advertising_payment_readback(
                key,
                alias,
                amount_cny=extracted["amountCny"],
                paid_at=extracted["paidAt"],
                source_type=source_type,
                platform_reference=extracted.get("platformReference"),
                raw_event_hash=raw_hash,
                evidence={
                    "source": "adv/v1/payments",
                    "amountField": extracted.get("amountField"),
                    "raw": extracted["raw"],
                },
                legacy_debt_payment=True,
            )
        )
    return {
        "storeAlias": alias,
        "importedPaymentIds": imported,
        "importedCount": len(imported),
        "unresolvedRows": skipped,
        "debt": ledger.advertising_debt_summary(store_alias=alias),
    }


def import_advertising_payment_events_from_ledger(
    ledger: FinanceLedger,
    store_alias: str,
    *,
    period_start: datetime | str | None = None,
    period_end: datetime | str | None = None,
) -> dict[str, Any]:
    clauses = ["store_alias=?"]
    params: list[Any] = [store_alias]
    if period_start is not None:
        clauses.append("occurred_at>=?")
        params.append(normalize_iso(period_start))
    if period_end is not None:
        clauses.append("occurred_at<?")
        params.append(normalize_iso(period_end))
    rows = ledger.connection.execute(
        "SELECT * FROM raw_ad WHERE "
        + " AND ".join(clauses)
        + " ORDER BY occurred_at, event_hash",
        params,
    ).fetchall()
    result = import_advertising_payment_rows(
        ledger, store_alias, [dict(row) for row in rows]
    )
    result["sourceRowsScanned"] = len(rows)
    return result


def _is_explicit_legacy_debt_payment(
    candidates: Iterable[Mapping[str, Any]],
) -> bool:
    rows = list(candidates)
    successful_status_seen = False
    payment_type_seen = False
    for candidate in rows:
        payment_type = candidate.get("type")
        if payment_type not in (None, ""):
            payment_type_seen = True
            if isinstance(payment_type, bool):
                return False
            if isinstance(payment_type, int):
                parsed_payment_type = payment_type
            elif isinstance(payment_type, str) and re.fullmatch(
                r"[0-9]+", payment_type.strip()
            ):
                parsed_payment_type = int(payment_type.strip())
            else:
                return False
            if parsed_payment_type == 1:
                return False
        status_id = candidate.get("statusId", candidate.get("status_id"))
        if status_id not in (None, ""):
            if isinstance(status_id, bool) or status_id not in (1, "1"):
                return False
            successful_status_seen = True
        card_status = candidate.get("cardStatus", candidate.get("card_status"))
        if card_status not in (None, ""):
            if str(card_status).strip().casefold() not in _SUCCESS_CARD_STATUSES:
                return False
            successful_status_seen = True
    if not payment_type_seen or not successful_status_seen:
        return False
    for candidate in rows:
        if candidate.get("legacyDebtPayment") is True:
            return True
        evidence = candidate.get("evidence")
        if isinstance(evidence, Mapping) and evidence.get("legacyDebtPayment") is True:
            return True
        for key in _LEGACY_MARKER_FIELDS:
            value = candidate.get(key)
            if value in (None, ""):
                continue
            marker = re.sub(r"[\s-]+", "_", str(value).strip().casefold())
            if marker in _LEGACY_DEBT_PAYMENT_MARKERS:
                return True
    return False


def _normalize_payment_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min, tzinfo=_MOSCOW)
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                parsed = datetime.combine(date.fromisoformat(raw), time.min, tzinfo=_MOSCOW)
            else:
                parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_MOSCOW)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "AdvertisingDebtImportError",
    "extract_advertising_payment",
    "import_advertising_payment_events_from_ledger",
    "import_advertising_payment_rows",
    "record_withdrawal_from_user_record",
]
