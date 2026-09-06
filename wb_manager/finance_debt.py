"""Withdrawal and historical ad-fee debt reconciliation helpers.

The finance service deliberately does not execute withdrawals.  A person may
withdraw money in the seller portal and later give us the portal record.  This
module turns that record into an immutable :class:`FinanceLedger` event and
can import the *actual* advertising-credit transactions returned by WB's
``adv/v1/payments`` endpoint.  Importing a payment is readback-only; no POST is
performed here.  Current withdrawals use ``settlement_balance`` and create no
advertising debt because campaign budgets are funded from WB balance directly;
the FIFO path remains only for historical debt evidence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from .finance_ledger import (
    FinanceLedger,
    FinanceLedgerError,
    canonical_json,
    normalize_iso,
    sha256_text,
)


_PAYMENT_AMOUNT_KEYS = (
    "amount",
    "sum",
    "paymentSum",
    "paymentAmount",
    "depositAmount",
    "replenishmentAmount",
    "value",
)
_PAYMENT_REFERENCE_KEYS = (
    "paymentId",
    "transactionId",
    "operationId",
    "id",
    "eventId",
    "operation",
)
_PAYMENT_TIME_KEYS = (
    "paidAt",
    "paymentDate",
    "createdAt",
    "date",
    "time",
    "updTime",
)


class AdvertisingDebtImportError(FinanceLedgerError):
    """Raised when a platform payment row is ambiguous or malformed."""


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
) -> dict[str, Any]:
    """Convenience wrapper for the operator-supplied withdrawal record.

    Use ``settlement_balance`` for current withdrawals, where fee due,
    withheld, and debt are all zero.  For historical records, omitted portal
    fee facts remain ``unknown/PENDING_DATA`` rather than becoming known zero.
    """

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
    """Extract one positive top-up from a normalized WB payment row.

    ``raw_ad.payload_json`` stores the normalized wrapper under ``raw``.  WB
    has returned several shapes over time, so only explicit transaction amount
    fields are accepted; current balance fields such as ``net``/``balance``
    are intentionally excluded.  ``None`` means the row is not an actual
    advertising-funding transaction (or has no unambiguous amount).
    """

    normalized = dict(row)
    db_occurred_at = normalized.get("occurred_at") or normalized.get("observed_at")
    if str(normalized.get("eventType") or "").strip() not in {
        "advertising_funding",
        "advertising_payment",
    }:
        # A raw DB row wraps the event JSON in payload_json.
        payload_json = normalized.get("payload_json")
        if payload_json:
            try:
                parsed = json.loads(str(payload_json))
            except json.JSONDecodeError:
                return None
            if isinstance(parsed, Mapping):
                normalized = dict(parsed)
    event_type = str(normalized.get("eventType") or "").strip()
    if event_type not in {"advertising_funding", "advertising_payment"}:
        return None
    raw = normalized.get("raw")
    candidates: list[Mapping[str, Any]] = [normalized]
    if isinstance(raw, Mapping):
        candidates.append(raw)

    amount: Decimal | None = None
    amount_key: str | None = None
    for candidate in candidates:
        for key in _PAYMENT_AMOUNT_KEYS:
            if key not in candidate or candidate[key] in (None, ""):
                continue
            try:
                value = Decimal(str(candidate[key]))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if not value.is_finite() or value <= Decimal("0"):
                continue
            amount = value.quantize(Decimal("0.01"))
            amount_key = key
            break
        if amount is not None:
            break
    if amount is None:
        return None

    occurred: datetime | str | None = None
    for candidate in candidates:
        for key in _PAYMENT_TIME_KEYS:
            if candidate.get(key) not in (None, ""):
                occurred = candidate[key]
                break
        if occurred is not None:
            break
    if occurred is None:
        occurred = normalized.get("occurred_at") or normalized.get("occurredAt")
    if occurred is None:
        occurred = db_occurred_at
    if occurred is None:
        occurred = fallback_occurred_at
    if occurred is None:
        # Payment rows without a timestamp cannot be placed in the FIFO audit
        # sequence and are therefore left for manual review.
        return None
    try:
        paid_at = normalize_iso(occurred)
    except FinanceLedgerError:
        return None

    reference: str | None = None
    for candidate in candidates:
        for key in _PAYMENT_REFERENCE_KEYS:
            value = candidate.get(key)
            if value not in (None, ""):
                reference = str(value).strip()
                if reference:
                    break
        if reference:
            break
    event_id = str(normalized.get("eventId") or "").strip() or None
    return {
        "amountCny": format(amount, "f"),
        "paidAt": paid_at,
        "platformReference": reference,
        "eventId": event_id,
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
    """Import platform payment rows and FIFO-settle historical debt.

    The function is safe to call after every daily sync.  Stable event hashes
    make repeated API pages idempotent.  Rows that are not provably positive
    top-ups are returned in ``unresolvedRows`` instead of being booked as zero.
    """

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
            skipped.append({
                "index": index,
                "reason": "not an unambiguous positive advertising funding row",
            })
            continue
        event_id = extracted.get("eventId")
        raw = extracted["raw"]
        raw_hash = sha256_text(canonical_json(raw))
        key = f"platform-ad-payment:{alias}:{event_id or raw_hash}"
        payment_id = ledger.record_advertising_payment_readback(
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
                "raw": raw,
            },
        )
        imported.append(payment_id)
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
    """Read normalized ``raw_ad`` API evidence and import payment credits."""

    clauses = ["store_alias=?"]
    params: list[Any] = [store_alias]
    if period_start is not None:
        clauses.append("occurred_at>=?")
        params.append(normalize_iso(period_start))
    if period_end is not None:
        clauses.append("occurred_at<?")
        params.append(normalize_iso(period_end))
    rows = ledger.connection.execute(
        "SELECT * FROM raw_ad WHERE " + " AND ".join(clauses)
        + " ORDER BY occurred_at, event_hash",
        params,
    ).fetchall()
    payloads = [dict(row) for row in rows]
    result = import_advertising_payment_rows(ledger, store_alias, payloads)
    result["sourceRowsScanned"] = len(payloads)
    # Preserve the sync health next to the imported amounts.  An empty page
    # is only evidence of zero when the corresponding platform request was
    # complete; a failed/partial request must remain visible for follow-up.
    sync_row = ledger.connection.execute(
        """
        SELECT source, requested_from, requested_through, status, error_text,
               completed_at, run_id
        FROM sync_runs
        WHERE store_alias=? AND source='advertising_payments'
        ORDER BY COALESCE(completed_at, started_at) DESC, run_id DESC
        LIMIT 1
        """,
        (store_alias,),
    ).fetchone()
    if sync_row is not None:
        result["sourceSync"] = {
            "source": str(sync_row["source"]),
            "requestedFrom": str(sync_row["requested_from"]),
            "requestedThrough": str(sync_row["requested_through"]),
            "status": str(sync_row["status"]),
            "error": sync_row["error_text"],
            "completedAt": sync_row["completed_at"],
            "runId": str(sync_row["run_id"]),
        }
        if str(sync_row["status"]) != "complete":
            result.setdefault("unresolvedRows", []).append(
                {
                    "reason": "advertising_payments source sync is not complete",
                    "sourceStatus": str(sync_row["status"]),
                    "error": sync_row["error_text"],
                    "runId": str(sync_row["run_id"]),
                }
            )
    return result


__all__ = [
    "AdvertisingDebtImportError",
    "extract_advertising_payment",
    "record_withdrawal_from_user_record",
    "import_advertising_payment_rows",
    "import_advertising_payment_events_from_ledger",
]
