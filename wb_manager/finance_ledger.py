from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_FINANCE_DB = Path("data/finance/wb_finance.sqlite3")
# The public schema version remains 3 for backwards compatibility with the
# existing workbook/ledger migration contract.  The withdrawal/ad-debt tables
# added in this revision are additive and are created idempotently below, so a
# pre-existing v3 database can be opened without a destructive migration.
SCHEMA_VERSION = 3
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SYNC_STATUSES = {"running", "complete", "failed", "partial"}
_REPORT_STATUSES = {"PENDING_DATA", "FINAL", "RESTATED"}
_FUNDING_STATUSES = {
    "proposed",
    "approved",
    "executing",
    "unknown",
    "readback_pending",
    "readback_confirmed",
    "executed",
    "failed",
    "cancelled",
}
_LEGACY_FUNDING_STATUSES = {"proposed", "approved", "executed", "failed", "cancelled"}
_WITHDRAWAL_RECORD_STATUSES = {"reported", "readback_confirmed", "voided"}
_ADVERTISING_FEE_STATUSES = {
    "deducted",
    "not_deducted",
    "partial",
    "unknown",
    "settlement_balance",
}
_ADVERTISING_PAYMENT_SOURCES = {"platform_api", "user_provided", "funding_action"}


class FinanceLedgerError(RuntimeError):
    pass


class MissingCostVersionError(FinanceLedgerError):
    pass


class OverlappingCostVersionError(FinanceLedgerError):
    pass


class ReportImmutableError(FinanceLedgerError):
    pass


@dataclass(frozen=True)
class UpsertResult:
    key: str
    inserted: bool
    # ``changed`` describes a content change, not merely an idempotent replay.
    # ``inserted`` is retained as the public compatibility field used by
    # existing callers; a replay with the same payload is therefore
    # ``inserted=False, changed=False`` while a corrected existing row is
    # ``inserted=False, changed=True``.
    changed: bool = False


@dataclass(frozen=True)
class CostVersion:
    cost_version_id: int
    cost_version_uid: str
    sku: str
    model: str | None
    capacity: str | None
    color: str | None
    sku_aliases: tuple[str, ...]
    effective_from: str
    effective_to: str | None
    purchase_cost_rub: Decimal | None
    purchase_cost_cny: Decimal
    source_currency: str
    quote_path: str | None
    quote_sha256: str
    quote_sheet: str | None
    quote_row: int
    row_sha256: str | None
    exchange_rate_rub_per_cny: Decimal
    approved_by: str | None
    approved_at: str | None


@dataclass(frozen=True)
class CoverageSnapshot:
    coverage_id: str
    store_alias: str
    source: str
    period_start: str
    period_end: str
    coverage_status: str
    snapshot_hash: str | None
    consecutive_complete_hash_count: int
    captured_at: str


class FinanceLedger:
    """Persistent, idempotent WB finance ledger backed by standard-library SQLite."""

    def __init__(self, path: Path | str = DEFAULT_FINANCE_DB):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        if str(path) != ":memory:":
            self.connection.execute("PRAGMA journal_mode=WAL")
        self.initialize_schema()

    def __enter__(self) -> FinanceLedger:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def initialize_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS store_registry(
                store_alias TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                currency TEXT NOT NULL CHECK(currency = 'CNY'),
                active INTEGER NOT NULL CHECK(active IN (0, 1)),
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS raw_finance(
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                rrd_id TEXT NOT NULL,
                order_id TEXT,
                srid TEXT,
                shk_id TEXT,
                occurred_at TEXT,
                buyer_paid_cny TEXT CHECK(buyer_paid_cny IS NULL OR typeof(buyer_paid_cny) = 'text'),
                for_pay_cny TEXT CHECK(for_pay_cny IS NULL OR typeof(for_pay_cny) = 'text'),
                commission_cny TEXT CHECK(commission_cny IS NULL OR typeof(commission_cny) = 'text'),
                logistics_cny TEXT CHECK(logistics_cny IS NULL OR typeof(logistics_cny) = 'text'),
                return_logistics_cny TEXT CHECK(return_logistics_cny IS NULL OR typeof(return_logistics_cny) = 'text'),
                acquiring_fee_cny TEXT CHECK(acquiring_fee_cny IS NULL OR typeof(acquiring_fee_cny) = 'text'),
                other_cost_cny TEXT CHECK(other_cost_cny IS NULL OR typeof(other_cost_cny) = 'text'),
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY(store_alias, rrd_id)
            );

            CREATE TABLE IF NOT EXISTS raw_sales(
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                event_hash TEXT NOT NULL,
                sale_id TEXT,
                order_id TEXT,
                srid TEXT,
                occurred_at TEXT NOT NULL,
                buyer_paid_cny TEXT CHECK(buyer_paid_cny IS NULL OR typeof(buyer_paid_cny) = 'text'),
                for_pay_cny TEXT CHECK(for_pay_cny IS NULL OR typeof(for_pay_cny) = 'text'),
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY(store_alias, event_hash)
            );

            CREATE TABLE IF NOT EXISTS raw_returns(
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                event_hash TEXT NOT NULL,
                return_id TEXT,
                order_id TEXT,
                srid TEXT,
                shk_id TEXT,
                occurred_at TEXT NOT NULL,
                actual_logistics_cny TEXT CHECK(actual_logistics_cny IS NULL OR typeof(actual_logistics_cny) = 'text'),
                refund_cny TEXT CHECK(refund_cny IS NULL OR typeof(refund_cny) = 'text'),
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY(store_alias, event_hash)
            );

            CREATE TABLE IF NOT EXISTS raw_ad(
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                event_hash TEXT NOT NULL,
                campaign_id TEXT,
                nm_id TEXT,
                occurred_at TEXT NOT NULL,
                spend_cny TEXT CHECK(spend_cny IS NULL OR typeof(spend_cny) = 'text'),
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY(store_alias, event_hash)
            );

            CREATE TABLE IF NOT EXISTS raw_balance(
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                event_hash TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                account_balance_cny TEXT CHECK(account_balance_cny IS NULL OR typeof(account_balance_cny) = 'text'),
                withdrawable_balance_cny TEXT CHECK(withdrawable_balance_cny IS NULL OR typeof(withdrawable_balance_cny) = 'text'),
                ad_balance_cny TEXT CHECK(ad_balance_cny IS NULL OR typeof(ad_balance_cny) = 'text'),
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY(store_alias, event_hash)
            );

            CREATE TABLE IF NOT EXISTS sync_runs(
                run_id TEXT PRIMARY KEY,
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                source TEXT NOT NULL,
                requested_from TEXT NOT NULL,
                requested_through TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL CHECK(status IN ('running', 'complete', 'failed', 'partial')),
                rows_seen INTEGER NOT NULL DEFAULT 0,
                rows_inserted INTEGER NOT NULL DEFAULT 0,
                rows_updated INTEGER NOT NULL DEFAULT 0,
                error_text TEXT
            );

            CREATE TABLE IF NOT EXISTS sync_watermarks(
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                source TEXT NOT NULL,
                watermark_at TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES sync_runs(run_id),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(store_alias, source)
            );

            CREATE TABLE IF NOT EXISTS sync_gaps(
                gap_id TEXT PRIMARY KEY,
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                source TEXT NOT NULL,
                gap_from TEXT NOT NULL,
                gap_through TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('open', 'resolved')),
                detected_at TEXT NOT NULL,
                resolved_at TEXT,
                run_id TEXT REFERENCES sync_runs(run_id),
                UNIQUE(store_alias, source, gap_from, gap_through)
            );

            CREATE TABLE IF NOT EXISTS cost_versions(
                cost_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                cost_version_uid TEXT NOT NULL UNIQUE,
                sku TEXT NOT NULL,
                model TEXT,
                capacity TEXT,
                color TEXT,
                sku_aliases_json TEXT NOT NULL DEFAULT '[]',
                effective_from TEXT NOT NULL,
                effective_to TEXT,
                purchase_cost_rub TEXT CHECK(purchase_cost_rub IS NULL OR typeof(purchase_cost_rub) = 'text'),
                purchase_cost_cny TEXT NOT NULL CHECK(typeof(purchase_cost_cny) = 'text'),
                source_currency TEXT NOT NULL DEFAULT 'RUB',
                quote_path TEXT,
                quote_sha256 TEXT NOT NULL,
                quote_sheet TEXT,
                quote_row INTEGER NOT NULL CHECK(quote_row > 0),
                row_sha256 TEXT,
                exchange_rate_rub_per_cny TEXT NOT NULL CHECK(typeof(exchange_rate_rub_per_cny) = 'text'),
                approved_by TEXT,
                approved_at TEXT,
                imported_at TEXT NOT NULL,
                CHECK(effective_to IS NULL OR effective_to > effective_from),
                UNIQUE(sku, effective_from, quote_sha256, quote_row)
            );

            CREATE TABLE IF NOT EXISTS order_profit(
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                order_key TEXT NOT NULL,
                profit_version TEXT NOT NULL,
                order_created_at TEXT NOT NULL,
                completed_at TEXT,
                sku TEXT NOT NULL,
                cost_version_id INTEGER NOT NULL REFERENCES cost_versions(cost_version_id),
                policy_id TEXT NOT NULL,
                currency TEXT NOT NULL CHECK(currency = 'CNY'),
                revenue_cny TEXT NOT NULL CHECK(typeof(revenue_cny) = 'text'),
                purchase_cost_cny TEXT NOT NULL CHECK(typeof(purchase_cost_cny) = 'text'),
                commission_cny TEXT NOT NULL CHECK(typeof(commission_cny) = 'text'),
                managed_operations_cny TEXT NOT NULL CHECK(typeof(managed_operations_cny) = 'text'),
                combined_pool_cny TEXT NOT NULL CHECK(typeof(combined_pool_cny) = 'text'),
                actual_return_logistics_cny TEXT NOT NULL CHECK(typeof(actual_return_logistics_cny) = 'text'),
                actual_ad_spend_reconciliation_cny TEXT CHECK(actual_ad_spend_reconciliation_cny IS NULL OR typeof(actual_ad_spend_reconciliation_cny) = 'text'),
                advertising_provision_cny TEXT NOT NULL DEFAULT '0' CHECK(typeof(advertising_provision_cny) = 'text'),
                excess_return_loss_cny TEXT NOT NULL DEFAULT '0' CHECK(typeof(excess_return_loss_cny) = 'text'),
                forward_logistics_cny TEXT NOT NULL DEFAULT '0' CHECK(typeof(forward_logistics_cny) = 'text'),
                net_quantity TEXT NOT NULL DEFAULT '0' CHECK(typeof(net_quantity) = 'text'),
                order_result TEXT NOT NULL DEFAULT 'unknown',
                payment_cny TEXT NOT NULL CHECK(typeof(payment_cny) = 'text'),
                fixed_operations_cny TEXT NOT NULL CHECK(typeof(fixed_operations_cny) = 'text'),
                other_cost_cny TEXT NOT NULL CHECK(typeof(other_cost_cny) = 'text'),
                owner_profit_cny TEXT NOT NULL CHECK(typeof(owner_profit_cny) = 'text'),
                calculation_status TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                calculated_at TEXT NOT NULL,
                PRIMARY KEY(store_alias, order_key, profit_version)
            );

            CREATE TABLE IF NOT EXISTS reports(
                report_id TEXT PRIMARY KEY,
                report_key TEXT NOT NULL,
                report_version TEXT NOT NULL,
                report_type TEXT NOT NULL CHECK(report_type IN ('weekly', 'monthly', 'bootstrap')),
                report_status TEXT NOT NULL DEFAULT 'FINAL' CHECK(report_status IN ('PENDING_DATA', 'FINAL', 'RESTATED')),
                previous_report_id TEXT,
                correction_reason TEXT,
                correction_metadata_json TEXT NOT NULL DEFAULT '{}',
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                policy_id TEXT NOT NULL,
                currency TEXT NOT NULL CHECK(currency = 'CNY'),
                revenue_cny TEXT NOT NULL CHECK(typeof(revenue_cny) = 'text'),
                cost_cny TEXT NOT NULL CHECK(typeof(cost_cny) = 'text'),
                profit_cny TEXT NOT NULL CHECK(typeof(profit_cny) = 'text'),
                managed_operations_cny TEXT NOT NULL CHECK(typeof(managed_operations_cny) = 'text'),
                return_fee_cny TEXT NOT NULL CHECK(typeof(return_fee_cny) = 'text'),
                advertising_cny TEXT NOT NULL CHECK(typeof(advertising_cny) = 'text'),
                content_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                UNIQUE(report_key, report_version)
            );

            CREATE TABLE IF NOT EXISTS report_orders(
                report_id TEXT NOT NULL REFERENCES reports(report_id),
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                order_key TEXT NOT NULL,
                profit_version TEXT NOT NULL,
                owner_profit_cny TEXT NOT NULL CHECK(typeof(owner_profit_cny) = 'text'),
                snapshot_json TEXT NOT NULL,
                PRIMARY KEY(report_id, store_alias, order_key, profit_version)
            );

            CREATE TABLE IF NOT EXISTS funding_actions(
                action_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                report_id TEXT REFERENCES reports(report_id),
                action_type TEXT NOT NULL CHECK(action_type IN ('withdrawal', 'advertising_top_up')),
                requested_amount_cny TEXT NOT NULL CHECK(typeof(requested_amount_cny) = 'text'),
                status TEXT NOT NULL CHECK(status IN ('proposed', 'approved', 'executing', 'unknown', 'readback_pending', 'readback_confirmed', 'executed', 'failed', 'cancelled')),
                lifecycle_status TEXT NOT NULL DEFAULT 'proposed' CHECK(lifecycle_status IN ('proposed', 'approved', 'executing', 'unknown', 'readback_pending', 'readback_confirmed', 'executed', 'failed', 'cancelled')),
                requested_at TEXT NOT NULL,
                approved_at TEXT,
                executed_at TEXT,
                external_reference TEXT,
                evidence_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS funding_action_events(
                action_id TEXT NOT NULL REFERENCES funding_actions(action_id),
                event_id TEXT NOT NULL,
                lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN ('proposed', 'approved', 'executing', 'unknown', 'readback_pending', 'readback_confirmed', 'executed', 'failed', 'cancelled')),
                occurred_at TEXT NOT NULL,
                external_reference TEXT,
                evidence_json TEXT NOT NULL,
                PRIMARY KEY(action_id, event_id)
            );

            -- Human-executed withdrawals are kept separately from funding
            -- *plans*.  The row is immutable; corrections/status changes are
            -- represented by withdrawal_record_events.  This lets the owner
            -- paste a seller-portal withdrawal record after somebody else
            -- performs the withdrawal without pretending that our system
            -- executed it.
            CREATE TABLE IF NOT EXISTS withdrawal_records(
                record_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                withdrawal_amount_cny TEXT NOT NULL
                    CHECK(typeof(withdrawal_amount_cny) = 'text'),
                withdrawn_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'reported'
                    CHECK(status IN ('reported', 'readback_confirmed', 'voided')),
                advertising_fee_status TEXT NOT NULL DEFAULT 'unknown'
                    CHECK(advertising_fee_status IN ('deducted', 'not_deducted', 'partial', 'unknown', 'settlement_balance')),
                advertising_fee_due_cny TEXT NOT NULL DEFAULT '0'
                    CHECK(typeof(advertising_fee_due_cny) = 'text'),
                advertising_fee_withheld_cny TEXT NOT NULL DEFAULT '0'
                    CHECK(typeof(advertising_fee_withheld_cny) = 'text'),
                advertising_fee_debt_cny TEXT NOT NULL DEFAULT '0'
                    CHECK(typeof(advertising_fee_debt_cny) = 'text'),
                settled_at TEXT,
                external_reference TEXT,
                source_type TEXT NOT NULL DEFAULT 'user_provided',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                payload_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS withdrawal_record_events(
                record_id TEXT NOT NULL REFERENCES withdrawal_records(record_id),
                event_id TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK(status IN ('reported', 'readback_confirmed', 'voided')),
                occurred_at TEXT NOT NULL,
                external_reference TEXT,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(record_id, event_id)
            );

            -- A payment/readback is a positive amount actually credited to a
            -- WB advertising balance.  It may be discovered by the platform
            -- API after a manual top-up, or linked to one of our approved
            -- funding actions.  It is deliberately not a P&L expense.
            CREATE TABLE IF NOT EXISTS advertising_payment_readbacks(
                payment_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                amount_cny TEXT NOT NULL CHECK(typeof(amount_cny) = 'text'),
                paid_at TEXT NOT NULL,
                source_type TEXT NOT NULL
                    CHECK(source_type IN ('platform_api', 'user_provided', 'funding_action')),
                platform_reference TEXT,
                raw_event_hash TEXT,
                funding_action_id TEXT REFERENCES funding_actions(action_id),
                evidence_json TEXT NOT NULL DEFAULT '{}',
                payload_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            -- FIFO allocations are explicit, immutable audit rows.  The
            -- unallocated remainder of a payment is visible in the debt
            -- summary and can settle a debt entered later.
            CREATE TABLE IF NOT EXISTS advertising_debt_allocations(
                allocation_id TEXT PRIMARY KEY,
                payment_id TEXT NOT NULL REFERENCES advertising_payment_readbacks(payment_id),
                withdrawal_record_id TEXT NOT NULL REFERENCES withdrawal_records(record_id),
                amount_cny TEXT NOT NULL CHECK(typeof(amount_cny) = 'text'),
                allocated_at TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(payment_id, withdrawal_record_id)
            );

            CREATE TABLE IF NOT EXISTS source_coverage(
                coverage_id TEXT PRIMARY KEY,
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                source TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                coverage_status TEXT NOT NULL CHECK(coverage_status IN ('complete', 'partial', 'missing', 'pending')),
                row_count INTEGER NOT NULL DEFAULT 0,
                snapshot_hash TEXT,
                consecutive_complete_hash_count INTEGER NOT NULL DEFAULT 0,
                sync_run_id TEXT REFERENCES sync_runs(run_id),
                captured_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                UNIQUE(store_alias, source, period_start, period_end, captured_at)
            );

            CREATE TABLE IF NOT EXISTS source_snapshot_history(
                snapshot_id TEXT PRIMARY KEY,
                coverage_id TEXT NOT NULL REFERENCES source_coverage(coverage_id),
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                source TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                is_complete INTEGER NOT NULL CHECK(is_complete IN (0, 1)),
                snapshot_hash TEXT,
                consecutive_complete_hash_count INTEGER NOT NULL DEFAULT 0,
                captured_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS locks(
                lock_name TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_finance_order ON raw_finance(store_alias, order_id, srid, shk_id);
            CREATE INDEX IF NOT EXISTS idx_sales_order ON raw_sales(store_alias, order_id, srid);
            CREATE INDEX IF NOT EXISTS idx_returns_order ON raw_returns(store_alias, order_id, srid, shk_id);
            CREATE INDEX IF NOT EXISTS idx_sync_gaps_open ON sync_gaps(store_alias, source, status, gap_from);
            CREATE INDEX IF NOT EXISTS idx_source_coverage_latest
                ON source_coverage(store_alias, source, captured_at DESC, coverage_id DESC);
            CREATE INDEX IF NOT EXISTS idx_cost_lookup ON cost_versions(sku, effective_from, effective_to);
            CREATE INDEX IF NOT EXISTS idx_profit_period ON order_profit(store_alias, completed_at, order_created_at);

            CREATE TRIGGER IF NOT EXISTS sync_watermarks_complete_insert
            BEFORE INSERT ON sync_watermarks
            BEGIN
                SELECT CASE WHEN (SELECT status FROM sync_runs WHERE run_id = NEW.run_id) != 'complete'
                    THEN RAISE(ABORT, 'watermark requires a complete sync run') END;
            END;

            CREATE TRIGGER IF NOT EXISTS sync_watermarks_complete_update
            BEFORE UPDATE ON sync_watermarks
            BEGIN
                SELECT CASE WHEN (SELECT status FROM sync_runs WHERE run_id = NEW.run_id) != 'complete'
                    THEN RAISE(ABORT, 'watermark requires a complete sync run') END;
            END;

            CREATE TRIGGER IF NOT EXISTS reports_no_update
            BEFORE UPDATE ON reports BEGIN
                SELECT RAISE(ABORT, 'reports are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS reports_no_delete
            BEFORE DELETE ON reports BEGIN
                SELECT RAISE(ABORT, 'reports are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS report_orders_no_update
            BEFORE UPDATE ON report_orders BEGIN
                SELECT RAISE(ABORT, 'report order snapshots are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS report_orders_no_delete
            BEFORE DELETE ON report_orders BEGIN
                SELECT RAISE(ABORT, 'report order snapshots are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS withdrawal_records_no_update
            BEFORE UPDATE ON withdrawal_records BEGIN
                SELECT RAISE(ABORT, 'withdrawal records are immutable; append a correction event');
            END;
            CREATE TRIGGER IF NOT EXISTS withdrawal_records_no_delete
            BEFORE DELETE ON withdrawal_records BEGIN
                SELECT RAISE(ABORT, 'withdrawal records are immutable; append a void/correction event');
            END;
            CREATE TRIGGER IF NOT EXISTS withdrawal_record_events_no_update
            BEFORE UPDATE ON withdrawal_record_events BEGIN
                SELECT RAISE(ABORT, 'withdrawal record events are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS withdrawal_record_events_no_delete
            BEFORE DELETE ON withdrawal_record_events BEGIN
                SELECT RAISE(ABORT, 'withdrawal record events are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS advertising_payment_readbacks_no_update
            BEFORE UPDATE ON advertising_payment_readbacks BEGIN
                SELECT RAISE(ABORT, 'advertising payment readbacks are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS advertising_payment_readbacks_no_delete
            BEFORE DELETE ON advertising_payment_readbacks BEGIN
                SELECT RAISE(ABORT, 'advertising payment readbacks are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS advertising_debt_allocations_no_update
            BEFORE UPDATE ON advertising_debt_allocations BEGIN
                SELECT RAISE(ABORT, 'advertising debt allocations are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS advertising_debt_allocations_no_delete
            BEFORE DELETE ON advertising_debt_allocations BEGIN
                SELECT RAISE(ABORT, 'advertising debt allocations are immutable');
            END;
            """
        )
        self._migrate_schema()
        self.connection.commit()

    def _migrate_schema(self) -> None:
        current_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version > SCHEMA_VERSION:
            raise FinanceLedgerError(
                f"Finance DB schema {current_version} is newer than supported {SCHEMA_VERSION}"
            )

        additions = {
            "cost_versions": {
                "cost_version_uid": "TEXT",
                "model": "TEXT",
                "capacity": "TEXT",
                "color": "TEXT",
                "sku_aliases_json": "TEXT NOT NULL DEFAULT '[]'",
                "purchase_cost_rub": "TEXT",
                "source_currency": "TEXT NOT NULL DEFAULT 'RUB'",
                "quote_path": "TEXT",
                "quote_sheet": "TEXT",
                "row_sha256": "TEXT",
                "approved_by": "TEXT",
                "approved_at": "TEXT",
            },
            "order_profit": {
                "advertising_provision_cny": "TEXT NOT NULL DEFAULT '0'",
                "excess_return_loss_cny": "TEXT NOT NULL DEFAULT '0'",
                "forward_logistics_cny": "TEXT NOT NULL DEFAULT '0'",
                "net_quantity": "TEXT NOT NULL DEFAULT '0'",
                "order_result": "TEXT NOT NULL DEFAULT 'unknown'",
            },
            "reports": {
                "report_status": "TEXT NOT NULL DEFAULT 'FINAL'",
                "previous_report_id": "TEXT",
                "correction_reason": "TEXT",
                "correction_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            },
            "funding_actions": {
                "lifecycle_status": "TEXT NOT NULL DEFAULT 'proposed'",
            },
        }
        with self.connection:
            for table, columns in additions.items():
                existing = self._table_columns(table)
                for column, definition in columns.items():
                    if column not in existing:
                        self.connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                        )
                        existing.add(column)

            self.connection.execute(
                """
                UPDATE funding_actions SET lifecycle_status=status
                WHERE status IN ('proposed', 'approved', 'executed', 'failed', 'cancelled')
                  AND lifecycle_status='proposed'
                """
            )
            # Additive debt-ledger columns/tables are intentionally kept
            # compatible with v3 databases that may have been opened by an
            # earlier build of this feature.
            withdrawal_columns = self._table_columns("withdrawal_records")
            if withdrawal_columns and "advertising_fee_status" not in withdrawal_columns:
                self.connection.execute(
                    """ALTER TABLE withdrawal_records
                       ADD COLUMN advertising_fee_status TEXT NOT NULL DEFAULT 'unknown'
                       CHECK(advertising_fee_status IN ('deducted', 'not_deducted', 'partial', 'unknown', 'settlement_balance'))"""
                )
            self._migrate_withdrawal_advertising_fee_status_constraint()
            self._migrate_funding_status_constraints()
            self._backfill_cost_version_metadata()
            self.connection.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_cost_version_uid
                    ON cost_versions(cost_version_uid) WHERE cost_version_uid IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_report_period
                    ON reports(report_type, report_status, period_start, period_end);
                -- ``report_status`` is added by the legacy-schema migration
                -- above, so this index must be created after column
                -- additions rather than in the initial CREATE TABLE script.
                CREATE INDEX IF NOT EXISTS idx_reports_status_period
                    ON reports(report_status, period_end, generated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_funding_query
                    ON funding_actions(store_alias, lifecycle_status, action_type, requested_at);
                CREATE INDEX IF NOT EXISTS idx_coverage_period
                    ON source_coverage(store_alias, source, period_start, period_end, captured_at);
                -- Period scans are the hot path during reconciliation/report
                -- refresh.  Keep the store key first so the index is useful
                -- for both single-store and multi-store reads.
                CREATE INDEX IF NOT EXISTS idx_raw_finance_period
                    ON raw_finance(store_alias, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_raw_sales_period
                    ON raw_sales(store_alias, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_raw_returns_period
                    ON raw_returns(store_alias, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_raw_ad_period
                    ON raw_ad(store_alias, occurred_at, campaign_id);
                CREATE INDEX IF NOT EXISTS idx_raw_balance_period
                    ON raw_balance(store_alias, observed_at, fetched_at);
                CREATE INDEX IF NOT EXISTS idx_sync_runs_store_source_status
                    ON sync_runs(store_alias, source, status, requested_through);
            CREATE INDEX IF NOT EXISTS idx_snapshot_stability
                    ON source_snapshot_history(store_alias, source, period_start, period_end, captured_at);
            CREATE INDEX IF NOT EXISTS idx_withdrawal_records_store_time
                    ON withdrawal_records(store_alias, withdrawn_at, record_id);
            CREATE INDEX IF NOT EXISTS idx_withdrawal_events_record_time
                    ON withdrawal_record_events(record_id, occurred_at, event_id);
            CREATE INDEX IF NOT EXISTS idx_ad_payment_store_time
                    ON advertising_payment_readbacks(store_alias, paid_at, payment_id);
            CREATE INDEX IF NOT EXISTS idx_ad_debt_alloc_record
                    ON advertising_debt_allocations(withdrawal_record_id, allocated_at);
                """
            )
            self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _migrate_withdrawal_advertising_fee_status_constraint(self) -> None:
        """Add ``settlement_balance`` without losing immutable audit children.

        SQLite cannot alter a CHECK constraint in place.  Keep foreign-key
        enforcement enabled and rebuild the parent plus both dependent tables
        inside a savepoint.  Capturing explicit indexes/triggers before the
        rename also preserves any site-local audit objects, not only the ones
        declared by this package.
        """

        table_row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='withdrawal_records'"
        ).fetchone()
        if table_row is None or "settlement_balance" in str(table_row["sql"] or ""):
            return

        schema_objects = self.connection.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE type IN ('index', 'trigger')
              AND tbl_name IN (
                  'withdrawal_records',
                  'withdrawal_record_events',
                  'advertising_debt_allocations'
              )
              AND sql IS NOT NULL
            ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END, name
            """
        ).fetchall()

        savepoint = "migrate_withdrawal_ad_fee_status"
        self.connection.execute(f"SAVEPOINT {savepoint}")
        try:
            for item in schema_objects:
                kind = str(item["type"])
                name = '"' + str(item["name"]).replace('"', '""') + '"'
                self.connection.execute(f"DROP {kind.upper()} IF EXISTS {name}")

            # Rename children first.  With foreign_keys=ON, renaming the parent
            # then redirects only the old children to the old parent name.
            self.connection.execute(
                "ALTER TABLE withdrawal_record_events "
                "RENAME TO withdrawal_record_events_before_settlement_balance"
            )
            self.connection.execute(
                "ALTER TABLE advertising_debt_allocations "
                "RENAME TO advertising_debt_allocations_before_settlement_balance"
            )
            self.connection.execute(
                "ALTER TABLE withdrawal_records "
                "RENAME TO withdrawal_records_before_settlement_balance"
            )

            self.connection.execute(
                """
                CREATE TABLE withdrawal_records(
                    record_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                    withdrawal_amount_cny TEXT NOT NULL
                        CHECK(typeof(withdrawal_amount_cny) = 'text'),
                    withdrawn_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'reported'
                        CHECK(status IN ('reported', 'readback_confirmed', 'voided')),
                    advertising_fee_status TEXT NOT NULL DEFAULT 'unknown'
                        CHECK(advertising_fee_status IN ('deducted', 'not_deducted', 'partial', 'unknown', 'settlement_balance')),
                    advertising_fee_due_cny TEXT NOT NULL DEFAULT '0'
                        CHECK(typeof(advertising_fee_due_cny) = 'text'),
                    advertising_fee_withheld_cny TEXT NOT NULL DEFAULT '0'
                        CHECK(typeof(advertising_fee_withheld_cny) = 'text'),
                    advertising_fee_debt_cny TEXT NOT NULL DEFAULT '0'
                        CHECK(typeof(advertising_fee_debt_cny) = 'text'),
                    settled_at TEXT,
                    external_reference TEXT,
                    source_type TEXT NOT NULL DEFAULT 'user_provided',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE withdrawal_record_events(
                    record_id TEXT NOT NULL REFERENCES withdrawal_records(record_id),
                    event_id TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('reported', 'readback_confirmed', 'voided')),
                    occurred_at TEXT NOT NULL,
                    external_reference TEXT,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(record_id, event_id)
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE advertising_debt_allocations(
                    allocation_id TEXT PRIMARY KEY,
                    payment_id TEXT NOT NULL REFERENCES advertising_payment_readbacks(payment_id),
                    withdrawal_record_id TEXT NOT NULL REFERENCES withdrawal_records(record_id),
                    amount_cny TEXT NOT NULL CHECK(typeof(amount_cny) = 'text'),
                    allocated_at TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(payment_id, withdrawal_record_id)
                )
                """
            )

            withdrawal_columns = (
                "record_id, idempotency_key, store_alias, withdrawal_amount_cny, "
                "withdrawn_at, status, advertising_fee_status, "
                "advertising_fee_due_cny, advertising_fee_withheld_cny, "
                "advertising_fee_debt_cny, settled_at, external_reference, "
                "source_type, evidence_json, payload_hash, created_at"
            )
            self.connection.execute(
                f"INSERT INTO withdrawal_records({withdrawal_columns}) "
                f"SELECT {withdrawal_columns} "
                "FROM withdrawal_records_before_settlement_balance"
            )
            event_columns = (
                "record_id, event_id, status, occurred_at, external_reference, evidence_json"
            )
            self.connection.execute(
                f"INSERT INTO withdrawal_record_events({event_columns}) "
                f"SELECT {event_columns} "
                "FROM withdrawal_record_events_before_settlement_balance"
            )
            allocation_columns = (
                "allocation_id, payment_id, withdrawal_record_id, amount_cny, "
                "allocated_at, evidence_json"
            )
            self.connection.execute(
                f"INSERT INTO advertising_debt_allocations({allocation_columns}) "
                f"SELECT {allocation_columns} "
                "FROM advertising_debt_allocations_before_settlement_balance"
            )

            self.connection.execute(
                "DROP TABLE withdrawal_record_events_before_settlement_balance"
            )
            self.connection.execute(
                "DROP TABLE advertising_debt_allocations_before_settlement_balance"
            )
            self.connection.execute(
                "DROP TABLE withdrawal_records_before_settlement_balance"
            )

            for item in schema_objects:
                self.connection.execute(str(item["sql"]))

            violations: list[sqlite3.Row] = []
            for table in (
                "withdrawal_records",
                "withdrawal_record_events",
                "advertising_debt_allocations",
            ):
                violations.extend(
                    self.connection.execute(f"PRAGMA foreign_key_check({table})").fetchall()
                )
            if violations:
                details = [tuple(row) for row in violations]
                raise FinanceLedgerError(
                    f"Withdrawal status migration produced foreign-key violations: {details}"
                )
        except Exception:
            self.connection.execute(f"ROLLBACK TO {savepoint}")
            self.connection.execute(f"RELEASE {savepoint}")
            raise
        else:
            self.connection.execute(f"RELEASE {savepoint}")

    def _migrate_funding_status_constraints(self) -> None:
        actions_sql = str(
            self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='funding_actions'"
            ).fetchone()[0]
            or ""
        )
        events_sql = str(
            self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='funding_action_events'"
            ).fetchone()[0]
            or ""
        )
        if "readback_confirmed" in actions_sql and "readback_confirmed" in events_sql:
            return

        self.connection.executescript(
            """
            ALTER TABLE funding_action_events RENAME TO funding_action_events_before_v3;
            ALTER TABLE funding_actions RENAME TO funding_actions_before_v3;

            CREATE TABLE funding_actions(
                action_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                store_alias TEXT NOT NULL REFERENCES store_registry(store_alias),
                report_id TEXT REFERENCES reports(report_id),
                action_type TEXT NOT NULL CHECK(action_type IN ('withdrawal', 'advertising_top_up')),
                requested_amount_cny TEXT NOT NULL CHECK(typeof(requested_amount_cny) = 'text'),
                status TEXT NOT NULL CHECK(status IN ('proposed', 'approved', 'executing', 'unknown', 'readback_pending', 'readback_confirmed', 'executed', 'failed', 'cancelled')),
                lifecycle_status TEXT NOT NULL DEFAULT 'proposed' CHECK(lifecycle_status IN ('proposed', 'approved', 'executing', 'unknown', 'readback_pending', 'readback_confirmed', 'executed', 'failed', 'cancelled')),
                requested_at TEXT NOT NULL,
                approved_at TEXT,
                executed_at TEXT,
                external_reference TEXT,
                evidence_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL
            );

            CREATE TABLE funding_action_events(
                action_id TEXT NOT NULL REFERENCES funding_actions(action_id),
                event_id TEXT NOT NULL,
                lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN ('proposed', 'approved', 'executing', 'unknown', 'readback_pending', 'readback_confirmed', 'executed', 'failed', 'cancelled')),
                occurred_at TEXT NOT NULL,
                external_reference TEXT,
                evidence_json TEXT NOT NULL,
                PRIMARY KEY(action_id, event_id)
            );

            INSERT INTO funding_actions SELECT * FROM funding_actions_before_v3;
            INSERT INTO funding_action_events SELECT * FROM funding_action_events_before_v3;
            DROP TABLE funding_action_events_before_v3;
            DROP TABLE funding_actions_before_v3;
            """
        )

    def _table_columns(self, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _backfill_cost_version_metadata(self) -> None:
        rows = self.connection.execute("SELECT * FROM cost_versions").fetchall()
        for row in rows:
            sku = str(row["sku"])
            model, capacity, color = _product_parts(sku)
            purchase_rub = row["purchase_cost_rub"]
            if purchase_rub is None:
                purchase_rub = decimal_text(
                    Decimal(str(row["purchase_cost_cny"]))
                    * Decimal(str(row["exchange_rate_rub_per_cny"])),
                    required=True,
                )
            uid = str(row["cost_version_uid"] or "").strip() or _cost_version_uid(
                {
                    "sku": sku,
                    "effective_from": row["effective_from"],
                    "effective_to": row["effective_to"],
                    "purchase_cost_cny": row["purchase_cost_cny"],
                    "quote_sha256": row["quote_sha256"],
                    "quote_row": row["quote_row"],
                    "exchange_rate_rub_per_cny": row["exchange_rate_rub_per_cny"],
                }
            )
            self.connection.execute(
                """
                UPDATE cost_versions SET
                    cost_version_uid=?, model=COALESCE(model, ?),
                    capacity=COALESCE(capacity, ?), color=COALESCE(color, ?),
                    sku_aliases_json=CASE WHEN sku_aliases_json='[]' THEN ? ELSE sku_aliases_json END,
                    purchase_cost_rub=COALESCE(purchase_cost_rub, ?),
                    source_currency=COALESCE(NULLIF(source_currency, ''), 'RUB')
                WHERE cost_version_id=?
                """,
                (
                    uid, model, capacity, color, canonical_json([sku]), purchase_rub,
                    row["cost_version_id"],
                ),
            )

    def register_store(
        self,
        store_alias: str,
        *,
        scope_id: str,
        currency: str = "CNY",
        active: bool = True,
        metadata: Mapping[str, Any] | None = None,
        at: datetime | str | None = None,
    ) -> None:
        alias = _required_text(store_alias, "store_alias")
        scope = _required_text(scope_id, "scope_id")
        if currency != "CNY":
            raise FinanceLedgerError(f"Store currency must be CNY, got {currency}")
        timestamp = normalize_iso(at or datetime.now(timezone.utc))
        metadata_json = canonical_json(dict(metadata or {}))
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO store_registry(
                    store_alias, scope_id, currency, active, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(store_alias) DO UPDATE SET
                    scope_id=excluded.scope_id,
                    currency=excluded.currency,
                    active=excluded.active,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (alias, scope, currency, int(active), metadata_json, timestamp, timestamp),
            )

    def upsert_finance(
        self,
        store_alias: str,
        rrd_id: str | int,
        payload: Mapping[str, Any],
        *,
        order_id: str | int | None = None,
        srid: str | None = None,
        shk_id: str | int | None = None,
        occurred_at: datetime | str | None = None,
        buyer_paid_cny: Any = None,
        for_pay_cny: Any = None,
        commission_cny: Any = None,
        logistics_cny: Any = None,
        return_logistics_cny: Any = None,
        acquiring_fee_cny: Any = None,
        other_cost_cny: Any = None,
        fetched_at: datetime | str | None = None,
    ) -> UpsertResult:
        key = _required_text(str(rrd_id), "rrd_id")
        alias = _required_text(store_alias, "store_alias")
        serialized = canonical_json(dict(payload))
        payload_hash = sha256_text(serialized)
        fetched = normalize_iso(fetched_at or datetime.now(timezone.utc))
        occurred = normalize_iso(occurred_at) if occurred_at is not None else None
        normalized_values = {
            "order_id": _optional_text(order_id),
            "srid": _optional_text(srid),
            "shk_id": _optional_text(shk_id),
            "occurred_at": occurred,
            "buyer_paid_cny": decimal_text(buyer_paid_cny),
            "for_pay_cny": decimal_text(for_pay_cny),
            "commission_cny": decimal_text(commission_cny),
            "logistics_cny": decimal_text(logistics_cny),
            "return_logistics_cny": decimal_text(return_logistics_cny),
            "acquiring_fee_cny": decimal_text(acquiring_fee_cny),
            "other_cost_cny": decimal_text(other_cost_cny),
        }
        existing = self.connection.execute(
            """
            SELECT payload_hash, order_id, srid, shk_id, occurred_at,
                   buyer_paid_cny, for_pay_cny, commission_cny, logistics_cny,
                   return_logistics_cny, acquiring_fee_cny, other_cost_cny
            FROM raw_finance WHERE store_alias=? AND rrd_id=?
            """,
            (alias, key),
        ).fetchone()
        existed = existing is not None
        changed = not existed or str(existing["payload_hash"] or "") != payload_hash
        if existing is not None and not changed:
            changed = any(
                existing[column] != value
                for column, value in normalized_values.items()
            )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO raw_finance(
                    store_alias, rrd_id, order_id, srid, shk_id, occurred_at,
                    buyer_paid_cny, for_pay_cny, commission_cny, logistics_cny,
                    return_logistics_cny, acquiring_fee_cny, other_cost_cny,
                    payload_json, payload_hash, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(store_alias, rrd_id) DO UPDATE SET
                    order_id=excluded.order_id, srid=excluded.srid, shk_id=excluded.shk_id,
                    occurred_at=excluded.occurred_at, buyer_paid_cny=excluded.buyer_paid_cny,
                    for_pay_cny=excluded.for_pay_cny, commission_cny=excluded.commission_cny,
                    logistics_cny=excluded.logistics_cny,
                    return_logistics_cny=excluded.return_logistics_cny,
                    acquiring_fee_cny=excluded.acquiring_fee_cny,
                    other_cost_cny=excluded.other_cost_cny,
                    payload_json=excluded.payload_json, payload_hash=excluded.payload_hash,
                    fetched_at=excluded.fetched_at
                """,
                (
                    alias, key, normalized_values["order_id"], normalized_values["srid"],
                    normalized_values["shk_id"], normalized_values["occurred_at"],
                    normalized_values["buyer_paid_cny"], normalized_values["for_pay_cny"],
                    normalized_values["commission_cny"], normalized_values["logistics_cny"],
                    normalized_values["return_logistics_cny"], normalized_values["acquiring_fee_cny"],
                    normalized_values["other_cost_cny"], serialized, payload_hash, fetched,
                ),
            )
        return UpsertResult(key=key, inserted=not existed, changed=changed)

    def upsert_sale(
        self,
        store_alias: str,
        identity: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        occurred_at: datetime | str,
        sale_id: str | None = None,
        order_id: str | int | None = None,
        srid: str | None = None,
        buyer_paid_cny: Any = None,
        for_pay_cny: Any = None,
        fetched_at: datetime | str | None = None,
    ) -> UpsertResult:
        event_hash = stable_event_hash(store_alias, "sales", identity)
        return self._upsert_generic(
            "raw_sales", store_alias, event_hash, payload,
            occurred_at=occurred_at, fetched_at=fetched_at,
            values={
                "sale_id": _optional_text(sale_id),
                "order_id": _optional_text(order_id),
                "srid": _optional_text(srid),
                "buyer_paid_cny": decimal_text(buyer_paid_cny),
                "for_pay_cny": decimal_text(for_pay_cny),
            },
        )

    def upsert_return(
        self,
        store_alias: str,
        identity: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        occurred_at: datetime | str,
        return_id: str | None = None,
        order_id: str | int | None = None,
        srid: str | None = None,
        shk_id: str | int | None = None,
        actual_logistics_cny: Any = None,
        refund_cny: Any = None,
        fetched_at: datetime | str | None = None,
    ) -> UpsertResult:
        event_hash = stable_event_hash(store_alias, "returns", identity)
        return self._upsert_generic(
            "raw_returns", store_alias, event_hash, payload,
            occurred_at=occurred_at, fetched_at=fetched_at,
            values={
                "return_id": _optional_text(return_id),
                "order_id": _optional_text(order_id),
                "srid": _optional_text(srid),
                "shk_id": _optional_text(shk_id),
                "actual_logistics_cny": decimal_text(actual_logistics_cny),
                "refund_cny": decimal_text(refund_cny),
            },
        )

    def upsert_ad(
        self,
        store_alias: str,
        identity: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        occurred_at: datetime | str,
        campaign_id: str | int | None = None,
        nm_id: str | int | None = None,
        spend_cny: Any = None,
        fetched_at: datetime | str | None = None,
    ) -> UpsertResult:
        event_hash = stable_event_hash(store_alias, "advertising", identity)
        return self._upsert_generic(
            "raw_ad", store_alias, event_hash, payload,
            occurred_at=occurred_at, fetched_at=fetched_at,
            values={
                "campaign_id": _optional_text(campaign_id),
                "nm_id": _optional_text(nm_id),
                "spend_cny": decimal_text(spend_cny),
            },
        )

    def upsert_balance(
        self,
        store_alias: str,
        identity: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        observed_at: datetime | str,
        account_balance_cny: Any = None,
        withdrawable_balance_cny: Any = None,
        ad_balance_cny: Any = None,
        fetched_at: datetime | str | None = None,
    ) -> UpsertResult:
        event_hash = stable_event_hash(store_alias, "balance", identity)
        return self._upsert_generic(
            "raw_balance", store_alias, event_hash, payload,
            occurred_at=observed_at, fetched_at=fetched_at,
            values={
                "account_balance_cny": decimal_text(account_balance_cny),
                "withdrawable_balance_cny": decimal_text(withdrawable_balance_cny),
                "ad_balance_cny": decimal_text(ad_balance_cny),
            },
        )

    def _upsert_generic(
        self,
        table: str,
        store_alias: str,
        event_hash: str,
        payload: Mapping[str, Any],
        *,
        occurred_at: datetime | str,
        fetched_at: datetime | str | None,
        values: Mapping[str, Any],
    ) -> UpsertResult:
        specs = {
            "raw_sales": ("occurred_at", tuple(values)),
            "raw_returns": ("occurred_at", tuple(values)),
            "raw_ad": ("occurred_at", tuple(values)),
            "raw_balance": ("observed_at", tuple(values)),
        }
        if table not in specs:
            raise FinanceLedgerError(f"Unsupported raw event table: {table}")
        time_column, columns = specs[table]
        allowed = {
            "raw_sales": {"sale_id", "order_id", "srid", "buyer_paid_cny", "for_pay_cny"},
            "raw_returns": {"return_id", "order_id", "srid", "shk_id", "actual_logistics_cny", "refund_cny"},
            "raw_ad": {"campaign_id", "nm_id", "spend_cny"},
            "raw_balance": {"account_balance_cny", "withdrawable_balance_cny", "ad_balance_cny"},
        }[table]
        if set(columns) != allowed:
            raise FinanceLedgerError(f"Invalid columns for {table}: {set(columns) ^ allowed}")

        alias = _required_text(store_alias, "store_alias")
        occurred = normalize_iso(occurred_at)
        fetched = normalize_iso(fetched_at or datetime.now(timezone.utc))
        serialized = canonical_json(dict(payload))
        payload_hash = sha256_text(serialized)
        existing = self.connection.execute(
            f"SELECT payload_hash, {', '.join(columns)}, {time_column} "
            f"FROM {table} WHERE store_alias=? AND event_hash=?",
            (alias, event_hash),
        ).fetchone()
        existed = existing is not None
        changed = not existed or str(existing["payload_hash"] or "") != payload_hash
        if existing is not None and not changed:
            changed = any(
                existing[column] != values[column] for column in columns
            ) or existing[time_column] != occurred
        all_columns = ["store_alias", "event_hash", *columns, time_column, "payload_json", "payload_hash", "fetched_at"]
        placeholders = ", ".join("?" for _ in all_columns)
        updates = ", ".join(
            f"{column}=excluded.{column}"
            for column in [*columns, time_column, "payload_json", "payload_hash", "fetched_at"]
        )
        params = [alias, event_hash, *(values[column] for column in columns), occurred, serialized, payload_hash, fetched]
        with self.connection:
            self.connection.execute(
                f"INSERT INTO {table}({', '.join(all_columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(store_alias, event_hash) DO UPDATE SET {updates}",
                params,
            )
        return UpsertResult(key=event_hash, inserted=not existed, changed=changed)

    def start_sync(
        self,
        store_alias: str,
        source: str,
        requested_from: datetime | str,
        requested_through: datetime | str,
        *,
        run_id: str | None = None,
        started_at: datetime | str | None = None,
    ) -> str:
        alias = _required_text(store_alias, "store_alias")
        source_name = _required_text(source, "source")
        start = normalize_iso(requested_from)
        through = normalize_iso(requested_through)
        if through < start:
            raise FinanceLedgerError("requested_through must be on or after requested_from")
        identifier = run_id or str(uuid.uuid4())
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO sync_runs(
                    run_id, store_alias, source, requested_from, requested_through,
                    started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    identifier, alias, source_name, start, through,
                    normalize_iso(started_at or datetime.now(timezone.utc)),
                ),
            )
        return identifier

    def finish_sync(
        self,
        run_id: str,
        status: str,
        *,
        completed_at: datetime | str | None = None,
        watermark_at: datetime | str | None = None,
        rows_seen: int = 0,
        rows_inserted: int = 0,
        rows_updated: int = 0,
        error_text: str | None = None,
    ) -> None:
        if status not in _SYNC_STATUSES - {"running"}:
            raise FinanceLedgerError(f"Invalid final sync status: {status}")
        row = self.connection.execute(
            "SELECT * FROM sync_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise FinanceLedgerError(f"Unknown sync run: {run_id}")
        if row["status"] != "running":
            raise FinanceLedgerError(f"Sync run is already final: {run_id}")
        if status == "complete" and watermark_at is None:
            raise FinanceLedgerError("A complete sync run requires watermark_at")
        completed = normalize_iso(completed_at or datetime.now(timezone.utc))

        with self.connection:
            self.connection.execute(
                """
                UPDATE sync_runs SET
                    completed_at=?, status=?, rows_seen=?, rows_inserted=?, rows_updated=?, error_text=?
                WHERE run_id=?
                """,
                (
                    completed, status, int(rows_seen), int(rows_inserted), int(rows_updated),
                    error_text, run_id,
                ),
            )
            if status == "complete":
                watermark = normalize_iso(watermark_at)
                self.connection.execute(
                    """
                    INSERT INTO sync_watermarks(store_alias, source, watermark_at, run_id, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(store_alias, source) DO UPDATE SET
                        watermark_at=CASE
                            WHEN excluded.watermark_at > sync_watermarks.watermark_at
                            THEN excluded.watermark_at ELSE sync_watermarks.watermark_at END,
                        run_id=CASE
                            WHEN excluded.watermark_at > sync_watermarks.watermark_at
                            THEN excluded.run_id ELSE sync_watermarks.run_id END,
                        updated_at=CASE
                            WHEN excluded.watermark_at > sync_watermarks.watermark_at
                            THEN excluded.updated_at ELSE sync_watermarks.updated_at END
                    """,
                    (row["store_alias"], row["source"], watermark, run_id, completed),
                )
                self.connection.execute(
                    """
                    UPDATE sync_gaps SET status='resolved', resolved_at=?
                    WHERE store_alias=? AND source=? AND status='open'
                      AND gap_from >= ? AND gap_through <= ?
                    """,
                    (
                        completed, row["store_alias"], row["source"],
                        row["requested_from"], row["requested_through"],
                    ),
                )
            else:
                self._record_gap_in_transaction(
                    row["store_alias"], row["source"], row["requested_from"],
                    row["requested_through"], error_text or status, completed, run_id,
                )

    def get_watermark(self, store_alias: str, source: str) -> str | None:
        row = self.connection.execute(
            "SELECT watermark_at FROM sync_watermarks WHERE store_alias=? AND source=?",
            (store_alias, source),
        ).fetchone()
        return str(row["watermark_at"]) if row else None

    def record_gap(
        self,
        store_alias: str,
        source: str,
        gap_from: datetime | str,
        gap_through: datetime | str,
        reason: str,
        *,
        run_id: str | None = None,
        detected_at: datetime | str | None = None,
    ) -> str:
        start = normalize_iso(gap_from)
        through = normalize_iso(gap_through)
        if through < start:
            raise FinanceLedgerError("gap_through must be on or after gap_from")
        detected = normalize_iso(detected_at or datetime.now(timezone.utc))
        with self.connection:
            return self._record_gap_in_transaction(
                store_alias, source, start, through, reason, detected, run_id
            )

    def _record_gap_in_transaction(
        self,
        store_alias: str,
        source: str,
        start: str,
        through: str,
        reason: str,
        detected: str,
        run_id: str | None,
    ) -> str:
        gap_id = stable_event_hash(
            store_alias, "sync-gap", {"source": source, "from": start, "through": through}
        )
        self.connection.execute(
            """
            INSERT INTO sync_gaps(
                gap_id, store_alias, source, gap_from, gap_through, reason,
                status, detected_at, resolved_at, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, NULL, ?)
            ON CONFLICT(store_alias, source, gap_from, gap_through) DO UPDATE SET
                reason=excluded.reason, status='open', detected_at=excluded.detected_at,
                resolved_at=NULL, run_id=excluded.run_id
            """,
            (
                gap_id, store_alias, source, start, through,
                _required_text(reason, "reason"), detected, run_id,
            ),
        )
        return gap_id

    def list_open_gaps(self, store_alias: str | None = None) -> list[sqlite3.Row]:
        if store_alias is None:
            rows = self.connection.execute(
                "SELECT * FROM sync_gaps WHERE status='open' ORDER BY store_alias, source, gap_from"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM sync_gaps WHERE status='open' AND store_alias=? ORDER BY source, gap_from",
                (store_alias,),
            ).fetchall()
        return list(rows)

    def trim_open_gaps_before(
        self,
        store_alias: str,
        cutoff: datetime | str,
        *,
        resolved_at: datetime | str | None = None,
    ) -> None:
        """Remove pre-opening portions from unresolved collection gaps.

        Store opening dates can be learned after an exploratory bootstrap.  A
        failed broad scan must not keep dates before the store existed open
        forever, but the in-scope tail remains auditable and retryable.
        """
        alias = _required_text(store_alias, "store_alias")
        boundary = normalize_iso(cutoff)
        resolved = normalize_iso(resolved_at or datetime.now(timezone.utc))
        rows = self.connection.execute(
            """
            SELECT * FROM sync_gaps
            WHERE store_alias=? AND status='open' AND gap_from < ?
            ORDER BY source, gap_from
            """,
            (alias, boundary),
        ).fetchall()
        with self.connection:
            for row in rows:
                self.connection.execute(
                    "UPDATE sync_gaps SET status='resolved', resolved_at=? WHERE gap_id=?",
                    (resolved, row["gap_id"]),
                )
                if row["gap_through"] < boundary:
                    continue
                covered = self.connection.execute(
                    """
                    SELECT 1 FROM sync_runs
                    WHERE store_alias=? AND source=? AND status='complete'
                      AND requested_from <= ? AND requested_through >= ?
                    LIMIT 1
                    """,
                    (alias, row["source"], boundary, row["gap_through"]),
                ).fetchone()
                if covered is not None:
                    continue
                self._record_gap_in_transaction(
                    alias,
                    row["source"],
                    boundary,
                    row["gap_through"],
                    f"{row['reason']} (trimmed to store opening)",
                    resolved,
                    row["run_id"],
                )

    def add_cost_version(
        self,
        sku: str,
        *,
        effective_from: datetime | str,
        effective_to: datetime | str | None,
        purchase_cost_cny: Any,
        quote_sha256: str,
        quote_row: int,
        exchange_rate_rub_per_cny: Any,
        imported_at: datetime | str | None = None,
        cost_version_uid: str | None = None,
        model: str | None = None,
        capacity: str | None = None,
        color: str | None = None,
        sku_aliases: Sequence[str] | None = None,
        purchase_cost_rub: Any = None,
        source_currency: str = "RUB",
        quote_path: str | None = None,
        quote_sheet: str | None = None,
        row_sha256: str | None = None,
        approved_by: str | None = None,
        approved_at: datetime | str | None = None,
    ) -> int:
        return self.import_cost_versions(
            [
                {
                    "sku": sku,
                    "effective_from": effective_from,
                    "effective_to": effective_to,
                    "purchase_cost_cny": purchase_cost_cny,
                    "quote_sha256": quote_sha256,
                    "quote_row": quote_row,
                    "exchange_rate_rub_per_cny": exchange_rate_rub_per_cny,
                    "imported_at": imported_at,
                    "cost_version_uid": cost_version_uid,
                    "model": model,
                    "capacity": capacity,
                    "color": color,
                    "sku_aliases": list(sku_aliases or [sku]),
                    "purchase_cost_rub": purchase_cost_rub,
                    "source_currency": source_currency,
                    "quote_path": quote_path,
                    "quote_sheet": quote_sheet,
                    "row_sha256": row_sha256,
                    "approved_by": approved_by,
                    "approved_at": approved_at,
                }
            ]
        )[0]

    def import_cost_versions(self, records: Iterable[Mapping[str, Any]]) -> list[int]:
        normalized = sorted(
            (self._normalize_cost_record(record) for record in records),
            key=lambda item: (item["sku"], item["effective_from"]),
        )
        identifiers: list[int] = []
        with self.connection:
            for record in normalized:
                existing = self.connection.execute(
                    """
                    SELECT * FROM cost_versions
                    WHERE cost_version_uid=?
                    """,
                    (record["cost_version_uid"],),
                ).fetchone()
                if existing is not None:
                    expected_effective_to = record["effective_to"]
                    if expected_effective_to is None and existing["effective_to"] is not None:
                        # Open-ended quote imports are closed by the ledger when a newer
                        # version arrives. Re-importing the original quote stays idempotent.
                        expected_effective_to = existing["effective_to"]
                    immutable_columns = (
                        "sku", "model", "capacity", "color", "sku_aliases_json",
                        "effective_from", "purchase_cost_rub", "purchase_cost_cny",
                        "source_currency", "quote_path", "quote_sha256", "quote_sheet",
                        "quote_row", "row_sha256", "exchange_rate_rub_per_cny",
                        "approved_by", "approved_at",
                    )
                    comparable = tuple(existing[column] for column in immutable_columns)
                    expected = tuple(record[column] for column in immutable_columns)
                    comparable += (existing["effective_to"],)
                    expected += (expected_effective_to,)
                    if comparable != expected:
                        raise OverlappingCostVersionError(
                            f"Cost version ID already has different financial content: {record['cost_version_uid']}"
                        )
                    identifiers.append(int(existing["cost_version_id"]))
                    continue

                prior_open = self.connection.execute(
                    """
                    SELECT cost_version_id, effective_from FROM cost_versions
                    WHERE sku=? AND effective_to IS NULL AND effective_from < ?
                    ORDER BY effective_from DESC
                    """,
                    (record["sku"], record["effective_from"]),
                ).fetchall()
                if len(prior_open) > 1:
                    raise OverlappingCostVersionError(
                        f"Multiple open cost versions prevent interval closure for {record['sku']}"
                    )
                if prior_open:
                    self.connection.execute(
                        "UPDATE cost_versions SET effective_to=? WHERE cost_version_id=?",
                        (record["effective_from"], prior_open[0]["cost_version_id"]),
                    )

                overlap = self.connection.execute(
                    """
                    SELECT cost_version_id, effective_from, effective_to
                    FROM cost_versions
                    WHERE sku=?
                      AND (effective_to IS NULL OR effective_to > ?)
                      AND (? IS NULL OR effective_from < ?)
                    LIMIT 1
                    """,
                    (
                        record["sku"], record["effective_from"],
                        record["effective_to"], record["effective_to"],
                    ),
                ).fetchone()
                if overlap is not None:
                    raise OverlappingCostVersionError(
                        f"Cost interval overlaps for {record['sku']}: "
                        f"{record['effective_from']}..{record['effective_to'] or 'open'} conflicts "
                        f"with {overlap['effective_from']}..{overlap['effective_to'] or 'open'}"
                    )
                cursor = self.connection.execute(
                    """
                    INSERT INTO cost_versions(
                        cost_version_uid, sku, model, capacity, color, sku_aliases_json,
                        effective_from, effective_to, purchase_cost_rub, purchase_cost_cny,
                        source_currency, quote_path, quote_sha256, quote_sheet, quote_row,
                        row_sha256, exchange_rate_rub_per_cny, approved_by, approved_at,
                        imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["cost_version_uid"], record["sku"], record["model"],
                        record["capacity"], record["color"], record["sku_aliases_json"],
                        record["effective_from"], record["effective_to"],
                        record["purchase_cost_rub"], record["purchase_cost_cny"],
                        record["source_currency"], record["quote_path"],
                        record["quote_sha256"], record["quote_sheet"],
                        record["quote_row"], record["row_sha256"],
                        record["exchange_rate_rub_per_cny"], record["approved_by"],
                        record["approved_at"], record["imported_at"],
                    ),
                )
                identifiers.append(int(cursor.lastrowid))
        return identifiers

    def _normalize_cost_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        start_value = _pick(record, "effective_from", "effectiveFrom")
        if start_value is None:
            raise FinanceLedgerError("Cost effective_from is required")
        start = normalize_iso(start_value)
        end_value = _pick(record, "effective_to", "effectiveTo")
        end = normalize_iso(end_value) if end_value is not None else None
        if end is not None and end <= start:
            raise FinanceLedgerError("Cost effective_to must be later than effective_from")
        quote_hash = str(
            _pick(record, "quote_sha256", "sourceSha256", "source_hash") or ""
        ).strip().lower()
        if not _SHA256_RE.fullmatch(quote_hash):
            raise FinanceLedgerError("quote_sha256 must be a 64-character lowercase SHA-256")
        quote_row = int(_pick(record, "quote_row", "sourceRow", "source_row") or 0)
        if quote_row <= 0:
            raise FinanceLedgerError("quote_row must be greater than zero")
        purchase_cost = decimal_text(
            _pick(record, "purchase_cost_cny", "purchaseCostCny", "cost_cny"),
            required=True,
        )
        exchange_rate = decimal_text(
            _pick(
                record,
                "exchange_rate_rub_per_cny",
                "exchangeRateRubPerCny",
                "rub_per_cny",
            ),
            required=True,
        )
        if Decimal(purchase_cost) < 0 or Decimal(exchange_rate) <= 0:
            raise FinanceLedgerError("Cost must be non-negative and exchange rate must be positive")
        sku = _required_text(
            str(_pick(record, "sku", "productKey", "product_key") or ""), "sku"
        )
        derived_model, derived_capacity, derived_color = _product_parts(sku)
        aliases_value = _pick(record, "sku_aliases", "skuAliases", "aliases")
        if aliases_value is None:
            # ``finance_costs.CostVersionRow.as_dict`` preserves the original
            # quote specification.  Keep it as an alias so later order feeds can
            # resolve either the normalized product key or the supplier spelling.
            specification = _optional_text(_pick(record, "specification"))
            aliases_value = [sku, specification] if specification else [sku]
        if isinstance(aliases_value, str):
            aliases_value = [aliases_value]
        aliases = sorted(
            {_required_text(str(alias), "sku alias") for alias in aliases_value} | {sku}
        )
        purchase_rub_value = _pick(record, "purchase_cost_rub", "costRub", "cost_rub")
        purchase_rub = decimal_text(purchase_rub_value)
        if purchase_rub is None:
            purchase_rub = decimal_text(
                Decimal(purchase_cost) * Decimal(exchange_rate), required=True
            )
        source_currency = _required_text(
            str(_pick(record, "source_currency", "sourceCurrency") or "RUB"),
            "source_currency",
        ).upper()
        row_hash_value = str(
            _pick(record, "row_sha256", "rowSha256", "row_hash") or ""
        ).strip().lower()
        if row_hash_value and not _SHA256_RE.fullmatch(row_hash_value):
            raise FinanceLedgerError("row_sha256 must be a 64-character lowercase SHA-256")
        approved_by = _optional_text(_pick(record, "approved_by", "approvedBy"))
        approved_at_value = _pick(record, "approved_at", "approvedAt")
        approved_at = normalize_iso(approved_at_value) if approved_at_value is not None else None
        if (approved_by is None) != (approved_at is None):
            raise FinanceLedgerError("approved_by and approved_at must be supplied together")
        normalized = {
            "sku": sku,
            "model": _optional_text(_pick(record, "model")) or derived_model,
            "capacity": _optional_text(_pick(record, "capacity")) or derived_capacity,
            "color": _optional_text(_pick(record, "color")) or derived_color,
            "sku_aliases_json": canonical_json(aliases),
            "effective_from": start,
            "effective_to": end,
            "purchase_cost_rub": purchase_rub,
            "purchase_cost_cny": purchase_cost,
            "source_currency": source_currency,
            "quote_path": _optional_text(
                _pick(record, "quote_path", "sourcePath", "source_path")
            ),
            "quote_sha256": quote_hash,
            "quote_sheet": _optional_text(
                _pick(record, "quote_sheet", "sourceSheet", "source_sheet")
            ),
            "quote_row": quote_row,
            "row_sha256": row_hash_value or None,
            "exchange_rate_rub_per_cny": exchange_rate,
            "approved_by": approved_by,
            "approved_at": approved_at,
            "imported_at": normalize_iso(
                _pick(record, "imported_at", "importedAt") or datetime.now(timezone.utc)
            ),
        }
        normalized["cost_version_uid"] = _required_text(
            str(
                _pick(
                    record,
                    "cost_version_uid",
                    "costVersionId",
                    "version_id",
                )
                or _cost_version_uid(normalized)
            ),
            "cost_version_uid",
        )
        return normalized

    def select_cost_version(
        self,
        sku: str,
        order_created_at: datetime | str,
        *,
        order_completed_at: datetime | str | None = None,
    ) -> CostVersion:
        created = normalize_iso(order_created_at)
        if order_completed_at is not None:
            normalize_iso(order_completed_at)  # Validate only; completion never selects cost.
        rows = self.connection.execute(
            """
            SELECT * FROM cost_versions
            WHERE sku=? AND effective_from <= ?
              AND (effective_to IS NULL OR ? < effective_to)
            ORDER BY effective_from DESC
            """,
            (_required_text(sku, "sku"), created, created),
        ).fetchall()
        if not rows:
            raise MissingCostVersionError(
                f"No cost version for {sku} at order creation time {created}"
            )
        if len(rows) != 1:
            raise OverlappingCostVersionError(
                f"Multiple cost versions for {sku} at order creation time {created}"
            )
        row = rows[0]
        return CostVersion(
            cost_version_id=int(row["cost_version_id"]),
            cost_version_uid=str(row["cost_version_uid"]),
            sku=str(row["sku"]),
            model=str(row["model"]) if row["model"] is not None else None,
            capacity=str(row["capacity"]) if row["capacity"] is not None else None,
            color=str(row["color"]) if row["color"] is not None else None,
            sku_aliases=tuple(json.loads(row["sku_aliases_json"] or "[]")),
            effective_from=str(row["effective_from"]),
            effective_to=str(row["effective_to"]) if row["effective_to"] is not None else None,
            purchase_cost_rub=(
                Decimal(row["purchase_cost_rub"])
                if row["purchase_cost_rub"] is not None else None
            ),
            purchase_cost_cny=Decimal(row["purchase_cost_cny"]),
            source_currency=str(row["source_currency"]),
            quote_path=str(row["quote_path"]) if row["quote_path"] is not None else None,
            quote_sha256=str(row["quote_sha256"]),
            quote_sheet=str(row["quote_sheet"]) if row["quote_sheet"] is not None else None,
            quote_row=int(row["quote_row"]),
            row_sha256=str(row["row_sha256"]) if row["row_sha256"] is not None else None,
            exchange_rate_rub_per_cny=Decimal(row["exchange_rate_rub_per_cny"]),
            approved_by=str(row["approved_by"]) if row["approved_by"] is not None else None,
            approved_at=str(row["approved_at"]) if row["approved_at"] is not None else None,
        )

    def upsert_order_profit(
        self,
        store_alias: str,
        order_key: str,
        *,
        order_created_at: datetime | str,
        completed_at: datetime | str | None,
        sku: str,
        cost_version_id: int,
        policy_id: str,
        revenue_cny: Any,
        purchase_cost_cny: Any,
        commission_cny: Any,
        managed_operations_cny: Any,
        combined_pool_cny: Any,
        actual_return_logistics_cny: Any,
        actual_ad_spend_reconciliation_cny: Any,
        payment_cny: Any,
        fixed_operations_cny: Any,
        other_cost_cny: Any,
        owner_profit_cny: Any,
        calculation_status: str,
        evidence: Mapping[str, Any],
        advertising_provision_cny: Any = None,
        excess_return_loss_cny: Any = None,
        forward_logistics_cny: Any = 0,
        net_quantity: Any = 1,
        order_result: str | None = None,
        calculated_at: datetime | str | None = None,
        profit_version: str | None = None,
    ) -> str:
        calculated = normalize_iso(calculated_at or datetime.now(timezone.utc))
        combined_pool_text = decimal_text(combined_pool_cny, required=True)
        actual_return_text = decimal_text(actual_return_logistics_cny, required=True)
        combined_pool_amount = Decimal(combined_pool_text)
        actual_return_amount = abs(Decimal(actual_return_text))
        if advertising_provision_cny is None:
            advertising_provision_cny = max(
                Decimal("0"), combined_pool_amount - actual_return_amount
            )
        if excess_return_loss_cny is None:
            excess_return_loss_cny = max(
                Decimal("0"), actual_return_amount - combined_pool_amount
            )
        values = {
            "store_alias": _required_text(store_alias, "store_alias"),
            "order_key": _required_text(order_key, "order_key"),
            "order_created_at": normalize_iso(order_created_at),
            "completed_at": normalize_iso(completed_at) if completed_at is not None else None,
            "sku": _required_text(sku, "sku"),
            "cost_version_id": int(cost_version_id),
            "policy_id": _required_text(policy_id, "policy_id"),
            "currency": "CNY",
            "revenue_cny": decimal_text(revenue_cny, required=True),
            "purchase_cost_cny": decimal_text(purchase_cost_cny, required=True),
            "commission_cny": decimal_text(commission_cny, required=True),
            "managed_operations_cny": decimal_text(managed_operations_cny, required=True),
            "combined_pool_cny": combined_pool_text,
            "actual_return_logistics_cny": actual_return_text,
            "actual_ad_spend_reconciliation_cny": decimal_text(actual_ad_spend_reconciliation_cny),
            "advertising_provision_cny": decimal_text(
                advertising_provision_cny, required=True
            ),
            "excess_return_loss_cny": decimal_text(
                excess_return_loss_cny, required=True
            ),
            "forward_logistics_cny": decimal_text(forward_logistics_cny, required=True),
            "net_quantity": decimal_text(net_quantity, required=True),
            "order_result": _required_text(
                order_result or calculation_status, "order_result"
            ),
            "payment_cny": decimal_text(payment_cny, required=True),
            "fixed_operations_cny": decimal_text(fixed_operations_cny, required=True),
            "other_cost_cny": decimal_text(other_cost_cny, required=True),
            "owner_profit_cny": decimal_text(owner_profit_cny, required=True),
            "calculation_status": _required_text(calculation_status, "calculation_status"),
            "evidence_json": canonical_json(dict(evidence)),
            "calculated_at": calculated,
        }
        # Calculation time is audit metadata, not an economic input. Replaying
        # identical evidence must reuse the same immutable profit version.
        version_values = {
            key: value for key, value in values.items() if key != "calculated_at"
        }
        payload_hash = sha256_text(canonical_json(version_values))
        version = profit_version or payload_hash
        existing = self.connection.execute(
            """
            SELECT payload_hash FROM order_profit
            WHERE store_alias=? AND order_key=? AND profit_version=?
            """,
            (values["store_alias"], values["order_key"], version),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise FinanceLedgerError("An order-profit version cannot be overwritten")
            return version
        columns = [*values, "profit_version", "payload_hash"]
        with self.connection:
            self.connection.execute(
                f"INSERT INTO order_profit({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [*(values[column] for column in values), version, payload_hash],
            )
        return version

    def create_report(
        self,
        report_key: str,
        report_version: str,
        *,
        report_type: str,
        period_start: datetime | str,
        period_end: datetime | str,
        policy_id: str,
        totals: Mapping[str, Any],
        orders: Sequence[Mapping[str, Any]] = (),
        generated_at: datetime | str | None = None,
        metadata: Mapping[str, Any] | None = None,
        report_status: str = "FINAL",
        previous_report_id: str | None = None,
        correction_reason: str | None = None,
        correction_metadata: Mapping[str, Any] | None = None,
    ) -> str:
        if report_type not in {"weekly", "monthly", "bootstrap"}:
            raise FinanceLedgerError(f"Unsupported report type: {report_type}")
        key = _required_text(report_key, "report_key")
        version = _required_text(report_version, "report_version")
        status = str(report_status or "").strip().upper()
        if status not in _REPORT_STATUSES:
            raise FinanceLedgerError(f"Unsupported report status: {report_status}")
        previous_id = _optional_text(previous_report_id)
        correction = _optional_text(correction_reason)
        correction_json = canonical_json(dict(correction_metadata or {}))
        if status == "RESTATED" and (previous_id is None or correction is None):
            raise FinanceLedgerError(
                "A RESTATED report requires previous_report_id and correction_reason"
            )
        if previous_id is not None:
            previous = self.connection.execute(
                "SELECT report_key FROM reports WHERE report_id=?", (previous_id,)
            ).fetchone()
            if previous is None:
                raise FinanceLedgerError(f"Previous report does not exist: {previous_id}")
            if previous["report_key"] != key:
                raise FinanceLedgerError("Previous report must use the same report_key")
        generated = normalize_iso(generated_at or datetime.now(timezone.utc))
        normalized_totals = {
            "revenue_cny": decimal_text(totals.get("revenue_cny"), required=True),
            "cost_cny": decimal_text(totals.get("cost_cny"), required=True),
            "profit_cny": decimal_text(totals.get("profit_cny"), required=True),
            "managed_operations_cny": decimal_text(
                totals.get("managed_operations_cny"), required=True
            ),
            "return_fee_cny": decimal_text(totals.get("return_fee_cny"), required=True),
            "advertising_cny": decimal_text(totals.get("advertising_cny"), required=True),
        }
        normalized_orders = []
        for order in orders:
            normalized_orders.append(
                {
                    "store_alias": _required_text(
                        str(order.get("store_alias") or ""), "order.store_alias"
                    ),
                    "order_key": _required_text(
                        str(order.get("order_key") or ""), "order.order_key"
                    ),
                    "profit_version": _required_text(
                        str(order.get("profit_version") or ""), "order.profit_version"
                    ),
                    "owner_profit_cny": decimal_text(
                        order.get("owner_profit_cny"), required=True
                    ),
                    "snapshot_json": canonical_json(dict(order.get("snapshot") or {})),
                }
            )
        content = {
            "report_key": key,
            "report_version": version,
            "report_type": report_type,
            "report_status": status,
            "previous_report_id": previous_id,
            "correction_reason": correction,
            "correction_metadata": dict(correction_metadata or {}),
            "period_start": normalize_iso(period_start),
            "period_end": normalize_iso(period_end),
            "policy_id": _required_text(policy_id, "policy_id"),
            "currency": "CNY",
            "totals": normalized_totals,
            "orders": normalized_orders,
            "metadata": dict(metadata or {}),
        }
        content_hash = sha256_text(canonical_json(content))
        report_id = stable_event_hash(
            "reports", "report-version", {"key": key, "version": version}
        )
        existing = self.connection.execute(
            "SELECT report_id, content_hash FROM reports WHERE report_key=? AND report_version=?",
            (key, version),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != content_hash:
                raise ReportImmutableError(
                    f"Report {key} version {version} already exists with different content"
                )
            return str(existing["report_id"])

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO reports(
                    report_id, report_key, report_version, report_type, report_status,
                    previous_report_id, correction_reason, correction_metadata_json,
                    period_start, period_end, generated_at, policy_id, currency, revenue_cny,
                    cost_cny, profit_cny, managed_operations_cny, return_fee_cny,
                    advertising_cny, content_hash, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CNY', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id, key, version, report_type, status, previous_id,
                    correction, correction_json, content["period_start"],
                    content["period_end"], generated, content["policy_id"],
                    normalized_totals["revenue_cny"], normalized_totals["cost_cny"],
                    normalized_totals["profit_cny"],
                    normalized_totals["managed_operations_cny"],
                    normalized_totals["return_fee_cny"],
                    normalized_totals["advertising_cny"], content_hash,
                    canonical_json(dict(metadata or {})),
                ),
            )
            for order in normalized_orders:
                self.connection.execute(
                    """
                    INSERT INTO report_orders(
                        report_id, store_alias, order_key, profit_version,
                        owner_profit_cny, snapshot_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report_id, order["store_alias"], order["order_key"],
                        order["profit_version"], order["owner_profit_cny"],
                        order["snapshot_json"],
                    ),
                )
        return report_id

    def record_funding_action(
        self,
        idempotency_key: str,
        store_alias: str,
        *,
        action_type: str,
        requested_amount_cny: Any,
        report_id: str | None = None,
        status: str = "proposed",
        requested_at: datetime | str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> str:
        key = _required_text(idempotency_key, "idempotency_key")
        lifecycle_status = str(status or "").strip().lower()
        if lifecycle_status not in _FUNDING_STATUSES:
            raise FinanceLedgerError(f"Unsupported funding status: {status}")
        if action_type not in {"withdrawal", "advertising_top_up"}:
            raise FinanceLedgerError(f"Unsupported funding action type: {action_type}")
        legacy_status = (
            lifecycle_status
            if lifecycle_status in _LEGACY_FUNDING_STATUSES
            else "proposed"
        )
        action_id = stable_event_hash(store_alias, "funding-action", {"key": key})
        payload = {
            "store_alias": store_alias,
            "report_id": report_id,
            "action_type": action_type,
            "requested_amount_cny": decimal_text(requested_amount_cny, required=True),
            "status": lifecycle_status,
            "requested_at": normalize_iso(requested_at or datetime.now(timezone.utc)),
            "evidence_json": canonical_json(dict(evidence or {})),
        }
        payload_hash = sha256_text(canonical_json(payload))
        existing = self.connection.execute(
            "SELECT action_id, payload_hash FROM funding_actions WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise FinanceLedgerError("Funding action idempotency key has different content")
            return str(existing["action_id"])
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO funding_actions(
                    action_id, idempotency_key, store_alias, report_id, action_type,
                    requested_amount_cny, status, lifecycle_status, requested_at, approved_at,
                    executed_at, external_reference, evidence_json, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    action_id, key, store_alias, report_id, action_type,
                    payload["requested_amount_cny"], legacy_status, lifecycle_status,
                    payload["requested_at"],
                    payload["evidence_json"], payload_hash,
                ),
            )
            self._insert_funding_event(
                action_id,
                lifecycle_status,
                payload["requested_at"],
                external_reference=None,
                evidence=dict(evidence or {}),
            )
        return action_id

    def update_funding_action_status(
        self,
        action_id: str,
        lifecycle_status: str,
        *,
        occurred_at: datetime | str | None = None,
        external_reference: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        status = str(lifecycle_status or "").strip().lower()
        if status not in _FUNDING_STATUSES:
            raise FinanceLedgerError(f"Unsupported funding status: {lifecycle_status}")
        existing = self.connection.execute(
            "SELECT * FROM funding_actions WHERE action_id=?", (action_id,)
        ).fetchone()
        if existing is None:
            raise FinanceLedgerError(f"Unknown funding action: {action_id}")
        timestamp = normalize_iso(occurred_at or datetime.now(timezone.utc))
        legacy_status = (
            status if status in _LEGACY_FUNDING_STATUSES else str(existing["status"])
        )
        approved_at = timestamp if status == "approved" else existing["approved_at"]
        executed_at = (
            timestamp
            if status in {"executed", "readback_confirmed"}
            else existing["executed_at"]
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE funding_actions SET
                    status=?, lifecycle_status=?, approved_at=?, executed_at=?,
                    external_reference=COALESCE(?, external_reference)
                WHERE action_id=?
                """,
                (
                    legacy_status, status, approved_at, executed_at,
                    _optional_text(external_reference), action_id,
                ),
            )
            self._insert_funding_event(
                action_id, status, timestamp,
                external_reference=_optional_text(external_reference),
                evidence=dict(evidence or {}),
            )

    def _insert_funding_event(
        self,
        action_id: str,
        lifecycle_status: str,
        occurred_at: str,
        *,
        external_reference: str | None,
        evidence: Mapping[str, Any],
    ) -> None:
        event_id = stable_event_hash(
            action_id,
            "funding-status",
            {
                "status": lifecycle_status,
                "occurredAt": occurred_at,
                "externalReference": external_reference,
                "evidence": dict(evidence),
            },
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO funding_action_events(
                action_id, event_id, lifecycle_status, occurred_at,
                external_reference, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                action_id, event_id, lifecycle_status, occurred_at,
                external_reference, canonical_json(dict(evidence)),
            ),
        )

    def record_source_coverage(
        self,
        store_alias: str,
        source: str,
        period_start: datetime | str,
        period_end: datetime | str,
        *,
        coverage_status: str,
        row_count: int,
        snapshot_hash: str | None,
        captured_at: datetime | str | None = None,
        sync_run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> CoverageSnapshot:
        alias = _required_text(store_alias, "store_alias")
        source_name = _required_text(source, "source")
        start = normalize_iso(period_start)
        end = normalize_iso(period_end)
        if end <= start:
            raise FinanceLedgerError("Coverage period_end must be later than period_start")
        status = str(coverage_status or "").strip().lower()
        if status not in {"complete", "partial", "missing", "pending"}:
            raise FinanceLedgerError(f"Unsupported coverage status: {coverage_status}")
        captured = normalize_iso(captured_at or datetime.now(timezone.utc))
        digest = _optional_text(snapshot_hash)
        if digest is not None:
            digest = digest.lower()
            if not _SHA256_RE.fullmatch(digest):
                raise FinanceLedgerError("snapshot_hash must be a lowercase SHA-256")
        if status == "complete" and digest is None:
            raise FinanceLedgerError("Complete coverage requires snapshot_hash")
        if int(row_count) < 0:
            raise FinanceLedgerError("row_count cannot be negative")

        previous = self.connection.execute(
            """
            SELECT is_complete, snapshot_hash, consecutive_complete_hash_count
            FROM source_snapshot_history
            WHERE store_alias=? AND source=? AND period_start=? AND period_end=?
              AND captured_at < ?
            ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1
            """,
            (alias, source_name, start, end, captured),
        ).fetchone()
        consecutive = 0
        if status == "complete":
            consecutive = 1
            if (
                previous is not None
                and previous["is_complete"] == 1
                and previous["snapshot_hash"] == digest
            ):
                consecutive = int(previous["consecutive_complete_hash_count"]) + 1

        coverage_id = stable_event_hash(
            alias,
            "source-coverage",
            {
                "source": source_name,
                "periodStart": start,
                "periodEnd": end,
                "capturedAt": captured,
            },
        )
        metadata_json = canonical_json(dict(metadata or {}))
        existing = self.connection.execute(
            "SELECT * FROM source_coverage WHERE coverage_id=?", (coverage_id,)
        ).fetchone()
        if existing is not None:
            expected = (
                status, int(row_count), digest, consecutive, sync_run_id, metadata_json
            )
            actual = (
                existing["coverage_status"], existing["row_count"],
                existing["snapshot_hash"], existing["consecutive_complete_hash_count"],
                existing["sync_run_id"], existing["metadata_json"],
            )
            # ``captured_at`` is intentionally normalized to seconds for
            # stable period evidence.  A fast retry can therefore reuse the
            # same capture key with a new sync-run ID/diagnostic metadata.  If
            # the evidence itself is identical, treat that retry as an
            # idempotent replay rather than attempting to overwrite an
            # immutable row (the separate sync_run already records the retry).
            if actual[:4] == expected[:4]:
                return _coverage_from_row(existing)
            if actual != expected:
                raise FinanceLedgerError("A source coverage capture cannot be overwritten")
            return _coverage_from_row(existing)

        snapshot_id = stable_event_hash(
            alias,
            "source-snapshot",
            {"coverageId": coverage_id, "snapshotHash": digest, "status": status},
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO source_coverage(
                    coverage_id, store_alias, source, period_start, period_end,
                    coverage_status, row_count, snapshot_hash,
                    consecutive_complete_hash_count, sync_run_id, captured_at,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    coverage_id, alias, source_name, start, end, status,
                    int(row_count), digest, consecutive, sync_run_id, captured,
                    metadata_json,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO source_snapshot_history(
                    snapshot_id, coverage_id, store_alias, source, period_start,
                    period_end, is_complete, snapshot_hash,
                    consecutive_complete_hash_count, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id, coverage_id, alias, source_name, start, end,
                    int(status == "complete"), digest, consecutive, captured,
                ),
            )
        row = self.connection.execute(
            "SELECT * FROM source_coverage WHERE coverage_id=?", (coverage_id,)
        ).fetchone()
        return _coverage_from_row(row)

    def get_period_coverage(
        self,
        period_start: datetime | str,
        period_end: datetime | str,
        *,
        store_aliases: Sequence[str] | None = None,
        sources: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        start = normalize_iso(period_start)
        end = normalize_iso(period_end)
        clauses = ["period_start=?", "period_end=?"]
        params: list[Any] = [start, end]
        if store_aliases:
            clauses.append(f"store_alias IN ({', '.join('?' for _ in store_aliases)})")
            params.extend(store_aliases)
        if sources:
            clauses.append(f"source IN ({', '.join('?' for _ in sources)})")
            params.extend(sources)
        rows = self.connection.execute(
            f"SELECT * FROM source_coverage WHERE {' AND '.join(clauses)} "
            "ORDER BY store_alias, source, captured_at DESC, coverage_id DESC",
            params,
        ).fetchall()
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row["store_alias"]), str(row["source"]))
            latest.setdefault(key, dict(row))
        return list(latest.values())

    def get_latest_source_coverage(
        self, store_alias: str, source: str
    ) -> dict[str, Any] | None:
        """Return the most recent coverage capture for one store/source.

        This is intentionally a read-only helper used by the incremental
        synchronizer to decide whether a source snapshot changed.  It avoids
        inferring change from row counts alone and keeps the comparison tied
        to the immutable coverage history.
        """

        row = self.connection.execute(
            """
            SELECT * FROM source_coverage
            WHERE store_alias=? AND source=?
            ORDER BY captured_at DESC, coverage_id DESC
            LIMIT 1
            """,
            (str(store_alias), str(source)),
        ).fetchone()
        return dict(row) if row is not None else None

    def has_consecutive_stable_snapshot(
        self,
        store_alias: str,
        source: str,
        period_start: datetime | str,
        period_end: datetime | str,
        *,
        minimum_count: int = 2,
    ) -> bool:
        if minimum_count < 2:
            raise FinanceLedgerError("minimum_count must be at least 2")
        row = self.connection.execute(
            """
            SELECT is_complete, consecutive_complete_hash_count
            FROM source_snapshot_history
            WHERE store_alias=? AND source=? AND period_start=? AND period_end=?
            ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1
            """,
            (
                store_alias, source, normalize_iso(period_start),
                normalize_iso(period_end),
            ),
        ).fetchone()
        return bool(
            row is not None
            and row["is_complete"] == 1
            and int(row["consecutive_complete_hash_count"]) >= minimum_count
        )

    def get_report(self, report_id: str, *, include_orders: bool = True) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM reports WHERE report_id=?", (report_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        result["correction_metadata"] = json.loads(
            result.pop("correction_metadata_json") or "{}"
        )
        if include_orders:
            result["orders"] = self.get_report_orders(report_id)
        return result

    def get_report_orders(self, report_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM report_orders WHERE report_id=?
            ORDER BY store_alias, order_key, profit_version
            """,
            (report_id,),
        ).fetchall()
        output = []
        for row in rows:
            value = dict(row)
            value["snapshot"] = json.loads(value.pop("snapshot_json"))
            output.append(value)
        return output

    def find_reports(
        self,
        *,
        report_key: str | None = None,
        report_type: str | None = None,
        report_status: str | None = None,
        period_start: datetime | str | None = None,
        period_end: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("report_key", report_key),
            ("report_type", report_type),
            ("report_status", report_status.upper() if report_status else None),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if period_start is not None:
            clauses.append("period_start>=?")
            params.append(normalize_iso(period_start))
        if period_end is not None:
            clauses.append("period_end<=?")
            params.append(normalize_iso(period_end))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM reports {where} ORDER BY period_start DESC, generated_at DESC",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_order_profit(
        self,
        store_alias: str,
        order_key: str,
        *,
        profit_version: str | None = None,
    ) -> dict[str, Any] | None:
        if profit_version is None:
            row = self.connection.execute(
                """
                SELECT * FROM order_profit WHERE store_alias=? AND order_key=?
                ORDER BY calculated_at DESC, profit_version DESC LIMIT 1
                """,
                (store_alias, order_key),
            ).fetchone()
        else:
            row = self.connection.execute(
                """
                SELECT * FROM order_profit
                WHERE store_alias=? AND order_key=? AND profit_version=?
                """,
                (store_alias, order_key, profit_version),
            ).fetchone()
        if row is None:
            return None
        return _order_profit_from_row(row)

    def query_order_profits(
        self,
        period_start: datetime | str,
        period_end: datetime | str,
        *,
        store_alias: str | None = None,
        order_result: str | None = None,
        latest_only: bool = True,
    ) -> list[dict[str, Any]]:
        clauses = ["op.completed_at>=?", "op.completed_at<?"]
        params: list[Any] = [normalize_iso(period_start), normalize_iso(period_end)]
        if store_alias is not None:
            clauses.append("op.store_alias=?")
            params.append(store_alias)
        if order_result is not None:
            clauses.append("op.order_result=?")
            params.append(order_result)
        if latest_only:
            clauses.append(
                """
                NOT EXISTS (
                    SELECT 1 FROM order_profit AS newer
                    WHERE newer.store_alias=op.store_alias
                      AND newer.order_key=op.order_key
                      AND (
                          newer.calculated_at > op.calculated_at
                          OR (
                              newer.calculated_at=op.calculated_at
                              AND newer.profit_version > op.profit_version
                          )
                      )
                )
                """
            )
        rows = self.connection.execute(
            f"SELECT op.* FROM order_profit AS op WHERE {' AND '.join(clauses)} "
            "ORDER BY op.completed_at, op.store_alias, op.order_key, op.calculated_at",
            params,
        ).fetchall()
        return [_order_profit_from_row(row) for row in rows]

    def get_latest_balance(
        self, store_alias: str, *, as_of: datetime | str | None = None
    ) -> dict[str, Any] | None:
        if as_of is None:
            row = self.connection.execute(
                """
                SELECT * FROM raw_balance WHERE store_alias=?
                ORDER BY observed_at DESC, fetched_at DESC LIMIT 1
                """,
                (store_alias,),
            ).fetchone()
        else:
            row = self.connection.execute(
                """
                SELECT * FROM raw_balance WHERE store_alias=? AND observed_at<=?
                ORDER BY observed_at DESC, fetched_at DESC LIMIT 1
                """,
                (store_alias, normalize_iso(as_of)),
            ).fetchone()
        return dict(row) if row is not None else None

    def query_balance_snapshots(
        self,
        store_alias: str,
        period_start: datetime | str,
        period_end: datetime | str,
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM raw_balance
            WHERE store_alias=? AND observed_at>=? AND observed_at<?
            ORDER BY observed_at, fetched_at
            """,
            (store_alias, normalize_iso(period_start), normalize_iso(period_end)),
        ).fetchall()
        return [dict(row) for row in rows]

    def query_ad_events(
        self,
        store_alias: str,
        period_start: datetime | str,
        period_end: datetime | str,
        *,
        campaign_id: str | int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["store_alias=?", "occurred_at>=?", "occurred_at<?"]
        params: list[Any] = [
            store_alias, normalize_iso(period_start), normalize_iso(period_end)
        ]
        if campaign_id is not None:
            clauses.append("campaign_id=?")
            params.append(str(campaign_id))
        rows = self.connection.execute(
            f"SELECT * FROM raw_ad WHERE {' AND '.join(clauses)} "
            "ORDER BY occurred_at, campaign_id, event_hash",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def sum_ad_spend(
        self,
        store_alias: str,
        period_start: datetime | str,
        period_end: datetime | str,
        *,
        campaign_id: str | int | None = None,
    ) -> Decimal:
        return sum(
            (
                Decimal(row["spend_cny"])
                for row in self.query_ad_events(
                    store_alias, period_start, period_end, campaign_id=campaign_id
                )
                if row["spend_cny"] is not None
            ),
            Decimal("0"),
        )

    def query_funding_actions(
        self,
        *,
        store_alias: str | None = None,
        report_id: str | None = None,
        action_type: str | None = None,
        lifecycle_status: str | Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("store_alias", store_alias),
            ("report_id", report_id),
            ("action_type", action_type),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if lifecycle_status is not None:
            statuses = (
                [lifecycle_status]
                if isinstance(lifecycle_status, str)
                else list(lifecycle_status)
            )
            if not statuses:
                return []
            invalid = set(statuses) - _FUNDING_STATUSES
            if invalid:
                raise FinanceLedgerError(f"Unsupported funding statuses: {sorted(invalid)}")
            clauses.append(
                f"lifecycle_status IN ({', '.join('?' for _ in statuses)})"
            )
            params.extend(statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM funding_actions {where} ORDER BY requested_at, action_id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_funding_plan(self, report_id: str) -> list[dict[str, Any]]:
        return self.query_funding_actions(report_id=report_id)

    def get_funding_action_events(self, action_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM funding_action_events WHERE action_id=?
            ORDER BY occurred_at, event_id
            """,
            (action_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Human-executed withdrawal / historical advertising-fee debt ledger
    # ------------------------------------------------------------------
    # These methods intentionally live beside (but not inside) funding_actions.
    # funding_actions represents plans and API attempts owned by this tool;
    # withdrawal_records represents a cash movement performed by somebody else
    # and later supplied by the owner.  The latter is append-only and all
    # subsequent status/settlement facts are separate immutable rows.

    def record_withdrawal_record(
        self,
        idempotency_key: str,
        store_alias: str,
        *,
        amount_cny: Any | None = None,
        withdrawal_amount_cny: Any | None = None,
        withdrawn_at: datetime | str,
        advertising_fee_due_cny: Any | None = None,
        advertising_fee_withheld_cny: Any | None = None,
        advertising_fee_status: str | None = None,
        status: str = "reported",
        settled_at: datetime | str | None = None,
        external_reference: str | None = None,
        source_type: str = "user_provided",
        evidence: Mapping[str, Any] | None = None,
    ) -> str:
        """Record one externally executed withdrawal without executing money.

        Historical statuses retain the advertising/return reserve facts and any
        auditable debt exactly as supplied.  New withdrawals funded from the WB
        settlement balance use ``settlement_balance``: fee due, withheld, and
        debt are all zero because advertising is no longer linked to withdrawal.
        A genuinely missing historical fact remains explicit ``unknown`` and is
        never silently converted to known zero debt.
        """

        key = _required_text(idempotency_key, "idempotency_key")
        alias = _required_text(store_alias, "store_alias")
        raw_amount = amount_cny if amount_cny is not None else withdrawal_amount_cny
        if raw_amount is None:
            raise FinanceLedgerError("withdrawal amount_cny is required")
        amount = _money_decimal(raw_amount, "withdrawal amount_cny", positive=True)
        withdrawn = normalize_iso(withdrawn_at)
        # ``None`` means the operator did not provide an advertising-fee
        # amount.  It is deliberately different from an explicit numeric
        # zero: a portal withdrawal record alone does not prove that no fee
        # was due or withheld.  Persist zero in the legacy numeric columns,
        # but carry the distinction in ``advertising_fee_status=unknown`` and
        # the summary's PENDING_DATA marker.
        due_supplied = _money_value_supplied(advertising_fee_due_cny)
        withheld_supplied = _money_value_supplied(advertising_fee_withheld_cny)
        due = (
            _money_decimal(
                advertising_fee_due_cny, "advertising_fee_due_cny", non_negative=True
            )
            if due_supplied
            else Decimal("0.00")
        )
        withheld = (
            _money_decimal(
                advertising_fee_withheld_cny,
                "advertising_fee_withheld_cny",
                non_negative=True,
            )
            if withheld_supplied
            else Decimal("0.00")
        )
        if withheld > due:
            raise FinanceLedgerError(
                "advertising_fee_withheld_cny cannot exceed advertising_fee_due_cny"
            )
        explicit_fee_status = (
            advertising_fee_status is not None
            and bool(str(advertising_fee_status).strip())
        )
        if not explicit_fee_status and (not due_supplied or not withheld_supplied):
            # Fail closed when the user only supplies a withdrawal amount.
            # Do not infer ``not_deducted`` from an omitted withheld amount.
            fee_status = "unknown"
        else:
            fee_status = _normalise_advertising_fee_status(
                advertising_fee_status, due=due, withheld=withheld
            )
        if fee_status == "settlement_balance" and (
            due != Decimal("0") or withheld != Decimal("0")
        ):
            raise FinanceLedgerError(
                "settlement_balance withdrawal records require zero advertising "
                "fee due and withheld amounts"
            )
        if not due_supplied and fee_status not in {"unknown", "settlement_balance"}:
            raise FinanceLedgerError(
                "advertising_fee_due_cny is required when advertising_fee_status "
                f"is {fee_status!r}; omit the status (or use unknown) until the "
                "advertising reserve is evidenced"
            )
        if (
            fee_status in {"deducted", "partial"}
            and not withheld_supplied
        ):
            raise FinanceLedgerError(
                "advertising_fee_withheld_cny is required for deducted/partial "
                "advertising fee status"
            )
        lifecycle = str(status or "").strip().lower()
        if lifecycle not in _WITHDRAWAL_RECORD_STATUSES:
            raise FinanceLedgerError(f"Unsupported withdrawal record status: {status}")
        if fee_status == "not_deducted" and due <= Decimal("0"):
            raise FinanceLedgerError(
                "not_deducted withdrawal records require a positive advertising fee due"
            )
        settled = normalize_iso(settled_at) if settled_at is not None else None
        source = _required_text(source_type, "source_type")
        reference = _optional_text(external_reference)
        evidence_payload = dict(evidence or {})
        # Preserve the input facts in the immutable evidence payload so a
        # future policy/rate change cannot rewrite what the operator reported.
        evidence_payload.setdefault("recordedBy", "user")
        evidence_payload.setdefault("advertisingFeeStatus", fee_status)
        payload = {
            "idempotencyKey": key,
            "storeAlias": alias,
            "withdrawalAmountCny": _money_text(amount),
            "withdrawnAt": withdrawn,
            "status": lifecycle,
            "advertisingFeeStatus": fee_status,
            "advertisingFeeDueCny": _money_text(due),
            "advertisingFeeWithheldCny": _money_text(withheld),
            "advertisingFeeDebtCny": _money_text(due - withheld),
            "settledAt": settled,
            "externalReference": reference,
            "sourceType": source,
            "evidence": evidence_payload,
        }
        payload_hash = sha256_text(canonical_json(payload))
        record_id = stable_event_hash(
            alias, "withdrawal-record", {"idempotencyKey": key}
        )
        existing = self.connection.execute(
            "SELECT record_id, payload_hash FROM withdrawal_records WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if existing is not None:
            if str(existing["payload_hash"]) != payload_hash:
                raise FinanceLedgerError(
                    "Withdrawal record idempotency key has different content"
                )
            return str(existing["record_id"])

        created = normalize_iso(datetime.now(timezone.utc))
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO withdrawal_records(
                    record_id, idempotency_key, store_alias, withdrawal_amount_cny,
                    withdrawn_at, status, advertising_fee_status,
                    advertising_fee_due_cny, advertising_fee_withheld_cny,
                    advertising_fee_debt_cny, settled_at, external_reference,
                    source_type, evidence_json, payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    key,
                    alias,
                    _money_text(amount),
                    withdrawn,
                    lifecycle,
                    fee_status,
                    _money_text(due),
                    _money_text(withheld),
                    _money_text(due - withheld),
                    settled,
                    reference,
                    source,
                    canonical_json(evidence_payload),
                    payload_hash,
                    created,
                ),
            )
            self._insert_withdrawal_record_event(
                record_id,
                lifecycle,
                withdrawn,
                external_reference=reference,
                evidence={**evidence_payload, "source": "withdrawal_record"},
            )
        # A payment may have been entered before the operator supplied this
        # withdrawal.  Re-run deterministic FIFO allocation so that late entry
        # does not leave a false outstanding balance.
        self._allocate_unapplied_advertising_payments(alias)
        return record_id

    def update_withdrawal_record_status(
        self,
        record_id: str,
        status: str,
        *,
        occurred_at: datetime | str | None = None,
        external_reference: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        """Append a status/correction event for an immutable withdrawal row."""

        identifier = _required_text(record_id, "record_id")
        lifecycle = str(status or "").strip().lower()
        if lifecycle not in _WITHDRAWAL_RECORD_STATUSES:
            raise FinanceLedgerError(f"Unsupported withdrawal record status: {status}")
        found = self.connection.execute(
            "SELECT 1 FROM withdrawal_records WHERE record_id=?", (identifier,)
        ).fetchone()
        if found is None:
            raise FinanceLedgerError(f"Unknown withdrawal record: {identifier}")
        occurred = normalize_iso(occurred_at or datetime.now(timezone.utc))
        with self.connection:
            self._insert_withdrawal_record_event(
                identifier,
                lifecycle,
                occurred,
                external_reference=_optional_text(external_reference),
                evidence=dict(evidence or {}),
            )
        self._allocate_unapplied_advertising_payments(
            str(
                self.connection.execute(
                    "SELECT store_alias FROM withdrawal_records WHERE record_id=?",
                    (identifier,),
                ).fetchone()["store_alias"]
            )
        )

    def _insert_withdrawal_record_event(
        self,
        record_id: str,
        status: str,
        occurred_at: str,
        *,
        external_reference: str | None,
        evidence: Mapping[str, Any],
    ) -> None:
        event_id = stable_event_hash(
            record_id,
            "withdrawal-status",
            {
                "status": status,
                "occurredAt": occurred_at,
                "externalReference": external_reference,
                "evidence": dict(evidence),
            },
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO withdrawal_record_events(
                record_id, event_id, status, occurred_at,
                external_reference, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                event_id,
                status,
                occurred_at,
                external_reference,
                canonical_json(dict(evidence)),
            ),
        )

    def record_advertising_payment_readback(
        self,
        idempotency_key: str,
        store_alias: str,
        *,
        amount_cny: Any,
        paid_at: datetime | str,
        source_type: str = "platform_api",
        platform_reference: str | None = None,
        raw_event_hash: str | None = None,
        funding_action_id: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> str:
        """Record a positive advertising-balance credit and settle debt FIFO.

        This method is readback-only: it never calls WB and never posts money.
        ``amount_cny`` must be the actual amount observed on the platform, not
        the amount somebody intended to transfer.
        """

        key = _required_text(idempotency_key, "idempotency_key")
        alias = _required_text(store_alias, "store_alias")
        amount = _money_decimal(amount_cny, "amount_cny", positive=True)
        paid = normalize_iso(paid_at)
        source = str(source_type or "").strip().lower()
        source = {
            "api": "platform_api",
            "platform": "platform_api",
            "manual": "user_provided",
            "user": "user_provided",
        }.get(source, source)
        if source not in _ADVERTISING_PAYMENT_SOURCES:
            raise FinanceLedgerError(f"Unsupported advertising payment source: {source_type}")
        reference = _optional_text(platform_reference)
        event_hash = _optional_text(raw_event_hash)
        action_id = _optional_text(funding_action_id)
        if action_id is not None:
            action_row = self.connection.execute(
                "SELECT store_alias FROM funding_actions WHERE action_id=?", (action_id,)
            ).fetchone()
            if action_row is None:
                raise FinanceLedgerError(f"Unknown funding action: {action_id}")
            if str(action_row["store_alias"]) != alias:
                raise FinanceLedgerError(
                    "Advertising payment readback store does not match its funding action"
                )
        evidence_payload = dict(evidence or {})
        evidence_payload.setdefault("readbackOnly", True)
        payload = {
            "idempotencyKey": key,
            "storeAlias": alias,
            "amountCny": _money_text(amount),
            "paidAt": paid,
            "sourceType": source,
            "platformReference": reference,
            "rawEventHash": event_hash,
            "fundingActionId": action_id,
            "evidence": evidence_payload,
        }
        payload_hash = sha256_text(canonical_json(payload))
        payment_id = stable_event_hash(
            alias, "advertising-payment-readback", {"idempotencyKey": key}
        )
        existing = self.connection.execute(
            "SELECT payment_id, payload_hash FROM advertising_payment_readbacks WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if existing is not None:
            if str(existing["payload_hash"]) != payload_hash:
                raise FinanceLedgerError(
                    "Advertising payment idempotency key has different content"
                )
            return str(existing["payment_id"])
        created = normalize_iso(datetime.now(timezone.utc))
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO advertising_payment_readbacks(
                    payment_id, idempotency_key, store_alias, amount_cny, paid_at,
                    source_type, platform_reference, raw_event_hash,
                    funding_action_id, evidence_json, payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payment_id,
                    key,
                    alias,
                    _money_text(amount),
                    paid,
                    source,
                    reference,
                    event_hash,
                    action_id,
                    canonical_json(evidence_payload),
                    payload_hash,
                    created,
                ),
            )
        self._allocate_unapplied_advertising_payments(alias)
        return payment_id

    def list_withdrawal_records(
        self,
        *,
        store_alias: str | None = None,
        include_voided: bool = False,
        as_of: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        """Return immutable withdrawal facts plus latest status and settlement."""

        clauses: list[str] = []
        params: list[Any] = []
        if store_alias is not None:
            clauses.append("wr.store_alias=?")
            params.append(store_alias)
        if as_of is not None:
            clauses.append("wr.withdrawn_at<=?")
            params.append(normalize_iso(as_of))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT wr.* FROM withdrawal_records wr {where} "
            "ORDER BY wr.store_alias, wr.withdrawn_at, wr.record_id",
            params,
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            latest = self._latest_withdrawal_record_event(item["record_id"], as_of=as_of)
            current_status = str(latest.get("status") or item["status"])
            if not include_voided and current_status == "voided":
                continue
            item["current_status"] = current_status
            if latest is not None:
                latest = dict(latest)
                try:
                    latest["evidence"] = json.loads(
                        latest.pop("evidence_json") or "{}"
                    )
                except json.JSONDecodeError:
                    latest["evidence"] = {}
            item["latest_status_event"] = latest or None
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            item["advertising_fee_due_cny"] = _money_text(
                _money_decimal(item["advertising_fee_due_cny"], "advertising_fee_due_cny", non_negative=True)
            )
            item["advertising_fee_withheld_cny"] = _money_text(
                _money_decimal(item["advertising_fee_withheld_cny"], "advertising_fee_withheld_cny", non_negative=True)
            )
            item["advertising_fee_debt_cny"] = _money_text(
                _money_decimal(item["advertising_fee_debt_cny"], "advertising_fee_debt_cny", non_negative=True)
            )
            item["settled_advertising_fee_cny"] = _money_text(
                self._withdrawal_settled_amount(item["record_id"], as_of=as_of)
            )
            item["outstanding_advertising_fee_cny"] = _money_text(
                max(
                    Decimal("0"),
                    _money_decimal(item["advertising_fee_debt_cny"], "advertising_fee_debt_cny", non_negative=True)
                    - _money_decimal(item["settled_advertising_fee_cny"], "settled_advertising_fee_cny", non_negative=True),
                )
            )
            output.append(item)
        return output

    def list_advertising_payment_readbacks(
        self,
        *,
        store_alias: str | None = None,
        as_of: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if store_alias is not None:
            clauses.append("store_alias=?")
            params.append(store_alias)
        if as_of is not None:
            clauses.append("paid_at<=?")
            params.append(normalize_iso(as_of))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM advertising_payment_readbacks {where} "
            "ORDER BY store_alias, paid_at, payment_id",
            params,
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            amount = _money_decimal(item["amount_cny"], "amount_cny", positive=True)
            item["amount_cny"] = _money_text(amount)
            item["applied_cny"] = _money_text(
                self._payment_applied_amount(item["payment_id"], as_of=as_of)
            )
            item["unapplied_cny"] = _money_text(
                max(
                    Decimal("0"),
                    amount - _money_decimal(item["applied_cny"], "applied_cny", non_negative=True),
                )
            )
            output.append(item)
        return output

    def advertising_debt_summary(
        self,
        *,
        store_alias: str | None = None,
        as_of: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Return store-separated historical ad-fee debt and FIFO evidence."""

        aliases = [store_alias] if store_alias is not None else [
            str(row["store_alias"])
            for row in self.connection.execute(
                "SELECT store_alias FROM store_registry WHERE active=1 ORDER BY store_alias"
            ).fetchall()
        ]
        stores: list[dict[str, Any]] = []
        for alias in aliases:
            withdrawals = self.list_withdrawal_records(
                store_alias=alias, include_voided=False, as_of=as_of
            )
            payments = self.list_advertising_payment_readbacks(
                store_alias=alias, as_of=as_of
            )
            due = sum(
                (_money_decimal(row["advertising_fee_debt_cny"], "debt", non_negative=True) for row in withdrawals),
                Decimal("0"),
            )
            settled = sum(
                (_money_decimal(row["settled_advertising_fee_cny"], "settled", non_negative=True) for row in withdrawals),
                Decimal("0"),
            )
            payment_total = sum(
                (_money_decimal(row["amount_cny"], "payment", positive=True) for row in payments),
                Decimal("0"),
            )
            payment_applied = sum(
                (_money_decimal(row["applied_cny"], "applied", non_negative=True) for row in payments),
                Decimal("0"),
            )
            unverified_count = sum(
                1
                for row in withdrawals
                if str(row.get("advertising_fee_status") or "unknown").strip().lower()
                == "unknown"
            )
            debt_status = "PENDING_DATA" if unverified_count else "KNOWN"
            store_payload = {
                "storeAlias": alias,
                "withdrawalCount": len(withdrawals),
                "withdrawalAmountCny": _money_text(
                    sum((_money_decimal(row["withdrawal_amount_cny"], "withdrawal", positive=True) for row in withdrawals), Decimal("0"))
                ),
                "advertisingFeeDueCny": _money_text(due),
                "advertisingFeeSettledCny": _money_text(settled),
                "advertisingDebtOutstandingCny": _money_text(max(Decimal("0"), due - settled)),
                "advertisingPaymentReadbackCny": _money_text(payment_total),
                "advertisingPaymentAppliedCny": _money_text(payment_applied),
                "advertisingPaymentUnappliedCny": _money_text(max(Decimal("0"), payment_total - payment_applied)),
                "advertisingFeeUnverifiedCount": unverified_count,
                # A zero numeric debt is not evidence that no advertising fee
                # is owed when one or more withdrawal records omit the fee
                # facts.  Keep the legacy numeric fields for compatibility,
                # but expose an explicit state and an unknown amount marker.
                "advertisingDebtStatus": debt_status,
                "advertisingDebtAmountKnown": unverified_count == 0,
                "advertisingDebtUnknownAmountCny": (
                    None if unverified_count else "0.00"
                ),
                "withdrawals": withdrawals,
                "payments": payments,
            }
            stores.append(store_payload)
        total_keys = (
            "withdrawalAmountCny",
            "advertisingFeeDueCny",
            "advertisingFeeSettledCny",
            "advertisingDebtOutstandingCny",
            "advertisingPaymentReadbackCny",
            "advertisingPaymentAppliedCny",
            "advertisingPaymentUnappliedCny",
        )
        totals = {
            key: _money_text(
                sum((_money_decimal(row[key], key, non_negative=True) for row in stores), Decimal("0"))
            )
            for key in total_keys
        }
        # Counts are kept alongside monetary totals so a report can distinguish
        # a genuine zero debt from an empty/unverified withdrawal population.
        totals["withdrawalCount"] = sum(
            int(row.get("withdrawalCount") or 0) for row in stores
        )
        totals["advertisingFeeUnverifiedCount"] = sum(
            int(row.get("advertisingFeeUnverifiedCount") or 0) for row in stores
        )
        totals["advertisingDebtStatus"] = (
            "PENDING_DATA"
            if totals["advertisingFeeUnverifiedCount"]
            else "KNOWN"
        )
        totals["advertisingDebtAmountKnown"] = (
            totals["advertisingFeeUnverifiedCount"] == 0
        )
        totals["advertisingDebtUnknownAmountCny"] = (
            None
            if totals["advertisingFeeUnverifiedCount"]
            else "0.00"
        )
        return {
            "schemaVersion": 1,
            "currency": "CNY",
            "asOf": normalize_iso(as_of) if as_of is not None else None,
            "stores": stores,
            "totals": totals,
        }

    def _latest_withdrawal_record_event(
        self, record_id: str, *, as_of: datetime | str | None = None
    ) -> dict[str, Any] | None:
        params: list[Any] = [record_id]
        clause = ""
        if as_of is not None:
            clause = " AND occurred_at<=?"
            params.append(normalize_iso(as_of))
        row = self.connection.execute(
            "SELECT * FROM withdrawal_record_events WHERE record_id=?"
            + clause
            + " ORDER BY occurred_at DESC, event_id DESC LIMIT 1",
            params,
        ).fetchone()
        return dict(row) if row is not None else None

    def _withdrawal_is_active(
        self, record_id: str, *, as_of: datetime | str | None = None
    ) -> bool:
        row = self.connection.execute(
            "SELECT status FROM withdrawal_records WHERE record_id=?", (record_id,)
        ).fetchone()
        if row is None:
            return False
        latest = self._latest_withdrawal_record_event(record_id, as_of=as_of)
        return str((latest or row)["status"]) != "voided"

    def _withdrawal_settled_amount(
        self, record_id: str, *, as_of: datetime | str | None = None
    ) -> Decimal:
        clauses = ["a.withdrawal_record_id=?"]
        params: list[Any] = [record_id]
        if as_of is not None:
            cutoff = normalize_iso(as_of)
            clauses.append("a.allocated_at<=?")
            params.append(cutoff)
            clauses.append("p.paid_at<=?")
            params.append(cutoff)
        # Read individual text values rather than aggregating through SQLite
        # REAL, preserving cent precision and avoiding floating-point drift.
        rows = self.connection.execute(
            "SELECT a.amount_cny FROM advertising_debt_allocations a "
            "JOIN advertising_payment_readbacks p ON p.payment_id=a.payment_id "
            f"WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()
        return sum((_money_decimal(item["amount_cny"], "allocation", non_negative=True) for item in rows), Decimal("0")).quantize(Decimal("0.01"))

    def _payment_applied_amount(
        self, payment_id: str, *, as_of: datetime | str | None = None
    ) -> Decimal:
        clauses = ["a.payment_id=?"]
        params: list[Any] = [payment_id]
        if as_of is not None:
            cutoff = normalize_iso(as_of)
            clauses.append("a.allocated_at<=?")
            params.append(cutoff)
            clauses.append("p.paid_at<=?")
            params.append(cutoff)
        rows = self.connection.execute(
            "SELECT a.amount_cny, a.withdrawal_record_id "
            "FROM advertising_debt_allocations a "
            "JOIN advertising_payment_readbacks p ON p.payment_id=a.payment_id "
            f"WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()
        total = Decimal("0")
        for row in rows:
            if self._withdrawal_is_active(str(row["withdrawal_record_id"]), as_of=as_of):
                total += _money_decimal(row["amount_cny"], "allocation", non_negative=True)
        return total.quantize(Decimal("0.01"))

    def _allocate_unapplied_advertising_payments(self, store_alias: str) -> None:
        """Allocate all currently unallocated credits to active debts FIFO."""

        alias = _required_text(store_alias, "store_alias")
        payments = self.connection.execute(
            "SELECT * FROM advertising_payment_readbacks WHERE store_alias=? "
            "ORDER BY paid_at, payment_id",
            (alias,),
        ).fetchall()
        withdrawals = self.list_withdrawal_records(store_alias=alias)
        # Keep only active records with positive outstanding debt and use a
        # stable order, making repeated reconciliation deterministic.
        debts = [
            {
                "recordId": str(row["record_id"]),
                "withdrawnAt": str(row["withdrawn_at"]),
                "paidAt": None,
                "remaining": _money_decimal(
                    row["outstanding_advertising_fee_cny"],
                    "outstanding_advertising_fee_cny",
                    non_negative=True,
                ),
            }
            for row in withdrawals
            if _money_decimal(row["outstanding_advertising_fee_cny"], "outstanding", non_negative=True) > Decimal("0")
        ]
        if not debts or not payments:
            return
        debts.sort(key=lambda item: (item["withdrawnAt"], item["recordId"]))
        with self.connection:
            for payment in payments:
                payment_id = str(payment["payment_id"])
                payment_amount = _money_decimal(payment["amount_cny"], "payment amount", positive=True)
                applied = self._payment_applied_amount(payment_id)
                remaining_payment = payment_amount - applied
                if remaining_payment <= Decimal("0"):
                    continue
                paid_at = str(payment["paid_at"])
                for debt in debts:
                    if remaining_payment <= Decimal("0"):
                        break
                    if debt["remaining"] <= Decimal("0"):
                        continue
                    # A credit cannot settle a withdrawal that happened after
                    # the credit; that would be a temporal accounting error.
                    if str(debt["withdrawnAt"]) > paid_at:
                        continue
                    allocation = min(remaining_payment, debt["remaining"]).quantize(Decimal("0.01"))
                    if allocation <= Decimal("0"):
                        continue
                    allocation_id = stable_event_hash(
                        alias,
                        "advertising-debt-allocation",
                        {"paymentId": payment_id, "withdrawalRecordId": debt["recordId"]},
                    )
                    # Idempotent retries must not subtract an allocation that
                    # is already present.  Check before INSERT OR IGNORE so a
                    # repeated reconciliation cannot over-apply a payment.
                    existing_allocation = self.connection.execute(
                        "SELECT amount_cny FROM advertising_debt_allocations "
                        "WHERE allocation_id=?",
                        (allocation_id,),
                    ).fetchone()
                    if existing_allocation is not None:
                        continue
                    self.connection.execute(
                        """
                        INSERT OR IGNORE INTO advertising_debt_allocations(
                            allocation_id, payment_id, withdrawal_record_id,
                            amount_cny, allocated_at, evidence_json
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            allocation_id,
                            payment_id,
                            debt["recordId"],
                            _money_text(allocation),
                            normalize_iso(datetime.now(timezone.utc)),
                            canonical_json(
                                {
                                    "method": "fifo",
                                    "paymentPaidAt": paid_at,
                                    "withdrawalAt": debt["withdrawnAt"],
                                }
                            ),
                        ),
                    )
                    actual = allocation
                    remaining_payment -= actual
                    debt["remaining"] = max(Decimal("0"), debt["remaining"] - actual)

    def acquire_lock(
        self,
        lock_name: str,
        owner_id: str,
        *,
        ttl_seconds: int,
        now: datetime | str | None = None,
    ) -> bool:
        if ttl_seconds <= 0:
            raise FinanceLedgerError("ttl_seconds must be positive")
        current = parse_aware_datetime(now or datetime.now(timezone.utc))
        acquired = normalize_iso(current)
        expires = normalize_iso(current + timedelta(seconds=ttl_seconds))
        with self.connection:
            self.connection.execute(
                "DELETE FROM locks WHERE lock_name=? AND expires_at <= ?",
                (lock_name, acquired),
            )
            try:
                self.connection.execute(
                    "INSERT INTO locks(lock_name, owner_id, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                    (
                        _required_text(lock_name, "lock_name"),
                        _required_text(owner_id, "owner_id"), acquired, expires,
                    ),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def release_lock(self, lock_name: str, owner_id: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM locks WHERE lock_name=? AND owner_id=?",
                (lock_name, owner_id),
            )
        return cursor.rowcount == 1


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def stable_event_hash(
    store_alias: str, source: str, identity: Mapping[str, Any]
) -> str:
    material = {
        "storeAlias": _required_text(store_alias, "store_alias"),
        "source": _required_text(source, "source"),
        "identity": dict(identity),
    }
    return sha256_text(canonical_json(material))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def decimal_text(value: Any, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise FinanceLedgerError("A required monetary value is missing")
        return None
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinanceLedgerError(f"Invalid monetary value: {value!r}") from exc
    if not amount.is_finite():
        raise FinanceLedgerError("Monetary values must be finite")
    return format(amount, "f")


def _money_decimal(
    value: Any,
    label: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    """Parse a finite monetary value and normalize it to cents."""

    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinanceLedgerError(f"Invalid {label}: {value!r}") from exc
    if not amount.is_finite():
        raise FinanceLedgerError(f"{label} must be finite")
    if positive and amount <= Decimal("0"):
        raise FinanceLedgerError(f"{label} must be positive")
    if non_negative and amount < Decimal("0"):
        raise FinanceLedgerError(f"{label} cannot be negative")
    return amount.quantize(Decimal("0.01"))


def _money_value_supplied(value: Any) -> bool:
    """Return whether a caller supplied a monetary fact rather than omitting it.

    Empty strings are treated like ``None`` because they are what an optional
    CLI/API field commonly becomes after form decoding.  Numeric zero remains
    supplied and therefore means an explicit zero, not an unknown amount.
    """

    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _money_text(value: Any) -> str:
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return format(amount.quantize(Decimal("0.01")), "f")


def _normalise_advertising_fee_status(
    value: str | None, *, due: Decimal, withheld: Decimal
) -> str:
    if value is not None and str(value).strip():
        status = str(value).strip().lower()
        aliases = {
            "deducted": "deducted",
            "withheld": "deducted",
            "已扣": "deducted",
            "not_deducted": "not_deducted",
            "not-deducted": "not_deducted",
            "none": "not_deducted",
            "未扣": "not_deducted",
            "partial": "partial",
            "部分": "partial",
            "unknown": "unknown",
            "未知": "unknown",
            "settlement_balance": "settlement_balance",
            "settlement-balance": "settlement_balance",
            "settlement balance": "settlement_balance",
            "not_applicable": "settlement_balance",
            "not-applicable": "settlement_balance",
            "not applicable": "settlement_balance",
            "n/a": "settlement_balance",
            "余额结算": "settlement_balance",
            "结算余额": "settlement_balance",
        }
        status = aliases.get(status, status)
        if status not in _ADVERTISING_FEE_STATUSES:
            raise FinanceLedgerError(f"Unsupported advertising fee status: {value}")
        return status
    if due <= Decimal("0") and withheld <= Decimal("0"):
        return "unknown"
    if withheld <= Decimal("0"):
        return "not_deducted"
    if withheld >= due:
        return "deducted"
    return "partial"


def normalize_iso(value: datetime | str) -> str:
    parsed = parse_aware_datetime(value)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_aware_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise FinanceLedgerError(f"Invalid ISO datetime: {value}") from exc
    else:
        raise FinanceLedgerError(f"Expected timezone-aware ISO datetime, got {type(value).__name__}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FinanceLedgerError(f"Timezone is required for datetime: {value}")
    return parsed


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return normalize_iso(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot JSON-encode {type(value).__name__}")


def _coverage_from_row(row: sqlite3.Row) -> CoverageSnapshot:
    return CoverageSnapshot(
        coverage_id=str(row["coverage_id"]),
        store_alias=str(row["store_alias"]),
        source=str(row["source"]),
        period_start=str(row["period_start"]),
        period_end=str(row["period_end"]),
        coverage_status=str(row["coverage_status"]),
        snapshot_hash=(
            str(row["snapshot_hash"]) if row["snapshot_hash"] is not None else None
        ),
        consecutive_complete_hash_count=int(row["consecutive_complete_hash_count"]),
        captured_at=str(row["captured_at"]),
    )


def _order_profit_from_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["evidence"] = json.loads(value.pop("evidence_json"))
    return value


def _pick(record: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def _product_parts(product_key: str) -> tuple[str | None, str | None, str | None]:
    parts = str(product_key or "").rsplit("-", 2)
    if len(parts) != 3:
        return None, None, None
    return tuple(part.strip() or None for part in parts)  # type: ignore[return-value]


def _cost_version_uid(record: Mapping[str, Any]) -> str:
    identity = {
        "sku": record.get("sku"),
        "effectiveFrom": record.get("effective_from"),
        "purchaseCostCny": record.get("purchase_cost_cny"),
        "quoteSha256": record.get("quote_sha256"),
        "quoteRow": record.get("quote_row"),
        "exchangeRateRubPerCny": record.get("exchange_rate_rub_per_cny"),
    }
    return f"costv_{sha256_text(canonical_json(identity))[:24]}"


def _required_text(value: str, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise FinanceLedgerError(f"{label} is required")
    return result


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None
