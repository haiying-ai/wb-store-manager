"""Endpoint cash positions use confirmed withdrawals and dated WB stocks only."""

from copy import deepcopy

from wb_manager.finance_cash_positions import build_owner_cash_positions


def _snapshot(store, day, balance, *, source="seller_balance", currency="CNY", withdrawable="999999"):
    return {"storeAlias": store, "observedAt": f"{day}T08:00:00Z", "source": source,
            "currency": currency, "accountBalanceCny": balance,
            "platformWithdrawableCny": withdrawable, "eventId": f"{store}:{day}:{source}"}


def _accounts(values):
    return {period: {"actual": {"stores": {store: {"cumulative": {"metrics": {
        metric: {"amount": None, "known_subtotal": str(amount), "complete": False}
        for metric, amount in metrics.items()}}} for store, metrics in stores.items()}}}
            for period, stores in values.items()}


def _sources(*snapshots):
    return {"stores": [{"storeAlias": "old", "openedAt": "2026-07-15"},
                       {"storeAlias": "new", "openedAt": "2026-08-22"}],
            "balanceSnapshots": list(snapshots), "cashReceipts": [], "cashReceiptCoverage": []}


def test_current_known_position_adds_account_stock_and_completed_not_pending_or_withdrawable():
    periods = [{"key": "cumulative", "start": "2026-07-15", "end": "2026-09-16"}]
    accounts = _accounts({"cumulative": {
        "old": {"withdrawal_completed": "1200.00", "withdrawal_pending": "0.00"},
        "new": {"withdrawal_completed": "800.00", "withdrawal_pending": "300.00"}}})
    sources = _sources(_snapshot("old", "2026-09-16", "50.00"),
                       _snapshot("new", "2026-09-16", "-25.00"))
    result = build_owner_cash_positions(sources, accounts, periods, ["old", "new"])
    rows = result["periods"]["cumulative"]
    assert rows["stores"]["old"]["knownSubtotalCny"] == "1250.00"
    assert rows["stores"]["new"]["knownSubtotalCny"] == "775.00"
    assert rows["ALL"]["knownSubtotalCny"] == "2025.00"
    assert rows["ALL"]["pendingWithdrawalKnownSubtotalCny"] == "300.00"
    assert rows["stores"]["new"]["pendingBalanceRelation"] == "unverified"
    assert rows["stores"]["new"]["pendingTreatment"] == "not_added_separately_balance_relation_unverified"
    assert "PENDING_BALANCE_RELATION_UNVERIFIED" in rows["stores"]["new"]["gaps"]
    assert rows["stores"]["old"]["snapshotEventId"] == "old:2026-09-16:seller_balance"
    assert result["isPeriodCashFlow"] is False and result["isBankReceiptEvidence"] is False


def test_each_period_uses_last_snapshot_on_or_before_its_own_moscow_end():
    periods = [{"key": "last_month", "start": "2026-08-01", "end": "2026-08-31"},
               {"key": "last_week", "start": "2026-09-07", "end": "2026-09-13"},
               {"key": "yesterday", "start": "2026-09-16", "end": "2026-09-16"}]
    accounts = _accounts({key: {"old": {"withdrawal_completed": "10", "withdrawal_pending": "0"},
                                  "new": {"withdrawal_completed": "20", "withdrawal_pending": "0"}}
                          for key in ("last_month", "last_week", "yesterday")})
    sources = _sources(_snapshot("old", "2026-08-30", "100"), _snapshot("new", "2026-08-30", "-10"),
                       _snapshot("old", "2026-09-10", "200"), _snapshot("new", "2026-09-10", "-20"),
                       _snapshot("old", "2026-09-17", "999"), _snapshot("new", "2026-09-17", "999"))
    result = build_owner_cash_positions(sources, accounts, periods, ["old", "new"])["periods"]
    assert result["last_month"]["ALL"]["knownSubtotalCny"] == "120.00"
    assert result["last_week"]["ALL"]["knownSubtotalCny"] == "210.00"
    assert result["yesterday"]["ALL"]["knownSubtotalCny"] == "210.00"
    assert result["last_week"]["stores"]["old"]["snapshotDay"] == "2026-09-10"
    assert result["yesterday"]["stores"]["old"]["snapshotFreshness"] == "stale"
    assert "STALE_SELLER_BALANCE_SNAPSHOT" in result["last_week"]["stores"]["old"]["gaps"]
    assert "STALE_SELLER_BALANCE_SNAPSHOT" in result["yesterday"]["stores"]["old"]["gaps"]
    assert result["yesterday"]["ALL"]["knownSubtotalCny"] == "210.00"


def test_missing_prior_snapshot_keeps_known_withdrawals_but_not_combined_total():
    periods = [{"key": "last_month", "start": "2026-08-01", "end": "2026-08-31"}]
    accounts = _accounts({"last_month": {
        "old": {"withdrawal_completed": "12263.81", "withdrawal_pending": "0"},
        "new": {"withdrawal_completed": "0", "withdrawal_pending": "800.00"}}})
    sources = _sources(_snapshot("old", "2026-09-16", "50.00"),
                       _snapshot("new", "2026-09-16", "-25.00"))
    rows = build_owner_cash_positions(sources, accounts, periods, ["old", "new"])["periods"]["last_month"]
    assert rows["stores"]["old"]["completedWithdrawalsKnownSubtotalCny"] == "12263.81"
    assert rows["stores"]["old"]["knownSubtotalCny"] is None
    assert rows["ALL"]["knownSubtotalCny"] is None
    assert rows["ALL"]["completedWithdrawalsKnownSubtotalCny"] == "12263.81"


def test_invalid_source_currency_future_and_other_store_cannot_supply_balance():
    periods = [{"key": "yesterday", "start": "2026-09-16", "end": "2026-09-16"}]
    accounts = _accounts({"yesterday": {"old": {"withdrawal_completed": "10", "withdrawal_pending": "0"}}})
    sources = _sources(_snapshot("old", "2026-09-16", "100", source="advertising_balance"),
                       _snapshot("old", "2026-09-16", "100", currency="RUB"),
                       _snapshot("new", "2026-09-16", "100"),
                       _snapshot("old", "2026-09-17", "100"))
    row = build_owner_cash_positions(sources, accounts, periods, ["old"])["periods"]["yesterday"]["stores"]["old"]
    assert row["accountBalanceCny"] is None
    assert row["knownSubtotalCny"] is None
    assert row["completedWithdrawalsKnownSubtotalCny"] == "10.00"
    assert "MISSING_ELIGIBLE_SELLER_BALANCE_SNAPSHOT" in row["gaps"]


def test_preopening_store_is_not_missing_and_store_order_does_not_change_total():
    periods = [{"key": "july", "start": "2026-07-15", "end": "2026-07-31"}]
    accounts = _accounts({"july": {"old": {"withdrawal_completed": "10", "withdrawal_pending": "0"}}})
    sources = _sources(_snapshot("old", "2026-07-30", "100"))
    sources["stores"][0]["openedAt"] = "2026-07-14T21:00:00Z"  # Jul 15 in Moscow.
    sources["stores"][1]["openedAt"] = "2026-08-21T21:00:00Z"  # Aug 22 in Moscow.
    forward = build_owner_cash_positions(sources, accounts, periods, ["old", "new"])["periods"]["july"]
    reverse = build_owner_cash_positions(deepcopy(sources), accounts, periods, ["new", "old"])["periods"]["july"]
    assert forward["ALL"]["knownSubtotalCny"] == reverse["ALL"]["knownSubtotalCny"] == "110.00"
    assert forward["stores"]["new"]["status"] == "NOT_APPLICABLE"
    assert forward["stores"]["new"]["knownSubtotalCny"] == "0.00"


def test_conflicting_latest_timestamp_has_null_balance_and_both_evidence_ids():
    periods = [{"key": "yesterday", "start": "2026-09-16", "end": "2026-09-16"}]
    accounts = _accounts({"yesterday": {"old": {"withdrawal_completed": "10", "withdrawal_pending": "0"}}})
    first = _snapshot("old", "2026-09-16", "100")
    second = _snapshot("old", "2026-09-16", "200")
    first["eventId"], second["eventId"] = "snapshot-a", "snapshot-b"
    sources = _sources(_snapshot("old", "2026-09-10", "50"), first, second)
    period = build_owner_cash_positions(sources, accounts, periods, ["old"])["periods"]["yesterday"]
    row = period["stores"]["old"]
    assert row["accountBalanceCny"] is None and row["knownSubtotalCny"] is None
    assert row["snapshotEventId"] is None
    assert row["conflictingSnapshotEventIds"] == ["snapshot-a", "snapshot-b"]
    assert row["snapshotDay"] == "2026-09-16"
    assert row["completedWithdrawalsKnownSubtotalCny"] == "10.00"
    assert "CONFLICTING_SELLER_BALANCE_SNAPSHOT" in row["gaps"]
    assert period["ALL"]["knownSubtotalCny"] is None
