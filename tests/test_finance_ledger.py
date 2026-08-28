from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from wb_manager.finance_costs import CostVersionRow
from wb_manager.finance_ledger import (
    FinanceLedger,
    FinanceLedgerError,
    MissingCostVersionError,
    OverlappingCostVersionError,
    ReportImmutableError,
)


CN_TZ = timezone(timedelta(hours=8))


def moment(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=CN_TZ)


@pytest.fixture
def ledger(tmp_path):
    with FinanceLedger(tmp_path / "finance.sqlite3") as value:
        value.register_store(
            "test-store",
            scope_id="test-scope",
            at=moment(2026, 1, 1),
        )
        yield value


def test_schema_covers_financial_runtime_state(ledger):
    expected = {
        "store_registry",
        "raw_finance",
        "raw_sales",
        "raw_returns",
        "raw_ad",
        "raw_balance",
        "sync_runs",
        "sync_watermarks",
        "sync_gaps",
        "cost_versions",
        "order_profit",
        "reports",
        "report_orders",
        "funding_actions",
        "locks",
    }
    actual = {
        row["name"]
        for row in ledger.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }

    assert expected <= actual


def test_finance_and_generic_events_are_idempotent_and_money_is_text(ledger):
    first = ledger.upsert_finance(
        "test-store",
        101,
        {"rrdId": 101, "status": "first"},
        occurred_at=moment(2026, 1, 2),
        buyer_paid_cny=Decimal("12.30"),
        return_logistics_cny=Decimal("1.20"),
        fetched_at=moment(2026, 1, 3),
    )
    second = ledger.upsert_finance(
        "test-store",
        101,
        {"rrdId": 101, "status": "first"},
        occurred_at=moment(2026, 1, 2),
        buyer_paid_cny=Decimal("12.30"),
        return_logistics_cny=Decimal("1.20"),
        fetched_at=moment(2026, 1, 3),
    )

    assert first.inserted is True
    assert second.inserted is False
    row = ledger.connection.execute(
        """
        SELECT COUNT(*) AS count, buyer_paid_cny,
               typeof(buyer_paid_cny) AS buyer_type,
               typeof(return_logistics_cny) AS return_type
        FROM raw_finance WHERE store_alias='test-store' AND rrd_id='101'
        """
    ).fetchone()
    assert row["count"] == 1
    assert row["buyer_paid_cny"] == "12.30"
    assert row["buyer_type"] == "text"
    assert row["return_type"] == "text"

    sale_identity = {"saleId": "sale-test-1"}
    sale_first = ledger.upsert_sale(
        "test-store",
        sale_identity,
        {"saleId": "sale-test-1", "state": "created"},
        occurred_at=moment(2026, 1, 2),
        sale_id="sale-test-1",
        buyer_paid_cny="10.00",
    )
    sale_updated = ledger.upsert_sale(
        "test-store",
        sale_identity,
        {"saleId": "sale-test-1", "state": "complete"},
        occurred_at=moment(2026, 1, 2),
        sale_id="sale-test-1",
        buyer_paid_cny="10.00",
    )

    assert sale_first.key == sale_updated.key
    assert sale_first.inserted is True
    assert sale_updated.inserted is False
    assert ledger.connection.execute(
        "SELECT COUNT(*) FROM raw_sales WHERE store_alias='test-store'"
    ).fetchone()[0] == 1
    assert "complete" in ledger.connection.execute(
        "SELECT payload_json FROM raw_sales WHERE store_alias='test-store'"
    ).fetchone()[0]


def test_return_ad_and_balance_events_preserve_decimal_text(ledger):
    ledger.upsert_return(
        "test-store",
        {"returnId": "return-test-1"},
        {"returnId": "return-test-1"},
        occurred_at=moment(2026, 1, 4),
        return_id="return-test-1",
        actual_logistics_cny=Decimal("0.80"),
        refund_cny=Decimal("9.20"),
    )
    ledger.upsert_ad(
        "test-store",
        {"campaignId": "campaign-test-1", "day": "2026-01-04"},
        {"campaignId": "campaign-test-1"},
        occurred_at=moment(2026, 1, 4),
        campaign_id="campaign-test-1",
        spend_cny=Decimal("0.30"),
    )
    ledger.upsert_balance(
        "test-store",
        {"observedAt": "2026-01-04T09:00:00+08:00"},
        {"status": "captured"},
        observed_at=moment(2026, 1, 4, 9),
        account_balance_cny=Decimal("3.30"),
        withdrawable_balance_cny=Decimal("2.20"),
        ad_balance_cny=Decimal("1.10"),
    )

    assert ledger.connection.execute(
        "SELECT actual_logistics_cny, typeof(actual_logistics_cny) FROM raw_returns"
    ).fetchone()[:] == ("0.80", "text")
    assert ledger.connection.execute(
        "SELECT spend_cny, typeof(spend_cny) FROM raw_ad"
    ).fetchone()[:] == ("0.30", "text")
    assert ledger.connection.execute(
        "SELECT ad_balance_cny, typeof(ad_balance_cny) FROM raw_balance"
    ).fetchone()[:] == ("1.10", "text")


def test_failed_sync_does_not_advance_watermark_and_records_gap(ledger):
    failed = ledger.start_sync(
        "test-store",
        "finance",
        moment(2026, 1, 1),
        moment(2026, 1, 2),
        run_id="failed-run",
    )
    ledger.finish_sync(
        failed,
        "failed",
        watermark_at=moment(2026, 1, 2),
        error_text="temporary test failure",
    )

    assert ledger.get_watermark("test-store", "finance") is None
    gaps = ledger.list_open_gaps("test-store")
    assert len(gaps) == 1
    assert gaps[0]["source"] == "finance"
    assert gaps[0]["reason"] == "temporary test failure"

    complete = ledger.start_sync(
        "test-store",
        "finance",
        moment(2026, 1, 1),
        moment(2026, 1, 2),
        run_id="complete-run",
    )
    ledger.finish_sync(
        complete,
        "complete",
        watermark_at=moment(2026, 1, 2),
    )

    assert ledger.get_watermark("test-store", "finance") == (
        "2026-01-01T16:00:00+00:00"
    )
    assert ledger.list_open_gaps("test-store") == []


def test_partial_sync_creates_an_idempotent_open_gap(ledger):
    run = ledger.start_sync(
        "test-store",
        "returns",
        moment(2026, 1, 3),
        moment(2026, 1, 4),
    )
    ledger.finish_sync(run, "partial", error_text="one page unavailable")
    first_gap = ledger.list_open_gaps("test-store")[0]

    gap_id = ledger.record_gap(
        "test-store",
        "returns",
        moment(2026, 1, 3),
        moment(2026, 1, 4),
        "retry pending",
    )
    gaps = ledger.list_open_gaps("test-store")

    assert len(gaps) == 1
    assert gaps[0]["gap_id"] == first_gap["gap_id"] == gap_id
    assert gaps[0]["reason"] == "retry pending"


def test_preopening_gap_is_trimmed_and_in_scope_tail_can_resolve(ledger):
    ledger.record_gap(
        "test-store",
        "advertising_costs",
        moment(2024, 1, 29),
        moment(2026, 8, 27),
        "historical transport failure",
    )

    opening = moment(2026, 7, 15)
    ledger.trim_open_gaps_before("test-store", opening)
    gaps = ledger.list_open_gaps("test-store")
    assert len(gaps) == 1
    assert gaps[0]["gap_from"] == opening.astimezone(timezone.utc).isoformat()

    run = ledger.start_sync(
        "test-store",
        "advertising_costs",
        opening,
        moment(2026, 8, 27),
    )
    ledger.finish_sync(run, "complete", watermark_at=moment(2026, 8, 27))

    assert ledger.list_open_gaps("test-store") == []


def test_trim_closes_preopening_gap_when_in_scope_tail_already_synced(ledger):
    ledger.record_gap(
        "test-store",
        "advertising_costs",
        moment(2024, 1, 29),
        moment(2026, 8, 27),
        "historical transport failure",
    )
    opening = moment(2026, 7, 15)
    run = ledger.start_sync(
        "test-store",
        "advertising_costs",
        opening,
        moment(2026, 8, 27),
    )
    ledger.finish_sync(run, "complete", watermark_at=moment(2026, 8, 27))

    ledger.trim_open_gaps_before("test-store", opening)

    assert ledger.list_open_gaps("test-store") == []


def test_cost_selection_uses_creation_time_and_boundary_is_half_open(ledger):
    old_id = ledger.add_cost_version(
        "SKU-TEST",
        effective_from=moment(2026, 1, 1),
        effective_to=moment(2026, 2, 1),
        purchase_cost_cny="1.25",
        quote_sha256="a" * 64,
        quote_row=1,
        exchange_rate_rub_per_cny="10.50",
    )
    new_id = ledger.add_cost_version(
        "SKU-TEST",
        effective_from=moment(2026, 2, 1),
        effective_to=None,
        purchase_cost_cny="1.50",
        quote_sha256="b" * 64,
        quote_row=2,
        exchange_rate_rub_per_cny="10.75",
    )

    created_before_completed_after = ledger.select_cost_version(
        "SKU-TEST",
        moment(2026, 1, 31, 23),
        order_completed_at=moment(2026, 2, 2),
    )
    at_boundary = ledger.select_cost_version(
        "SKU-TEST",
        moment(2026, 2, 1),
    )

    assert created_before_completed_after.cost_version_id == old_id
    assert created_before_completed_after.purchase_cost_cny == Decimal("1.25")
    assert at_boundary.cost_version_id == new_id
    assert at_boundary.purchase_cost_cny == Decimal("1.50")
    assert ledger.connection.execute(
        "SELECT typeof(purchase_cost_cny) FROM cost_versions WHERE cost_version_id=?",
        (old_id,),
    ).fetchone()[0] == "text"


def test_cost_overlap_and_missing_version_fail_closed(ledger):
    ledger.add_cost_version(
        "SKU-OVERLAP",
        effective_from=moment(2026, 1, 1),
        effective_to=moment(2026, 3, 1),
        purchase_cost_cny="2.00",
        quote_sha256="c" * 64,
        quote_row=1,
        exchange_rate_rub_per_cny="11.00",
    )

    with pytest.raises(OverlappingCostVersionError):
        ledger.add_cost_version(
            "SKU-OVERLAP",
            effective_from=moment(2026, 2, 1),
            effective_to=None,
            purchase_cost_cny="2.25",
            quote_sha256="d" * 64,
            quote_row=2,
            exchange_rate_rub_per_cny="11.25",
        )

    with pytest.raises(MissingCostVersionError):
        ledger.select_cost_version("SKU-MISSING", moment(2026, 2, 1))


def test_report_version_is_idempotent_but_cannot_be_overwritten(ledger):
    totals = {
        "revenue_cny": "10.00",
        "cost_cny": "4.00",
        "profit_cny": "6.00",
        "managed_operations_cny": "0.20",
        "return_fee_cny": "0.00",
        "advertising_cny": "0.30",
    }
    orders = [
        {
            "store_alias": "test-store",
            "order_key": "order-test-1",
            "profit_version": "profit-v1",
            "owner_profit_cny": "6.00",
            "snapshot": {"status": "complete"},
        }
    ]
    report_id = ledger.create_report(
        "weekly:2026-01-05",
        "v1",
        report_type="weekly",
        period_start=moment(2026, 1, 5),
        period_end=moment(2026, 1, 12),
        policy_id="reporting-test-v1",
        totals=totals,
        orders=orders,
        generated_at=moment(2026, 1, 12, 9),
    )
    duplicate = ledger.create_report(
        "weekly:2026-01-05",
        "v1",
        report_type="weekly",
        period_start=moment(2026, 1, 5),
        period_end=moment(2026, 1, 12),
        policy_id="reporting-test-v1",
        totals=totals,
        orders=orders,
        generated_at=moment(2026, 1, 12, 10),
    )

    assert duplicate == report_id
    assert ledger.connection.execute(
        "SELECT typeof(revenue_cny) FROM reports WHERE report_id=?", (report_id,)
    ).fetchone()[0] == "text"

    changed = dict(totals, profit_cny="5.99")
    with pytest.raises(ReportImmutableError):
        ledger.create_report(
            "weekly:2026-01-05",
            "v1",
            report_type="weekly",
            period_start=moment(2026, 1, 5),
            period_end=moment(2026, 1, 12),
            policy_id="reporting-test-v1",
            totals=changed,
            orders=orders,
        )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.connection.execute(
            "UPDATE reports SET profit_cny='0.00' WHERE report_id=?", (report_id,)
        )
    ledger.connection.rollback()

    next_version = ledger.create_report(
        "weekly:2026-01-05",
        "v2",
        report_type="weekly",
        period_start=moment(2026, 1, 5),
        period_end=moment(2026, 1, 12),
        policy_id="reporting-test-v2",
        totals=changed,
        orders=orders,
    )
    assert next_version != report_id


def _report_totals(*, profit: str = "6.00") -> dict[str, str]:
    return {
        "revenue_cny": "10.00",
        "cost_cny": "4.00",
        "profit_cny": profit,
        "managed_operations_cny": "0.20",
        "return_fee_cny": "0.10",
        "advertising_cny": "0.20",
    }


def _create_legacy_v1_database(path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA user_version=1;
        CREATE TABLE store_registry(
            store_alias TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            currency TEXT NOT NULL CHECK(currency = 'CNY'),
            active INTEGER NOT NULL CHECK(active IN (0, 1)),
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE cost_versions(
            cost_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            effective_to TEXT,
            purchase_cost_cny TEXT NOT NULL CHECK(typeof(purchase_cost_cny) = 'text'),
            quote_sha256 TEXT NOT NULL,
            quote_row INTEGER NOT NULL CHECK(quote_row > 0),
            exchange_rate_rub_per_cny TEXT NOT NULL CHECK(typeof(exchange_rate_rub_per_cny) = 'text'),
            imported_at TEXT NOT NULL,
            CHECK(effective_to IS NULL OR effective_to > effective_from),
            UNIQUE(sku, effective_from, quote_sha256, quote_row)
        );
        CREATE TABLE order_profit(
            store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
            order_key TEXT NOT NULL,
            profit_version TEXT NOT NULL,
            order_created_at TEXT NOT NULL,
            completed_at TEXT,
            sku TEXT NOT NULL,
            cost_version_id INTEGER NOT NULL REFERENCES cost_versions(cost_version_id),
            policy_id TEXT NOT NULL,
            currency TEXT NOT NULL CHECK(currency = 'CNY'),
            revenue_cny TEXT NOT NULL,
            purchase_cost_cny TEXT NOT NULL,
            commission_cny TEXT NOT NULL,
            managed_operations_cny TEXT NOT NULL,
            combined_pool_cny TEXT NOT NULL,
            actual_return_logistics_cny TEXT NOT NULL,
            actual_ad_spend_reconciliation_cny TEXT,
            payment_cny TEXT NOT NULL,
            fixed_operations_cny TEXT NOT NULL,
            other_cost_cny TEXT NOT NULL,
            owner_profit_cny TEXT NOT NULL,
            calculation_status TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            calculated_at TEXT NOT NULL,
            PRIMARY KEY(store_alias, order_key, profit_version)
        );
        CREATE TABLE reports(
            report_id TEXT PRIMARY KEY,
            report_key TEXT NOT NULL,
            report_version TEXT NOT NULL,
            report_type TEXT NOT NULL CHECK(report_type IN ('weekly', 'monthly', 'bootstrap')),
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            currency TEXT NOT NULL CHECK(currency = 'CNY'),
            revenue_cny TEXT NOT NULL,
            cost_cny TEXT NOT NULL,
            profit_cny TEXT NOT NULL,
            managed_operations_cny TEXT NOT NULL,
            return_fee_cny TEXT NOT NULL,
            advertising_cny TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            UNIQUE(report_key, report_version)
        );
        CREATE TABLE funding_actions(
            action_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
            report_id TEXT REFERENCES reports(report_id),
            action_type TEXT NOT NULL CHECK(action_type IN ('withdrawal', 'advertising_top_up')),
            requested_amount_cny TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('proposed', 'approved', 'executed', 'failed', 'cancelled')),
            requested_at TEXT NOT NULL,
            approved_at TEXT,
            executed_at TEXT,
            external_reference TEXT,
            evidence_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO store_registry VALUES(
            'legacy-store', 'legacy-scope', 'CNY', 1, '{}',
            '2025-12-31T16:00:00+00:00', '2025-12-31T16:00:00+00:00'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO cost_versions(
            sku, effective_from, effective_to, purchase_cost_cny,
            quote_sha256, quote_row, exchange_rate_rub_per_cny, imported_at
        ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
        """,
        (
            "IPHONE 13-128GB-BLACK",
            "2025-12-31T16:00:00+00:00",
            "100.00",
            "a" * 64,
            7,
            "10.50",
            "2025-12-31T16:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()


def test_v1_database_is_migrated_in_place_and_old_constraints_remain_usable(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    _create_legacy_v1_database(database)

    with FinanceLedger(database) as migrated:
        assert migrated.connection.execute("PRAGMA user_version").fetchone()[0] == 3
        expected_columns = {
            "cost_version_uid",
            "model",
            "capacity",
            "color",
            "sku_aliases_json",
            "purchase_cost_rub",
            "source_currency",
            "quote_path",
            "quote_sheet",
            "row_sha256",
            "approved_by",
            "approved_at",
        }
        assert expected_columns <= migrated._table_columns("cost_versions")
        assert {
            "advertising_provision_cny",
            "excess_return_loss_cny",
            "forward_logistics_cny",
            "net_quantity",
            "order_result",
        } <= migrated._table_columns("order_profit")
        assert {
            "report_status",
            "previous_report_id",
            "correction_reason",
            "correction_metadata_json",
        } <= migrated._table_columns("reports")

        cost = migrated.select_cost_version(
            "IPHONE 13-128GB-BLACK", moment(2026, 1, 2)
        )
        assert cost.cost_version_uid.startswith("costv_")
        assert cost.sku_aliases == ("IPHONE 13-128GB-BLACK",)
        assert cost.purchase_cost_rub == Decimal("1050.0000")
        assert (cost.model, cost.capacity, cost.color) == (
            "IPHONE 13",
            "128GB",
            "BLACK",
        )

        # The legacy CHECK constraint cannot store the new transitional status in
        # ``status``.  The migration keeps it in ``lifecycle_status`` until final.
        action_id = migrated.record_funding_action(
            "legacy-action",
            "legacy-store",
            action_type="advertising_top_up",
            requested_amount_cny="5.00",
            status="executing",
            requested_at=moment(2026, 1, 2),
        )
        transitional = migrated.query_funding_actions(
            lifecycle_status="executing"
        )[0]
        assert transitional["action_id"] == action_id
        assert transitional["status"] == "proposed"
        assert transitional["lifecycle_status"] == "executing"
        migrated.update_funding_action_status(
            action_id, "readback_pending", occurred_at=moment(2026, 1, 2, 1)
        )
        migrated.update_funding_action_status(
            action_id,
            "readback_confirmed",
            occurred_at=moment(2026, 1, 2, 2),
            external_reference="test-readback",
        )
        final = migrated.query_funding_actions(lifecycle_status="readback_confirmed")[0]
        assert final["status"] == "proposed"
        assert final["lifecycle_status"] == "readback_confirmed"
        assert final["executed_at"] is not None


def test_cost_import_preserves_full_quote_metadata_and_accepts_cost_row_shape(ledger):
    full_record = {
        "costVersionId": "costv_test_full_1",
        "productKey": "IPHONE 13-128GB-BLACK",
        "model": "IPHONE 13",
        "capacity": "128GB",
        "color": "BLACK",
        "specification": "iPhone 13 / 128 GB / Black",
        "costRub": "22500",
        "purchaseCostCny": "1956.52",
        "sourceCurrency": "RUB",
        "effectiveFrom": moment(2026, 1, 1),
        "effectiveTo": None,
        "sourcePath": "quotes/test.xlsx",
        "sourceSha256": "b" * 64,
        "sourceSheet": "手机报价",
        "sourceRow": 3,
        "rowSha256": "c" * 64,
        "exchangeRateRubPerCny": "11.5",
        "approvedBy": "test-approver",
        "approvedAt": moment(2026, 1, 1, 1),
    }
    cost_id = ledger.import_cost_versions([full_record])[0]
    assert ledger.import_cost_versions([full_record]) == [cost_id]

    selected = ledger.select_cost_version(
        "IPHONE 13-128GB-BLACK", moment(2026, 1, 2)
    )
    assert selected.cost_version_id == cost_id
    assert selected.cost_version_uid == "costv_test_full_1"
    assert selected.purchase_cost_rub == Decimal("22500")
    assert selected.purchase_cost_cny == Decimal("1956.52")
    assert selected.source_currency == "RUB"
    assert selected.quote_path == "quotes/test.xlsx"
    assert selected.quote_sheet == "手机报价"
    assert selected.quote_row == 3
    assert selected.quote_sha256 == "b" * 64
    assert selected.row_sha256 == "c" * 64
    assert set(selected.sku_aliases) == {
        "IPHONE 13-128GB-BLACK",
        "iPhone 13 / 128 GB / Black",
    }
    assert selected.approved_by == "test-approver"
    assert selected.approved_at == "2025-12-31T17:00:00+00:00"

    changed_metadata = dict(full_record, sourceSheet="different-sheet")
    with pytest.raises(OverlappingCostVersionError, match="different financial content"):
        ledger.import_cost_versions([changed_metadata])

    parsed_row = CostVersionRow(
        version_id="costv_parser_shape",
        product_key="IPHONE 14-256GB-BLUE",
        model="IPHONE 14",
        capacity="256GB",
        color="BLUE",
        specification="iPhone 14 256GB Blue",
        cost_rub=Decimal("30000"),
        rub_per_cny=Decimal("12"),
        cost_cny=Decimal("2500"),
        effective_from=moment(2026, 1, 3),
        source_path="quotes/parser.xlsx",
        source_sheet="报价",
        source_row=9,
        source_hash="d" * 64,
        row_hash="e" * 64,
    )
    parser_id = ledger.import_cost_versions([parsed_row.as_ledger_record()])[0]
    parser_cost = ledger.select_cost_version(
        "IPHONE 14-256GB-BLUE", moment(2026, 1, 4)
    )
    assert parser_cost.cost_version_id == parser_id
    assert parser_cost.purchase_cost_rub == Decimal("30000")


def test_open_cost_versions_auto_close_and_reimport_stays_idempotent(ledger):
    first = {
        "sku": "SKU-CLOSE",
        "effective_from": moment(2026, 1, 1),
        "effective_to": None,
        "purchase_cost_cny": "10.00",
        "quote_sha256": "f" * 64,
        "quote_row": 1,
        "exchange_rate_rub_per_cny": "10",
    }
    second = {
        "sku": "SKU-CLOSE",
        "effective_from": moment(2026, 2, 1),
        "effective_to": None,
        "purchase_cost_cny": "11.00",
        "quote_sha256": "1" * 64,
        "quote_row": 2,
        "exchange_rate_rub_per_cny": "10",
    }
    first_id = ledger.import_cost_versions([first])[0]
    second_id = ledger.import_cost_versions([second])[0]

    assert ledger.import_cost_versions([first]) == [first_id]
    assert ledger.select_cost_version(
        "SKU-CLOSE", moment(2026, 1, 31, 23)
    ).cost_version_id == first_id
    assert ledger.select_cost_version(
        "SKU-CLOSE", moment(2026, 2, 1)
    ).cost_version_id == second_id
    closed_at = ledger.connection.execute(
        "SELECT effective_to FROM cost_versions WHERE cost_version_id=?", (first_id,)
    ).fetchone()[0]
    assert closed_at == "2026-01-31T16:00:00+00:00"


def test_report_status_revision_metadata_and_read_queries_are_immutable(ledger):
    pending_id = ledger.create_report(
        "weekly:2026-02-02",
        "v1",
        report_type="weekly",
        report_status="PENDING_DATA",
        period_start=moment(2026, 2, 2),
        period_end=moment(2026, 2, 9),
        policy_id="reporting-test-v1",
        totals=_report_totals(),
        metadata={"missingSources": ["finance"]},
        generated_at=moment(2026, 2, 9, 8),
    )
    final_id = ledger.create_report(
        "weekly:2026-02-02",
        "v2",
        report_type="weekly",
        report_status="FINAL",
        previous_report_id=pending_id,
        period_start=moment(2026, 2, 2),
        period_end=moment(2026, 2, 9),
        policy_id="reporting-test-v1",
        totals=_report_totals(),
        generated_at=moment(2026, 2, 9, 9),
    )
    restated_id = ledger.create_report(
        "weekly:2026-02-02",
        "v3",
        report_type="weekly",
        report_status="RESTATED",
        previous_report_id=final_id,
        correction_reason="late settlement row",
        correction_metadata={"rrdIds": ["test-rrd"]},
        period_start=moment(2026, 2, 2),
        period_end=moment(2026, 2, 9),
        policy_id="reporting-test-v1",
        totals=_report_totals(profit="5.90"),
        generated_at=moment(2026, 2, 10, 9),
    )

    report = ledger.get_report(restated_id)
    assert report is not None
    assert report["report_status"] == "RESTATED"
    assert report["previous_report_id"] == final_id
    assert report["correction_reason"] == "late settlement row"
    assert report["correction_metadata"] == {"rrdIds": ["test-rrd"]}
    assert [row["report_id"] for row in ledger.find_reports(report_status="FINAL")] == [
        final_id
    ]

    with pytest.raises(FinanceLedgerError, match="requires"):
        ledger.create_report(
            "weekly:2026-02-02",
            "v4",
            report_type="weekly",
            report_status="RESTATED",
            period_start=moment(2026, 2, 2),
            period_end=moment(2026, 2, 9),
            policy_id="reporting-test-v1",
            totals=_report_totals(),
        )
    with pytest.raises(ReportImmutableError):
        ledger.create_report(
            "weekly:2026-02-02",
            "v3",
            report_type="weekly",
            report_status="RESTATED",
            previous_report_id=final_id,
            correction_reason="different correction",
            period_start=moment(2026, 2, 2),
            period_end=moment(2026, 2, 9),
            policy_id="reporting-test-v1",
            totals=_report_totals(profit="5.90"),
        )


def test_order_profit_extended_fields_and_latest_period_query(ledger):
    cost_id = ledger.add_cost_version(
        "SKU-PROFIT",
        effective_from=moment(2026, 1, 1),
        effective_to=None,
        purchase_cost_cny="4.00",
        quote_sha256="2" * 64,
        quote_row=1,
        exchange_rate_rub_per_cny="10",
    )
    common = {
        "order_created_at": moment(2026, 2, 3),
        "completed_at": moment(2026, 2, 4),
        "sku": "SKU-PROFIT",
        "cost_version_id": cost_id,
        "policy_id": "financial-test-v1",
        "revenue_cny": "10.00",
        "purchase_cost_cny": "4.00",
        "commission_cny": "1.00",
        "managed_operations_cny": "0.20",
        "combined_pool_cny": "0.30",
        "actual_return_logistics_cny": "0.40",
        "actual_ad_spend_reconciliation_cny": "0.25",
        "payment_cny": "0.10",
        "fixed_operations_cny": "0.10",
        "other_cost_cny": "0.00",
        "owner_profit_cny": "4.20",
        "calculation_status": "complete",
        "evidence": {"rrdIds": ["test-1"]},
        "forward_logistics_cny": "0.15",
        "net_quantity": "0",
        "order_result": "returned",
    }
    first_version = ledger.upsert_order_profit(
        "test-store",
        "order-profit-1",
        profit_version="v1",
        calculated_at=moment(2026, 2, 5, 8),
        **common,
    )
    assert ledger.upsert_order_profit(
        "test-store",
        "order-profit-1",
        profit_version="v1",
        calculated_at=moment(2026, 2, 5, 9),
        **common,
    ) == first_version
    second_values = dict(common, owner_profit_cny="4.10")
    second_version = ledger.upsert_order_profit(
        "test-store",
        "order-profit-1",
        profit_version="v2",
        calculated_at=moment(2026, 2, 5, 9),
        **second_values,
    )

    latest = ledger.query_order_profits(
        moment(2026, 2, 4), moment(2026, 2, 5), store_alias="test-store"
    )
    assert [row["profit_version"] for row in latest] == [second_version]
    assert latest[0]["advertising_provision_cny"] == "0"
    assert latest[0]["excess_return_loss_cny"] == "0.10"
    assert latest[0]["forward_logistics_cny"] == "0.15"
    assert latest[0]["net_quantity"] == "0"
    assert latest[0]["order_result"] == "returned"
    assert latest[0]["evidence"] == {"rrdIds": ["test-1"]}
    assert len(
        ledger.query_order_profits(
            moment(2026, 2, 4), moment(2026, 2, 5), latest_only=False
        )
    ) == 2
    money_types = ledger.connection.execute(
        """
        SELECT typeof(advertising_provision_cny), typeof(excess_return_loss_cny),
               typeof(forward_logistics_cny), typeof(net_quantity)
        FROM order_profit WHERE profit_version='v2'
        """
    ).fetchone()
    assert tuple(money_types) == ("text", "text", "text", "text")


def test_source_coverage_requires_two_consecutive_complete_equal_snapshots(ledger):
    start = moment(2026, 3, 2)
    end = moment(2026, 3, 9)
    digest = "3" * 64
    first = ledger.record_source_coverage(
        "test-store",
        "finance",
        start,
        end,
        coverage_status="complete",
        row_count=4,
        snapshot_hash=digest,
        captured_at=moment(2026, 3, 9, 8),
    )
    assert first.consecutive_complete_hash_count == 1
    assert ledger.has_consecutive_stable_snapshot(
        "test-store", "finance", start, end
    ) is False
    second = ledger.record_source_coverage(
        "test-store",
        "finance",
        start,
        end,
        coverage_status="complete",
        row_count=4,
        snapshot_hash=digest,
        captured_at=moment(2026, 3, 9, 9),
    )
    duplicate = ledger.record_source_coverage(
        "test-store",
        "finance",
        start,
        end,
        coverage_status="complete",
        row_count=4,
        snapshot_hash=digest,
        captured_at=moment(2026, 3, 9, 9),
    )
    assert duplicate == second
    assert second.consecutive_complete_hash_count == 2
    assert ledger.has_consecutive_stable_snapshot(
        "test-store", "finance", start, end
    ) is True
    coverage = ledger.get_period_coverage(
        start, end, store_aliases=["test-store"], sources=["finance"]
    )
    assert len(coverage) == 1
    assert coverage[0]["coverage_id"] == second.coverage_id

    changed = ledger.record_source_coverage(
        "test-store",
        "finance",
        start,
        end,
        coverage_status="complete",
        row_count=5,
        snapshot_hash="4" * 64,
        captured_at=moment(2026, 3, 9, 10),
    )
    assert changed.consecutive_complete_hash_count == 1
    assert ledger.has_consecutive_stable_snapshot(
        "test-store", "finance", start, end
    ) is False
    with pytest.raises(FinanceLedgerError, match="cannot be overwritten"):
        ledger.record_source_coverage(
            "test-store",
            "finance",
            start,
            end,
            coverage_status="partial",
            row_count=4,
            snapshot_hash=None,
            captured_at=moment(2026, 3, 9, 10),
        )


def test_precise_balance_ad_and_funding_plan_queries(ledger):
    ledger.upsert_balance(
        "test-store",
        {"capture": "before"},
        {"capture": "before"},
        observed_at=moment(2026, 4, 1, 8),
        account_balance_cny="9.00",
        withdrawable_balance_cny="8.00",
        ad_balance_cny="1.00",
    )
    ledger.upsert_balance(
        "test-store",
        {"capture": "after"},
        {"capture": "after"},
        observed_at=moment(2026, 4, 1, 10),
        account_balance_cny="12.00",
        withdrawable_balance_cny="10.00",
        ad_balance_cny="2.00",
    )
    assert ledger.get_latest_balance("test-store")["account_balance_cny"] == "12.00"
    assert ledger.get_latest_balance(
        "test-store", as_of=moment(2026, 4, 1, 9)
    )["account_balance_cny"] == "9.00"
    assert len(
        ledger.query_balance_snapshots(
            "test-store", moment(2026, 4, 1, 8), moment(2026, 4, 1, 10)
        )
    ) == 1

    ledger.upsert_ad(
        "test-store",
        {"event": "one"},
        {"event": "one"},
        occurred_at=moment(2026, 4, 1, 8),
        campaign_id="campaign-a",
        spend_cny="1.20",
    )
    ledger.upsert_ad(
        "test-store",
        {"event": "two"},
        {"event": "two"},
        occurred_at=moment(2026, 4, 1, 9),
        campaign_id="campaign-b",
        spend_cny="0.30",
    )
    ledger.upsert_ad(
        "test-store",
        {"event": "boundary"},
        {"event": "boundary"},
        occurred_at=moment(2026, 4, 2),
        campaign_id="campaign-a",
        spend_cny="9.00",
    )
    assert ledger.sum_ad_spend(
        "test-store", moment(2026, 4, 1), moment(2026, 4, 2)
    ) == Decimal("1.50")
    assert ledger.sum_ad_spend(
        "test-store",
        moment(2026, 4, 1),
        moment(2026, 4, 2),
        campaign_id="campaign-a",
    ) == Decimal("1.20")

    report_id = ledger.create_report(
        "weekly:2026-03-30",
        "v1",
        report_type="weekly",
        report_status="FINAL",
        period_start=moment(2026, 3, 30),
        period_end=moment(2026, 4, 6),
        policy_id="reporting-test-v1",
        totals=_report_totals(),
    )
    action_id = ledger.record_funding_action(
        "funding-plan-test",
        "test-store",
        report_id=report_id,
        action_type="withdrawal",
        requested_amount_cny="8.00",
        status="executing",
        requested_at=moment(2026, 4, 6, 8),
    )
    ledger.update_funding_action_status(
        action_id, "unknown", occurred_at=moment(2026, 4, 6, 9)
    )
    ledger.update_funding_action_status(
        action_id, "readback_pending", occurred_at=moment(2026, 4, 6, 10)
    )
    assert ledger.get_funding_plan(report_id)[0]["lifecycle_status"] == (
        "readback_pending"
    )
    assert ledger.query_funding_actions(
        lifecycle_status=["unknown", "readback_pending"]
    )[0]["action_id"] == action_id
    assert ledger.query_funding_actions(lifecycle_status=[]) == []
    assert [event["lifecycle_status"] for event in ledger.get_funding_action_events(action_id)] == [
        "executing",
        "unknown",
        "readback_pending",
    ]
