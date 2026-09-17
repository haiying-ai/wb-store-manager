from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from wb_manager.finance_debt import (
    extract_advertising_payment,
    import_advertising_payment_rows,
)
from wb_manager.finance_ledger import FinanceLedger, FinanceLedgerError


UTC = timezone.utc


def _ledger(tmp_path):
    ledger = FinanceLedger(tmp_path / "debt.sqlite3")
    ledger.register_store("owner-store", scope_id="owner", at="2026-01-01T00:00:00+00:00")
    return ledger


def test_withdrawal_without_ad_fee_creates_visible_debt_and_payment_settles_fifo(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        first = ledger.record_withdrawal_record(
            "withdrawal-1",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-08-01T10:00:00+00:00",
            advertising_fee_due_cny="12.50",
            advertising_fee_withheld_cny="0",
            advertising_fee_status="not_deducted",
        )
        second = ledger.record_withdrawal_record(
            "withdrawal-2",
            "owner-store",
            amount_cny="200.00",
            withdrawn_at="2026-08-02T10:00:00+00:00",
            advertising_fee_due_cny="8.00",
            advertising_fee_withheld_cny="0",
            advertising_fee_status="not_deducted",
        )
        before = ledger.advertising_debt_summary(store_alias="owner-store")
        assert before["stores"][0]["advertisingDebtOutstandingCny"] == "20.50"

        payment = ledger.record_advertising_payment_readback(
            "manual-ad-payment-1",
            "owner-store",
            amount_cny="15.00",
            paid_at="2026-08-03T10:00:00+00:00",
            source_type="platform_api",
            platform_reference="wb-pay-1",
        )
        after = ledger.advertising_debt_summary(store_alias="owner-store")
        assert after["stores"][0]["advertisingDebtOutstandingCny"] == "5.50"
        rows = ledger.list_withdrawal_records(store_alias="owner-store")
        assert rows[0]["record_id"] == first
        assert rows[0]["outstanding_advertising_fee_cny"] == "0.00"
        assert rows[1]["record_id"] == second
        assert rows[1]["outstanding_advertising_fee_cny"] == "5.50"
        payments = ledger.list_advertising_payment_readbacks(store_alias="owner-store")
        assert payments[0]["payment_id"] == payment
        assert payments[0]["unapplied_cny"] == "0.00"
    finally:
        ledger.close()


def test_omitted_ad_fee_is_unknown_not_zero_debt(tmp_path):
    """A bare user-supplied withdrawal must remain auditable as pending data."""

    ledger = _ledger(tmp_path)
    try:
        record_id = ledger.record_withdrawal_record(
            "withdrawal-without-fee-facts",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-08-01T10:00:00+00:00",
        )
        row = ledger.list_withdrawal_records(store_alias="owner-store")[0]
        assert row["record_id"] == record_id
        assert row["advertising_fee_status"] == "unknown"
        assert row["advertising_fee_due_cny"] == "0.00"
        summary = ledger.advertising_debt_summary(store_alias="owner-store")
        store = summary["stores"][0]
        # The legacy numeric debt remains zero because no amount is evidenced,
        # but the explicit state prevents consumers from reading it as proof
        # that no advertising fee is owed.
        assert store["advertisingDebtOutstandingCny"] == "0.00"
        assert store["advertisingFeeUnverifiedCount"] == 1
        assert store["advertisingDebtStatus"] == "PENDING_DATA"
        assert store["advertisingDebtAmountKnown"] is False
        assert store["advertisingDebtUnknownAmountCny"] is None
        assert summary["totals"]["advertisingDebtStatus"] == "PENDING_DATA"
    finally:
        ledger.close()


@pytest.mark.parametrize("fee_status", ("settlement_balance", "not_applicable"))
def test_settlement_balance_withdrawal_is_known_zero_and_never_creates_debt(
    tmp_path, fee_status
):
    ledger = _ledger(tmp_path)
    try:
        record_id = ledger.record_withdrawal_record(
            f"withdrawal-{fee_status}",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-09-05T10:00:00+00:00",
            advertising_fee_status=fee_status,
        )
        row = ledger.list_withdrawal_records(store_alias="owner-store")[0]
        assert row["record_id"] == record_id
        assert row["advertising_fee_status"] == "settlement_balance"
        assert row["advertising_fee_due_cny"] == "0.00"
        assert row["advertising_fee_withheld_cny"] == "0.00"
        assert row["advertising_fee_debt_cny"] == "0.00"
        assert row["outstanding_advertising_fee_cny"] == "0.00"

        ledger.record_advertising_payment_readback(
            "payment-after-settlement-balance",
            "owner-store",
            amount_cny="5.00",
            paid_at="2026-09-06T10:00:00+00:00",
        )
        summary = ledger.advertising_debt_summary(store_alias="owner-store")
        store = summary["stores"][0]
        assert store["advertisingDebtOutstandingCny"] == "0.00"
        assert store["advertisingPaymentAppliedCny"] == "0.00"
        assert store["advertisingPaymentUnappliedCny"] == "5.00"
        assert store["advertisingFeeUnverifiedCount"] == 0
        assert store["advertisingDebtStatus"] == "KNOWN"
        assert summary["totals"]["advertisingDebtStatus"] == "KNOWN"
    finally:
        ledger.close()


def test_settlement_balance_rejects_nonzero_advertising_fee_amounts(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        with pytest.raises(FinanceLedgerError, match="settlement_balance"):
            ledger.record_withdrawal_record(
                "withdrawal-invalid-settlement-balance",
                "owner-store",
                amount_cny="100.00",
                withdrawn_at="2026-09-05T10:00:00+00:00",
                advertising_fee_due_cny="1.00",
                advertising_fee_withheld_cny="1.00",
                advertising_fee_status="settlement_balance",
            )
    finally:
        ledger.close()


def test_fee_status_requires_amount_when_operator_claims_deduction(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        with pytest.raises(FinanceLedgerError, match="advertising_fee_due_cny"):
            ledger.record_withdrawal_record(
                "withdrawal-ambiguous-status",
                "owner-store",
                amount_cny="100.00",
                withdrawn_at="2026-08-01T10:00:00+00:00",
                advertising_fee_status="not_deducted",
            )
    finally:
        ledger.close()


def test_idempotency_and_void_correction_are_append_only(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        record_id = ledger.record_withdrawal_record(
            "withdrawal-1",
            "owner-store",
            amount_cny="100",
            withdrawn_at="2026-08-01T10:00:00+00:00",
            advertising_fee_due_cny="10",
            advertising_fee_withheld_cny="0",
        )
        assert ledger.record_withdrawal_record(
            "withdrawal-1",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-08-01T10:00:00+00:00",
            advertising_fee_due_cny="10.00",
            advertising_fee_withheld_cny="0.00",
        ) == record_id
        with pytest.raises(FinanceLedgerError):
            ledger.record_withdrawal_record(
                "withdrawal-1",
                "owner-store",
                amount_cny="101",
                withdrawn_at="2026-08-01T10:00:00+00:00",
                advertising_fee_due_cny="10",
                advertising_fee_withheld_cny="0",
            )
        ledger.update_withdrawal_record_status(
            record_id,
            "voided",
            occurred_at="2026-08-04T10:00:00+00:00",
            evidence={"reason": "corrected by a replacement record"},
        )
        summary = ledger.advertising_debt_summary(store_alias="owner-store")
        assert summary["stores"][0]["advertisingDebtOutstandingCny"] == "0.00"
    finally:
        ledger.close()


def test_reported_withdrawal_accepts_later_settlement_evidence_without_replacing_record(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        record_id = ledger.record_withdrawal_record(
            "portal-payment-1",
            "owner-store",
            amount_cny="1200.00",
            withdrawn_at="2026-09-01T00:00:00+03:00",
            advertising_fee_status="settlement_balance",
            external_reference="portal-payment-1",
        )
        original = ledger.list_withdrawal_records(store_alias="owner-store")[0]
        assert original["current_status"] == "reported"
        assert original["settled_at"] is None
        assert original["effective_settled_at"] is None

        with pytest.raises(FinanceLedgerError, match="requires settledAt evidence"):
            ledger.update_withdrawal_record_status(
                record_id, "readback_confirmed",
                occurred_at="2026-09-16T10:00:00+03:00",
            )
        with pytest.raises(FinanceLedgerError, match="settledAt cannot precede withdrawn_at"):
            ledger.update_withdrawal_record_status(
                record_id, "readback_confirmed",
                occurred_at="2026-09-16T10:00:00+03:00",
                evidence={"settledAt": "2026-08-31T23:59:00+03:00"},
            )
        assert ledger.list_withdrawal_records(store_alias="owner-store")[0]["current_status"] == "reported"

        evidence = {
            "settledAt": "2026-09-10T00:00:00+03:00",
            "source": "user-provided portal payment-record screenshot",
        }
        for _ in range(2):
            ledger.update_withdrawal_record_status(
                record_id, "readback_confirmed",
                occurred_at="2026-09-16T10:00:00+03:00",
                external_reference="portal-payment-1",
                evidence=evidence,
            )
        rows = ledger.list_withdrawal_records(store_alias="owner-store")
        assert len(rows) == 1
        row = rows[0]
        assert row["record_id"] == record_id
        assert row["current_status"] == "readback_confirmed"
        assert row["settled_at"] is None  # The original immutable request is unchanged.
        assert row["effective_settled_at"] == "2026-09-09T21:00:00+00:00"
        assert row["latest_status_event"]["evidence"]["settledAt"] == row["effective_settled_at"]
        assert ledger.connection.execute(
            "SELECT COUNT(*) FROM withdrawal_record_events WHERE record_id=?", (record_id,)
        ).fetchone()[0] == 2  # Initial request plus one idempotent success event.
    finally:
        ledger.close()


def test_platform_payment_import_rejects_balance_only_rows_and_is_idempotent(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        ledger.record_withdrawal_record(
            "withdrawal-1",
            "owner-store",
            amount_cny="100",
            withdrawn_at="2026-08-01T10:00:00+00:00",
            advertising_fee_due_cny="10",
            advertising_fee_withheld_cny="0",
        )
        rows = [
            {
                "eventType": "advertising_funding",
                "eventId": "p-1",
                "raw": {"id": "p-1", "sum": "4.25", "date": "2026-08-02T10:00:00+00:00"},
            },
            {"eventType": "advertising_funding", "raw": {"balance": "99"}},
            {"eventType": "advertising_expense", "amount": "8"},
        ]
        result = import_advertising_payment_rows(ledger, "owner-store", rows)
        assert result["importedCount"] == 1
        assert result["unresolvedRows"]
        # Re-reading the same API page must not double the credit.
        import_advertising_payment_rows(ledger, "owner-store", rows)
        summary = ledger.advertising_debt_summary(store_alias="owner-store")
        assert summary["stores"][0]["advertisingPaymentReadbackCny"] == "4.25"
        assert summary["stores"][0]["advertisingDebtOutstandingCny"] == "5.75"
    finally:
        ledger.close()


def test_repeated_fifo_reconciliation_does_not_overapply_existing_allocation(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        ledger.record_withdrawal_record(
            "withdrawal-1",
            "owner-store",
            amount_cny="100",
            withdrawn_at="2026-08-01T10:00:00+00:00",
            advertising_fee_due_cny="10",
            advertising_fee_withheld_cny="0",
            advertising_fee_status="not_deducted",
        )
        ledger.record_advertising_payment_readback(
            "payment-1",
            "owner-store",
            amount_cny="6",
            paid_at="2026-08-02T10:00:00+00:00",
        )
        # The allocator is called by both record methods and can also be
        # retriggered by a daily reconciliation.  Existing FIFO rows must be
        # treated as already applied, not subtracted a second time.
        ledger._allocate_unapplied_advertising_payments("owner-store")
        ledger._allocate_unapplied_advertising_payments("owner-store")
        summary = ledger.advertising_debt_summary(store_alias="owner-store")
        assert summary["stores"][0]["advertisingDebtOutstandingCny"] == "4.00"
        assert summary["stores"][0]["advertisingPaymentAppliedCny"] == "6.00"
    finally:
        ledger.close()


def test_extract_payment_supports_db_wrapper_and_requires_timestamp():
    row = {
        "occurred_at": "2026-08-02T10:00:00+00:00",
        "payload_json": '{"eventType":"advertising_funding","eventId":"x","raw":{"sum":5}}',
    }
    parsed = extract_advertising_payment(row)
    assert parsed is not None
    assert parsed["amountCny"] == "5.00"
    assert extract_advertising_payment(
        {"eventType": "advertising_funding", "raw": {"sum": 5}}
    ) is None
