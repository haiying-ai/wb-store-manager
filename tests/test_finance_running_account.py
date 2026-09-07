from dataclasses import replace
from decimal import Decimal

import pytest

from wb_manager.finance_running_account import (
    EventConflict, FinanceEvent, METRICS, RunningAccount, RunningAccountError,
    cash_event, materialize_events, refund_event, sale_event,
)


def _sale(identifier="sale-1", *, store="first", day="2026-08-20", **values):
    amounts = {"gross_sales": "2000", "seller_proceeds": "1800", "product_cost": "1000",
               "agency_accrued": "100", "promotion_accrued": "60", "shared_pool_accrued": "60", "return_reserve_accrued": "0",
               "logistics_cost": "0", "other_cost": "0"}
    amounts.update(values)
    return sale_event(store, identifier, day, amounts=amounts, order_id=identifier.removeprefix("sale-"), evidence=(identifier,))


def _complete(account, store="first", start="2026-08-01", end="2026-09-30"):
    account.register_store(store, start)
    for metric in METRICS:
        account.set_coverage(store, "CNY", metric, start, end, True, evidence=("fixture-full-history",))


def _value(report, metric, view="cumulative", store="first"):
    return report["stores"][store][view]["metrics"][metric]["amount"]


def test_ten_orders_then_return_reverses_previous_fee_in_return_period(tmp_path):
    with RunningAccount(tmp_path / "account.sqlite3") as account:
        _complete(account)
        for index in range(10):
            account.upsert_event(_sale(f"sale-{index}"))
        earlier = account.report("2026-08-17", "2026-08-23", ["first"])
        assert _value(earlier, "agency_accrued") == "1000.00"
        snapshot = account.save_report_snapshot(earlier)
        account.upsert_event(refund_event("first", "return-1", "2026-09-02", original_sale_id="sale-1", saleable_return=True))
        current = account.report("2026-08-31", "2026-09-06", ["first"])
        assert _value(current, "agency_accrued", "period") == "-100.00"
        assert _value(current, "agency_accrued") == "900.00"
        assert _value(current, "promotion_accrued", "period") == "-60.00"
        assert _value(current, "product_cost", "period") == "-1000.00"
        assert _value(current, "profit", "period") == "-640.00"
        assert account.read_report_snapshot(snapshot["report_id"]) == earlier


def test_refund_preserves_actual_ads_and_logistics_and_excludes_promo(tmp_path):
    with RunningAccount(tmp_path / "account.sqlite3") as account:
        _complete(account)
        account.upsert_event(_sale(logistics_cost="12"))
        account.upsert_event(cash_event("first", "cash-ad", "2026-09-01", metric="cash_ad_spend", amount="35"))
        account.upsert_event(cash_event("first", "promo-ad", "2026-09-01", metric="promo_credit_spend", amount="500"))
        account.upsert_event(refund_event("first", "refund", "2026-09-02", original_sale_id="sale-1", saleable_return=True,
                                          amounts={"logistics_cost": "9"}))
        report = account.report("2026-08-31", "2026-09-06", ["first"])
        assert _value(report, "product_cost") == "0.00"
        assert _value(report, "cash_ad_spend") == "35.00"
        assert _value(report, "promo_credit_spend") == "500.00"
        assert _value(report, "profit") == "-21.00"
        assert _value(report, "operating_contribution") == "-56.00"
        assert _value(report, "advertising_available") == "-35.00"
        assert _value(report, "advertising_spendable") == "0.00"


def test_cash_balances_are_distinct_from_statistical_proceeds_and_store_isolated(tmp_path):
    with RunningAccount(tmp_path / "account.sqlite3") as account:
        for store in ("first", "second"):
            _complete(account, store)
            account.upsert_event(_sale(store=store))
        for identity, metric, amount in (
            ("payment", "agency_paid", 40), ("receipt", "receipts_received", 900),
            ("request-a", "withdrawal_requested", 300), ("complete-a", "withdrawal_completed", 300),
            ("request-b", "withdrawal_requested", 100), ("request-c", "withdrawal_requested", 50),
            ("cancel-c", "withdrawal_cancelled", 50), ("ad", "cash_ad_spend", 90),
            ("return-pool", "shared_pool_return_cost", 10),
        ):
            account.upsert_event(cash_event("first", identity, "2026-09-01", metric=metric, amount=amount))
        report = account.report("2026-08-31", "2026-09-06", ["first", "second"])
        assert _value(report, "agency_payable") == "60.00"
        assert _value(report, "seller_proceeds") == "1800.00"
        assert _value(report, "receipts_received") == "900.00"
        assert _value(report, "withdrawal_pending") == "100.00"
        assert _value(report, "withdrawal_available") == "500.00"
        assert _value(report, "advertising_available") == "-40.00"
        assert _value(report, "advertising_available", store="second") == "60.00"
        assert _value(report, "advertising_available", store="ALL") == "20.00"
        assert _value(report, "advertising_spendable", store="ALL") == "60.00"


def test_repeat_idempotent_changed_source_revision_explicit_and_history_immutable(tmp_path):
    path = tmp_path / "account.sqlite3"
    with RunningAccount(path) as account:
        _complete(account)
        original = _sale()
        assert account.upsert_event(original, recorded_at="2026-09-01T10:00:00Z")["revision"] == 1
        assert account.refresh()["changed"] is True
        assert account.upsert_event(original)["changed"] is False
        assert account.refresh()["changed"] is False
        changed = _sale(agency_accrued="80")
        with pytest.raises(EventConflict):
            account.upsert_event(changed)
        with pytest.raises(EventConflict):
            account.upsert_event(changed, expected_revision=0, correction_reason="late statement")
        account.upsert_event(changed, expected_revision=1, correction_reason="late statement", recorded_at="2026-09-02T10:00:00Z")
        assert len(account.event_history("first", "sale-1")) == 2
        assert account.refresh()["changed"] is True
        assert account.refresh()["changed"] is False
    with RunningAccount(path) as account:
        assert len(account.current_events()) == 1
        assert _value(account.report("2026-08-01", "2026-09-06", ["first"]), "agency_accrued") == "80.00"


def test_late_evidence_and_revision_asof_does_not_rewrite_publication(tmp_path):
    with RunningAccount(tmp_path / "account.sqlite3") as account:
        _complete(account)
        account.upsert_event(_sale(), recorded_at="2026-09-01T10:00:00Z")
        published = account.report("2026-08-01", "2026-08-31", ["first"])
        saved = account.save_report_snapshot(published)
        account.upsert_event(refund_event("first", "late-return", "2026-08-31", original_sale_id="sale-1", saleable_return=True),
                             recorded_at="2026-09-05T10:00:00Z")
        known_then = account.report("2026-08-01", "2026-08-31", ["first"], known_at="2026-09-02T00:00:00Z")
        assert known_then["stores"]["first"]["cumulative"]["metrics"]["agency_accrued"]["known_subtotal"] == "100.00"
        assert _value(account.report("2026-08-01", "2026-08-31", ["first"]), "agency_accrued") == "0.00"
        assert account.read_report_snapshot(saved["report_id"]) == published


def test_partial_refunds_round_cumulative_target_not_each_fraction():
    original = _sale(agency_accrued="0.05", product_cost="0.05")
    refunds = [refund_event("first", f"refund-{index}", f"2026-09-0{index}", original_sale_id="sale-1",
                            refund_fraction=fraction, saleable_return=True)
               for index, fraction in enumerate(("0.333333", "0.333333", "0.333334"), 1)]
    postings, issues = materialize_events([original, *refunds])
    assert not issues
    assert [row["amounts"]["agency_accrued"] for row in postings[1:]] == ["-0.02", "-0.01", "-0.02"]
    for metric in ("agency_accrued", "product_cost"):
        assert sum(Decimal(row["amounts"][metric]) for row in postings) == 0


@pytest.mark.parametrize("saleable,expected,code", [(True, "0.00", None), (False, "1000.00", None), (None, None, "RETURN_INVENTORY_STATUS_UNKNOWN")])
def test_inventory_evidence_does_not_silently_assume_recovery(tmp_path, saleable, expected, code):
    with RunningAccount(tmp_path / "account.sqlite3") as account:
        _complete(account)
        account.upsert_event(_sale())
        account.upsert_event(refund_event("first", "refund", "2026-09-01", original_sale_id="sale-1", saleable_return=saleable))
        report = account.report("2026-09-01", "2026-09-06", ["first"])
        assert _value(report, "product_cost") == expected
        if code:
            assert code in {issue["code"] for issue in report["issues"]}


def test_missing_coverage_never_becomes_zero_and_partial_known_subtotal_survives(tmp_path):
    with RunningAccount(tmp_path / "account.sqlite3") as account:
        account.register_store("first", "2026-08-01")
        account.upsert_event(_sale())
        report = account.report("2026-09-01", "2026-09-06", ["first", "missing-store"])
        assert _value(report, "agency_paid") is None
        assert _value(report, "agency_accrued") is None
        assert report["stores"]["first"]["cumulative"]["metrics"]["agency_accrued"]["known_subtotal"] == "100.00"
        assert _value(report, "agency_accrued", store="ALL") is None
        assert {issue["code"] for issue in report["issues"]} == {"STORE_OPENING_UNKNOWN"}


def test_coverage_gap_failure_and_repair_are_auditable(tmp_path):
    with RunningAccount(tmp_path / "account.sqlite3") as account:
        _complete(account)
        account.set_coverage("first", "CNY", "agency_paid", "2026-09-03", "2026-09-03", False, evidence=("source-down",))
        assert _value(account.report("2026-09-01", "2026-09-06", ["first"]), "agency_paid", "period") is None
        account.set_coverage("first", "CNY", "agency_paid", "2026-09-03", "2026-09-03", True, evidence=("repair",))
        assert _value(account.report("2026-09-01", "2026-09-06", ["first"]), "agency_paid", "period") == "0.00"


@pytest.mark.parametrize("mutation,code", [
    ({"original_sale_id": "missing"}, "ORIGINAL_SALE_MISSING"),
    ({"currency": "RUB"}, "REFUND_CURRENCY_MISMATCH"),
    ({"event_date": "2026-08-01"}, "REFUND_PRECEDES_ORIGINAL_SALE"),
    ({"order_id": "different"}, "REFUND_ORDER_MISMATCH"),
])
def test_invalid_refund_link_is_open_item_not_unrelated_account_mutation(mutation, code):
    refund = refund_event("first", "return", "2026-09-01", original_sale_id="sale-1", saleable_return=True)
    postings, issues = materialize_events([_sale(), replace(refund, **mutation)])
    assert issues[0]["code"] == code
    returned = next(row for row in postings if row["kind"] == "refund")
    assert returned["amounts"]["agency_accrued"] is None
    assert "cash_ad_spend" not in returned["amounts"]


def test_overrefund_duplicate_identifiers_and_cash_reversal_rejected():
    refund = refund_event("first", "return", "2026-09-01", original_sale_id="sale-1", saleable_return=True)
    _, issues = materialize_events([_sale(), refund, replace(refund, event_id="second-return")])
    assert issues[0]["code"] == "REFUND_EXCEEDS_ORIGINAL_QUANTITY"
    with pytest.raises(EventConflict):
        materialize_events([_sale(), _sale()])
    with pytest.raises(RunningAccountError):
        replace(refund, amounts={"cash_ad_spend": "-10"}).to_dict()
    with pytest.raises(RunningAccountError):
        replace(refund, refund_fraction="nan").to_dict()


def test_business_date_cutoff_and_currency_isolation(tmp_path):
    with RunningAccount(tmp_path / "account.sqlite3") as account:
        _complete(account)
        account.upsert_event(_sale())
        account.upsert_event(_sale("future", day="2026-09-08"))
        account.upsert_event(replace(_sale("native-rubles"), currency="RUB"))
        report = account.report("2026-09-01", "2026-09-07", ["first"])
        assert _value(report, "agency_accrued") == "100.00"
        assert _value(report, "agency_accrued", "period") == "0.00"


def test_unknown_original_amount_stays_unknown_after_refund():
    postings, _ = materialize_events([_sale(seller_proceeds=None), refund_event("first", "refund", "2026-09-01",
                                                                            original_sale_id="sale-1", saleable_return=True)])
    assert postings[-1]["amounts"]["seller_proceeds"] is None


def test_coverage_union_and_gap(tmp_path):
    with RunningAccount(tmp_path / "account.sqlite3") as account:
        account.register_store("first", "2026-09-01")
        account.set_coverage("first", "CNY", "agency_paid", "2026-09-01", "2026-09-02", True)
        account.set_coverage("first", "CNY", "agency_paid", "2026-09-04", "2026-09-06", True)
        assert _value(account.report("2026-09-01", "2026-09-06", ["first"]), "agency_paid") is None
        account.set_coverage("first", "CNY", "agency_paid", "2026-09-03", "2026-09-03", True)
        assert _value(account.report("2026-09-01", "2026-09-06", ["first"]), "agency_paid") == "0.00"


def test_original_correction_recalculates_linked_refund_and_preserves_old_snapshot(tmp_path):
    with RunningAccount(tmp_path / "account.sqlite3") as account:
        _complete(account)
        original = _sale()
        account.upsert_event(original)
        account.upsert_event(refund_event("first", "refund", "2026-09-01", original_sale_id="sale-1", saleable_return=True))
        before = account.report("2026-09-01", "2026-09-06", ["first"])
        saved = account.save_report_snapshot(before)
        account.upsert_event(_sale(agency_accrued="80"), expected_revision=1, correction_reason="authoritative correction")
        after = account.report("2026-09-01", "2026-09-06", ["first"])
        assert _value(after, "agency_accrued", "period") == "-80.00"
        assert _value(after, "agency_accrued") == "0.00"
        assert _value(account.read_report_snapshot(saved["report_id"]), "agency_accrued", "period") == "-100.00"


def test_events_before_registered_opening_remain_visible_and_incomplete(tmp_path):
    with RunningAccount(tmp_path / "account.sqlite3") as account:
        _complete(account, start="2026-09-01")
        account.upsert_event(_sale())
        report = account.report("2026-09-01", "2026-09-06", ["first"])
        assert "EVENT_PRECEDES_STORE_OPENING" in {issue["code"] for issue in report["issues"]}
        assert _value(report, "agency_accrued") is None
        assert report["stores"]["first"]["cumulative"]["metrics"]["agency_accrued"]["known_subtotal"] == "100.00"


def test_report_before_store_opening_is_known_nonoperating_zero(tmp_path):
    with RunningAccount(tmp_path / "account.sqlite3") as account:
        _complete(account, start="2026-09-01")
        report = account.report("2026-08-01", "2026-08-31", ["first"])
        assert _value(report, "agency_accrued") == "0.00"
        assert report["status"] == "FINAL"


def test_projection_version_change_rebuilds_without_new_event(tmp_path):
    with RunningAccount(tmp_path / "account.sqlite3") as account:
        account.upsert_event(_sale())
        assert account.refresh()["changed"]
        with account.connection:
            account.connection.execute("UPDATE ra_projection SET projection_version=0")
        assert account.refresh()["changed"]
        assert not account.refresh()["changed"]


def test_full_refund_reverses_original_profit_and_retains_excess_return_freight():
    with RunningAccount(":memory:") as account:
        _complete(account)
        account.upsert_event(_sale())
        account.upsert_event(refund_event(
            "first", "refund", "2026-09-02", original_sale_id="sale-1", saleable_return=True,
            amounts={"shared_pool_return_cost": "9"},
        ))
        report = account.report("2026-08-31", "2026-09-06", ["first"])
        assert _value(report, "shared_pool_accrued") == "0.00"
        assert _value(report, "excess_return_loss") == "9.00"
        assert _value(report, "profit", "period") == "-649.00"
        assert _value(report, "profit") == "-9.00"
        assert _value(report, "operating_contribution") == "-9.00"
        assert _value(report, "advertising_available") == "-9.00"


def test_pool_excess_period_deltas_reconcile_across_weekly_and_cumulative_reports():
    with RunningAccount(":memory:") as account:
        _complete(account)
        account.upsert_event(_sale())
        account.upsert_event(cash_event("first", "freight", "2026-09-02", metric="shared_pool_return_cost", amount="80"))
        account.upsert_event(_sale("sale-2", day="2026-09-09"))
        first = account.report("2026-08-17", "2026-08-30", ["first"])
        second = account.report("2026-08-31", "2026-09-06", ["first"])
        third = account.report("2026-09-07", "2026-09-13", ["first"])
        assert _value(first, "profit", "period") == "640.00"
        assert _value(second, "excess_return_loss", "period") == "20.00"
        assert _value(second, "profit", "period") == "-20.00"
        assert _value(third, "excess_return_loss", "period") == "-20.00"
        assert _value(third, "profit", "period") == "660.00"
        assert sum(Decimal(_value(item, "profit", "period")) for item in (first, second, third)) == Decimal(_value(third, "profit"))
        assert _value(third, "profit") == "1280.00"


def test_separate_return_risk_covers_only_return_cost_without_double_charging():
    with RunningAccount(":memory:") as account:
        _complete(account)
        account.upsert_event(_sale(shared_pool_accrued="0", return_reserve_accrued="40"))
        account.upsert_event(cash_event("first", "return-a", "2026-09-02", metric="separate_return_cost", amount="30"))
        first = account.report("2026-08-31", "2026-09-06", ["first"])
        assert _value(first, "excess_return_loss") == "0.00"
        assert _value(first, "profit") == "600.00"
        assert _value(first, "operating_contribution") == "670.00"
        assert _value(first, "advertising_available") == "60.00"
        account.upsert_event(cash_event("first", "return-b", "2026-09-08", metric="separate_return_cost", amount="25"))
        second = account.report("2026-09-07", "2026-09-13", ["first"])
        assert _value(second, "separate_return_excess") == "15.00"
        assert _value(second, "profit") == "585.00"
        assert _value(second, "operating_contribution") == "645.00"
        assert _value(second, "advertising_available") == "60.00"


def test_pooled_excess_is_store_isolated_and_separate_promotion_cannot_cover_legacy_freight():
    with RunningAccount(":memory:") as account:
        for store in ("first", "second"):
            _complete(account, store)
        account.upsert_event(_sale())
        account.upsert_event(_sale("separate", promotion_accrued="600", shared_pool_accrued="0"))
        account.upsert_event(_sale("other-store", store="second", promotion_accrued="100", shared_pool_accrued="100"))
        account.upsert_event(cash_event("first", "return", "2026-09-02", metric="shared_pool_return_cost", amount="80"))
        report = account.report("2026-08-31", "2026-09-06", ["first", "second"])
        assert _value(report, "promotion_accrued") == "660.00"
        assert _value(report, "shared_return_excess") == "20.00"
        assert _value(report, "shared_return_excess", store="second") == "0.00"
        assert _value(report, "shared_return_excess", store="ALL") == "20.00"
        assert _value(report, "excess_return_loss", "period", "ALL") == "20.00"
        assert Decimal(_value(report, "profit", store="ALL")) == sum(
            Decimal(_value(report, "profit", store=store)) for store in ("first", "second")
        )


@pytest.mark.parametrize("known_at", [None, "2026-09-20T00:00:00Z"])
def test_future_refund_issue_does_not_change_earlier_cutoff_report_status(known_at):
    with RunningAccount(":memory:") as account:
        account.register_store("first", "2026-08-01")
        for metric in METRICS:
            account.set_coverage("first", "CNY", metric, "2026-08-01", "2026-09-30", True,
                                 recorded_at="2026-09-01T00:00:00Z")
        account.upsert_event(_sale(), recorded_at="2026-09-01T00:00:00Z")
        account.upsert_event(refund_event("first", "future-return", "2026-09-10", original_sale_id="sale-1"),
                             recorded_at="2026-09-11T00:00:00Z")
        earlier = account.report("2026-08-01", "2026-08-31", ["first"], known_at=known_at)
        assert earlier["status"] == "FINAL"
        assert earlier["issues"] == []
        assert _value(earlier, "profit") == "640.00"
        current = account.report("2026-09-01", "2026-09-13", ["first"], known_at=known_at)
        assert current["status"] == "PENDING_DATA"
        assert current["issues"][0]["event_date"] == "2026-09-10"
