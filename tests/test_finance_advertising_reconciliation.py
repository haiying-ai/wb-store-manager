from copy import deepcopy

import pytest

from wb_manager.finance_sources import (
    advertising_pnl_events,
    classify_advertising_payment_type,
    normalize_advertising_cost_events,
    normalize_advertising_payment_events,
    reconcile_advertising_cost_events,
)


def expense(number=0, amount="36", payment="Баланс", *, store="new-store", campaign=39841968, time="2026-09-04T23:59:59+03:00"):
    return normalize_advertising_cost_events(
        [{"updNum": number, "updTime": time, "updSum": amount,
          "paymentType": payment, "advertId": campaign, "currency": "CNY"}], store
    )[0]


@pytest.mark.parametrize("payment", ["Кэшбэк", "Бонусы", "cashback"])
def test_platform_contribution_is_kept_but_not_seller_expense(payment):
    row = expense(amount="100", payment=payment)
    audited = reconcile_advertising_cost_events([row])
    assert row["pnl"] is False
    assert audited["cashEvents"] == []
    assert audited["byStore"]["new-store"]["platformPromoSpendCny"] == "100.00"
    assert audited["byStore"]["new-store"]["sellerCashSpendCny"] == "0.00"


def test_posted_expense_supersedes_exact_provisional_snapshot_without_mutating_history():
    rows = [expense(), expense(number=313775365), expense(number=313775365)]
    original = deepcopy(rows)
    audited = reconcile_advertising_cost_events(rows)
    assert rows == original
    assert len(audited["events"]) == 1
    assert audited["events"][0]["raw"]["updNum"] == 313775365
    assert audited["byStore"]["new-store"]["sellerCashSpendCny"] == "36.00"
    assert len(audited["duplicateReplacements"]) == 1


def test_same_time_and_amount_different_posted_documents_are_not_merged():
    audited = reconcile_advertising_cost_events([expense(101), expense(102)])
    assert len(audited["events"]) == 2
    assert audited["byStore"]["new-store"]["sellerCashSpendCny"] == "72.00"
    assert not audited["duplicateReplacements"]


def test_multiple_posted_matches_for_provisional_are_visible_and_not_confirmed_cash():
    audited = reconcile_advertising_cost_events([expense(0), expense(101), expense(102)])
    assert len(audited["events"]) == 3
    assert len(audited["ambiguousDuplicates"]) == 1
    assert audited["byStore"]["new-store"]["sellerCashSpendCny"] is None
    assert audited["cashEvents"] == []


@pytest.mark.parametrize("change", [
    {"amount": "37"}, {"campaign": 39938492}, {"store": "owner-store"},
    {"payment": "Кэшбэк"}, {"time": "2026-09-05T23:59:59+03:00"},
])
def test_matching_partial_fingerprint_never_removes_distinct_expense(change):
    audited = reconcile_advertising_cost_events([expense(), expense(101, **change)])
    assert len(audited["events"]) == 2
    assert not audited["duplicateReplacements"]


def test_unknown_source_and_missing_amount_do_not_become_cash_or_zero():
    audited = reconcile_advertising_cost_events([
        expense(101, "10", "unknown-source"), expense(102, None, "Баланс")
    ])
    assert classify_advertising_payment_type(None) == "unknown"
    assert audited["cashEvents"] == []
    totals = audited["byStore"]["new-store"]
    assert totals["sellerCashSpendCny"] is None
    assert totals["unclassifiedSpendCny"] is None
    assert totals["unknownEventCount"] == 2


def test_funding_is_separate_and_prior_pnl_flags_are_reclassified():
    old_promo = expense(0, "100", "Кэшбэк")
    old_promo["pnl"] = True
    funding = normalize_advertising_payment_events([{"id": 1, "sum": 1000, "date": "2026-09-04"}], "new-store")
    audited = reconcile_advertising_cost_events([old_promo, expense(101)], funding)
    assert len(audited["events"]) == 3
    assert len(advertising_pnl_events([old_promo, expense(101)], funding)) == 1
    assert audited["byStore"]["new-store"]["sellerCashSpendCny"] == "36.00"


def test_same_event_identifier_from_different_stores_is_kept_separate():
    one = expense(101, store="owner-store")
    two = expense(101, store="new-store")
    two["eventId"] = one["eventId"]
    audited = reconcile_advertising_cost_events([one, two])
    assert len(audited["events"]) == 2
    assert audited["byStore"]["owner-store"]["sellerCashSpendCny"] == "36.00"
    assert audited["byStore"]["new-store"]["sellerCashSpendCny"] == "36.00"
