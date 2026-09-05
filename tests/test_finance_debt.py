from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from wb_manager.finance_debt import (
    import_advertising_payment_events_from_ledger,
    import_advertising_payment_rows,
)
from wb_manager.finance_ledger import FinanceLedger, FinanceLedgerError
from wb_manager.finance_sync import _persist_source_rows


CUTOVER = "2026-09-05"
FINANCIAL_TIMEZONE = "Europe/Moscow"


def ledger_with_store(tmp_path) -> FinanceLedger:
    ledger = FinanceLedger(tmp_path / "finance.sqlite3")
    ledger.register_store("owner-store", scope_id="owner-scope")
    return ledger


def record_withdrawal(ledger: FinanceLedger, *args, **kwargs) -> str:
    kwargs.setdefault("advertising_fee_cutover", CUTOVER)
    kwargs.setdefault("financial_timezone", FINANCIAL_TIMEZONE)
    return ledger.record_withdrawal_record(*args, **kwargs)


def test_post_cutover_settlement_balance_is_known_zero_and_preserves_legacy_debt(
    tmp_path,
):
    ledger = ledger_with_store(tmp_path)
    try:
        record_withdrawal(
            ledger,
            "legacy",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-09-04T10:00:00+03:00",
            advertising_fee_due_cny="12.00",
            advertising_fee_withheld_cny="2.00",
            advertising_fee_status="partial",
        )
        record_withdrawal(
            ledger,
            "current",
            "owner-store",
            amount_cny="200.00",
            withdrawn_at="2026-09-05T10:00:00+03:00",
            advertising_fee_status="settlement_balance",
            advertising_fee_cutover=CUTOVER,
            financial_timezone=FINANCIAL_TIMEZONE,
        )

        rows = ledger.list_withdrawal_records(store_alias="owner-store")
        legacy, current = rows
        assert legacy["advertising_fee_debt_cny"] == "10.00"
        assert current["advertising_fee_status"] == "settlement_balance"
        assert current["advertising_fee_due_cny"] == "0.00"
        assert current["advertising_fee_withheld_cny"] == "0.00"
        assert current["advertising_fee_debt_cny"] == "0.00"
        assert current["outstanding_advertising_fee_cny"] == "0.00"
        assert current["evidence"]["advertisingFeeCutover"] == CUTOVER
        assert current["evidence"]["financialTimezone"] == FINANCIAL_TIMEZONE
        summary = ledger.advertising_debt_summary(store_alias="owner-store")
        assert summary["stores"][0]["advertisingFeeDueCny"] == "12.00"
        assert summary["stores"][0]["advertisingFeeWithheldCny"] == "2.00"
        assert summary["stores"][0]["advertisingFeeDebtCny"] == "10.00"
        assert summary["stores"][0]["advertisingDebtOutstandingCny"] == "10.00"
        assert summary["totals"]["advertisingFeeDueCny"] == "12.00"
        assert summary["totals"]["advertisingFeeWithheldCny"] == "2.00"
        assert summary["totals"]["advertisingFeeDebtCny"] == "10.00"
    finally:
        ledger.close()


def test_settlement_balance_rejects_nonzero_fee_amounts(tmp_path):
    ledger = ledger_with_store(tmp_path)
    try:
        with pytest.raises(FinanceLedgerError, match="settlement_balance"):
            record_withdrawal(
                ledger,
                "invalid-current",
                "owner-store",
                amount_cny="100.00",
                withdrawn_at="2026-09-05T10:00:00+03:00",
                advertising_fee_due_cny="1.00",
                advertising_fee_withheld_cny="1.00",
                advertising_fee_status="settlement_balance",
                advertising_fee_cutover=CUTOVER,
                financial_timezone=FINANCIAL_TIMEZONE,
            )
    finally:
        ledger.close()


def test_actual_payment_fifo_only_settles_legacy_debt(tmp_path):
    ledger = ledger_with_store(tmp_path)
    try:
        record_withdrawal(
            ledger,
            "legacy",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-09-04T10:00:00+03:00",
            advertising_fee_due_cny="10.00",
            advertising_fee_withheld_cny="0.00",
            advertising_fee_status="not_deducted",
        )
        record_withdrawal(
            ledger,
            "current",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-09-05T10:00:00+03:00",
            advertising_fee_status="settlement_balance",
            advertising_fee_cutover=CUTOVER,
            financial_timezone=FINANCIAL_TIMEZONE,
        )
        result = import_advertising_payment_rows(
            ledger,
            "owner-store",
            [
                {
                    "eventType": "advertising_funding",
                    "paymentPurpose": "legacy-advertising-debt-payment",
                    "eventId": "payment-1",
                    "amount": "4.00",
                    "paidAt": "2026-09-06T10:00:00+03:00",
                    "statusId": 1,
                    "type": 3,
                }
            ],
        )

        assert result["importedCount"] == 1
        rows = ledger.list_withdrawal_records(store_alias="owner-store")
        assert rows[0]["outstanding_advertising_fee_cny"] == "6.00"
        assert rows[1]["outstanding_advertising_fee_cny"] == "0.00"
        assert Decimal(
            result["debt"]["stores"][0]["advertisingPaymentAppliedCny"]
        ) == Decimal("4.00")
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("status", "due", "withheld"),
    [
        ("deducted", "10", "9"),
        ("deducted", "0", "0"),
        ("not_deducted", "10", "1"),
        ("partial", "10", "0"),
        ("partial", "10", "10"),
    ],
)
def test_legacy_fee_status_amount_relationships_are_strict(
    tmp_path, status, due, withheld
):
    ledger = ledger_with_store(tmp_path)
    try:
        with pytest.raises(FinanceLedgerError, match=status):
            record_withdrawal(
                ledger,
                f"invalid-{status}-{due}-{withheld}",
                "owner-store",
                amount_cny="100.00",
                withdrawn_at="2026-09-04T10:00:00+03:00",
                advertising_fee_due_cny=due,
                advertising_fee_withheld_cny=withheld,
                advertising_fee_status=status,
            )
    finally:
        ledger.close()


def test_settlement_balance_requires_authoritative_cutover_and_timezone(tmp_path):
    ledger = ledger_with_store(tmp_path)
    try:
        with pytest.raises(FinanceLedgerError, match="authoritative"):
            ledger.record_withdrawal_record(
                "missing-cutover",
                "owner-store",
                amount_cny="100.00",
                withdrawn_at="2026-09-05T00:00:00+03:00",
                advertising_fee_due_cny="10.00",
                advertising_fee_withheld_cny="0.00",
                advertising_fee_status="not_deducted",
            )
        with pytest.raises(FinanceLedgerError, match="pre-cutover"):
            record_withdrawal(
                ledger,
                "before-cutover",
                "owner-store",
                amount_cny="100.00",
                withdrawn_at="2026-09-04T23:59:59+03:00",
                advertising_fee_status="settlement_balance",
                advertising_fee_cutover=CUTOVER,
                financial_timezone=FINANCIAL_TIMEZONE,
            )
        with pytest.raises(FinanceLedgerError, match="must use settlement_balance"):
            record_withdrawal(
                ledger,
                "wrong-post-cutover-status",
                "owner-store",
                amount_cny="100.00",
                withdrawn_at="2026-09-05T00:00:00+03:00",
                advertising_fee_due_cny="10",
                advertising_fee_withheld_cny="0",
                advertising_fee_status="not_deducted",
                advertising_fee_cutover=CUTOVER,
                financial_timezone=FINANCIAL_TIMEZONE,
            )
    finally:
        ledger.close()


def test_ordinary_type_one_campaign_top_up_never_repays_legacy_debt(tmp_path):
    ledger = ledger_with_store(tmp_path)
    try:
        record_withdrawal(
            ledger,
            "legacy",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-09-04T10:00:00+03:00",
            advertising_fee_due_cny="10.00",
            advertising_fee_withheld_cny="0.00",
            advertising_fee_status="not_deducted",
        )
        result = import_advertising_payment_rows(
            ledger,
            "owner-store",
            [
                {
                    "eventType": "advertising_funding",
                    "eventId": "ordinary-balance-allocation",
                    "amount": "10.00",
                    "paidAt": "2026-09-06",
                    "type": 1,
                    "statusId": 1,
                    "cardStatus": "succeeded",
                },
                {
                    "eventType": "advertising_funding",
                    "eventId": "contradictory-marker",
                    "amount": "10.00",
                    "paidAt": "2026-09-06",
                    "type": 1,
                    "statusId": 1,
                    "legacyDebtPayment": True,
                },
                {
                    "eventType": "legacy_debt_payment",
                    "eventId": "missing-success-status",
                    "amount": "10.00",
                    "paidAt": "2026-09-06",
                    "type": 3,
                },
                {
                    "eventType": "legacy_debt_payment",
                    "eventId": "failed-status",
                    "amount": "10.00",
                    "paidAt": "2026-09-06",
                    "type": 3,
                    "statusId": 2,
                    "cardStatus": "failed",
                },
                {
                    "eventType": "legacy_debt_payment",
                    "eventId": "missing-payment-type",
                    "amount": "10.00",
                    "paidAt": "2026-09-06",
                    "statusId": 1,
                },
            ],
        )

        assert result["importedCount"] == 0
        assert len(result["unresolvedRows"]) == 5
        assert result["debt"]["stores"][0]["advertisingDebtOutstandingCny"] == "10.00"
    finally:
        ledger.close()


def test_raw_payment_date_only_uses_moscow_day_end_to_end(tmp_path):
    ledger = ledger_with_store(tmp_path)
    try:
        record_withdrawal(
            ledger,
            "legacy",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-09-04T10:00:00+03:00",
            advertising_fee_due_cny="10.00",
            advertising_fee_withheld_cny="0.00",
            advertising_fee_status="not_deducted",
        )
        inserted = _persist_source_rows(
            ledger,
            "owner-store",
            "advertising_payments",
            [
                {
                    "eventId": "raw-date-only",
                    "eventType": "advertising_funding",
                    "pnl": False,
                    "occurredAt": "2026-09-01-not-a-timestamp",
                    "raw": {
                        "sum": "4.00",
                        "date": "2026-09-06",
                        "type": 3,
                        "statusId": 1,
                        "cardStatus": "succeeded",
                        "paymentPurpose": "legacy_debt_payment",
                    },
                }
            ],
            fetched_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        )
        raw_row = ledger.connection.execute(
            "SELECT occurred_at, payload_json FROM raw_ad"
        ).fetchone()
        result = import_advertising_payment_events_from_ledger(
            ledger, "owner-store"
        )

        assert inserted == 1
        assert raw_row["occurred_at"] == "2026-09-05T21:00:00+00:00"
        assert json.loads(raw_row["payload_json"])["raw"]["date"] == "2026-09-06"
        assert result["importedCount"] == 1
        assert result["debt"]["stores"][0]["advertisingDebtOutstandingCny"] == "6.00"
    finally:
        ledger.close()


def test_invalid_payload_time_falls_back_to_raw_ad_occurred_at(tmp_path):
    ledger = ledger_with_store(tmp_path)
    try:
        record_withdrawal(
            ledger,
            "legacy",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-09-04T10:00:00+03:00",
            advertising_fee_due_cny="5.00",
            advertising_fee_withheld_cny="0.00",
            advertising_fee_status="not_deducted",
        )
        ledger.upsert_ad(
            "owner-store",
            {"eventId": "invalid-payload-time"},
            {
                "eventId": "invalid-payload-time",
                "eventType": "advertising_funding",
                "amount": "5.00",
                "paidAt": "not-a-date",
                "paymentPurpose": "legacy_debt_payment",
                "statusId": 1,
                "type": 3,
            },
            occurred_at="2026-09-06T10:00:00+03:00",
            fetched_at="2026-09-07T00:00:00+00:00",
        )

        result = import_advertising_payment_events_from_ledger(
            ledger, "owner-store"
        )

        assert result["importedCount"] == 1
        assert result["debt"]["stores"][0]["advertisingDebtOutstandingCny"] == "0.00"
    finally:
        ledger.close()


def test_voided_withdrawal_is_terminal_and_payment_is_not_double_applied(tmp_path):
    ledger = ledger_with_store(tmp_path)
    try:
        first = record_withdrawal(
            ledger,
            "legacy-first",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-09-02T10:00:00+03:00",
            advertising_fee_due_cny="10.00",
            advertising_fee_withheld_cny="0.00",
            advertising_fee_status="not_deducted",
        )
        record_withdrawal(
            ledger,
            "legacy-second",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-09-03T10:00:00+03:00",
            advertising_fee_due_cny="10.00",
            advertising_fee_withheld_cny="0.00",
            advertising_fee_status="not_deducted",
        )
        payment = ledger.record_advertising_payment_readback(
            "legacy-payment",
            "owner-store",
            amount_cny="10.00",
            paid_at="2026-09-04T10:00:00+03:00",
            evidence={"legacyDebtPayment": True},
            legacy_debt_payment=True,
        )
        ledger.update_withdrawal_record_status(
            first,
            "voided",
            occurred_at="2026-09-07T10:00:00+03:00",
        )

        with pytest.raises(FinanceLedgerError, match="terminal"):
            ledger.update_withdrawal_record_status(
                first,
                "readback_confirmed",
                occurred_at="2026-09-08T10:00:00+03:00",
            )

        before_void = ledger.advertising_debt_summary(
            store_alias="owner-store",
            as_of="2026-09-05T10:00:00+03:00",
        )["stores"][0]
        after_void = ledger.advertising_debt_summary(
            store_alias="owner-store",
            as_of="2026-09-08T10:00:00+03:00",
        )["stores"][0]
        rows = ledger.list_withdrawal_records(store_alias="owner-store")
        assert before_void["advertisingFeeSettledCny"] == "10.00"
        assert before_void["advertisingDebtOutstandingCny"] == "10.00"
        assert after_void["advertisingFeeSettledCny"] == "10.00"
        assert after_void["advertisingDebtOutstandingCny"] == "0.00"
        assert len(rows) == 1
        assert rows[0]["outstanding_advertising_fee_cny"] == "0.00"
        payment_row = ledger.list_advertising_payment_readbacks(
            store_alias="owner-store"
        )[0]
        assert payment_row["payment_id"] == payment
        assert payment_row["applied_cny"] == "10.00"
    finally:
        ledger.close()


def test_void_reallocation_can_append_to_an_existing_payment_debt_pair(tmp_path):
    ledger = ledger_with_store(tmp_path)
    try:
        first = record_withdrawal(
            ledger,
            "legacy-first-partial",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-09-02T10:00:00+03:00",
            advertising_fee_due_cny="5.00",
            advertising_fee_withheld_cny="0.00",
            advertising_fee_status="not_deducted",
        )
        second = record_withdrawal(
            ledger,
            "legacy-second-partial",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-09-03T10:00:00+03:00",
            advertising_fee_due_cny="10.00",
            advertising_fee_withheld_cny="0.00",
            advertising_fee_status="not_deducted",
        )
        payment = ledger.record_advertising_payment_readback(
            "legacy-payment-partial",
            "owner-store",
            amount_cny="10.00",
            paid_at="2026-09-04T10:00:00+03:00",
            evidence={"legacyDebtPayment": True},
            legacy_debt_payment=True,
        )
        ledger.update_withdrawal_record_status(
            first,
            "voided",
            occurred_at="2026-09-07T10:00:00+03:00",
        )

        allocations = ledger.connection.execute(
            "SELECT amount_cny, allocated_at, evidence_json "
            "FROM advertising_debt_allocations "
            "WHERE payment_id=? AND withdrawal_record_id=? "
            "ORDER BY allocated_at, allocation_id",
            (payment, second),
        ).fetchall()
        before_void = ledger.advertising_debt_summary(
            store_alias="owner-store",
            as_of="2026-09-05T10:00:00+03:00",
        )["stores"][0]
        after_void = ledger.advertising_debt_summary(
            store_alias="owner-store",
            as_of="2026-09-08T10:00:00+03:00",
        )["stores"][0]
        payment_row = ledger.list_advertising_payment_readbacks(
            store_alias="owner-store"
        )[0]

        assert [row["amount_cny"] for row in allocations] == ["5.00", "5.00"]
        assert [row["allocated_at"] for row in allocations] == [
            "2026-09-04T07:00:00+00:00",
            "2026-09-07T07:00:00+00:00",
        ]
        assert [
            json.loads(row["evidence_json"])["allocationSequence"]
            for row in allocations
        ] == [1, 2]
        assert before_void["advertisingFeeSettledCny"] == "10.00"
        assert before_void["advertisingDebtOutstandingCny"] == "5.00"
        assert after_void["advertisingFeeSettledCny"] == "10.00"
        assert after_void["advertisingDebtOutstandingCny"] == "0.00"
        assert payment_row["applied_cny"] == "10.00"
        assert payment_row["unapplied_cny"] == "0.00"
    finally:
        ledger.close()


def test_allocation_uses_payment_business_time_and_keeps_reconcile_time(tmp_path):
    ledger = ledger_with_store(tmp_path)
    try:
        record_withdrawal(
            ledger,
            "legacy",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-09-02T10:00:00+03:00",
            advertising_fee_due_cny="10.00",
            advertising_fee_withheld_cny="0.00",
            advertising_fee_status="not_deducted",
        )
        ledger.record_advertising_payment_readback(
            "late-readback",
            "owner-store",
            amount_cny="4.00",
            paid_at="2026-09-03T10:00:00+03:00",
            evidence={"legacyDebtPayment": True},
            legacy_debt_payment=True,
        )

        allocation = ledger.connection.execute(
            "SELECT allocated_at, evidence_json FROM advertising_debt_allocations"
        ).fetchone()
        historical = ledger.advertising_debt_summary(
            store_alias="owner-store",
            as_of="2026-09-03T10:00:01+03:00",
        )

        assert allocation["allocated_at"] == "2026-09-03T07:00:00+00:00"
        assert json.loads(allocation["evidence_json"])["reconciledAt"]
        assert historical["stores"][0]["advertisingFeeSettledCny"] == "4.00"
        assert historical["stores"][0]["advertisingDebtOutstandingCny"] == "6.00"
    finally:
        ledger.close()


def test_idempotent_withdrawal_replay_retries_allocator(tmp_path, monkeypatch):
    ledger = ledger_with_store(tmp_path)
    try:
        ledger.record_advertising_payment_readback(
            "payment-before-debt",
            "owner-store",
            amount_cny="4.00",
            paid_at="2026-09-04T10:00:00+03:00",
            evidence={"legacyDebtPayment": True},
            legacy_debt_payment=True,
        )
        allocator = ledger._allocate_unapplied_advertising_payments
        monkeypatch.setattr(
            ledger, "_allocate_unapplied_advertising_payments", lambda _alias: None
        )
        kwargs = {
            "amount_cny": "100.00",
            "withdrawn_at": "2026-09-03T10:00:00+03:00",
            "advertising_fee_due_cny": "10.00",
            "advertising_fee_withheld_cny": "0.00",
            "advertising_fee_status": "not_deducted",
        }
        record_withdrawal(ledger, "late-debt", "owner-store", **kwargs)
        monkeypatch.setattr(
            ledger, "_allocate_unapplied_advertising_payments", allocator
        )

        record_withdrawal(ledger, "late-debt", "owner-store", **kwargs)

        row = ledger.list_withdrawal_records(store_alias="owner-store")[0]
        assert row["outstanding_advertising_fee_cny"] == "6.00"
    finally:
        ledger.close()


def test_idempotent_payment_replay_retries_allocator(tmp_path, monkeypatch):
    ledger = ledger_with_store(tmp_path)
    try:
        record_withdrawal(
            ledger,
            "legacy",
            "owner-store",
            amount_cny="100.00",
            withdrawn_at="2026-09-03T10:00:00+03:00",
            advertising_fee_due_cny="10.00",
            advertising_fee_withheld_cny="0.00",
            advertising_fee_status="not_deducted",
        )
        allocator = ledger._allocate_unapplied_advertising_payments
        monkeypatch.setattr(
            ledger, "_allocate_unapplied_advertising_payments", lambda _alias: None
        )
        kwargs = {
            "amount_cny": "4.00",
            "paid_at": "2026-09-04T10:00:00+03:00",
            "evidence": {"legacyDebtPayment": True},
            "legacy_debt_payment": True,
        }
        ledger.record_advertising_payment_readback(
            "late-payment", "owner-store", **kwargs
        )
        monkeypatch.setattr(
            ledger, "_allocate_unapplied_advertising_payments", allocator
        )

        ledger.record_advertising_payment_readback(
            "late-payment", "owner-store", **kwargs
        )

        row = ledger.list_withdrawal_records(store_alias="owner-store")[0]
        assert row["outstanding_advertising_fee_cny"] == "6.00"
    finally:
        ledger.close()
