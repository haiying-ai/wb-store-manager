"""Confirmed combined receipts never become fictional store-level cash."""
from __future__ import annotations

from copy import deepcopy
from datetime import date

import pytest

from wb_manager.finance_agency_receipts import (
    AgencyReceiptError,
    prepare_agency_receipt_batches,
    prepare_agency_settlement_adjustments,
)


STORES = ("old-store", "new-store")
PERIODS = [
    {"key": "cumulative", "start": "2026-07-15", "end": "2026-09-16"},
    {"key": "last_month", "start": "2026-08-01", "end": "2026-08-31"},
    {"key": "last_week", "start": "2026-09-07", "end": "2026-09-13"},
    {"key": "yesterday", "start": "2026-09-16", "end": "2026-09-16"},
]


def _batch(**changes):
    row = {
        "eventId": "owner-batch-1",
        "date": "2026-09-16",
        "amountCny": "1234.56",
        "evidence": "owner-confirmed combined agency receipt",
        "stores": ["old-store", "new-store"],
    }
    row.update(changes)
    return row


def _prepare(*batches, legacy=None, through="2026-09-16", periods=PERIODS,
             report_stores=None):
    return prepare_agency_receipt_batches(
        {"agencyReceiptBatches": list(batches), "agencyPayments": legacy or []},
        STORES, through=through, periods=periods, report_stores=report_stores,
    )


def test_confirmed_combined_receipt_is_exact_cash_without_a_fake_store():
    result = _prepare(_batch())
    assert result["allocatedPayments"] == []
    assert result["batches"][0]["amountCny"] == "1234.56"
    assert result["batches"][0]["allocatedCny"] == "0.00"
    assert result["batches"][0]["unallocatedCny"] == "1234.56"
    assert result["unallocatedReceipts"][0]["amountCny"] == "1234.56"
    assert "store" not in result["unallocatedReceipts"][0]
    amounts = result["unallocatedByPeriod"]
    assert amounts["last_month"]["periodCny"] == "0.00"
    assert amounts["last_week"]["cumulativeCny"] == "0.00"
    assert amounts["yesterday"]["periodCny"] == "1234.56"
    assert amounts["cumulative"]["cumulativeCny"] == "1234.56"


def test_partial_allocations_only_create_confirmed_store_cash_events():
    batch = _batch(amountCny="1000.00", date="2026-09-12",
                   allocations=[{"store": "old-store", "amountCny": "400.00"}])
    result = _prepare(batch)
    assert len(result["allocatedPayments"]) == 1
    payment = result["allocatedPayments"][0]
    assert payment["store"] == "old-store"
    assert payment["eventId"] == "batch:owner-batch-1:old-store"
    assert payment["amountCny"] == "400.00"
    assert payment["metadata"]["batchAmountCny"] == "1000.00"
    assert payment["metadata"]["batchAllocatedCny"] == "400.00"
    assert payment["metadata"]["batchUnallocatedCny"] == "600.00"
    assert payment["metadata"]["eligibleStores"] == ["new-store", "old-store"]
    assert result["unallocatedReceipts"][0]["amountCny"] == "600.00"
    assert result["unallocatedByPeriod"]["last_week"]["periodCny"] == "600.00"
    assert result["unallocatedByPeriod"]["yesterday"]["periodCny"] == "0.00"
    assert result["unallocatedByPeriod"]["yesterday"]["cumulativeCny"] == "600.00"
    assert _prepare(batch) == result  # stable identities and ordering on replay


def test_full_allocation_creates_two_store_events_and_no_unallocated_cash():
    batch = _batch(allocations=[
        {"store": "new-store", "amountCny": "500.00"},
        {"store": "old-store", "amountCny": "734.56"},
    ])
    result = _prepare(batch)
    assert [(row["store"], row["amountCny"]) for row in result["allocatedPayments"]] == [
        ("new-store", "500.00"), ("old-store", "734.56")]
    assert result["unallocatedReceipts"] == []
    assert result["unallocatedByPeriod"]["cumulative"]["periodCny"] == "0.00"


def test_future_receipt_is_validated_but_absent_from_historical_payload():
    future = _batch(date="2026-09-17", allocations=[
        {"store": "old-store", "amountCny": "100.00"}])
    current = _prepare(future)
    assert current["batches"] == []
    assert current["allocatedPayments"] == []
    assert current["unallocatedReceipts"] == []
    assert current["unallocatedByPeriod"]["cumulative"]["cumulativeCny"] == "0.00"
    later = _prepare(future, through="2026-09-17", periods=[
        {"key": "cutoff", "start": "2026-09-17", "end": "2026-09-17"}])
    assert later["batches"][0]["eventId"] == "owner-batch-1"
    assert later["allocatedPayments"][0]["amountCny"] == "100.00"
    assert later["unallocatedByPeriod"]["cutoff"]["periodCny"] == "1134.56"


@pytest.mark.parametrize("bad", [
    "1234.567", "1234", "0.00", "-1.00", "01.00", "1e2", 1234.56, None,
])
def test_amount_requires_exact_positive_decimal_cents(bad):
    with pytest.raises(AgencyReceiptError):
        _prepare(_batch(amountCny=bad))
    with pytest.raises(AgencyReceiptError):
        _prepare(_batch(allocations=[{"store": "old-store", "amountCny": bad}]))


def test_duplicate_ids_and_legacy_overlap_are_rejected_even_if_future():
    future = _batch(date="2026-09-17")
    with pytest.raises(AgencyReceiptError, match="duplicates"):
        _prepare(_batch(), future)
    with pytest.raises(AgencyReceiptError, match="duplicates"):
        _prepare(future, legacy=[{"eventId": "owner-batch-1"}])
    with pytest.raises(AgencyReceiptError, match="collides"):
        _prepare(_batch(allocations=[{"store": "old-store", "amountCny": "1.00"}]),
                 legacy=[{"eventId": "batch:owner-batch-1:old-store"}])


def test_overallocation_duplicate_store_and_disabled_store_are_rejected():
    with pytest.raises(AgencyReceiptError, match="exceed"):
        _prepare(_batch(allocations=[
            {"store": "old-store", "amountCny": "1234.56"},
            {"store": "new-store", "amountCny": "0.01"},
        ]))
    with pytest.raises(AgencyReceiptError, match="duplicated"):
        _prepare(_batch(allocations=[
            {"store": "old-store", "amountCny": "1.00"},
            {"store": "old-store", "amountCny": "1.00"},
        ]))
    with pytest.raises(AgencyReceiptError, match="disabled"):
        _prepare(_batch(stores=["old-store", "not-enabled"]))
    with pytest.raises(AgencyReceiptError, match="outside batch"):
        _prepare(_batch(allocations=[{"store": "not-enabled", "amountCny": "1.00"}]))


@pytest.mark.parametrize("changes", [
    {"date": "2026-09-31"}, {"date": "20260916"}, {"eventId": " "},
    {"evidence": ""}, {"stores": []}, {"stores": ["old-store", "old-store"]},
])
def test_invalid_batch_identity_date_evidence_or_stores_is_rejected(changes):
    with pytest.raises(AgencyReceiptError):
        _prepare(_batch(**changes))


def test_period_cutoff_is_inclusive_and_must_be_within_through():
    batch = _batch(date="2026-09-16")
    assert _prepare(batch)["unallocatedByPeriod"]["yesterday"]["periodCny"] == "1234.56"
    bad_periods = deepcopy(PERIODS)
    bad_periods[-1]["end"] = "2026-09-17"
    with pytest.raises(AgencyReceiptError, match="invalid bounds"):
        _prepare(batch, periods=bad_periods)


def test_period_windows_mapping_and_date_cutoff_match_adapter_contract():
    periods = {row["key"]: row for row in PERIODS}
    periods["report"] = {"start": "2026-09-07", "end": "2026-09-16"}
    result = _prepare(_batch(), through=date(2026, 9, 16), periods=periods)
    assert list(result["unallocatedByPeriod"]) == list(periods)
    assert result["unallocatedByPeriod"]["cumulative"]["cumulativeStart"] == "2026-07-15"
    assert result["unallocatedByPeriod"]["yesterday"]["periodCny"] == "1234.56"
    assert result["unallocatedByPeriod"]["report"]["periodCny"] == "1234.56"


def test_combined_unallocated_cash_is_excluded_from_one_store_report():
    batch = _batch(allocations=[
        {"store": "old-store", "amountCny": "200.00"},
        {"store": "new-store", "amountCny": "100.00"},
    ])
    result = _prepare(batch, report_stores=["old-store"])
    assert result["batches"][0]["inReportScope"] is False
    assert {row["store"] for row in result["allocatedPayments"]} == set(STORES)
    assert result["unallocatedReceipts"] == []
    assert result["excludedGroupReceipts"][0]["amountCny"] == "934.56"
    assert result["excludedGroupReceipts"][0]["stores"] == sorted(STORES)
    assert result["unallocatedByPeriod"]["cumulative"]["cumulativeCny"] == "0.00"
    full = _prepare(batch)
    assert full["batches"][0]["inReportScope"] is True
    assert full["unallocatedByPeriod"]["cumulative"]["cumulativeCny"] == "934.56"


def _settlement(*entries, through="2026-09-16", periods=PERIODS, report_stores=STORES):
    decisions = {"agencyReceiptBatches": [_batch()], "agencyPayments": [],
                 "agencySettlementAdjustments": list(entries)}
    receipts = prepare_agency_receipt_batches(decisions, STORES, through=through,
                                               periods=periods, report_stores=report_stores)
    result = prepare_agency_settlement_adjustments(
        decisions, receipts, through=through, periods=periods, report_stores=report_stores)
    return result, receipts


def _tail(**changes):
    entry = {"eventId": "rounding-one-cent", "date": "2026-09-16",
             "amountCny": "0.01", "batchEventId": "owner-batch-1",
             "evidence": "owner confirmed the settlement tail difference"}
    entry.update(changes)
    return entry


def test_one_cent_settlement_is_non_cash_and_dated_to_its_own_period():
    result, receipts = _settlement(_tail(), periods=list(reversed(PERIODS)))
    assert result == _settlement(_tail(), periods=list(reversed(PERIODS)))[0]  # stable replay
    assert receipts["unallocatedReceipts"][0]["amountCny"] == "1234.56"
    assert receipts["unallocatedByPeriod"]["cumulative"]["cumulativeCny"] == "1234.56"
    assert result["entries"][0]["basis"] == "owner_confirmed_non_cash_agency_settlement_tail"
    assert result["entries"][0]["batchEventId"] == "owner-batch-1"
    assert result["byPeriod"]["last_week"]["periodCny"] == "0.00"
    assert result["byPeriod"]["yesterday"]["periodCny"] == "0.01"
    assert result["byPeriod"]["cumulative"]["cumulativeCny"] == "0.01"
    assert result["byPeriod"]["cumulative"]["cumulativeStart"] == "2026-07-15"


def test_cumulative_start_uses_opening_even_when_previous_month_is_earlier():
    periods = [
        {"key": "last_month", "start": "2026-08-01", "end": "2026-08-31"},
        {"key": "yesterday", "start": "2026-09-16", "end": "2026-09-16"},
        {"key": "cumulative", "start": "2026-09-01", "end": "2026-09-16"},
    ]
    adjustment, receipts = _settlement(_tail(), periods=periods)
    assert receipts["unallocatedByPeriod"]["cumulative"]["cumulativeStart"] == "2026-09-01"
    assert adjustment["byPeriod"]["cumulative"]["cumulativeStart"] == "2026-09-01"
    assert adjustment["byPeriod"]["cumulative"]["cumulativeCny"] == "0.01"


def test_future_tail_is_validated_but_excluded_before_its_cutoff():
    future = _tail(date="2026-09-17")
    current, receipts = _settlement(future)
    assert current["entries"] == []
    assert current["byPeriod"]["cumulative"]["cumulativeCny"] == "0.00"
    assert receipts["unallocatedReceipts"][0]["amountCny"] == "1234.56"
    later, _ = _settlement(future, through="2026-09-17", periods=[
        {"key": "cutoff", "start": "2026-09-17", "end": "2026-09-17"}])
    assert later["entries"][0]["amountCny"] == "0.01"


def test_tail_cannot_be_claimed_by_single_store_or_second_event_id():
    one_store, receipts = _settlement(_tail(), report_stores=["old-store"])
    assert one_store["entries"] == []
    assert one_store["excludedGroupAdjustments"][0]["excludedReason"] == "batch_stores_outside_report_scope"
    assert one_store["byPeriod"]["cumulative"]["cumulativeCny"] == "0.00"
    assert receipts["unallocatedReceipts"] == []
    with pytest.raises(AgencyReceiptError, match="already has"):
        _settlement(_tail(), _tail(eventId="second-id"))


@pytest.mark.parametrize("changes", [
    {"amountCny": "0.02"}, {"amountCny": "0.00"}, {"amountCny": 0.01},
    {"date": "2026-09-15"}, {"date": "2026-09-31"},
    {"eventId": "owner-batch-1"}, {"batchEventId": "missing-batch"},
    {"evidence": ""},
])
def test_tail_validates_exact_cent_date_identity_link_and_evidence(changes):
    with pytest.raises(AgencyReceiptError):
        _settlement(_tail(**changes))
