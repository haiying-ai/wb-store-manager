from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .client import WbClient
from .finance_calculations import aggregate_pool_for_orders, calculate_financial_report
from .finance_costs import normalize_product_key, parse_cost_quote
from .finance_debt import (
    import_advertising_payment_events_from_ledger,
    record_withdrawal_from_user_record,
)
from .finance_funds import (
    AdvertisingAccrual,
    DepositCommitment,
    build_advertising_deposit_plan,
    build_funding_plan,
    preflight_deposit_execution,
    validate_advertising_deposit_plan,
    validate_funding_plan,
)
from .finance_ledger import FinanceLedger, canonical_json, normalize_iso
from .finance_periods import (
    FinancePeriod,
    parse_report_period,
    previous_month,
    previous_week,
    report_maturity_blocker,
)
from .finance_policy import (
    ADVERTISING_DEPOSIT_CAP_RULE,
    load_phone_financial_policy_timeline,
)
from .finance_reporting import ensure_readable_workbook, write_report_artifacts
from .finance_reporting_standard import load_active_wb_periodic_reporting_standard
from .finance_sources import (
    ADVERTISING_BASE_URL,
    SELLER_BALANCE_URL,
    StoreSpec,
    deposit_campaign_budget,
    load_store_specs,
    reconcile_advertising_cost_events,
    resolve_store_credentials,
)
from .finance_sync import (
    MOSCOW,
    PRIMARY_SOURCES,
    read_registry,
    register_enabled_stores,
    resolve_through,
    sync_enabled_stores,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = Path("outputs/wb-finance-reports")
_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_MAX_FUNDING_BALANCE_EVIDENCE_AGE = timedelta(hours=1)
_MAX_FUTURE_EVIDENCE_SKEW = timedelta(minutes=1)
_DEFAULT_DEPOSIT_PLAN_LIFETIME = timedelta(minutes=15)
# Ordinary daily captures only need a short correction sweep.  Scheduled
# Monday/month-start runs use the full 120-day repair window; an explicit
# bootstrap still backfills from each store's configured history/opening date.
# Delayed historical corrections therefore remain covered at report close
# without paying the long-window API cost every ordinary day.
_DAILY_CORRECTION_WINDOW_DAYS = 30
_FULL_CORRECTION_WINDOW_DAYS = 120
_DEFAULT_STORE_WORKERS = 2
_ADVERTISING_SOURCE_FIELDS = {
    "account": "balance",
    "balance": "net",
    "bonus": "bonus",
}
_FUNDABLE_CAMPAIGN_STATUSES = {4, 9, 11}
_LOCKING_ADVERTISING_ACTION_STATUSES = {
    "approved",
    "executing",
    "executed",
    "succeeded",
    "unknown",
}
_DELTA_MONEY_FIELDS = (
    "salesAmountGCny",
    "netTurnoverCny",
    "forPayCny",
    "purchaseCostCny",
    "actualCommissionCCny",
    "actualWbCommissionCny",
    "commissionFormulaCny",
    "commissionReconciliationDifferenceCny",
    "wbCommissionCny",
    "acquiringFeeCny",
    "actualPaymentFeeCny",
    "paymentFeeCny",
    "accountIncrementCny",
    "forPayReconciliationDifferenceCny",
    "forwardLogisticsCny",
    "storageAndOperationsCny",
    "otherPlatformCostCny",
    "advertisingSettlementDeductionCny",
    "forwardOperationsUsedCny",
    "managedOperationsCny",
    "returnRiskReserveCny",
    "promotionReserveCny",
    "totalPolicyReserveCny",
    "combinedAdvertisingAndReturnPoolCny",
    "combinedPoolCny",
    "returnLogisticsCny",
    "advertisingProvisionCny",
    "excessReturnLossCny",
    "ruleWithdrawableCny",
    "strictOwnerProfitCny",
    "ownerProfitCny",
)


class FinanceServiceError(ValueError):
    pass


class FinanceService:
    """End-to-end local controller for the multi-store WB finance ledger."""

    def __init__(
        self,
        *,
        db_path: Path = Path("data/finance/wb_finance.sqlite3"),
        registry_path: Path = Path("config/finance/stores.json"),
        output_root: Path | None = None,
        repo_root: Path = ROOT,
        now: datetime | None = None,
        source_factory=None,
        max_store_workers: int = _DEFAULT_STORE_WORKERS,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.registry_path = self._resolve_path(registry_path)
        self.periodic_standard = load_active_wb_periodic_reporting_standard(
            self.repo_root / "config" / "finance" / "active_wb_periodic_reporting_standard.json",
            repo_root=self.repo_root,
        )
        configured_output = (
            output_root
            or os.getenv("WB_FINANCE_REPORT_ROOT", "").strip()
            or self.periodic_standard.raw.get("reportRoot")
            or DEFAULT_OUTPUT_ROOT
        )
        self.output_root = self._resolve_path(Path(configured_output)).resolve()
        try:
            normalized_workers = max(1, int(max_store_workers))
        except (TypeError, ValueError) as exc:
            raise FinanceServiceError(
                "max_store_workers must be a positive integer"
            ) from exc
        self.max_store_workers = normalized_workers
        self.ledger = FinanceLedger(self._resolve_path(db_path))
        self.now = now
        self.source_factory = source_factory
        self.registry = read_registry(self.registry_path)
        self.policy_timeline = load_phone_financial_policy_timeline()
        self.policy = self.policy_timeline.active_policy
        self.store_aliases = register_enabled_stores(
            self.ledger,
            self.registry,
            registry_path=self.registry_path,
            repo_root=self.repo_root,
        )

    def close(self) -> None:
        self.ledger.close()

    def _funding_activation_info(
        self,
        store_alias: str,
        *,
        required: bool = False,
    ) -> dict[str, Any]:
        """Resolve the first event that starts the advertising-funding ledger.

        The starting point is deliberately data-driven.  When the registry
        uses ``first_completed_or_return_event`` we use the earliest lifecycle
        event evidenced by the platform finance/order ledger for this store:
        persisted order-profit lifecycle timestamps, return readbacks, and
        explicit finance sale/cancellation rows.  A fixed activation timestamp
        is retained only as a backwards-compatible fallback for older test or
        migrated registries that have not opted into the dynamic rule.

        ``required=False`` is used while publishing a report: missing evidence
        is shown as ``PENDING_DATA`` instead of preventing the report from
        being emitted.  Allocation/plan callers pass ``required=True`` and
        fail closed until a real platform event is available.
        """

        alias = str(store_alias or "").strip()
        if not alias:
            raise FinanceServiceError("store alias is required for funding activation")
        rule = self.registry.get("fundingActivationRule")
        mode = ""
        if isinstance(rule, Mapping):
            mode = str(rule.get("mode") or "").strip().lower()
        dynamic_modes = {
            "first_completed_or_return_event",
            "first_completed_or_return",
            "first_completed_or_returned_event",
        }
        if mode in dynamic_modes:
            candidates = self._funding_activation_candidates(alias)
            if candidates:
                activation, source = min(
                    candidates,
                    key=lambda item: (item[0].astimezone(timezone.utc), item[1]),
                )
                return {
                    "mode": "first_completed_or_return_event",
                    "status": "RESOLVED",
                    "at": activation,
                    "atIso": activation.isoformat(),
                    "source": source,
                }
            pending = {
                "mode": "first_completed_or_return_event",
                "status": "PENDING_DATA",
                "at": None,
                "atIso": None,
                "source": None,
                "message": (
                    "No platform completed-order or return event is available "
                    f"for {alias}; advertising funding remains unresolved"
                ),
            }
            if required:
                raise FinanceServiceError(str(pending["message"]))
            return pending

        # Compatibility path for pre-dynamic registries and isolated tests.
        fixed = _parse_datetime(self.registry.get("fundingActivationAt"))
        if fixed is not None:
            return {
                "mode": "fixed_legacy",
                "status": "RESOLVED",
                "at": fixed,
                "atIso": fixed.isoformat(),
                "source": "registry.fundingActivationAt (legacy fallback)",
            }
        pending = {
            "mode": mode or "unconfigured",
            "status": "PENDING_DATA",
            "at": None,
            "atIso": None,
            "source": None,
            "message": f"Store registry has no funding activation evidence for {alias}",
        }
        if required:
            raise FinanceServiceError(str(pending["message"]))
        return pending

    def _funding_activation_candidates(
        self,
        store_alias: str,
    ) -> list[tuple[datetime, str]]:
        """Collect auditable lifecycle timestamps for one enabled store."""

        candidates: list[tuple[datetime, str]] = []
        opened_at: date | None = None
        for store in self.registry.get("stores", []):
            if not isinstance(store, Mapping) or str(store.get("alias")) != store_alias:
                continue
            raw_opened = str(store.get("openedAt") or "").strip()
            if raw_opened:
                try:
                    opened_at = date.fromisoformat(raw_opened[:10])
                except ValueError:
                    opened_at = None
            break

        def add(value: Any, source: str) -> None:
            parsed = _parse_datetime(value)
            if parsed is None:
                return
            local_day = parsed.astimezone(MOSCOW).date()
            if opened_at is not None and local_day < opened_at:
                return
            now_utc = self._now().astimezone(timezone.utc)
            if parsed.astimezone(timezone.utc) - now_utc > _MAX_FUTURE_EVIDENCE_SKEW:
                return
            candidates.append((parsed, source))

        # The persisted order-profit rows are the same lifecycle timestamps
        # used by report order snapshots.  Select only the latest version per
        # order so a prior restatement cannot move the start backwards/forwards
        # merely because it was recalculated later.
        latest_profit = self.ledger.connection.execute(
            """
            SELECT op.store_alias, op.order_key, op.completed_at, op.order_result
            FROM order_profit AS op
            WHERE op.store_alias=?
              AND NOT EXISTS (
                SELECT 1 FROM order_profit AS newer
                WHERE newer.store_alias=op.store_alias
                  AND newer.order_key=op.order_key
                  AND (
                    newer.calculated_at > op.calculated_at
                    OR (newer.calculated_at=op.calculated_at
                        AND newer.profit_version>op.profit_version)
                  )
              )
            """,
            (store_alias,),
        ).fetchall()
        for row in latest_profit:
            outcome = str(row["order_result"] or "").lower()
            if outcome not in {"completed", "returned"}:
                continue
            add(
                row["completed_at"],
                f"order_profit:{row['order_key']}:{outcome}",
            )

        # Return API readbacks are authoritative for a return event.  A return
        # still in transit has no completedDt; its fetched/occurred timestamp
        # is retained as the first observed return event rather than guessed
        # away.
        for row in self.ledger.connection.execute(
            "SELECT event_hash, occurred_at, payload_json FROM raw_returns "
            "WHERE store_alias=? ORDER BY occurred_at, event_hash",
            (store_alias,),
        ).fetchall():
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            value = (
                payload.get("completedDt")
                or payload.get("completedAt")
                or payload.get("returnDate")
                or row["occurred_at"]
            )
            add(value, f"raw_returns:{row['event_hash']}")

        # Finance rows can explicitly evidence a sale or cancellation before
        # an order-profit row has been persisted (for example, an order with a
        # temporarily missing cost version).  Use only unambiguous markers.
        return_markers = ("возврат", "отмен", "退货", "退单", "取消", "return", "cancel")
        sale_markers = ("продаж", "sale", "销售")
        for row in self.ledger.connection.execute(
            "SELECT rrd_id, occurred_at, payload_json FROM raw_finance "
            "WHERE store_alias=? ORDER BY occurred_at, rrd_id",
            (store_alias,),
        ).fetchall():
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            marker_text = " ".join(
                str(payload.get(key) or "").lower()
                for key in ("docTypeName", "bonusTypeName", "sellerOperName", "operationName")
            )
            is_return = any(marker in marker_text for marker in return_markers)
            is_sale = any(marker in marker_text for marker in sale_markers)
            if not (is_return or is_sale):
                continue
            if is_return:
                value = (
                    payload.get("returnDate")
                    or payload.get("saleDt")
                    or payload.get("rrDate")
                    or row["occurred_at"]
                )
                add(value, f"raw_finance:{row['rrd_id']}:return_or_cancel")
            else:
                try:
                    positive = _decimal(payload.get("retailPriceWithDisc")) > _ZERO
                except (InvalidOperation, TypeError, ValueError):
                    positive = False
                if not positive:
                    continue
                value = payload.get("saleDt") or payload.get("rrDate") or row["occurred_at"]
                add(value, f"raw_finance:{row['rrd_id']}:completed_sale")

        # If no financial/lifecycle candidate exists, the online order feed is
        # a read-only fallback.  It is intentionally not mixed with stronger
        # finance evidence because its `date` is often creation time.
        if not candidates:
            for row in self.ledger.connection.execute(
                "SELECT event_hash, occurred_at, payload_json FROM raw_sales "
                "WHERE store_alias=? ORDER BY occurred_at, event_hash",
                (store_alias,),
            ).fetchall():
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                if payload.get("isRealization") is not True:
                    continue
                if payload.get("isCancel") is True:
                    continue
                add(
                    payload.get("lastChangeDate") or payload.get("saleDt") or row["occurred_at"],
                    f"raw_sales:{row['event_hash']}:realized",
                )
        return candidates

    def _historical_withdrawal_events(
        self,
        store_alias: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read the append-only historical cash registry without inferring debt."""

        path = self.periodic_standard.historical_cash_events_registry
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FinanceServiceError(
                f"Historical cash-event registry is unreadable: {path}"
            ) from exc
        events = raw.get("events") if isinstance(raw, Mapping) else []
        if not isinstance(events, list):
            return []
        result = []
        for event in events:
            if not isinstance(event, Mapping):
                continue
            if str(event.get("eventType") or "").lower() != "withdrawal":
                continue
            if store_alias is not None and str(event.get("store") or "") != store_alias:
                continue
            result.append(dict(event))
        return result

    def _historical_withdrawal_amount(self, store_alias: str) -> Decimal:
        total = _ZERO
        for event in self._historical_withdrawal_events(store_alias):
            try:
                total += _decimal(event.get("amountCny"))
            except (InvalidOperation, TypeError, ValueError):
                continue
        return total.quantize(_CENT)

    def withdrawal_record(
        self,
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
        """Record a human-executed withdrawal; never calls a write API.

        Use ``settlement_balance`` for current withdrawals: advertising is paid
        directly from WB settlement balance and the due/withheld/debt fields are
        fixed at zero.  Omitted legacy fee facts remain
        ``unknown/PENDING_DATA`` until evidence is supplied.
        """

        if store_alias not in self.store_aliases:
            raise FinanceServiceError(f"Withdrawal store is not enabled: {store_alias}")
        debt_policy = (
            ((self.policy.raw.get("funding") or {}).get("withdrawalAdvertisingDebt"))
            or {}
        )
        cutover_raw = str(debt_policy.get("cutoverEffectiveFrom") or "").strip()
        try:
            cutover = date.fromisoformat(cutover_raw)
        except ValueError as exc:
            raise FinanceServiceError(
                "Active policy has an invalid withdrawal advertising-debt cutover"
            ) from exc
        withdrawn = _parse_datetime(withdrawn_at)
        if withdrawn is None:
            raise FinanceServiceError("withdrawn_at must be a valid date-time")
        withdrawal_date = withdrawn.astimezone(MOSCOW).date()
        supplied_fee_status = str(advertising_fee_status or "").strip().casefold()
        settlement_aliases = {
            "settlement_balance",
            "settlement-balance",
            "settlement balance",
            "not_applicable",
            "not-applicable",
            "not applicable",
            "n/a",
            "余额结算",
            "结算余额",
        }
        if withdrawal_date >= cutover:
            if supplied_fee_status and supplied_fee_status not in settlement_aliases:
                raise FinanceServiceError(
                    "Withdrawals on or after the policy cutover must use "
                    "advertising_fee_status=settlement_balance"
                )
            advertising_fee_status = "settlement_balance"
        elif supplied_fee_status in settlement_aliases:
            raise FinanceServiceError(
                "settlement_balance cannot be applied to a pre-cutover withdrawal; "
                "preserve its historical advertising-fee evidence"
            )
        return record_withdrawal_from_user_record(
            self.ledger,
            store_alias=store_alias,
            record_key=record_key,
            amount_cny=amount_cny,
            withdrawn_at=withdrawn_at,
            advertising_fee_due_cny=advertising_fee_due_cny,
            advertising_fee_withheld_cny=advertising_fee_withheld_cny,
            advertising_fee_status=advertising_fee_status,
            settled_at=settled_at,
            external_reference=external_reference,
            evidence=evidence,
        )

    def advertising_debt_status(
        self,
        *,
        store_alias: str | None = None,
        as_of: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Return the store-separated outstanding advertising-fee ledger."""

        if store_alias is not None and store_alias not in self.store_aliases:
            raise FinanceServiceError(f"Debt store is not enabled: {store_alias}")
        return self.ledger.advertising_debt_summary(
            store_alias=store_alias,
            as_of=as_of,
        )

    def reconcile_advertising_debt(
        self,
        *,
        store_alias: str,
        period_start: datetime | str | None = None,
        period_end: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Import synced WB advertising-payment readbacks and FIFO-settle debt."""

        if store_alias not in self.store_aliases:
            raise FinanceServiceError(f"Debt store is not enabled: {store_alias}")
        return import_advertising_payment_events_from_ledger(
            self.ledger,
            store_alias,
            period_start=period_start,
            period_end=period_end,
        )

    def _complete_financial_cutoff(self, value: str | date, *, now: datetime | None = None) -> date:
        """Resolve relative dates in the financial timezone, not the trigger's."""
        moment = now or self._now()
        financial_today = moment.astimezone(MOSCOW).date()
        cutoff = (financial_today - timedelta(days=1)
                  if str(value).strip().lower() == "yesterday"
                  else resolve_through(value, now=moment))
        if cutoff >= financial_today:
            raise FinanceServiceError("Running accounts and reports require a complete-day cutoff")
        return cutoff

    def account_refresh(
        self,
        *,
        through: str | date = "yesterday",
        source_statuses: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Materialize local source events once; never sum report snapshots.

        This is an on-disk bookkeeping operation, not a platform fetch or a
        payment. Partial coverage is passed through to the owner dashboard.
        """
        from .finance_running_adapter import refresh_running_accounts

        cutoff = self._complete_financial_cutoff(through)
        return refresh_running_accounts(
            self, through=cutoff, source_statuses=source_statuses
        )

    def account_report(
        self,
        *,
        period: str,
        through: str | date = "yesterday",
        period_start: str | date | None = None,
        _archive: bool = True,
    ) -> dict[str, Any]:
        """Publish the fixed owner template from the durable running accounts."""
        from .finance_running_adapter import build_running_account_report

        if period not in {"weekly", "monthly", "as-of"}:
            raise FinanceServiceError(f"Unsupported running-account period: {period}")
        cutoff = self._complete_financial_cutoff(through)
        start = (
            resolve_through(period_start, now=self._now())
            if period_start is not None
            else None
        )
        if period == "weekly":
            start = start or cutoff - timedelta(days=6)
            if start.weekday() != 0 or cutoff.weekday() != 6 or (cutoff - start).days != 6:
                raise FinanceServiceError("Weekly reports must cover Monday through Sunday")
        elif period == "monthly":
            start = start or cutoff.replace(day=1)
            if start.day != 1 or start != cutoff.replace(day=1) or (cutoff + timedelta(days=1)).day != 1:
                raise FinanceServiceError("Monthly reports must cover a full calendar month")
        if start is not None and start > cutoff:
            raise FinanceServiceError("Running report end precedes its start")
        result = build_running_account_report(
            self, period=period, period_start=start, through=cutoff
        )
        if _archive:
            try:
                archived = self._archive_owner_report(result)
                if archived.get("status") != "NOT_CONFIGURED":
                    result = {**result, "archive": archived}
            except Exception as exc:
                return {**result, "status": "PARTIAL_FAILURE", "archive": {"status": "FAILED", "error": str(exc)}}
        return result

    def _archive_owner_report(self, report: dict[str, Any]) -> dict[str, Any]:
        from .finance_running_adapter import archive_running_account_report

        return archive_running_account_report(self, report)

    def _running_report_published(self, period: FinancePeriod) -> bool:
        """Check one exact report manifest, never enumerate historical closes."""
        from .finance_running_adapter import running_report_published

        return running_report_published(
            self, period=period.kind, period_start=period.start, through=period.end
        )

    def bootstrap(self, *, through: str | date = "yesterday") -> dict[str, Any]:
        run_cache = self._new_report_run_cache()
        summaries = sync_enabled_stores(
            self.ledger,
            registry_path=self.registry_path,
            repo_root=self.repo_root,
            through=through,
            bootstrap=True,
            max_store_workers=self.max_store_workers,
            source_factory=self.source_factory,
            now=self._now(),
        )
        running_accounts = self.account_refresh(
            through=through, source_statuses=[summary.as_dict() for summary in summaries]
        )
        debt_reconciliation = self._reconcile_synced_advertising_payments()
        report = self.report(kind="bootstrap", period="auto", _run_cache=run_cache)
        return {
            "operation": "bootstrap",
            "stores": [summary.as_dict() for summary in summaries],
            "runningAccounts": running_accounts,
            "advertisingDebtReconciliation": debt_reconciliation,
            "report": report,
        }

    def daily(
        self,
        *,
        through: str | date = "yesterday",
        sync_workbook: bool = False,
        correction_window_days: int | None = None,
        max_store_workers: int | None = None,
    ) -> dict[str, Any]:
        started_at = perf_counter()
        phase_timings_ms: dict[str, float] = {}
        phase_failures: list[dict[str, str]] = []
        run_cache = self._new_report_run_cache()

        run_now = self._now()
        cutoff = self._complete_financial_cutoff(through, now=run_now)
        schedule_day = run_now.astimezone(SHANGHAI).date()
        weekly_due = schedule_day.weekday() == 0
        monthly_due = schedule_day.day == 1
        first_monthly_due = (
            schedule_day.isoformat()
            == self.periodic_standard.first_monthly_scheduled_for
        )
        if correction_window_days is None:
            correction_window_days = (
                _FULL_CORRECTION_WINDOW_DAYS
                if weekly_due or monthly_due or first_monthly_due
                else _DAILY_CORRECTION_WINDOW_DAYS
            )
        else:
            try:
                correction_window_days = max(1, int(correction_window_days))
            except (TypeError, ValueError) as exc:
                raise FinanceServiceError(
                    "correction_window_days must be a positive integer"
                ) from exc
        if max_store_workers is None:
            max_store_workers = self.max_store_workers
        else:
            try:
                max_store_workers = max(1, int(max_store_workers))
            except (TypeError, ValueError) as exc:
                raise FinanceServiceError(
                    "max_store_workers must be a positive integer"
                ) from exc

        def phase(name: str, callback):
            phase_started = perf_counter()
            try:
                return callback()
            finally:
                phase_timings_ms[name] = round(
                    (perf_counter() - phase_started) * 1000, 3
                )

        def independent_phase(name: str, callback):
            # An auxiliary artifact failure must not erase a completed owner
            # report or prevent another independent period being published.
            try:
                return phase(name, callback)
            except Exception as exc:
                phase_failures.append(
                    {"phase": name, "status": "FAILED", "error": str(exc)}
                )
                return None

        summaries = independent_phase(
            "sync",
            lambda: sync_enabled_stores(
                self.ledger,
                registry_path=self.registry_path,
                repo_root=self.repo_root,
                through=cutoff,
                bootstrap=False,
                correction_window_days=correction_window_days,
                max_store_workers=max_store_workers,
                source_factory=self.source_factory,
                now=run_now,
            ),
        )
        sync_succeeded = summaries is not None
        summaries = summaries or []
        source_statuses = [summary.as_dict() for summary in summaries]
        # Successful sources are durable even when another source failed. Do
        # this before legacy settlement/report gates and preserve coverage.
        running_accounts = independent_phase(
            "runningAccountRefresh",
            lambda: self.account_refresh(through=cutoff, source_statuses=source_statuses),
        ) if sync_succeeded else None
        owner_reports: list[dict[str, Any]] = []
        owner_ledger: dict[str, Any] | None = None
        owner_ledger_covers: list[dict[str, Any]] = []
        if running_accounts is not None:
            # This is the primary daily delivery, including all four fixed
            # windows. The adapter reuses an unchanged verified publication.
            owner_ledger = independent_phase(
                "ownerLedgerReport",
                lambda: self.account_report(period="as-of", through=cutoff, _archive=False),
            )
            if owner_ledger is not None:
                owner_reports.append(owner_ledger)
            # A historical cutoff must not borrow the current machine date's
            # week or month. Yesterday's closing date defines the run day.
            owner_schedule_day = cutoff + timedelta(days=1)
            for due, owner_period in (
                (owner_schedule_day.weekday() == 0, previous_week(today=owner_schedule_day)),
                (owner_schedule_day.day == 1, previous_month(today=owner_schedule_day)),
            ):
                if owner_period.end > cutoff:
                    continue
                if owner_ledger is not None and owner_period.end == cutoff:
                    # The closed period already occupies its own column in
                    # the same-cutoff dashboard; do not export it twice.
                    owner_ledger_covers.append({
                        "period": owner_period.kind,
                        "periodStart": owner_period.start.isoformat(),
                        "periodEnd": owner_period.end.isoformat(),
                        "coveredBy": "ownerLedger",
                        "workbook": owner_ledger.get("workbook"),
                        "templateVersion": owner_ledger.get("templateVersion"),
                    })
                    continue
                # A disconnected local host can miss Monday/month-start. On
                # resume inspect only the latest closed week and month; do
                # not scan or re-export the entire publication history.
                already_published = False if due else independent_phase(
                    f"owner{owner_period.kind.title()}PublicationCheck",
                    lambda selected=owner_period: self._running_report_published(selected),
                )
                if already_published is False:
                    owner_report = independent_phase(
                        f"owner{owner_period.kind.title()}Report",
                        lambda selected=owner_period: self.account_report(
                            period=selected.kind,
                            period_start=selected.start,
                            through=selected.end,
                            _archive=False,
                        ),
                    )
                    if owner_report is not None:
                        owner_reports.append(owner_report)
        if owner_ledger is None:
            from .finance_running_adapter import latest_verified_owner_report

            owner_ledger = independent_phase("ownerLedgerFallback",
                lambda: latest_verified_owner_report(self, through=cutoff))
            if owner_ledger is not None:
                owner_reports.insert(0, owner_ledger)
        owner_archives: list[dict[str, Any]] = []
        for owner_report in owner_reports:
            label = "ownerLedgerArchive" if owner_report is owner_ledger else f"owner{str(owner_report.get('period', 'Closed')).title()}Archive"
            archived = independent_phase(label, lambda selected=owner_report: self._archive_owner_report(selected))
            if archived is not None and archived.get("status") != "NOT_CONFIGURED":
                owner_archives.append(archived)
        # advertising_payments is a read-only WB source.  Import any positive
        # top-up rows before building reports so a later manual recharge can
        # settle the debt shown in the same run.  Malformed rows remain visible
        # as unresolved evidence and never become a guessed zero.
        debt_reconciliation = independent_phase(
            "advertisingDebtReconciliation",
            self._reconcile_synced_advertising_payments,
        )
        reports: list[dict[str, Any]] = []
        if (
            first_monthly_due
            or not self.ledger.find_reports(
                report_type="bootstrap", report_status="FINAL"
            )
        ):
            bootstrap_report = independent_phase(
                "bootstrapReport",
                lambda: self.report(
                    kind="bootstrap",
                    period="auto",
                    _publish_open_items=first_monthly_due,
                    _run_cache=run_cache,
                ),
            )
            if bootstrap_report is not None:
                reports.append(bootstrap_report)
        weekly_report = independent_phase(
            "weeklyReport",
            lambda: self.report(
                kind="weekly",
                period="auto",
                _publish_open_items=weekly_due,
                _run_cache=run_cache,
            ),
        )
        if weekly_report is not None:
            reports.append(weekly_report)
        # The scheduled monthly view is always the prior calendar month. The
        # opening-to-date stock remains a separate bootstrap view.
        monthly_report = independent_phase(
            "monthlyReport",
            lambda: self.report(
                kind="monthly",
                period="auto",
                _selected_period=previous_month(
                    today=self._now().astimezone(MOSCOW).date()
                ),
                _publish_open_items=monthly_due,
                _run_cache=run_cache,
            ),
        )
        if monthly_report is not None:
            reports.append(monthly_report)
        current_keys = {str(item["reportKey"]) for item in reports}
        historical_reports: list[dict[str, Any]] = []
        historical_checked = 0
        # Recalculate finalized historical periods only when a report-relevant
        # source changed (new/updated finance, lifecycle, return, or ad
        # evidence, including a coverage status transition).  Pure balance
        # snapshots do not change order economics and should not trigger full
        # historical recalculation on every daily run (candidate counting is
        # still retained for auditability).
        report_relevant_changed = bool(
            summaries
            and any(
                bool(
                    getattr(
                        summary,
                        "report_relevant_changed",
                        getattr(summary, "changed", False),
                    )
                )
                for summary in summaries
            )
        )
        if summaries:
            historical_result = independent_phase(
                "historicalRefresh",
                lambda: self._refresh_finalized_reports(
                    exclude_report_keys=current_keys,
                    affected_from=min(summary.requested_from for summary in summaries),
                    affected_through=max(summary.requested_through for summary in summaries),
                    refresh=report_relevant_changed,
                    _run_cache=run_cache,
                ),
            )
            if historical_result is not None:
                historical_reports, historical_checked = historical_result
        reports.extend(historical_reports)
        result = {
            "operation": "daily",
            "through": cutoff.isoformat(),
            "stores": source_statuses,
            "runningAccounts": running_accounts,
            "ownerLedger": owner_ledger,
            "ownerLedgerCovers": owner_ledger_covers,
            "ownerArchives": owner_archives,
            "ownerReports": owner_reports,
            "phaseFailures": phase_failures,
            "advertisingDebtReconciliation": debt_reconciliation,
            "reports": reports,
            "historicalReportsChecked": historical_checked,
            "historicalReportsRecalculated": len(historical_reports),
            "performance": {
                "totalMs": round((perf_counter() - started_at) * 1000, 3),
                "correctionWindowDays": correction_window_days,
                "maxStoreWorkers": max_store_workers,
                "phaseTimingsMs": dict(phase_timings_ms),
                "cacheHits": dict(run_cache.get("hits") or {}),
                "cacheEntries": self._cache_entry_counts(run_cache),
            },
        }
        if sync_workbook:
            # Keep the canonical workbook bridge outside the calculation
            # engine.  Only immutable JSON artifacts produced by this run are
            # handed to it; the bridge itself performs locking, backup,
            # temporary export, re-import and formula/readback checks.
            from .finance_workbook import sync_canonical_workbook

            report_paths = [
                item.get("artifacts", {}).get("json")
                for item in reports
                if isinstance(item.get("artifacts"), Mapping)
                and item.get("artifacts", {}).get("json")
            ]
            result["workbookSync"] = independent_phase(
                "workbookSync",
                lambda: sync_canonical_workbook(
                    report_paths=report_paths,
                    through=result["through"],
                    # Keep the bridge aligned with the service's effective report
                    # archive when a caller supplies --finance-output; explicit
                    # artifact paths remain the source of truth for this run.
                    report_root=self.output_root,
                    debt_snapshot=self.advertising_debt_status(),
                    repo_root=self.repo_root,
                ),
            )
            result["performance"]["totalMs"] = round(
                (perf_counter() - started_at) * 1000, 3
            )
            result["performance"]["phaseTimingsMs"] = dict(phase_timings_ms)
        result["status"] = (
            "PARTIAL_FAILURE"
            if phase_failures
            else "PENDING_DATA"
            if any(not bool(row.get("complete")) for item in source_statuses for row in item.get("sources", []))
            or (running_accounts or {}).get("status") == "PENDING_DATA"
            or any(item.get("reportStatus") == "PENDING_DATA" for item in owner_reports)
            else "SUCCESS"
        )
        return result

    def _reconcile_synced_advertising_payments(self) -> list[dict[str, Any]]:
        """Read synced advertising-payment rows and settle withdrawal debt.

        This is intentionally a local readback step.  It does not call any WB
        write endpoint and is safe to repeat because payment IDs are
        idempotent in the ledger.
        """

        results: list[dict[str, Any]] = []
        for alias in self.store_aliases:
            try:
                results.append(
                    import_advertising_payment_events_from_ledger(
                        self.ledger,
                        alias,
                    )
                )
            except Exception as exc:
                results.append(
                    {
                        "storeAlias": alias,
                        "importedCount": 0,
                        "unresolvedRows": [
                            {
                                "reason": f"payment readback import failed: {type(exc).__name__}: {exc}"
                            }
                        ],
                    }
                )
        return results

    def cost_import(
        self,
        *,
        quote: Path,
        effective_from: str,
        approved_by: str,
        rub_per_cny: str | Decimal | None = None,
        source_currency: str | None = None,
    ) -> dict[str, Any]:
        approver = str(approved_by or "").strip()
        if not approver:
            raise FinanceServiceError("--approved-by is required")
        currency = str(source_currency or "").strip().upper()
        if currency not in {"RUB", "CNY"}:
            raise FinanceServiceError("--source-currency must be explicitly RUB or CNY")
        rate = rub_per_cny
        if currency == "RUB" and rate in (None, ""):
            raise FinanceServiceError(
                "RUB-source cost versions require an explicit --rub-per-cny value"
            )
        rows = parse_cost_quote(
            quote,
            effective_from=effective_from,
            rub_per_cny=rate,
            source_currency=currency,
        )
        derived_rates = sorted({str(row.rub_per_cny) for row in rows})
        exchange_rate_evidence: str | list[str] = (
            str(rate)
            if rate not in (None, "")
            else (derived_rates[0] if len(derived_rates) == 1 else derived_rates)
        )
        existing_approvals = self.ledger.connection.execute(
            f"SELECT cost_version_uid, approved_by, approved_at FROM cost_versions "
            f"WHERE cost_version_uid IN ({', '.join('?' for _ in rows)})",
            [row.version_id for row in rows],
        ).fetchall() if rows else []
        if existing_approvals:
            if len(existing_approvals) != len(rows):
                raise FinanceServiceError(
                    "The quote import is only partially present; manual ledger review is required"
                )
            if {str(item["approved_by"]) for item in existing_approvals} != {approver}:
                raise FinanceServiceError("The existing cost import has a different approver")
            approval_times = {str(item["approved_at"]) for item in existing_approvals}
            if len(approval_times) != 1:
                raise FinanceServiceError("The existing quote rows have inconsistent approval times")
            approved_at = approval_times.pop()
        else:
            approved_at = self._now().isoformat()
        records = []
        for row in rows:
            record = row.as_dict()
            record.update(
                {
                    "approvedBy": approver,
                    "approvedAt": approved_at,
                    "sourceCurrency": row.source_currency,
                    "skuAliases": sorted({row.product_key, row.specification}),
                }
            )
            records.append(record)
        internal_ids = self.ledger.import_cost_versions(records)
        manifest = {
            "schemaVersion": 1,
            "quote": str(Path(quote).resolve()),
            "quoteSha256": rows[0].source_hash if rows else None,
            "effectiveFrom": rows[0].effective_from.isoformat() if rows else None,
            "approvedBy": approver,
            "approvedAt": approved_at,
            "exchangeRateRubPerCny": exchange_rate_evidence,
            "sourceCurrency": currency,
            "costVersions": [
                {**row.as_dict(), "internalLedgerId": identifier}
                for row, identifier in zip(rows, internal_ids)
            ],
        }
        manifest_dir = self.ledger.path.parent / "cost-imports"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / (
            f"{rows[0].source_hash[:16]}-{rows[0].effective_from.date().isoformat()}.json"
        )
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            # Approval time is audit metadata and may differ on an idempotent
            # retry; immutable cost content itself must remain identical.
            comparable_existing = dict(existing)
            comparable_manifest = dict(manifest)
            comparable_existing.pop("approvedAt", None)
            comparable_manifest.pop("approvedAt", None)
            if comparable_existing != comparable_manifest:
                raise FinanceServiceError(f"Cost import manifest differs: {manifest_path}")
        else:
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return {
            "operation": "cost-import",
            "quote": str(Path(quote).resolve()),
            "quoteSha256": rows[0].source_hash if rows else None,
            "effectiveFrom": rows[0].effective_from.isoformat() if rows else None,
            "approvedBy": approver,
            "approvedAt": approved_at,
            "exchangeRateRubPerCny": exchange_rate_evidence,
            "sourceCurrency": currency,
            "rowCount": len(rows),
            "manifest": str(manifest_path),
            "costVersionIdSample": [row.version_id for row in rows[:5]],
        }

    def report(
        self,
        *,
        kind: str,
        period: str,
        reason: str | None = None,
        _selected_period: FinancePeriod | None = None,
        _publish_open_items: bool = True,
        _run_cache: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if _selected_period is None:
            first_monthly = kind == "monthly" and not any(
                report
                for status in ("FINAL", "RESTATED")
                for report in self.ledger.find_reports(
                    report_type="monthly", report_status=status
                )
            )
            selected = parse_report_period(
                kind=kind,
                value=period,
                registry=self.registry,
                today=self._now().astimezone(MOSCOW).date(),
                first_monthly=first_monthly,
            )
        else:
            if _selected_period.kind != kind:
                raise FinanceServiceError("Selected report period kind does not match")
            selected = _selected_period
        selected = self._adjust_cumulative_start(selected, _run_cache=_run_cache)
        evidence = self._load_period_evidence(selected, _run_cache=_run_cache)
        advertising_reconciliation = reconcile_advertising_cost_events(
            evidence["advertising"]
        )
        evidence["advertising"] = advertising_reconciliation["events"]
        period_cache_key = self._period_cache_key(selected)
        if (
            _run_cache is not None
            and period_cache_key in _run_cache.setdefault("fullCalculations", {})
        ):
            self._cache_hit(_run_cache, "fullCalculation")
            full_calculation = deepcopy(
                _run_cache["fullCalculations"][period_cache_key]
            )
        else:
            calculated = calculate_financial_report(
                order_rows=evidence["order"],
                finance_rows=evidence["finance"],
                return_rows=evidence["return"],
                advertising_events=evidence["advertising"],
                cost_selector=self._select_cost,
                policy=self.policy_timeline,
            )
            full_calculation = calculated.to_dict()
            if _run_cache is not None:
                _run_cache.setdefault("fullCalculations", {})[
                    period_cache_key
                ] = deepcopy(full_calculation)
        prior_calculation = self._prior_period_calculation(
            evidence, selected, _run_cache=_run_cache
        )
        blockers = list(full_calculation.get("blockers") or [])
        for event in advertising_reconciliation["unknownEvents"]:
            blockers.append({
                "code": "ADVERTISING_FUNDING_SOURCE_UNRESOLVED",
                "storeAlias": str(event.get("storeAlias") or ""),
                "message": "Advertising consumption cannot be classified as seller cash or platform promotional funds.",
                "evidenceId": str(event.get("eventId") or ""),
            })
        if prior_calculation is not None:
            blockers.extend(prior_calculation.get("blockers") or [])
        if (
            _run_cache is not None
            and period_cache_key in _run_cache.setdefault("sourceBlockers", {})
        ):
            self._cache_hit(_run_cache, "sourceBlockers")
            blockers.extend(deepcopy(_run_cache["sourceBlockers"][period_cache_key]))
        else:
            source_blockers = self._source_blockers(selected, evidence)
            blockers.extend(source_blockers)
            if _run_cache is not None:
                _run_cache.setdefault("sourceBlockers", {})[
                    period_cache_key
                ] = deepcopy(source_blockers)
        maturity = report_maturity_blocker(
            selected, today=self._now().astimezone(MOSCOW).date()
        )
        if maturity:
            blockers.append({"code": "SETTLEMENT_NOT_MATURE", "message": maturity})
        blockers = _dedupe_blockers(blockers)
        if (
            _run_cache is not None
            and period_cache_key in _run_cache.setdefault("periodDeltas", {})
        ):
            self._cache_hit(_run_cache, "periodDelta")
            calculation = deepcopy(_run_cache["periodDeltas"][period_cache_key])
        else:
            calculation = self._period_delta_calculation(
                full_calculation,
                selected,
                prior_calculation=prior_calculation,
            )
            if _run_cache is not None:
                _run_cache.setdefault("periodDeltas", {})[
                    period_cache_key
                ] = deepcopy(calculation)
        calculation = _with_advertising_reconciliation(
            calculation, advertising_reconciliation
        )
        calculation["statisticsEvents"] = _statistics_period_events(
            evidence["order"], selected
        )
        snapshot_hash = hashlib.sha256(
            canonical_json(
                {
                    "period": selected.as_dict(),
                    "policy": {
                        "reporting": self.policy.reporting_policy_id,
                        "pricing": self.policy.pricing_policy_id,
                    },
                    "calculation": calculation,
                    "sourceBlockers": blockers,
                }
            ).encode("utf-8")
        ).hexdigest()
        if (
            _run_cache is not None
            and period_cache_key in _run_cache.setdefault("stability", {})
        ):
            self._cache_hit(_run_cache, "stability")
            stable = bool(_run_cache["stability"][period_cache_key])
        else:
            stable = self._record_report_stability(selected, evidence, blockers)
            if _run_cache is not None:
                _run_cache.setdefault("stability", {})[period_cache_key] = bool(stable)
        if selected.kind in {"weekly", "monthly"} and not stable:
            blockers.append(
                {
                    "code": "SOURCE_NOT_STABLE_TWICE",
                    "message": "Official finance and advertising evidence must be complete and unchanged in two consecutive captures.",
                }
            )
        blockers = _dedupe_blockers(blockers)
        status = "PENDING_DATA" if blockers else "FINAL"
        report_key = f"{kind}:{selected.start.isoformat()}:{selected.end.isoformat()}"
        previous: dict[str, Any] | None = None
        publish_with_open_items = bool(
            self.policy.raw.get("governance", {}).get(
                "scheduledReportsAlwaysPublish", False
            )
        )
        if status == "PENDING_DATA":
            # Pending candidates use a content-addressed version.  Historical
            # refresh now revisits them on later captures, so an unchanged
            # candidate must be returned idempotently instead of attempting a
            # duplicate insert into the immutable reports table.
            for candidate in self.ledger.find_reports(
                report_key=report_key, report_status="PENDING_DATA"
            ):
                candidate_full = self.ledger.get_report(
                    str(candidate["report_id"]), include_orders=False
                )
                candidate_hash = str(
                    ((candidate_full or {}).get("metadata") or {}).get(
                        "calculationHash"
                    )
                    or ""
                )
                if candidate_hash == snapshot_hash and candidate_full is not None:
                    existing = self._existing_report_result(candidate_full)
                    artifact_state, _ = self._report_artifact_state_for_report(
                        candidate_full
                    )
                    if (
                        artifact_state == "INCOMPLETE"
                        or (_publish_open_items and publish_with_open_items)
                    ) and existing["artifacts"] is None:
                        payload = self._report_payload(
                            calculation,
                            selected,
                            status=str(candidate_full["report_status"]),
                            blockers=blockers,
                            previous_report=self._previous_published_report_payload(
                                selected,
                                exclude_report_id=str(candidate_full["report_id"]),
                            ),
                        )
                        payload["reportId"] = candidate_full["report_id"]
                        payload["reportVersion"] = candidate_full["report_version"]
                        payload["status"] = candidate_full["report_status"]
                        existing["period"] = selected.as_dict()
                        existing["orderCount"] = len(payload["orders"])
                        existing["blockers"] = blockers
                        existing["artifacts"] = self._write_report(payload, selected)
                    return existing
        if status == "FINAL":
            matching = self._latest_final_report(
                report_key,
                snapshot_hash=snapshot_hash,
            )
            if matching is not None:
                existing = self._existing_report_result(matching)
                if existing["artifacts"] is None:
                    payload = self._report_payload(
                        calculation,
                        selected,
                        status=str(matching["report_status"]),
                        blockers=blockers,
                        previous_report=self._previous_published_report_payload(
                            selected,
                            exclude_report_id=str(matching["report_id"]),
                            preferred_report_id=(
                                str(matching.get("previous_report_id"))
                                if matching.get("previous_report_id")
                                else None
                            ),
                        ),
                    )
                    payload["reportId"] = matching["report_id"]
                    payload["reportVersion"] = matching["report_version"]
                    payload["status"] = matching["report_status"]
                    existing["period"] = selected.as_dict()
                    existing["orderCount"] = len(payload["orders"])
                    existing["blockers"] = blockers
                    existing["artifacts"] = self._write_report(payload, selected)
                return existing
            previous = self._latest_final_report(
                report_key,
                require_complete_artifacts=True,
            )
            if previous is not None:
                status = "RESTATED"

        # Persist complete lifecycle rows before building funding metadata so
        # a first-ever report can resolve its dynamic activation timestamp in
        # the same run (without waiting for the next scheduled cycle).
        persisted_orders = self._persist_order_profits(
            full_calculation.get("orders") or []
        )
        payload = self._report_payload(
            calculation,
            selected,
            status=status,
            blockers=blockers,
            previous_report=self._previous_published_report_payload(
                selected,
                preferred_report_id=(
                    str(previous["report_id"])
                    if status == "RESTATED" and previous is not None
                    else None
                ),
            ),
        )
        version = self._next_report_version(
            report_key,
            status=status,
            snapshot_hash=snapshot_hash,
        )
        report_orders = []
        for order in payload["orders"]:
            key = (str(order.get("storeAlias")), str(order.get("orderKey")))
            profit_version = persisted_orders.get(key)
            if profit_version is None:
                continue
            report_orders.append(
                {
                    "store_alias": key[0],
                    "order_key": key[1],
                    "profit_version": profit_version,
                    "owner_profit_cny": order.get("ownerProfitCny"),
                    "snapshot": order,
                }
            )
        totals = payload["totals"]
        previous_id = previous.get("report_id") if status == "RESTATED" and previous else None
        report_id = self.ledger.create_report(
            report_key,
            version,
            report_type=kind,
            period_start=_period_start(selected.start),
            period_end=_period_end_exclusive(selected.end),
            policy_id=self.policy.reporting_policy_id,
            totals={
                "revenue_cny": totals["netTurnoverCny"],
                "cost_cny": totals["purchaseCostCny"],
                "profit_cny": totals["ownerProfitCny"],
                "managed_operations_cny": totals["managedOperationsCny"],
                "return_fee_cny": totals["returnLogisticsCny"],
                "advertising_cny": totals["advertisingProvisionCny"],
            },
            orders=report_orders,
            generated_at=self._now(),
            metadata={
                "calculationHash": snapshot_hash,
                "blockers": blockers,
                "period": selected.as_dict(),
                "pricingPolicyId": self.policy.pricing_policy_id,
                "cumulative": selected.cumulative,
            },
            report_status=status,
            previous_report_id=previous_id,
            correction_reason=(
                (reason or "WB late refund/correction or financial-policy restatement")
                if status == "RESTATED"
                else None
            ),
            correction_metadata=(
                {"previousCalculationHash": (previous.get("metadata") or {}).get("calculationHash")}
                if status == "RESTATED" and previous
                else None
            ),
        )
        payload["reportId"] = report_id
        payload["reportVersion"] = version
        payload["status"] = status
        artifact_paths: dict[str, Any] | None = None
        if status in {"FINAL", "RESTATED"} or (
            publish_with_open_items and _publish_open_items
        ):
            artifact_paths = self._write_report(payload, selected)
        return {
            "reportId": report_id,
            "reportKey": report_key,
            "reportVersion": version,
            "status": status,
            "period": selected.as_dict(),
            "orderCount": len(payload["orders"]),
            "blockers": blockers,
            "artifacts": artifact_paths,
        }

    def funds_plan(self, *, report_id: str) -> dict[str, Any]:
        report = self.ledger.get_report(report_id)
        if report is None:
            raise FinanceServiceError(f"Unknown report: {report_id}")
        if not self._report_is_allocation_ready(report):
            raise FinanceServiceError(
                "Funds plans require an allocation-ready weekly report with at least "
                "one settled, cash-reconciled row when evidence remains open"
            )
        latest_weekly = self._latest_weekly_allocation_report()
        if latest_weekly is None:
            raise FinanceServiceError("No allocation-ready weekly report is available")
        if str(latest_weekly["report_id"]) != report_id:
            raise FinanceServiceError(
                "The requested weekly report has been superseded; use the latest "
                f"allocation-ready weekly report: {latest_weekly['report_id']}"
            )
        plans = []
        debt_rows = []
        activation_rows = []
        for store_alias in self.store_aliases:
            activation_info = self._funding_activation_info(store_alias, required=True)
            activation_at = activation_info["at"]
            activation = activation_info["atIso"]
            activation_rows.append(
                {
                    "storeAlias": store_alias,
                    "status": activation_info["status"],
                    "at": activation,
                    "source": activation_info.get("source"),
                }
            )
            balance = self._latest_withdrawable_balance(store_alias)
            if balance is None:
                raise FinanceServiceError(
                    f"No platform withdrawable-balance evidence for {store_alias}"
                )
            balance_evidence_at = self._require_fresh_balance_evidence(
                balance,
                store_alias=store_alias,
            )
            accruals = self._advertising_accruals(
                store_alias,
                formal_activation_at=activation,
            )
            commitments = [
                DepositCommitment(
                    action_id=str(item["action_id"]),
                    amount_cny=Decimal(str(item["requested_amount_cny"])),
                    status=str(item["lifecycle_status"]),
                    readback_at=(
                        datetime.fromisoformat(str(item["executed_at"]))
                        if item.get("lifecycle_status") == "readback_confirmed"
                        and item.get("executed_at")
                        else None
                    ),
                )
                for item in self.ledger.query_funding_actions(
                    store_alias=store_alias, action_type="advertising_top_up"
                )
                if self._funding_action_locks_reserve(item)
            ]
            plan = build_funding_plan(
                report_id=report_id,
                store=store_alias,
                platform_available_balance_cny=balance["withdrawable_balance_cny"],
                balance_evidence_at=balance_evidence_at,
                formal_activation_at=activation,
                advertising_accruals=accruals,
                deposit_commitments=commitments,
                historical_bootstrap=False,
            )
            plan_dict = plan.as_dict()
            plans.append(plan_dict)
            debt_summary = self.ledger.advertising_debt_summary(store_alias=store_alias)
            debt_store = next(
                (
                    item
                    for item in debt_summary.get("stores", [])
                    if str(item.get("storeAlias")) == store_alias
                ),
                {},
            )
            debt_outstanding = _decimal(
                debt_store.get("advertisingDebtOutstandingCny")
            )
            unverified_count = int(
                debt_store.get("advertisingFeeUnverifiedCount") or 0
            )
            debt_status = str(
                debt_store.get("advertisingDebtStatus")
                or ("PENDING_DATA" if unverified_count else "KNOWN")
            )
            debt_rows.append(
                {
                    "storeAlias": store_alias,
                    "historicalWithdrawalCny": _money(
                        self._historical_withdrawal_amount(store_alias)
                    ),
                    "advertisingDebtOutstandingCny": _money(debt_outstanding),
                    "advertisingPaymentReadbackCny": debt_store.get(
                        "advertisingPaymentReadbackCny", "0.00"
                    ),
                    "advertisingPaymentAppliedCny": debt_store.get(
                        "advertisingPaymentAppliedCny", "0.00"
                    ),
                    "advertisingPaymentUnappliedCny": debt_store.get(
                        "advertisingPaymentUnappliedCny", "0.00"
                    ),
                    "advertisingFeeUnverifiedCount": unverified_count,
                    "advertisingDebtStatus": debt_status,
                    "advertisingDebtAmountKnown": bool(
                        debt_store.get(
                            "advertisingDebtAmountKnown", unverified_count == 0
                        )
                    ),
                    "advertisingDebtUnknownAmountCny": debt_store.get(
                        "advertisingDebtUnknownAmountCny"
                    ),
                    "suggestedWithdrawalBeforeAdvertisingDebtCny": _money(
                        plan.suggested_withdrawal_cny
                    ),
                    "suggestedWithdrawalAfterAdvertisingDebtCny": _money(
                        max(_ZERO, plan.suggested_withdrawal_cny - debt_outstanding)
                    ),
                    "evidence": {
                        "withdrawalRecordIds": [
                            str(item.get("record_id"))
                            for item in debt_store.get("withdrawals", [])
                        ],
                        "advertisingPaymentIds": [
                            str(item.get("payment_id"))
                            for item in debt_store.get("payments", [])
                        ],
                    },
                }
            )
            self.ledger.record_funding_action(
                f"withdrawal-plan:{plan.fingerprint}",
                store_alias,
                action_type="withdrawal",
                requested_amount_cny=plan.suggested_withdrawal_cny,
                report_id=report_id,
                status="proposed",
                evidence=plan_dict,
            )
        output = {
            "schemaVersion": 1,
            "planType": "weekly_funding",
            "reportId": report_id,
            "reportEvidenceStatus": str(report["report_status"]),
            "allocationMode": (
                "SETTLED_ONLY_WITH_OPEN_ITEMS"
                if str(report["report_status"]) == "PENDING_DATA"
                else "FINALIZED_REPORT"
            ),
            "generatedAt": self._now().isoformat(),
            "withdrawalExecutionSupported": False,
            "stores": plans,
            "advertisingFundingActivation": activation_rows,
            "historicalCashEvents": self._historical_withdrawal_events(),
            # Kept outside each validated store funding plan so existing
            # advertising-deposit plan contracts remain backwards compatible.
            "advertisingDebt": debt_rows,
        }
        digest = hashlib.sha256(canonical_json(output).encode("utf-8")).hexdigest()[:16]
        path = self.output_root / "funds" / f"{report_id}-{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != output:
                raise FinanceServiceError(f"Funding plan artifact differs: {path}")
        else:
            path.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return {**output, "path": str(path)}

    def ads_deposit(
        self,
        *,
        plan_path: Path,
        approved_by: str,
        confirm_live: bool,
    ) -> dict[str, Any]:
        payload = validate_advertising_deposit_plan(
            json.loads(Path(plan_path).read_text(encoding="utf-8-sig"))
        )
        if payload["source"] != "balance":
            raise FinanceServiceError(
                "Advertising deposits must use WB balance (type=1); "
                "account, bonus and cashback funding are disabled"
            )
        if payload["approvedBy"] != str(approved_by).strip():
            raise FinanceServiceError("CLI approver must exactly match the immutable plan")
        if payload["store"] not in self.store_aliases:
            raise FinanceServiceError(f"Deposit plan store is not enabled: {payload['store']}")

        existing = self.ledger.query_funding_actions(
            store_alias=payload["store"],
            report_id=payload["reportId"],
            action_type="advertising_top_up",
        )
        matching = [
            item
            for item in existing
            if str(item["idempotency_key"]) == f"ads-deposit:{payload['fingerprint']}"
        ]
        if not confirm_live:
            self._require_current_deposit_report(payload["reportId"])
            self._require_governed_deposit_cap(
                payload,
                exclude_action_id=(str(matching[0]["action_id"]) if matching else None),
            )
            proposal_preflight = preflight_deposit_execution(
                payload,
                current_status="approved",
                now=self._now(),
            )
            if not proposal_preflight.allowed:
                raise FinanceServiceError(proposal_preflight.reason)
            if matching:
                action = matching[0]
                if str(action["lifecycle_status"]) != "proposed":
                    raise FinanceServiceError(
                        "This exact proposal already left proposed status; create a new plan"
                    )
                action_id = str(action["action_id"])
                idempotent = True
            else:
                action_id = self.ledger.record_funding_action(
                    f"ads-deposit:{payload['fingerprint']}",
                    payload["store"],
                    action_type="advertising_top_up",
                    requested_amount_cny=payload["amountCny"],
                    report_id=payload["reportId"],
                    status="proposed",
                    requested_at=payload["approvedAt"],
                    evidence=payload,
                )
                idempotent = False
            return {
                "status": "proposal_recorded",
                "live": False,
                "actionId": action_id,
                "idempotent": idempotent,
                "planFingerprint": payload["fingerprint"],
                "expiresAt": payload["expiresAt"],
                "automaticRetryAllowed": False,
                "nextStep": (
                    "Review the exact store, campaign, amount, currency, source balance, "
                    "policy cap, advertising reserve and expiry, then repeat with --confirm-live."
                ),
            }
        if not matching:
            raise FinanceServiceError(
                "A matching prior dry-run proposal is required before --confirm-live"
            )
        action = matching[0]
        if str(action["lifecycle_status"]) in {"proposed", "approved"}:
            self._require_current_deposit_report(payload["reportId"])
        owner = f"finance-{uuid.uuid4()}"
        lock_name = f"wb-advertising-funds:{payload['store']}"
        # The strict preflight performs four read calls, followed by one write
        # and one readback. Keep the shared money lock alive beyond the worst
        # supported request-timeout envelope so another writer cannot enter
        # while this operation is still resolving.
        if not self.ledger.acquire_lock(lock_name, owner, ttl_seconds=900, now=self._now()):
            raise FinanceServiceError("Another finance/advertising funding operation holds the store lock")
        try:
            action = next(
                item
                for item in self.ledger.query_funding_actions(
                    store_alias=payload["store"],
                    action_type="advertising_top_up",
                )
                if item["action_id"] == action["action_id"]
            )
            self._require_exact_proposal(action, payload)
            lifecycle_status = str(action["lifecycle_status"])
            if lifecycle_status in {"proposed", "approved"}:
                try:
                    self._require_governed_deposit_cap(
                        payload,
                        exclude_action_id=str(action["action_id"]),
                    )
                except Exception as exc:
                    self._cancel_pre_post_action(action, payload, str(exc))
                    raise
            try:
                credentials = self._promotion_credentials(payload["store"])
                client = WbClient(credentials.token)
            except Exception as exc:
                if lifecycle_status in {"proposed", "approved"}:
                    self._cancel_pre_post_action(action, payload, str(exc))
                raise
            if lifecycle_status in {"unknown", "executing", "executed", "succeeded"}:
                return self._resolve_deposit_readback_only(client, payload, action)
            check_status = "approved" if lifecycle_status == "proposed" else lifecycle_status
            preflight = preflight_deposit_execution(
                payload,
                current_status=check_status,
                readback_complete=lifecycle_status == "readback_confirmed",
                now=self._now(),
            )
            if not preflight.allowed:
                if lifecycle_status in {"proposed", "approved"}:
                    self._cancel_pre_post_action(action, payload, preflight.reason)
                raise FinanceServiceError(preflight.reason)

            try:
                live_preflight = self._live_advertising_deposit_preflight(
                    client,
                    payload,
                    exclude_action_id=str(action["action_id"]),
                )
                preflight = preflight_deposit_execution(
                    payload,
                    current_status="approved",
                    readback_complete=False,
                    now=self._now(),
                )
            except Exception as exc:
                self._cancel_pre_post_action(action, payload, str(exc))
                raise
            if not preflight.allowed:
                self._cancel_pre_post_action(action, payload, preflight.reason)
                raise FinanceServiceError(preflight.reason)
            if lifecycle_status == "proposed":
                self.ledger.update_funding_action_status(
                    action["action_id"],
                    "approved",
                    occurred_at=self._now(),
                    external_reference=payload["fingerprint"],
                    evidence={
                        "planFingerprint": payload["fingerprint"],
                        "approvedBy": approved_by,
                        "proposalActionId": action["action_id"],
                    },
                )
            self.ledger.update_funding_action_status(
                action["action_id"],
                "executing",
                evidence={
                    "planFingerprint": payload["fingerprint"],
                    "livePreflight": live_preflight,
                },
            )
            result = deposit_campaign_budget(
                client,
                campaign_id=int(payload["campaign"]),
                amount=int(Decimal(payload["amountCny"])),
                source=payload["source"],
                approved_by=approved_by,
                confirm_live=True,
            )
            final_status = result.state
            readback: dict[str, Any] | None = None
            if result.state in {"succeeded", "unknown"}:
                expected_total = (
                    Decimal(live_preflight["campaignBudgetBeforeCny"])
                    + Decimal(payload["amountCny"])
                ).quantize(_CENT)
                readback = self._campaign_budget_readback(
                    client,
                    payload,
                    expected_total=expected_total,
                )
                final_status = (
                    "readback_confirmed" if readback["matched"] else "unknown"
                )
            self.ledger.update_funding_action_status(
                action["action_id"],
                final_status,
                external_reference=payload["fingerprint"],
                evidence={
                    "httpStatus": result.status,
                    "requiresReadback": result.requires_readback,
                    "livePreflight": live_preflight,
                    "readback": readback,
                    "error": result.error,
                },
            )
            return {
                "actionId": action["action_id"],
                "status": final_status,
                "store": payload["store"],
                "campaign": payload["campaign"],
                "amountCny": payload["amountCny"],
                "readback": readback,
                "automaticRetryAllowed": False,
            }
        finally:
            self.ledger.release_lock(lock_name, owner)

    def ads_deposit_plan(
        self,
        *,
        funds_plan_path: Path,
        store_alias: str,
        campaign: int,
        amount_cny: Any,
        approved_by: str,
    ) -> dict[str, Any]:
        """Create one immutable balance-funded campaign plan after GET-only preflight."""

        source_path = self._resolve_path(Path(funds_plan_path))
        try:
            funding_payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FinanceServiceError(f"Funding plan is unreadable: {source_path}") from exc
        if not isinstance(funding_payload, Mapping):
            raise FinanceServiceError("Funding plan must contain an object")
        if (
            funding_payload.get("schemaVersion") != 1
            or funding_payload.get("planType") != "weekly_funding"
            or funding_payload.get("withdrawalExecutionSupported") is not False
        ):
            raise FinanceServiceError("Funding plan schema or type is invalid")

        report_id = str(funding_payload.get("reportId") or "").strip()
        if not report_id:
            raise FinanceServiceError("Funding plan reportId is required")
        self._require_current_deposit_report(report_id)

        store_name = str(store_alias or "").strip()
        if store_name not in self.store_aliases:
            raise FinanceServiceError(f"Deposit-plan store is not enabled: {store_name}")
        stores = funding_payload.get("stores")
        if not isinstance(stores, list):
            raise FinanceServiceError("Funding plan stores are missing")
        matching_stores = [
            item
            for item in stores
            if isinstance(item, Mapping) and str(item.get("store") or "") == store_name
        ]
        if len(matching_stores) != 1:
            raise FinanceServiceError(
                "Funding plan must contain exactly one entry for the requested store"
            )
        try:
            store_plan = validate_funding_plan(matching_stores[0])
        except Exception as exc:
            raise FinanceServiceError("Store funding plan failed validation") from exc
        if store_plan["reportId"] != report_id:
            raise FinanceServiceError("Store funding plan reportId differs from its parent")

        amount = _exact_positive_int(amount_cny)
        if amount is None:
            raise FinanceServiceError("Advertising deposit amount must be whole positive CNY")
        campaign_id = _exact_positive_int(campaign)
        if campaign_id is None:
            raise FinanceServiceError("Advertising campaign must be a positive integer")
        approver = str(approved_by or "").strip()
        if not approver:
            raise FinanceServiceError("approved_by is required")

        components = store_plan["components"]
        plan_accrual = _strict_non_negative_money(
            components["postActivationUnallocatedAdvertisingAccrualCny"],
            "funding-plan advertising accrual",
        )
        plan_locked = _strict_non_negative_money(
            components["approvedOrExecutingUnreadAdvertisingDepositsCny"],
            "funding-plan locked advertising deposits",
        )
        plan_available = max(_ZERO, plan_accrual - plan_locked).quantize(_CENT)
        governed_reserve = self._available_advertising_reserve_cny(store_name)
        if plan_available != governed_reserve:
            raise FinanceServiceError(
                "Funding plan advertising reserve no longer matches the current ledger; "
                "create a new funds plan"
            )
        if Decimal(amount) > governed_reserve:
            raise FinanceServiceError(
                "Advertising deposit amount exceeds the ledger-derived available reserve"
            )

        credentials = self._promotion_credentials(store_name)
        client = WbClient(credentials.token)
        live_evidence = self._live_advertising_funding_snapshot(
            client,
            store_alias=store_name,
            campaign_id=campaign_id,
            currency="CNY",
            source="balance",
        )
        source_balance = _strict_non_negative_money(
            live_evidence["sourceBalanceCny"],
            "live advertising balance",
        )
        if Decimal(amount) > source_balance:
            raise FinanceServiceError(
                "Advertising deposit amount exceeds the live WB balance source"
            )

        approved_at = self._now()
        evidence_at = _parse_datetime(live_evidence["sourceBalanceEvidenceAt"])
        if evidence_at is None:
            raise FinanceServiceError("Live advertising balance evidence has no timestamp")
        plan = build_advertising_deposit_plan(
            report_id=report_id,
            store=store_name,
            campaign=campaign_id,
            amount_cny=amount,
            source="balance",
            currency="CNY",
            source_balance_cny=source_balance,
            source_balance_evidence_at=evidence_at,
            policy_cap_cny=governed_reserve,
            available_advertising_reserve_cny=governed_reserve,
            approved_by=approver,
            approved_at=approved_at,
            expires_at=approved_at + _DEFAULT_DEPOSIT_PLAN_LIFETIME,
        )
        plan_payload = plan.as_dict()
        validate_advertising_deposit_plan(plan_payload)

        output_dir = self.output_root / "funds" / "advertising-deposit"
        stem = f"{report_id}-{store_name}-{campaign_id}-{plan.fingerprint[:16]}"
        plan_path = output_dir / f"{stem}.json"
        evidence_path = output_dir / f"{stem}.preflight.json"
        evidence_payload = {
            "schemaVersion": 1,
            "evidenceType": "advertising_deposit_plan_preflight",
            "generatedAt": approved_at.isoformat(),
            "networkWriteAttempted": False,
            "fundingPlanPath": str(source_path),
            "fundingPlanFingerprint": store_plan["fingerprint"],
            "planFingerprint": plan.fingerprint,
            "reportId": report_id,
            "store": store_name,
            "campaign": campaign_id,
            "amountCny": str(amount),
            "source": "balance",
            "availableAdvertisingReserveCny": _money(governed_reserve),
            "livePreflight": live_evidence,
        }
        self._write_immutable_json(plan_path, plan_payload)
        self._write_immutable_json(evidence_path, evidence_payload)
        return {
            "status": "immutable_plan_created",
            "live": False,
            "networkWriteAttempted": False,
            "store": store_name,
            "campaign": campaign_id,
            "amountCny": str(amount),
            "source": "balance",
            "planFingerprint": plan.fingerprint,
            "expiresAt": plan_payload["expiresAt"],
            "path": str(plan_path),
            "preflightPath": str(evidence_path),
            "nextStep": (
                "Review the exact immutable plan, then record it with finance "
                "ads-deposit without --confirm-live. A later exact authorization is "
                "required before --confirm-live."
            ),
        }

    @staticmethod
    def _write_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        canonical = json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
        if path.exists():
            if path.read_text(encoding="utf-8-sig") != canonical:
                raise FinanceServiceError(f"Immutable funding artifact differs: {path}")
            return
        path.write_text(canonical, encoding="utf-8")

    def _resolve_path(self, path: Path) -> Path:
        value = Path(path)
        return value if value.is_absolute() else self.repo_root / value

    def _now(self) -> datetime:
        value = self.now or datetime.now(SHANGHAI)
        return value if value.tzinfo is not None else value.replace(tzinfo=SHANGHAI)

    @staticmethod
    def _new_report_run_cache() -> dict[str, Any]:
        """Create an ephemeral cache shared by reports in one ``daily`` run.

        Raw platform evidence is immutable once a sync has completed, so the
        bootstrap/weekly/monthly views can safely reuse the same rows and
        order grouping.  The cache deliberately lives only for the current
        invocation; a later scheduled run must still capture fresh evidence
        and source-stability snapshots.
        """

        return {
            "rawEvidence": None,
            "grouping": None,
            "periodEvidence": {},
            "fullCalculations": {},
            "priorCalculations": {},
            "periodDeltas": {},
            "sourceBlockers": {},
            "stability": {},
            "adjustedPeriods": {},
            "hits": {},
        }

    @staticmethod
    def _period_cache_key(period: FinancePeriod) -> tuple[str, str, str, bool]:
        return (
            str(period.kind),
            period.start.isoformat(),
            period.end.isoformat(),
            bool(period.cumulative),
        )

    @staticmethod
    def _cache_hit(run_cache: dict[str, Any] | None, name: str) -> None:
        if run_cache is None:
            return
        hits = run_cache.setdefault("hits", {})
        hits[name] = int(hits.get(name, 0)) + 1

    @staticmethod
    def _cache_entry_counts(run_cache: Mapping[str, Any]) -> dict[str, int]:
        """Return small, JSON-safe cache diagnostics for the daily response."""

        return {
            "rawEvidence": 1 if run_cache.get("rawEvidence") is not None else 0,
            "grouping": 1 if run_cache.get("grouping") is not None else 0,
            "periodEvidence": len(run_cache.get("periodEvidence") or {}),
            "fullCalculations": len(run_cache.get("fullCalculations") or {}),
            "priorCalculations": len(run_cache.get("priorCalculations") or {}),
            "periodDeltas": len(run_cache.get("periodDeltas") or {}),
            "sourceBlockers": len(run_cache.get("sourceBlockers") or {}),
            "stability": len(run_cache.get("stability") or {}),
            "adjustedPeriods": len(run_cache.get("adjustedPeriods") or {}),
        }

    def _run_raw_evidence(
        self, run_cache: dict[str, Any]
    ) -> dict[str, list[dict[str, Any]]]:
        """Load each persisted raw source at most once for a report run."""

        cached = run_cache.get("rawEvidence")
        if cached is not None:
            self._cache_hit(run_cache, "rawEvidence")
            return cached
        raw = {
            table: self._payload_rows(table)
            for table in ("raw_finance", "raw_sales", "raw_returns", "raw_ad")
        }
        run_cache["rawEvidence"] = raw
        return raw

    def _select_cost(self, sku: str, created_at: str, *, order_completed_at=None):
        normalized = normalize_product_key(sku)
        selected = self.ledger.select_cost_version(
            normalized,
            created_at,
            order_completed_at=order_completed_at,
        )
        return {
            "costVersionId": selected.cost_version_uid,
            "internalCostVersionId": selected.cost_version_id,
            "purchaseCostCny": selected.purchase_cost_cny,
            "effectiveFrom": selected.effective_from,
            "effectiveTo": selected.effective_to,
        }

    def _load_period_evidence(
        self,
        period: FinancePeriod,
        *,
        _run_cache: dict[str, Any] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Load and filter persisted evidence for one report period.

        A scheduled ``daily`` run builds bootstrap, weekly, monthly and (when
        needed) historical-restatement views from the same post-sync snapshot.
        Reusing the decoded raw rows and order grouping within that invocation
        avoids repeated SQLite/JSON work while keeping the cache ephemeral;
        the next run always reads a fresh platform snapshot.
        """

        period_key = self._period_cache_key(period)
        if _run_cache is not None:
            cached_period = _run_cache.setdefault("periodEvidence", {}).get(period_key)
            if cached_period is not None:
                self._cache_hit(_run_cache, "periodEvidence")
                return deepcopy(cached_period)
            raw = self._run_raw_evidence(_run_cache)
        else:
            raw = {
                table: self._payload_rows(table)
                for table in ("raw_finance", "raw_sales", "raw_returns", "raw_ad")
            }

        finance = raw["raw_finance"]
        orders = raw["raw_sales"]
        returns = raw["raw_returns"]
        from .finance_calculations import build_order_groups

        if _run_cache is not None and _run_cache.get("grouping") is not None:
            grouping = _run_cache["grouping"]
            self._cache_hit(_run_cache, "grouping")
        else:
            combined = [
                *(dict(row, _source="finance") for row in finance),
                *(dict(row, _source="order") for row in orders),
                *(dict(row, _source="return") for row in returns),
            ]
            grouping = build_order_groups(combined, source="evidence")
            if _run_cache is not None:
                _run_cache["grouping"] = grouping
        selected_groups = []
        for group in grouping.groups:
            if any(_record_in_period(record.row, period) for record in group.records):
                selected_groups.append(group)
        output: dict[str, list[dict[str, Any]]] = {
            "finance": [],
            "order": [],
            "return": [],
            "advertising": [],
        }
        end_exclusive = _period_end_exclusive(period.end)
        for group in selected_groups:
            for record in group.records:
                if not _record_at_or_before(record.row, end_exclusive):
                    continue
                source = str(record.row.get("_source") or "order")
                row = {key: value for key, value in record.row.items() if key != "_source"}
                output[source].append(row)
        for row in raw["raw_ad"]:
            occurred = _parse_datetime(row.get("occurredAt") or row.get("updTime"))
            if occurred and period.start <= occurred.astimezone(MOSCOW).date() <= period.end:
                output["advertising"].append(row)
        if _run_cache is not None:
            _run_cache.setdefault("periodEvidence", {})[period_key] = deepcopy(output)
        return output

    def _payload_rows(self, table: str) -> list[dict[str, Any]]:
        if table not in {"raw_finance", "raw_sales", "raw_returns", "raw_ad"}:
            raise FinanceServiceError(f"Unsupported evidence table: {table}")
        rows = self.ledger.connection.execute(
            f"SELECT * FROM {table} ORDER BY store_alias"
        ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload.setdefault("storeAlias", row["store_alias"])
            if table == "raw_finance":
                if row["buyer_paid_cny"] is not None:
                    payload["buyerPaidCny"] = row["buyer_paid_cny"]
                if row["for_pay_cny"] is not None:
                    payload["forPayCny"] = row["for_pay_cny"]
                if row["commission_cny"] is not None:
                    payload["commissionCny"] = row["commission_cny"]
                if row["acquiring_fee_cny"] is not None:
                    payload["paymentFeeCny"] = row["acquiring_fee_cny"]
                if payload.get("paidStorage") not in (None, ""):
                    payload["storageCny"] = payload["paidStorage"]
                if payload.get("paidAcceptance") not in (None, ""):
                    payload["acceptanceFeeCny"] = payload["paidAcceptance"]
                if payload.get("additionalPayment") not in (None, ""):
                    payload["otherPlatformFeeCny"] = str(
                        -_decimal(payload["additionalPayment"])
                    )
            result.append(payload)
        return result

    def _source_blockers(
        self,
        period: FinancePeriod,
        evidence: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> list[dict[str, str]]:
        blockers: list[dict[str, str]] = []
        end = normalize_iso(_period_end_inclusive(period.end))
        for alias in self.store_aliases:
            store_start = self._store_reporting_start(alias, period.start)
            if store_start > period.end:
                continue
            start = normalize_iso(_period_start(store_start))
            for source in PRIMARY_SOURCES:
                if source == "seller_balance":
                    if self._latest_withdrawable_balance(alias) is None:
                        blockers.append(
                            {
                                "code": "MISSING_BALANCE_EVIDENCE",
                                "message": f"{alias} has no seller-balance evidence.",
                                "storeAlias": alias,
                                "source": source,
                            }
                        )
                    continue
                row = self.ledger.connection.execute(
                    """
                    SELECT status FROM sync_runs
                    WHERE store_alias=? AND source=? AND requested_from<=? AND requested_through>=?
                    ORDER BY completed_at DESC LIMIT 1
                    """,
                    (alias, source, start, end),
                ).fetchone()
                if row is None or row["status"] != "complete":
                    blockers.append(
                        {
                            "code": "MISSING_SOURCE_COVERAGE",
                            "message": f"{alias}/{source} is not complete for the report period.",
                            "storeAlias": alias,
                            "source": source,
                        }
                    )
            for gap in self.ledger.list_open_gaps(alias):
                if str(gap["gap_from"]) < end and str(gap["gap_through"]) >= start:
                    blockers.append(
                        {
                            "code": "OPEN_SYNC_GAP",
                            "message": f"{alias}/{gap['source']} has an unresolved collection gap.",
                            "storeAlias": alias,
                            "source": gap["source"],
                        }
                    )
        return blockers

    def _record_report_stability(
        self,
        period: FinancePeriod,
        evidence: Mapping[str, Sequence[Mapping[str, Any]]],
        blockers: Sequence[Mapping[str, Any]],
    ) -> bool:
        end = _period_end_exclusive(period.end)
        all_stable = True
        for alias in self.store_aliases:
            store_start = self._store_reporting_start(alias, period.start)
            if store_start > period.end:
                continue
            start = _period_start(store_start)
            for source, key in (
                ("finance_detailed", "finance"),
                ("advertising_costs", "advertising"),
            ):
                coverage_source = f"report:{period.kind}:{source}"
                rows = [row for row in evidence[key] if row.get("storeAlias") == alias]
                complete = not any(
                    str(item.get("storeAlias") or "") == alias
                    and str(item.get("source") or "") == source
                    and str(item.get("code") or "")
                    in {"MISSING_SOURCE_COVERAGE", "OPEN_SYNC_GAP"}
                    for item in blockers
                )
                digest = hashlib.sha256(canonical_json(rows).encode("utf-8")).hexdigest()
                self.ledger.record_source_coverage(
                    alias,
                    coverage_source,
                    start,
                    end,
                    coverage_status="complete" if complete else "missing",
                    row_count=len(rows),
                    snapshot_hash=digest if complete else None,
                    captured_at=self._now(),
                    metadata={"reportKind": period.kind},
                )
                if period.kind in {"weekly", "monthly"} and not self.ledger.has_consecutive_stable_snapshot(
                    alias,
                    coverage_source,
                    start,
                    end,
                    minimum_count=2,
                ):
                    all_stable = False
        return all_stable

    def _store_reporting_start(self, store_alias: str, requested: date) -> date:
        history_start = date.fromisoformat(
            str(self.registry.get("officialHistoryStart") or "2024-01-29")[:10]
        )
        store = next(
            (
                item
                for item in self.registry.get("stores", [])
                if isinstance(item, Mapping)
                and str(item.get("alias") or "") == store_alias
            ),
            None,
        )
        if store is not None and store.get("openedAt"):
            history_start = max(
                history_start,
                date.fromisoformat(str(store["openedAt"])[:10]),
            )
        return max(requested, history_start)

    def _period_delta_calculation(
        self,
        calculation: Mapping[str, Any],
        period: FinancePeriod,
        *,
        prior_calculation: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        result = deepcopy(dict(calculation))
        if period.cumulative or period.kind not in {"weekly", "monthly"}:
            return result

        prior_by_order = {
            (str(value.get("storeAlias") or ""), str(value.get("orderKey") or "")): dict(value)
            for value in (prior_calculation or {}).get("orders") or []
        }
        orders: list[dict[str, Any]] = []
        for value in result.get("orders") or []:
            current = dict(value)
            prior = prior_by_order.get(
                (
                    str(current.get("storeAlias") or ""),
                    str(current.get("orderKey") or ""),
                )
            )
            if prior is None:
                current["periodRecognition"] = "INITIAL"
                orders.append(current)
                continue
            delta = _order_period_delta(current, prior)
            # An unchanged historical order is audit evidence, not a new
            # confirmed order in this period.  Keep real monetary/quantity
            # corrections and incomplete evidence visible.
            if not _is_unchanged_period_correction(delta):
                orders.append(delta)

        issues = [dict(value) for value in result.get("issues") or []]
        aliases = sorted(
            {str(order.get("storeAlias") or "") for order in orders}
            | {
                str(issue.get("storeAlias") or "")
                for issue in issues
                if issue.get("storeAlias")
            }
        )
        stores = [
            _summarize_order_rows(
                alias,
                [order for order in orders if order.get("storeAlias") == alias],
                [issue for issue in issues if issue.get("storeAlias") == alias],
            )
            for alias in aliases
            if alias
        ]
        result["orders"] = orders
        result["stores"] = stores
        result["totals"] = _summarize_order_rows("ALL", orders, issues)
        return result

    def _prior_period_calculation(
        self,
        evidence: Mapping[str, Sequence[Mapping[str, Any]]],
        period: FinancePeriod,
        *,
        _run_cache: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if period.cumulative or period.kind not in {"weekly", "monthly"}:
            return None
        period_key = self._period_cache_key(period)
        if _run_cache is not None:
            cached = _run_cache.setdefault("priorCalculations", {}).get(period_key)
            if cached is not None:
                self._cache_hit(_run_cache, "priorCalculation")
                return deepcopy(cached)
        boundary = _period_start(period.start)
        prior = calculate_financial_report(
            order_rows=[
                row for row in evidence["order"] if _record_at_or_before(row, boundary)
            ],
            finance_rows=[
                row for row in evidence["finance"] if _record_at_or_before(row, boundary)
            ],
            return_rows=[
                row for row in evidence["return"] if _record_at_or_before(row, boundary)
            ],
            advertising_events=[],
            cost_selector=self._select_cost,
            policy=self.policy_timeline,
        )
        result = prior.to_dict()
        if _run_cache is not None:
            _run_cache.setdefault("priorCalculations", {})[period_key] = deepcopy(result)
        return result

    def _report_payload(
        self,
        calculation: Mapping[str, Any],
        period: FinancePeriod,
        *,
        status: str,
        blockers: Sequence[Mapping[str, Any]],
        previous_report: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        orders = []
        for value in calculation.get("orders") or []:
            row = dict(value)
            row["actualAdvertisingAllocatedCny"] = row.get("actualAdvertisingSpendCny")
            row["otherCostCny"] = row.get("otherPlatformCostCny")
            orders.append(row)
        stores = [dict(value) for value in calculation.get("stores") or []]
        present_aliases = {str(value.get("storeAlias") or "") for value in stores}
        for alias in self.store_aliases:
            if alias in present_aliases:
                continue
            if self._store_reporting_start(alias, period.start) > period.end:
                continue
            stores.append(_empty_store_summary(alias))
        stores.sort(key=lambda value: str(value.get("storeAlias") or ""))
        activation_rows = [
            self._funding_activation_info(alias, required=False)
            | {"storeAlias": alias}
            for alias in self.store_aliases
        ]
        raw_totals = dict(calculation.get("totals") or {})
        platform_components = [
            raw_totals.get(key)
            for key in (
                "wbCommissionCny",
                "paymentFeeCny",
                "forwardOperationsCny",
                "otherPlatformCostCny",
            )
        ]
        platform = (
            None
            if any(value is None for value in platform_components)
            else _money(sum((_decimal(value) for value in platform_components), _ZERO))
        )

        def optional_money(value: Any) -> str | None:
            return None if value is None else _money(value)

        totals = {
            **raw_totals,
            "salesAmountGCny": optional_money(raw_totals.get("salesAmountGCny")),
            "netTurnoverCny": optional_money(raw_totals.get("netTurnoverCny")),
            "actualCommissionCCny": optional_money(
                raw_totals.get("actualCommissionCCny")
            ),
            "acquiringFeeCny": optional_money(raw_totals.get("acquiringFeeCny")),
            "forPayCny": optional_money(raw_totals.get("forPayCny")),
            "accountIncrementCny": optional_money(
                raw_totals.get("accountIncrementCny")
            ),
            "purchaseCostCny": optional_money(raw_totals.get("purchaseCostCny")),
            "platformAndOperationsCostCny": platform,
            "managedOperationsCny": optional_money(
                raw_totals.get("managedOperationsCny")
            ),
            "combinedAdvertisingAndReturnPoolCny": optional_money(
                raw_totals.get("combinedAdvertisingAndReturnPoolCny")
            ),
            "returnLogisticsCny": optional_money(raw_totals.get("returnLogisticsCny")),
            "advertisingProvisionCny": optional_money(
                raw_totals.get("advertisingProvisionCny")
            ),
            "ruleWithdrawableCny": optional_money(
                raw_totals.get("ruleWithdrawableCny")
            ),
            "strictOwnerProfitCny": optional_money(
                raw_totals.get("strictOwnerProfitCny")
            ),
            "ownerProfitCny": optional_money(raw_totals.get("strictOwnerProfitCny")),
        }
        historical_events = self._historical_withdrawal_events()
        historical_total = sum(
            (_decimal(event.get("amountCny")) for event in historical_events),
            _ZERO,
        ).quantize(_CENT)
        totals["historicalWithdrawalCny"] = _money(historical_total)
        payment_fallback = (
            self.policy.raw.get("costs", {})
            .get("paymentAndWithdrawal", {})
            .get("fallbackRate")
        )
        fixed_operations_fallback = (
            self.policy.raw.get("costs", {})
            .get("fixedOperations", {})
            .get("fallbackPerCompletedUnitCny")
        )
        payload = {
            "reportId": "pending",
            "status": status,
            "kind": period.kind,
            "generatedAt": self._now().isoformat(),
            "period": period.as_dict(),
            "policy": {
                **dict(calculation.get("policy") or {}),
                "paymentFallbackRate": (
                    None if payment_fallback is None else str(payment_fallback)
                ),
                "fixedOperationsFallbackCny": (
                    None
                    if fixed_operations_fallback is None
                    else str(fixed_operations_fallback)
                ),
            },
            "orders": orders,
            "stores": stores,
            "totals": totals,
            "advertisingReconciliation": calculation.get("advertisingReconciliation") or {},
            "statisticsEvents": calculation.get("statisticsEvents") or [],
            "costVersions": self._used_cost_versions(orders),
            "historicalCashEvents": historical_events,
            "funding": self._report_funding_rows(stores),
            "advertisingFundingActivation": [
                {
                    "storeAlias": row["storeAlias"],
                    "mode": row.get("mode"),
                    "status": row.get("status"),
                    "at": row.get("atIso"),
                    "source": row.get("source"),
                    "message": row.get("message"),
                }
                for row in activation_rows
            ],
            "blockers": list(blockers),
            "issues": list(calculation.get("issues") or []),
        }
        if previous_report is not None:
            payload["previousReport"] = _build_previous_report_bridge(
                previous_report,
                current_orders=orders,
                current_totals=totals,
                current_policy=payload["policy"],
            )
        return payload

    def _persist_order_profits(self, orders: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], str]:
        persisted: dict[tuple[str, str], str] = {}
        for order in orders:
            if order.get("evidenceStatus") != "COMPLETE" or order.get("ownerProfitCny") is None:
                continue
            selected = self.ledger.select_cost_version(
                normalize_product_key(order["sku"]), order["orderCreatedAt"]
            )
            version = self.ledger.upsert_order_profit(
                order["storeAlias"],
                order["orderKey"],
                order_created_at=order["orderCreatedAt"],
                completed_at=order.get("completedAt"),
                sku=normalize_product_key(order["sku"]),
                cost_version_id=selected.cost_version_id,
                policy_id=order.get("financialReportingPolicyVersionId")
                or self.policy.reporting_policy_id,
                revenue_cny=order["netTurnoverCny"],
                purchase_cost_cny=order["purchaseCostCny"],
                commission_cny=order["wbCommissionCny"],
                managed_operations_cny=order["managedOperationsCny"],
                combined_pool_cny=order["combinedPoolCny"],
                actual_return_logistics_cny=order["returnLogisticsCny"],
                actual_ad_spend_reconciliation_cny=order.get("actualAdvertisingSpendCny"),
                advertising_provision_cny=order["advertisingProvisionCny"],
                excess_return_loss_cny=order["excessReturnLossCny"],
                forward_logistics_cny=order.get("forwardLogisticsCny") or 0,
                net_quantity=order["netQuantity"],
                order_result=order["outcome"],
                payment_cny=order["paymentFeeCny"],
                fixed_operations_cny=order["forwardOperationsUsedCny"],
                other_cost_cny=_decimal(order.get("storageAndOperationsCny"))
                + _decimal(order.get("otherPlatformCostCny")),
                owner_profit_cny=order["ownerProfitCny"],
                calculation_status=order["evidenceStatus"],
                evidence={
                    "evidenceIds": order.get("evidenceIds") or [],
                    "componentSources": order.get("componentSources") or {},
                    "pricingPolicyId": order.get("sellingPricePolicyVersionId")
                    or self.policy.pricing_policy_id,
                    "financialReportingPolicyId": order.get(
                        "financialReportingPolicyVersionId"
                    )
                    or self.policy.reporting_policy_id,
                    "fullOrderSnapshot": dict(order),
                },
            )
            persisted[(str(order["storeAlias"]), str(order["orderKey"]))] = version
        return persisted

    def _used_cost_versions(self, orders: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        identifiers = {str(row.get("costVersionId")) for row in orders if row.get("costVersionId")}
        if not identifiers:
            return []
        rows = self.ledger.connection.execute(
            f"SELECT * FROM cost_versions WHERE cost_version_uid IN ({', '.join('?' for _ in identifiers)})",
            sorted(identifiers),
        ).fetchall()
        return [
            {
                "costVersionId": row["cost_version_uid"],
                "productKey": row["sku"],
                "costRub": row["purchase_cost_rub"],
                "sourceCurrency": row["source_currency"],
                "exchangeRateRubPerCny": row["exchange_rate_rub_per_cny"],
                "costCny": row["purchase_cost_cny"],
                "effectiveFrom": row["effective_from"],
                "effectiveTo": row["effective_to"],
                "sourceQuote": row["quote_path"],
                "sourceSheet": row["quote_sheet"],
                "sourceRow": row["quote_row"],
            }
            for row in rows
        ]

    def _report_funding_rows(self, stores: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for store in stores:
            alias = str(store["storeAlias"])
            activation = self._funding_activation_info(alias, required=False)
            historical_withdrawal = self._historical_withdrawal_amount(alias)
            balance = self._latest_withdrawable_balance(alias)
            debt_summary = self.ledger.advertising_debt_summary(store_alias=alias)
            debt_store = next(
                (
                    item
                    for item in debt_summary.get("stores", [])
                    if str(item.get("storeAlias")) == alias
                ),
                {},
            )
            debt_outstanding = _decimal(
                debt_store.get("advertisingDebtOutstandingCny")
            )
            unverified_count = int(
                debt_store.get("advertisingFeeUnverifiedCount") or 0
            )
            debt_status = str(
                debt_store.get("advertisingDebtStatus")
                or ("PENDING_DATA" if unverified_count else "KNOWN")
            )
            platform_withdrawable = (
                _decimal(balance.get("withdrawable_balance_cny"))
                if balance and balance.get("withdrawable_balance_cny") is not None
                else None
            )
            pending_campaign_allocation = _money(
                sum(
                    (
                        Decimal(row["requested_amount_cny"])
                        for row in self.ledger.query_funding_actions(
                            store_alias=alias,
                            action_type="advertising_top_up",
                            lifecycle_status=("approved", "executing", "unknown"),
                        )
                    ),
                    _ZERO,
                )
            )
            result.append(
                {
                    "storeAlias": alias,
                    "historicalWithdrawalCny": _money(historical_withdrawal),
                    "historicalWithdrawalEventCount": len(
                        self._historical_withdrawal_events(alias)
                    ),
                    "withdrawableBalanceCny": balance["withdrawable_balance_cny"] if balance else None,
                    "unallocatedAdvertisingReserveCny": store.get("advertisingProvisionCny"),
                    "actualAdvertisingSpendCny": store.get("actualAdvertisingSpendCny"),
                    "advertisingSettlementDeductionCny": store.get(
                        "advertisingSettlementDeductionCny"
                    ),
                    # The legacy key remains for artifact compatibility; both
                    # fields represent a campaign-budget allocation from WB
                    # settlement balance, not an external cash top-up.
                    "approvedPendingDepositCny": pending_campaign_allocation,
                    "approvedPendingCampaignAllocationCny": pending_campaign_allocation,
                    "otherLockedFundsCny": "0.00",
                    # Preserve historical withdrawals that explicitly failed
                    # to retain the ad fee.  Current settlement-balance
                    # withdrawals create no such liability; the legacy amount
                    # stays separate for backwards-compatible FIFO evidence.
                    "advertisingDebtOutstandingCny": _money(debt_outstanding),
                    "advertisingPaymentReadbackCny": debt_store.get(
                        "advertisingPaymentReadbackCny", "0.00"
                    ),
                    "advertisingPaymentAppliedCny": debt_store.get(
                        "advertisingPaymentAppliedCny", "0.00"
                    ),
                    "advertisingPaymentUnappliedCny": debt_store.get(
                        "advertisingPaymentUnappliedCny", "0.00"
                    ),
                    "advertisingFeeUnverifiedCount": int(
                        debt_store.get("advertisingFeeUnverifiedCount") or 0
                    ),
                    "advertisingDebtStatus": debt_status,
                    "advertisingDebtAmountKnown": bool(
                        debt_store.get(
                            "advertisingDebtAmountKnown", unverified_count == 0
                        )
                    ),
                    "advertisingDebtUnknownAmountCny": debt_store.get(
                        "advertisingDebtUnknownAmountCny"
                    ),
                    "withdrawableAfterAdvertisingDebtCny": (
                        _money(max(_ZERO, platform_withdrawable - debt_outstanding))
                        if platform_withdrawable is not None
                        else None
                    ),
                    "advertisingFundingStartAt": activation.get("atIso"),
                    "advertisingFundingStartSource": activation.get("source"),
                    "advertisingFundingStartStatus": activation.get("status"),
                    "status": "PLAN_ONLY",
                    "note": (
                        "提现需人工操作；正式计划以 funds-plan 为准。"
                        "后续提现与广告费不关联：活动预算直接使用 WB 结算余额，"
                        "平台按结算周期扣除。仅历史明确未扣/少扣记录保留欠费及 FIFO 对账。"
                        + (
                            f"当前有 {unverified_count} 条历史提现记录的广告费状态未核实。"
                            if unverified_count
                            else ""
                        )
                    ),
                }
            )
        return result

    def _latest_withdrawable_balance(self, store_alias: str) -> dict[str, Any] | None:
        row = self.ledger.connection.execute(
            """
            SELECT * FROM raw_balance
            WHERE store_alias=? AND withdrawable_balance_cny IS NOT NULL
            ORDER BY observed_at DESC, fetched_at DESC LIMIT 1
            """,
            (store_alias,),
        ).fetchone()
        return dict(row) if row else None

    def _require_fresh_balance_evidence(
        self,
        balance: Mapping[str, Any],
        *,
        store_alias: str,
    ) -> datetime:
        observed_at = _parse_datetime(balance.get("observed_at"))
        if observed_at is None:
            raise FinanceServiceError(
                f"Withdrawable-balance evidence timestamp is missing for {store_alias}"
            )
        _strict_non_negative_money(
            balance.get("withdrawable_balance_cny"),
            f"withdrawable balance for {store_alias}",
        )
        current = self._now().astimezone(timezone.utc)
        observed = observed_at.astimezone(timezone.utc)
        if observed - current > _MAX_FUTURE_EVIDENCE_SKEW:
            raise FinanceServiceError(
                f"Withdrawable-balance evidence is future-dated for {store_alias}"
            )
        if current - observed > _MAX_FUNDING_BALANCE_EVIDENCE_AGE:
            raise FinanceServiceError(
                f"Withdrawable-balance evidence is stale for {store_alias}; "
                "run finance daily again before creating a funds plan"
            )
        return observed_at

    def _require_current_deposit_report(self, report_id: str) -> dict[str, Any]:
        report = self.ledger.get_report(report_id)
        if report is None:
            raise FinanceServiceError(f"Unknown report in advertising deposit plan: {report_id}")
        if not self._report_is_allocation_ready(report):
            raise FinanceServiceError(
                "Advertising deposits require an allocation-ready weekly report"
            )
        latest = self._latest_weekly_allocation_report()
        if latest is None or str(latest["report_id"]) != str(report_id):
            raise FinanceServiceError(
                "Advertising deposit report is not the latest allocation-ready weekly report"
            )
        if str(report.get("policy_id")) != self.policy.reporting_policy_id:
            raise FinanceServiceError(
                "Advertising deposit report does not use the active financial policy"
            )
        return report

    def _require_governed_deposit_cap(
        self,
        payload: Mapping[str, Any],
        *,
        exclude_action_id: str | None = None,
    ) -> Decimal:
        if self.policy.advertising_deposit_cap_rule != ADVERTISING_DEPOSIT_CAP_RULE:
            raise FinanceServiceError("Active advertising-deposit cap rule is unsupported")
        governed = self._available_advertising_reserve_cny(
            str(payload["store"]),
            exclude_action_id=exclude_action_id,
        )
        frozen_reserve = _strict_non_negative_money(
            payload["availableAdvertisingReserveCny"],
            "frozen advertising reserve",
        )
        frozen_cap = _strict_non_negative_money(payload["policyCapCny"], "policy cap")
        if frozen_reserve != governed or frozen_cap != governed:
            raise FinanceServiceError(
                "Policy cap and advertising reserve must both equal the current "
                "ledger-derived available advertising reserve"
            )
        return governed

    def _require_exact_proposal(
        self,
        action: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        try:
            evidence = json.loads(str(action.get("evidence_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise FinanceServiceError("Dry-run proposal evidence is unreadable") from exc
        if evidence != dict(payload):
            raise FinanceServiceError(
                "Dry-run proposal evidence does not match the immutable live plan"
            )
        events = self.ledger.get_funding_action_events(str(action["action_id"]))
        if not any(str(event["lifecycle_status"]) == "proposed" for event in events):
            raise FinanceServiceError(
                "Funding action has no prior dry-run proposed ledger event"
            )

    def _available_advertising_reserve_cny(
        self,
        store_alias: str,
        *,
        exclude_action_id: str | None = None,
    ) -> Decimal:
        activation_info = self._funding_activation_info(store_alias, required=True)
        activation = activation_info["at"]
        reserve = sum(
            (
                max(_ZERO, accrual.amount_cny - accrual.allocated_cny)
                for accrual in self._advertising_accruals(
                    store_alias,
                    formal_activation_at=activation,
                )
                if not accrual.historical_bootstrap and accrual.accrued_at >= activation
            ),
            _ZERO,
        )
        for action in self.ledger.query_funding_actions(
            store_alias=store_alias,
            action_type="advertising_top_up",
        ):
            if str(action["action_id"]) == str(exclude_action_id or ""):
                continue
            if self._funding_action_locks_reserve(action):
                reserve -= _strict_non_negative_money(
                    action["requested_amount_cny"],
                    "locked advertising deposit",
                )
        return max(_ZERO, reserve).quantize(_CENT)

    def _funding_action_locks_reserve(self, action: Mapping[str, Any]) -> bool:
        status = str(action.get("lifecycle_status") or "").lower()
        if status not in _LOCKING_ADVERTISING_ACTION_STATUSES:
            return False
        if status != "approved":
            return True
        try:
            evidence = json.loads(str(action.get("evidence_json") or "{}"))
        except json.JSONDecodeError:
            return True
        expires_at = _parse_datetime(evidence.get("expiresAt"))
        if expires_at is None:
            return True
        return self._now().astimezone(timezone.utc) < expires_at.astimezone(timezone.utc)

    def _live_advertising_deposit_preflight(
        self,
        client: WbClient,
        payload: Mapping[str, Any],
        *,
        exclude_action_id: str,
    ) -> dict[str, Any]:
        live = self._live_advertising_funding_snapshot(
            client,
            store_alias=str(payload["store"]),
            campaign_id=int(payload["campaign"]),
            currency=str(payload["currency"]),
            source=str(payload["source"]),
        )
        current_source_balance = _strict_non_negative_money(
            live["sourceBalanceCny"],
            f"live advertising {payload['source']} balance",
        )
        frozen_source_balance = _strict_non_negative_money(
            payload["sourceBalanceCny"],
            "frozen source balance",
        )
        if current_source_balance != frozen_source_balance:
            raise FinanceServiceError(
                "Advertising source balance changed after approval; create a new plan"
            )

        amount = _strict_non_negative_money(payload["amountCny"], "deposit amount")
        policy_cap = _strict_non_negative_money(payload["policyCapCny"], "policy cap")
        if amount <= _ZERO or amount > policy_cap:
            raise FinanceServiceError("Deposit amount exceeds the approved policy cap")
        if amount > current_source_balance:
            raise FinanceServiceError("Live advertising source balance is insufficient")

        current_reserve = self._available_advertising_reserve_cny(
            str(payload["store"]),
            exclude_action_id=exclude_action_id,
        )
        frozen_reserve = _strict_non_negative_money(
            payload["availableAdvertisingReserveCny"],
            "frozen advertising reserve",
        )
        if current_reserve != frozen_reserve or policy_cap != current_reserve:
            raise FinanceServiceError(
                "Governed policy cap or advertising reserve changed after approval; "
                "create a new plan"
            )
        if amount > current_reserve:
            raise FinanceServiceError("Deposit amount exceeds the live advertising reserve")

        return {
            **live,
            "policyCapCny": _money(policy_cap),
            "availableAdvertisingReserveCny": _money(current_reserve),
            "permissionsConfirmed": True,
        }

    def _live_advertising_funding_snapshot(
        self,
        client: WbClient,
        *,
        store_alias: str,
        campaign_id: int,
        currency: str,
        source: str,
    ) -> dict[str, Any]:
        campaign_response = self._live_funding_get(
            client,
            f"{ADVERTISING_BASE_URL}/api/advert/v2/adverts?ids={campaign_id}",
            "campaign ownership/status",
        )
        campaign_data = campaign_response.data
        status = _campaign_status_from_known_shapes(campaign_data, campaign_id)
        if status not in _FUNDABLE_CAMPAIGN_STATUSES:
            raise FinanceServiceError(
                f"Campaign status {status} is not eligible for a live budget deposit"
            )

        budget_response = self._live_funding_get(
            client,
            f"{ADVERTISING_BASE_URL}/adv/v1/budget?id={campaign_id}",
            "campaign budget permission",
        )
        if not isinstance(budget_response.data, Mapping):
            raise FinanceServiceError(
                "WB campaign budget preflight returned an unknown shape"
            )
        budget_before = _strict_non_negative_money(
            budget_response.data.get("total"),
            "campaign budget total",
        )

        store = next(
            (
                item
                for item in self.registry["stores"]
                if item.get("alias") == store_alias and item.get("enabled", True)
            ),
            None,
        )
        if store is None:
            raise FinanceServiceError(f"Advertising store is not enabled: {store_alias}")
        store_currency = str(
            store.get("currency") or self.registry.get("reportingCurrency") or ""
        ).strip().upper()
        if store_currency != currency:
            raise FinanceServiceError(
                "Deposit-plan currency no longer matches the enabled store currency"
            )
        seller_balance_response = self._live_funding_get(
            client,
            SELLER_BALANCE_URL,
            "same-store seller-balance currency",
        )
        seller_balance_data = seller_balance_response.data
        if not isinstance(seller_balance_data, Mapping):
            raise FinanceServiceError(
                "WB seller-balance currency preflight returned an unknown shape"
            )
        live_currency = str(seller_balance_data.get("currency") or "").strip().upper()
        if not live_currency or live_currency != currency:
            raise FinanceServiceError(
                "WB seller-balance currency is missing or differs from the approved plan"
            )

        balance_response = self._live_funding_get(
            client,
            f"{ADVERTISING_BASE_URL}/adv/v1/balance",
            "advertising source balance",
        )
        balance_data = balance_response.data
        if not isinstance(balance_data, Mapping):
            raise FinanceServiceError(
                "WB advertising balance preflight returned an unknown shape"
            )
        advertising_currency = str(balance_data.get("currency") or "").strip().upper()
        if advertising_currency and advertising_currency != currency:
            raise FinanceServiceError(
                "WB advertising-balance currency differs from the approved plan"
            )
        source_field = _ADVERTISING_SOURCE_FIELDS.get(source)
        if source_field is None or source_field not in balance_data:
            raise FinanceServiceError(
                "Approved advertising funding source is absent from the live balance"
            )
        current_source_balance = _strict_non_negative_money(
            balance_data[source_field],
            f"live advertising {source} balance",
        )
        evidence_at = self._now()
        return {
            "checkedAt": evidence_at.isoformat(timespec="seconds"),
            "currency": currency,
            "sellerBalanceCurrency": live_currency,
            "source": source,
            "sourceBalanceCny": _money(current_source_balance),
            "sourceBalanceEvidenceAt": evidence_at.isoformat(),
            "campaignStatus": status,
            "campaignBudgetBeforeCny": _money(budget_before),
            "permissionsConfirmed": True,
        }

    @staticmethod
    def _live_funding_get(client: WbClient, url: str, label: str):
        try:
            response = client.request("GET", url, retries=0)
        except Exception as exc:
            raise FinanceServiceError(
                f"WB {label} preflight could not be read; live deposit is blocked"
            ) from exc
        if not response.ok:
            raise FinanceServiceError(
                f"WB {label} preflight failed with HTTP {response.status}; "
                "live deposit is blocked"
            )
        return response

    def _cancel_pre_post_action(
        self,
        action: Mapping[str, Any],
        payload: Mapping[str, Any],
        reason: str,
    ) -> None:
        self.ledger.update_funding_action_status(
            str(action["action_id"]),
            "cancelled",
            occurred_at=self._now(),
            external_reference=str(payload["fingerprint"]),
            evidence={
                "phase": "pre_post",
                "planFingerprint": payload["fingerprint"],
                "reason": str(reason),
                "postAttempted": False,
            },
        )

    def _campaign_budget_readback(
        self,
        client: WbClient,
        payload: Mapping[str, Any],
        *,
        expected_total: Decimal,
    ) -> dict[str, Any]:
        response = None
        try:
            response = client.request(
                "GET",
                f"{ADVERTISING_BASE_URL}/adv/v1/budget?id={int(payload['campaign'])}",
                retries=0,
            )
            observed_total = (
                _strict_non_negative_money(
                    response.data.get("total")
                    if response.ok and isinstance(response.data, Mapping)
                    else None,
                    "campaign budget readback total",
                )
                if response.ok
                else None
            )
            matched = observed_total == expected_total
            return {
                "status": response.status,
                "data": response.data,
                "expectedTotalCny": _money(expected_total),
                "observedTotalCny": (
                    _money(observed_total) if observed_total is not None else None
                ),
                "matched": matched,
            }
        except Exception as exc:
            return {
                "status": getattr(response, "status", None),
                "data": getattr(response, "data", None),
                "expectedTotalCny": _money(expected_total),
                "observedTotalCny": None,
                "matched": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _resolve_deposit_readback_only(
        self,
        client: WbClient,
        payload: Mapping[str, Any],
        action: Mapping[str, Any],
    ) -> dict[str, Any]:
        expected_total = self._expected_deposit_total(action, payload)
        if expected_total is None:
            readback = {
                "status": None,
                "data": None,
                "expectedTotalCny": None,
                "observedTotalCny": None,
                "matched": False,
                "error": "No archived pre-POST campaign budget evidence",
            }
        else:
            readback = self._campaign_budget_readback(
                client,
                payload,
                expected_total=expected_total,
            )
        final_status = "readback_confirmed" if readback["matched"] else "unknown"
        self.ledger.update_funding_action_status(
            str(action["action_id"]),
            final_status,
            occurred_at=self._now(),
            external_reference=str(payload["fingerprint"]),
            evidence={
                "phase": "readback_only",
                "postAttempted": False,
                "readback": readback,
            },
        )
        return {
            "actionId": action["action_id"],
            "status": final_status,
            "store": payload["store"],
            "campaign": payload["campaign"],
            "amountCny": payload["amountCny"],
            "readback": readback,
            "readbackOnly": True,
            "automaticRetryAllowed": False,
        }

    def _expected_deposit_total(
        self,
        action: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> Decimal | None:
        for event in reversed(
            self.ledger.get_funding_action_events(str(action["action_id"]))
        ):
            try:
                evidence = json.loads(str(event.get("evidence_json") or "{}"))
            except json.JSONDecodeError:
                continue
            live_preflight = evidence.get("livePreflight")
            if not isinstance(live_preflight, Mapping):
                continue
            before = _strict_non_negative_money(
                live_preflight.get("campaignBudgetBeforeCny"),
                "archived campaign budget before deposit",
            )
            return (before + Decimal(str(payload["amountCny"]))).quantize(_CENT)
        return None

    def _advertising_accruals(
        self,
        store_alias: str,
        *,
        formal_activation_at: str | datetime | None = None,
    ) -> list[AdvertisingAccrual]:
        if formal_activation_at is None:
            activation_info = self._funding_activation_info(store_alias, required=True)
            activation = activation_info["at"]
        else:
            activation = _parse_datetime(formal_activation_at)
        if activation is None:
            raise FinanceServiceError(
                f"Store registry has no valid funding activation evidence for {store_alias}"
            )
        candidates: list[dict[str, Any]] = []
        for status in ("FINAL", "RESTATED", "PENDING_DATA"):
            for report in self.ledger.find_reports(
                report_type="weekly", report_status=status
            ):
                full = self.ledger.get_report(report["report_id"])
                if full and self._report_is_allocation_ready(full):
                    candidates.append(full)
        latest_reports: dict[str, dict[str, Any]] = {}
        for report in sorted(
            candidates,
            key=_report_version_sort_key,
            reverse=True,
        ):
            latest_reports.setdefault(report["report_key"], report)

        # Advertising reserve is a store/period value.  Keep one accrual state
        # per weekly report (rather than summing each order's independent
        # max(pool-return) value), and apply signed correction reports to the
        # oldest outstanding states.  Order snapshots remain in the report for
        # auditability; they are not mutated or used as independent reserves.
        accrual_states: list[dict[str, Any]] = []
        for report in sorted(
            latest_reports.values(),
            key=lambda value: (str(value["period_end"]), str(value["report_key"])),
        ):
            snapshots = []
            for order in report.get("orders") or []:
                if str(order.get("store_alias") or "") != store_alias:
                    continue
                snapshot = dict(order.get("snapshot") or {})
                if not _snapshot_is_cash_allocatable(snapshot):
                    continue
                # Legacy report snapshots did not always repeat storeAlias;
                # the enclosing report-order row is authoritative here.
                snapshot.setdefault("storeAlias", store_alias)
                snapshots.append(snapshot)
            summary = next(
                (
                    dict(value)
                    for value in report.get("stores") or []
                    if str(value.get("storeAlias") or "") == store_alias
                ),
                None,
            )
            # Older/synthetic report rows may not carry store summaries.  Build
            # the same aggregate from their settled order snapshots as a
            # compatibility fallback.
            if summary is None and snapshots:
                summary = _summarize_order_rows(store_alias, snapshots, [])
            if not summary:
                continue
            raw_amount = summary.get("advertisingProvisionCny")
            if raw_amount is None:
                continue
            amount = _decimal(raw_amount)
            if amount > _ZERO:
                accrual_states.append(
                    {
                        "accrualId": f"weekly-store:{store_alias}:{report['report_key']}",
                        "amount": amount.quantize(_CENT),
                        "accruedAt": self._advertising_report_accrued_at(
                            report,
                            snapshots,
                        ),
                    }
                )
            elif amount < _ZERO:
                self._consume_advertising_accrual_states(
                    accrual_states,
                    -amount,
                )
        accruals = [
            AdvertisingAccrual(
                accrual_id=str(state["accrualId"]),
                amount_cny=_decimal(state["amount"]),
                allocated_cny=_ZERO,
                accrued_at=state["accruedAt"],
                historical_bootstrap=False,
            )
            for state in accrual_states
            if _decimal(state["amount"]) > _ZERO and state["accruedAt"] is not None
        ]
        # A confirmed campaign deposit consumes the oldest eligible reserves.
        consumed = _ZERO
        for row in self.ledger.query_funding_actions(
            store_alias=store_alias,
            action_type="advertising_top_up",
            lifecycle_status="readback_confirmed",
        ):
            effective_at = self._confirmed_deposit_effective_at(row)
            if effective_at is not None and effective_at >= activation:
                consumed += Decimal(row["requested_amount_cny"])
        consumed = max(_ZERO, consumed)
        adjusted = []
        for accrual in sorted(accruals, key=lambda item: (item.accrued_at, item.accrual_id)):
            allocation = _ZERO
            if accrual.accrued_at >= activation:
                allocation = min(accrual.amount_cny, consumed)
                consumed -= allocation
            adjusted.append(
                AdvertisingAccrual(
                    accrual_id=accrual.accrual_id,
                    amount_cny=accrual.amount_cny,
                    allocated_cny=allocation,
                    accrued_at=accrual.accrued_at,
                    historical_bootstrap=accrual.historical_bootstrap,
                )
            )
        return adjusted

    def _advertising_report_accrued_at(
        self,
        report: Mapping[str, Any],
        snapshots: Sequence[Mapping[str, Any]],
    ) -> datetime:
        """Choose one financial timestamp for a store/weekly reserve row."""

        # A signed correction belongs to the week in which the correction was
        # recognised, not the original order's completion date.
        if any(
            str(snapshot.get("periodRecognition") or "")
            == "PRIOR_PERIOD_CORRECTION"
            for snapshot in snapshots
        ):
            period_end = _parse_datetime(report.get("period_end"))
            if period_end is not None:
                return period_end
        completed = [
            _parse_datetime(snapshot.get("completedAt"))
            for snapshot in snapshots
            if _parse_datetime(snapshot.get("completedAt")) is not None
        ]
        if completed:
            # Use the latest settled event represented by the aggregate.  This
            # is conservative for a period that spans the funding activation
            # boundary and remains traceable to the order snapshots.
            return max(completed)
        period_end = _parse_datetime(report.get("period_end"))
        if period_end is not None:
            return period_end
        generated = _parse_datetime(report.get("generated_at"))
        if generated is not None:
            return generated
        return self._now()

    @staticmethod
    def _consume_advertising_accrual_states(
        states: list[dict[str, Any]],
        amount: Decimal,
    ) -> None:
        """Apply a signed store-period correction FIFO to open reserves."""

        remaining = amount.quantize(_CENT)
        for state in sorted(
            states,
            key=lambda value: (value["accruedAt"], value["accrualId"]),
        ):
            if remaining <= _ZERO:
                break
            available = max(_ZERO, _decimal(state.get("amount")))
            consumed = min(available, remaining)
            state["amount"] = (available - consumed).quantize(_CENT)
            remaining = (remaining - consumed).quantize(_CENT)

    def _confirmed_deposit_effective_at(
        self,
        action: Mapping[str, Any],
    ) -> datetime | None:
        """Return the first irreversible-attempt time, never late readback time."""

        events = self.ledger.get_funding_action_events(str(action["action_id"]))
        for target_statuses in (
            {"executing"},
            {"executed", "succeeded", "unknown"},
            {"readback_confirmed"},
        ):
            for event in events:
                if str(event.get("lifecycle_status") or "") in target_statuses:
                    occurred = _parse_datetime(event.get("occurred_at"))
                    if occurred is not None:
                        return occurred
        return _parse_datetime(action.get("requested_at"))

    def _promotion_credentials(self, store_alias: str):
        raw = next(
            item
            for item in self.registry["stores"]
            if item.get("alias") == store_alias and item.get("enabled", True)
        )
        spec = StoreSpec(
            alias=store_alias,
            token_env=str(raw.get("promotionTokenEnv") or raw.get("tokenEnv")),
            token_file=(self.repo_root / str(raw["tokenFile"])).resolve(),
        )
        return resolve_store_credentials(spec, environ=os.environ)

    def _latest_final_report(
        self,
        report_key: str,
        *,
        snapshot_hash: str | None = None,
        require_complete_artifacts: bool = False,
    ) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for status in ("FINAL", "RESTATED"):
            for row in self.ledger.find_reports(
                report_key=report_key,
                report_status=status,
            ):
                full = self.ledger.get_report(str(row["report_id"]))
                if full is None:
                    continue
                if snapshot_hash is not None and str(
                    (full.get("metadata") or {}).get("calculationHash") or ""
                ) != str(snapshot_hash):
                    continue
                if require_complete_artifacts:
                    artifact_state, _ = self._report_artifact_state_for_report(full)
                    if artifact_state != "COMPLETE":
                        continue
                candidates.append(full)
        if not candidates:
            return None
        return max(candidates, key=_report_version_sort_key)

    def _refresh_finalized_reports(
        self,
        *,
        exclude_report_keys: set[str],
        affected_from: date,
        affected_through: date,
        refresh: bool = True,
        _run_cache: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        latest_by_key: dict[str, dict[str, Any]] = {}
        # Keep pending candidates in the refresh queue as well.  A late
        # correction can create a PENDING_DATA version after its original
        # period has left the rolling API window; dropping that candidate here
        # would leave it stranded forever unless someone manually re-ran the
        # exact historical period.
        for status in ("FINAL", "RESTATED", "PENDING_DATA"):
            for report in self.ledger.find_reports(report_status=status):
                key = str(report["report_key"])
                current = latest_by_key.get(key)
                if (
                    current is None
                    or _report_recency_key(report) > _report_recency_key(current)
                ):
                    latest_by_key[key] = report

        refreshed: list[dict[str, Any]] = []
        checked = 0
        for report in sorted(
            latest_by_key.values(),
            key=lambda value: (
                str(value["period_end"]),
                str(value["report_key"]),
            ),
        ):
            report_key = str(report["report_key"])
            if report_key in exclude_report_keys:
                continue
            full = self.ledger.get_report(str(report["report_id"]), include_orders=False)
            metadata = dict((full or {}).get("metadata") or {})
            raw_period = dict(metadata.get("period") or {})
            try:
                selected = FinancePeriod(
                    str(report["report_type"]),
                    date.fromisoformat(str(raw_period.get("from"))),
                    date.fromisoformat(str(raw_period.get("to"))),
                    cumulative=bool(raw_period.get("cumulative", False)),
                )
            except (TypeError, ValueError) as exc:
                raise FinanceServiceError(
                    f"Historical report has invalid period metadata: {report['report_id']}"
                ) from exc
            is_pending = str(report.get("report_status") or "") == "PENDING_DATA"
            if (
                not is_pending
                and (selected.end < affected_from or selected.start > affected_through)
            ):
                continue
            checked += 1
            # Pending candidates are always re-evaluated: a second stable
            # capture can close them even when the rolling source hash did not
            # change on this particular day. Finalized snapshots remain gated
            # by the report-relevant source-change signal.
            if not refresh and not is_pending:
                # Keep the candidate count visible for auditability while
                # avoiding a full report recalculation when no relevant source
                # snapshot changed.
                continue
            result = self.report(
                kind=selected.kind,
                period="existing",
                reason="Daily late-refund/platform-correction review",
                _selected_period=selected,
                _run_cache=_run_cache,
            )
            if not result.get("idempotent"):
                refreshed.append(result)
        return refreshed, checked

    def _latest_weekly_allocation_report(self) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for status in ("FINAL", "RESTATED", "PENDING_DATA"):
            candidates.extend(
                self.ledger.find_reports(
                    report_type="weekly",
                    report_status=status,
                )
            )
        full_candidates = [
            full
            for report in candidates
            if (full := self.ledger.get_report(report["report_id"])) is not None
            and self._report_is_allocation_ready(full)
        ]
        if not full_candidates:
            return None
        row = max(
            full_candidates,
            key=lambda value: (
                str(value["period_end"]),
                *_report_version_sort_key(value),
            ),
        )
        return row

    def _report_is_allocation_ready(self, report: Mapping[str, Any]) -> bool:
        if str(report.get("report_type")) != "weekly":
            return False
        status = str(report.get("report_status") or "")
        if status in {"FINAL", "RESTATED"}:
            return True
        if status != "PENDING_DATA":
            return False
        publication = self.periodic_standard.raw.get("publication", {})
        if not (
            publication.get("settledRowsRemainUsableForAllocation") is True
            and publication.get("unsettledRowsExcludedFromCashAllocation") is True
        ):
            return False
        return any(
            _snapshot_is_cash_allocatable(order.get("snapshot") or {})
            for order in report.get("orders") or []
        )

    def _adjust_cumulative_start(
        self,
        period: FinancePeriod,
        *,
        _run_cache: dict[str, Any] | None = None,
    ) -> FinancePeriod:
        if not period.cumulative:
            return period
        period_key = self._period_cache_key(period)
        if _run_cache is not None:
            cached = _run_cache.setdefault("adjustedPeriods", {}).get(period_key)
            if cached is not None:
                self._cache_hit(_run_cache, "adjustedPeriod")
                return cached
        earliest: date | None = None
        if _run_cache is not None:
            rows = self._run_raw_evidence(_run_cache)["raw_finance"]
            payloads = rows
        else:
            rows = self.ledger.connection.execute(
                "SELECT payload_json FROM raw_finance"
            ).fetchall()
            payloads = []
            for row in rows:
                try:
                    payloads.append(json.loads(row["payload_json"]))
                except (TypeError, json.JSONDecodeError):
                    continue
        for payload in payloads:
            parsed = _parse_datetime(payload.get("orderDt"))
            if parsed is None:
                continue
            day = parsed.astimezone(MOSCOW).date()
            earliest = day if earliest is None else min(earliest, day)
        if earliest is None or earliest >= period.start:
            adjusted = period
        else:
            adjusted = FinancePeriod(period.kind, earliest, period.end, cumulative=True)
        if _run_cache is not None:
            _run_cache.setdefault("adjustedPeriods", {})[period_key] = adjusted
        return adjusted

    def _next_report_version(self, report_key: str, *, status: str, snapshot_hash: str) -> str:
        reports = self.ledger.find_reports(report_key=report_key)
        if status == "PENDING_DATA":
            return f"candidate-{snapshot_hash[:16]}"
        finals = [row for row in reports if row["report_status"] in {"FINAL", "RESTATED"}]
        return f"v{len(finals) + 1}"

    def _report_artifact_paths(
        self,
        report_type: str,
        year: str,
        report_id: str,
    ) -> dict[str, Path]:
        output_dir = self._report_output_dir(str(report_type), str(year))
        prefix = str(report_id)
        return {
            "json": output_dir / f"{prefix}.json",
            "markdown": output_dir / f"{prefix}.md",
            "workbook": output_dir / f"{prefix}.xlsx",
            "verification": output_dir / f"{prefix}.verification.json",
            "inspection": output_dir / f"{prefix}.xlsx.inspect.ndjson",
            "previews": output_dir / ".previews" / prefix,
        }

    def _report_artifact_state(
        self,
        report_type: str,
        year: str,
        report_id: str,
    ) -> tuple[str, dict[str, str] | None]:
        """Return ABSENT, INCOMPLETE, or COMPLETE for a report publication."""

        paths = self._report_artifact_paths(report_type, year, report_id)
        required_keys = ("json", "markdown", "workbook", "verification")
        required = [paths[key] for key in required_keys]
        if not any(path.exists() for path in required):
            return "ABSENT", None
        if not all(path.is_file() and path.stat().st_size > 0 for path in required):
            return "INCOMPLETE", None
        try:
            payload = json.loads(paths["json"].read_text(encoding="utf-8"))
            verification = json.loads(
                paths["verification"].read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, UnicodeError):
            return "INCOMPLETE", None
        if not isinstance(payload, Mapping) or not isinstance(verification, Mapping):
            return "INCOMPLETE", None
        prefix = str(report_id)
        if type(payload.get("reportId")) is not str or payload.get("reportId") != prefix:
            return "INCOMPLETE", None
        required_verification_keys = {
            "status",
            "formulaErrorCount",
            "renderedSheets",
            "reportId",
            "workbookStatus",
            "checkFailures",
            "consistencyFailures",
        }
        if not required_verification_keys.issubset(verification):
            return "INCOMPLETE", None
        if verification.get("status") != "PASS":
            return "INCOMPLETE", None
        if (
            type(verification.get("reportId")) is not str
            or verification.get("reportId") != prefix
        ):
            return "INCOMPLETE", None
        if (
            type(verification.get("formulaErrorCount")) is not int
            or verification.get("formulaErrorCount") != 0
        ):
            return "INCOMPLETE", None
        for key in ("checkFailures", "consistencyFailures"):
            if not isinstance(verification.get(key), list) or verification.get(key):
                return "INCOMPLETE", None
        payload_status = str(payload.get("status") or "").upper()
        expected_workbook_status = {
            "FINAL": "FINAL",
            "RESTATED": "FINAL",
            "PENDING_DATA": "PENDING_DATA",
        }.get(payload_status)
        if verification.get("workbookStatus") != expected_workbook_status:
            return "INCOMPLETE", None
        rendered = verification.get("renderedSheets")
        preview_root = paths["previews"].resolve()
        if not isinstance(rendered, list) or len(rendered) != 7:
            return "INCOMPLETE", None
        rendered_paths: list[Path] = []
        for raw_path in rendered:
            if not isinstance(raw_path, str) or not raw_path.strip():
                return "INCOMPLETE", None
            rendered_path = Path(raw_path).resolve()
            if (
                rendered_path.parent != preview_root
                or rendered_path.suffix.lower() != ".png"
                or not rendered_path.is_file()
                or rendered_path.stat().st_size <= 0
            ):
                return "INCOMPLETE", None
            rendered_paths.append(rendered_path)
        if len(set(rendered_paths)) != 7:
            return "INCOMPLETE", None
        actual_previews = {
            path.resolve()
            for path in paths["previews"].glob("*.png")
            if path.is_file() and path.stat().st_size > 0
        }
        if actual_previews != set(rendered_paths):
            return "INCOMPLETE", None
        published = {key: str(paths[key]) for key in required_keys}
        published["previews"] = str(paths["previews"])
        return "COMPLETE", published

    def _report_artifact_state_for_report(
        self,
        report: Mapping[str, Any],
    ) -> tuple[str, dict[str, str] | None]:
        return self._report_artifact_state(
            str(report["report_type"]),
            str(report["period_start"])[:4],
            str(report["report_id"]),
        )

    def _existing_report_result(self, report: Mapping[str, Any]) -> dict[str, Any]:
        metadata = report.get("metadata") or {}
        selected = metadata.get("period") or {}
        _, artifacts = self._report_artifact_state_for_report(report)
        # A ``PENDING_DATA`` candidate is intentionally not published on
        # ordinary daily captures, so its deterministic paths may not exist
        # yet.  Likewise, a partial or failed publication is not a usable
        # artifact set even if its JSON file exists.  Only a complete set whose
        # verification is an exact PASS for this report ID may be exposed.
        if artifacts is not None:
            delivery_period = selected or {
                "from": str(report.get("period_start") or "")[:10],
                "to": str(report.get("period_end") or "")[:10],
            }
            artifacts["deliveryWorkbook"] = str(ensure_readable_workbook(
                {
                    "reportId": report["report_id"],
                    "kind": report["report_type"],
                    "period": delivery_period,
                },
                Path(artifacts["workbook"]),
            ))
        return {
            "reportId": report["report_id"],
            "reportKey": report["report_key"],
            "reportVersion": report["report_version"],
            "status": report["report_status"],
            "period": selected,
            "orderCount": len(report.get("orders") or []),
            "blockers": metadata.get("blockers") or [],
            "artifacts": artifacts,
            "idempotent": True,
        }

    def _write_report(self, payload: dict[str, Any], period: FinancePeriod) -> dict[str, str]:
        output = self._report_output_dir(period.kind, str(period.start.year))
        prefix = str(payload["reportId"])
        delivery_payload = {
            **payload,
            "period": payload.get("period") or period.as_dict(),
            "kind": payload.get("kind") or period.kind,
        }
        artifact_state, existing = self._report_artifact_state(
            period.kind,
            str(period.start.year),
            prefix,
        )
        if artifact_state == "COMPLETE" and existing is not None:
            existing["deliveryWorkbook"] = str(
                ensure_readable_workbook(delivery_payload, Path(existing["workbook"]))
            )
            return existing
        write_report_artifacts(
            payload,
            output,
            repair_incomplete=True,
        )
        rebuilt_state, rebuilt = self._report_artifact_state(
            period.kind,
            str(period.start.year),
            prefix,
        )
        if rebuilt_state != "COMPLETE" or rebuilt is None:
            raise FinanceServiceError(
                f"Report artifacts failed completeness verification: {prefix}"
            )
        rebuilt["deliveryWorkbook"] = str(
            ensure_readable_workbook(delivery_payload, Path(rebuilt["workbook"]))
        )
        return rebuilt

    def _previous_published_report_payload(
        self,
        period: FinancePeriod,
        *,
        exclude_report_id: str | None = None,
        preferred_report_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Load a compatible, verified baseline for the variance bridge.

        A RESTATED report follows its explicit ledger predecessor.  Otherwise
        the closest compatible published report is chosen.  Invalid newer
        candidates are skipped instead of suppressing an older valid baseline.
        """

        current_end = _parse_datetime(_period_end_exclusive(period.end))
        if current_end is None:
            return None
        preferred = str(preferred_report_id or "")
        candidates: list[
            tuple[datetime, datetime, tuple[int, str, str], str, Mapping[str, Any], Path]
        ] = []
        for report in self.ledger.find_reports(report_type=period.kind):
            report_id = str(report.get("report_id") or "")
            if not report_id or report_id == str(exclude_report_id or ""):
                continue
            if preferred and report_id != preferred:
                continue
            if str(report.get("report_status") or "") not in {
                "FINAL",
                "RESTATED",
                "PENDING_DATA",
            }:
                continue
            report_end = _parse_datetime(report.get("period_end"))
            generated_at = _parse_datetime(report.get("generated_at"))
            report_start = _parse_datetime(report.get("period_start"))
            if report_end is None or generated_at is None or report_start is None:
                continue
            if report_end > current_end:
                continue
            if period.cumulative and report_start.astimezone(MOSCOW).date() != period.start:
                continue
            artifact_state, artifact_paths = self._report_artifact_state(
                period.kind,
                str(report_start.year),
                report_id,
            )
            if artifact_state == "COMPLETE" and artifact_paths is not None:
                candidates.append(
                    (
                        report_end,
                        generated_at,
                        _report_version_sort_key(report),
                        report_id,
                        report,
                        Path(artifact_paths["json"]),
                    )
                )
        if not candidates:
            return None
        for _, _, _, report_id, report, artifact in sorted(
            candidates,
            key=lambda item: (item[0], item[1], item[2], item[3]),
            reverse=True,
        ):
            try:
                value = json.loads(artifact.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                continue
            if not _report_payload_matches_ledger_baseline(
                value,
                report=report,
                current_period=period,
            ):
                continue
            return value
        return None

    def _report_output_dir(self, kind: str, year: str) -> Path:
        return self.output_root / kind / year


def _report_payload_matches_ledger_baseline(
    value: Any,
    *,
    report: Mapping[str, Any],
    current_period: FinancePeriod,
) -> bool:
    """Check that an artifact can support a same-basis version bridge."""

    if not isinstance(value, Mapping):
        return False
    report_id = str(report.get("report_id") or "")
    if str(value.get("reportId") or "") != report_id:
        return False
    if str(value.get("kind") or "") != str(report.get("report_type") or ""):
        return False
    if str(value.get("status") or "") not in {
        "FINAL",
        "RESTATED",
        "PENDING_DATA",
    }:
        return False
    payload_period = value.get("period")
    if not isinstance(payload_period, Mapping):
        return False
    report_start = _parse_datetime(report.get("period_start"))
    report_end = _parse_datetime(report.get("period_end"))
    if report_start is None or report_end is None:
        return False
    expected_from = report_start.astimezone(MOSCOW).date().isoformat()
    expected_to = (
        report_end.astimezone(MOSCOW).date() - timedelta(days=1)
    ).isoformat()
    if str(payload_period.get("from") or "") != expected_from:
        return False
    if str(payload_period.get("to") or "") != expected_to:
        return False
    try:
        payload_to = date.fromisoformat(str(payload_period.get("to")))
    except (TypeError, ValueError):
        return False
    if payload_to > current_period.end:
        return False
    if current_period.cumulative:
        if str(payload_period.get("from") or "") != current_period.start.isoformat():
            return False
        if payload_period.get("cumulative") is not True:
            return False

    totals = value.get("totals")
    orders = value.get("orders")
    if not isinstance(totals, Mapping) or not isinstance(orders, list):
        return False
    if not all(isinstance(row, Mapping) for row in orders):
        return False
    required_total_fields = {metric for metric, _ in _REPORT_VARIANCE_METRICS}
    if not required_total_fields.issubset(totals):
        return False
    required_order_fields = required_total_fields - {"confirmedOrderCount"}
    for row in orders:
        if str(row.get("evidenceStatus") or "").upper() != "COMPLETE":
            continue
        if str(row.get("cashReconciliationStatus") or "").upper() != "PASS":
            return False
        if not required_order_fields.issubset(row):
            return False
    return True


def _record_in_period(row: Mapping[str, Any], period: FinancePeriod) -> bool:
    if str(row.get("_source")) == "order":
        if not any(row.get(key) not in (None, "") for key in ("saleID", "saleId")):
            return False
        fields = ("saleDt", "date", "lastChangeDate")
    elif str(row.get("_source")) == "return":
        fields = ("returnDate", "saleDt", "lastChangeDate", "date")
    else:
        fields = ("rrDate", "saleDt", "createDate")
    for field in fields:
        parsed = _parse_datetime(row.get(field))
        if parsed:
            # The first usable business timestamp is authoritative.  A report
            # published on Monday (createDate) must not move Sunday's rrDate
            # into the following week; lastChangeDate likewise is not a sale.
            return period.start <= parsed.astimezone(MOSCOW).date() <= period.end
    return False


def _record_at_or_before(row: Mapping[str, Any], end_exclusive: str) -> bool:
    boundary = datetime.fromisoformat(end_exclusive)
    source = str(row.get("_source") or "")
    fields = {
        "finance": ("rrDate", "saleDt", "createDate"),
        "return": ("returnDate", "saleDt", "lastChangeDate", "date"),
        "order": ("saleDt", "date", "lastChangeDate"),
    }.get(
        source,
        ("rrDate", "saleDt", "returnDate", "lastChangeDate", "date", "createDate"),
    )
    for field in fields:
        parsed = _parse_datetime(row.get(field))
        if parsed:
            return parsed < boundary
    return True


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(raw[:10]), time.min)
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=MOSCOW)


def _order_period_delta(
    current: Mapping[str, Any],
    prior: Mapping[str, Any],
) -> dict[str, Any]:
    delta = deepcopy(dict(current))
    for field in _DELTA_MONEY_FIELDS:
        current_value = current.get(field)
        prior_value = prior.get(field)
        if current_value is None and prior_value is None:
            delta[field] = None
        elif current_value is None:
            delta[field] = _money(-_decimal(prior_value))
        elif prior_value is None:
            delta[field] = _money(current_value)
        else:
            delta[field] = _money(
                _decimal(current_value) - _decimal(prior_value)
            )

    delta["netQuantity"] = _quantity(
        _decimal(current.get("netQuantity")) - _decimal(prior.get("netQuantity"))
    )
    delta["actualAdvertisingSpendCny"] = _money(
        current.get("actualAdvertisingSpendCny")
    )
    if delta.get("advertisingProvisionCny") is None:
        delta["advertisingReconciliationVarianceCny"] = None
    else:
        delta["advertisingReconciliationVarianceCny"] = _money(
            _decimal(delta["advertisingProvisionCny"])
            - _decimal(delta.get("actualAdvertisingSpendCny"))
        )
    current_template = dict(current.get("template") or {})
    prior_template = dict(prior.get("template") or {})
    delta["template"] = {
        key: (
            None
            if current_template.get(key) is None and prior_template.get(key) is None
            else _money(
                _decimal(current_template.get(key))
                - _decimal(prior_template.get(key))
            )
        )
        for key in sorted(set(current_template) | set(prior_template))
    }
    delta["periodRecognition"] = "PRIOR_PERIOD_CORRECTION"
    delta["priorOutcome"] = prior.get("currentOutcome") or prior.get("outcome")
    delta["currentOutcome"] = current.get("outcome")
    delta["outcome"] = "corrected"
    return delta


def _statistics_period_events(
    rows: Sequence[Mapping[str, Any]], period: FinancePeriod,
) -> list[dict[str, Any]]:
    """Preserve official RUB sales/refunds separately from CNY settlement."""

    events: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        sale_id = str(row.get("saleID") or row.get("saleId") or "").strip()
        if not (
            len(sale_id) > 1 and sale_id[0].upper() in {"S", "R"}
            and sale_id[1:].isdigit()
        ) or not _record_in_period(dict(row, _source="order"), period):
            continue
        alias = str(row.get("storeAlias") or "")
        occurred = _parse_datetime(row.get("saleDt") or row.get("date"))
        events[(alias, sale_id)] = {
            "storeAlias": alias,
            "saleId": sale_id,
            "eventType": "return" if sale_id[0].upper() == "R" else "sale",
            "occurredAt": occurred.isoformat() if occurred is not None else None,
            "srid": row.get("srid"),
            "sku": row.get("supplierArticle") or row.get("vendorCode"),
            "nmId": row.get("nmId") or row.get("nmID"),
            "currency": "RUB",
            "sellerPriceWithDiscountRub": row.get("priceWithDisc"),
            "buyerPaidRub": row.get("finishedPrice"),
            "sellerProceedsRub": row.get("forPay"),
            "source": "statistics-api /api/v1/supplier/sales",
            "settlementUse": "operational evidence only; not converted to CNY",
        }
    return sorted(events.values(), key=lambda row: (
        str(row.get("occurredAt") or ""), row["storeAlias"], row["saleId"]
    ))


def _with_advertising_reconciliation(
    calculation: Mapping[str, Any], reconciliation: Mapping[str, Any],
) -> dict[str, Any]:
    """Report store expenses even when no settled order can absorb them."""

    result = deepcopy(dict(calculation))
    stores = {str(row["storeAlias"]): dict(row) for row in result.get("stores") or []}
    by_store = reconciliation.get("byStore") or {}
    fields = {
        "actualAdvertisingSpendCny": "sellerCashSpendCny",
        "confirmedActualAdvertisingSpendCny": "confirmedSellerCashSpendCny",
        "platformPromoAdvertisingSpendCny": "platformPromoSpendCny",
        "unclassifiedAdvertisingSpendCny": "unclassifiedSpendCny",
    }
    for alias in set(stores) | set(by_store):
        row = stores.setdefault(alias, _empty_store_summary(alias))
        evidence = by_store.get(alias) or {}
        for target, source in fields.items():
            value = evidence.get(source, "0.00")
            row[target] = None if value is None else _money(value)
    result["stores"] = [stores[alias] for alias in sorted(stores)]
    totals = dict(result.get("totals") or {})
    for field in fields:
        values = [row[field] for row in stores.values()]
        totals[field] = (
            None if any(value is None for value in values)
            else _money(sum((_decimal(value) for value in values), _ZERO))
        )
    result["totals"] = totals
    result["advertisingReconciliation"] = {
        "byStore": deepcopy(by_store),
        "duplicateReplacements": deepcopy(reconciliation.get("duplicateReplacements") or []),
        "ambiguousDuplicates": deepcopy(reconciliation.get("ambiguousDuplicates") or []),
        "cashEventIds": [row.get("eventId") for row in reconciliation.get("cashEvents") or []],
        "platformPromoEventIds": [row.get("eventId") for row in reconciliation.get("platformPromoEvents") or []],
        "unknownEventIds": [row.get("eventId") for row in reconciliation.get("unknownEvents") or []],
    }
    return result


def _is_unchanged_period_correction(order: Mapping[str, Any]) -> bool:
    """Ignore fully reconciled zero deltas without concealing open evidence."""

    return (
        order.get("periodRecognition") == "PRIOR_PERIOD_CORRECTION"
        and order.get("evidenceStatus") == "COMPLETE"
        and order.get("cashReconciliationStatus") == "PASS"
        and order.get("strictOwnerProfitCny") is not None
        and _decimal(order.get("netQuantity")) == _ZERO
        and all(
            value is None or _decimal(value) == _ZERO
            for value in (order.get(field) for field in _DELTA_MONEY_FIELDS)
        )
    )


def _summarize_order_rows(
    store_alias: str,
    orders: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if store_alias == "ALL":
        return _summarize_all_order_rows(orders, issues)

    confirmed_orders = [
        order
        for order in orders
        if order.get("evidenceStatus") == "COMPLETE"
        and order.get("strictOwnerProfitCny") is not None
        and order.get("cashReconciliationStatus") == "PASS"
    ]

    def total_decimal(field: str) -> Decimal | None:
        values = [order.get(field) for order in confirmed_orders]
        if not values:
            return None if orders else _ZERO
        if any(value is None for value in values):
            return None
        return sum((_decimal(value) for value in values), _ZERO).quantize(_CENT)

    def total(field: str) -> str | None:
        value = total_decimal(field)
        return None if value is None else _money(value)

    other_values: list[Decimal | None] = []
    for order in confirmed_orders:
        storage = order.get("storageAndOperationsCny")
        other = order.get("otherPlatformCostCny")
        if storage is None or other is None:
            other_values.append(None)
        else:
            other_values.append(_decimal(storage) + _decimal(other))
    other_total_decimal = (
        None
        if (orders and not confirmed_orders) or any(value is None for value in other_values)
        else sum((value or _ZERO for value in other_values), _ZERO).quantize(_CENT)
    )
    if not orders:
        other_total_decimal = _ZERO

    if confirmed_orders:
        pool = aggregate_pool_for_orders(confirmed_orders)
    elif orders:
        pool = None
    else:
        pool = aggregate_pool_for_orders(())

    account_increment = total_decimal("accountIncrementCny")
    purchase_cost = total_decimal("purchaseCostCny")
    forward_operations = total_decimal("forwardOperationsUsedCny")
    managed_operations = total_decimal("managedOperationsCny")
    actual_storage = total_decimal("storageAndOperationsCny")
    actual_other = total_decimal("otherPlatformCostCny")
    if not confirmed_orders:
        strict_owner_profit = None if orders else _ZERO
    elif pool is None or any(
        value is None
        for value in (
            account_increment,
            purchase_cost,
            forward_operations,
            actual_storage,
            actual_other,
            managed_operations,
            pool.combined_pool_cny,
            pool.excess_return_loss_cny,
        )
    ):
        strict_owner_profit = None
    else:
        # Return logistics consumes the combined store/period pool once.
        strict_owner_profit = (
            account_increment
            - purchase_cost
            - forward_operations
            - actual_storage
            - actual_other
            - managed_operations
            - pool.combined_pool_cny
            - pool.excess_return_loss_cny
        ).quantize(_CENT)

    pool_values = (
        {
            "combinedPoolCny": None,
            "totalPolicyReserveCny": None,
            "returnRiskReserveCny": None,
            "promotionReserveCny": None,
            "returnLogisticsCny": None,
            "advertisingProvisionCny": None,
            "excessReturnLossCny": None,
        }
        if pool is None
        else {
            key: (None if value is None else _money(value))
            for key, value in pool.to_dict().items()
        }
    )
    unresolved = sum(order.get("evidenceStatus") == "BLOCKED" for order in orders)
    blocking_issues = sum(bool(issue.get("blocking")) for issue in issues)
    status = "PARTIAL" if unresolved or blocking_issues else "FINAL"
    return {
        "storeAlias": store_alias,
        "status": status,
        "orderCount": len(orders),
        "confirmedOrderCount": len(confirmed_orders),
        "openItemOrderCount": len(orders) - len(confirmed_orders),
        "completedCount": sum(order.get("outcome") == "completed" for order in orders),
        "returnedCount": sum(order.get("outcome") == "returned" for order in orders),
        "correctedCount": sum(order.get("outcome") == "corrected" for order in orders),
        "netQuantity": _quantity(
            sum((_decimal(order.get("netQuantity")) for order in confirmed_orders), _ZERO)
        ),
        "salesAmountGCny": total("salesAmountGCny"),
        "netTurnoverCny": total("netTurnoverCny"),
        "forPayCny": total("forPayCny"),
        "purchaseCostCny": total("purchaseCostCny"),
        "actualCommissionCCny": total("actualCommissionCCny"),
        "wbCommissionCny": total("wbCommissionCny"),
        "acquiringFeeCny": total("acquiringFeeCny"),
        "paymentFeeCny": total("paymentFeeCny"),
        "accountIncrementCny": total("accountIncrementCny"),
        "forwardOperationsCny": total("forwardOperationsUsedCny"),
        "managedOperationsCny": total("managedOperationsCny"),
        "combinedAdvertisingAndReturnPoolCny": pool_values["combinedPoolCny"],
        "combinedPoolCny": pool_values["combinedPoolCny"],
        "totalPolicyReserveCny": pool_values["totalPolicyReserveCny"],
        "returnRiskReserveCny": pool_values["returnRiskReserveCny"],
        "promotionReserveCny": pool_values["promotionReserveCny"],
        "returnLogisticsCny": pool_values["returnLogisticsCny"],
        "advertisingProvisionCny": pool_values["advertisingProvisionCny"],
        "excessReturnLossCny": pool_values["excessReturnLossCny"],
        "ruleWithdrawableCny": total("ruleWithdrawableCny"),
        "actualAdvertisingSpendCny": total("actualAdvertisingSpendCny"),
        "advertisingSettlementDeductionCny": total(
            "advertisingSettlementDeductionCny"
        ),
        "otherPlatformCostCny": (
            None if other_total_decimal is None else _money(other_total_decimal)
        ),
        "strictOwnerProfitCny": (
            None if strict_owner_profit is None else _money(strict_owner_profit)
        ),
        "ownerProfitCny": (
            None if strict_owner_profit is None else _money(strict_owner_profit)
        ),
        "unresolvedOrderCount": unresolved,
        "issueCount": blocking_issues,
    }


def _summarize_all_order_rows(
    orders: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Combine store summaries without cross-store pool netting."""

    aliases = sorted(
        {str(order.get("storeAlias") or "") for order in orders if order.get("storeAlias")}
        | {
            str(issue.get("storeAlias") or "")
            for issue in issues
            if issue.get("storeAlias")
        }
    )
    summaries = [
        _summarize_order_rows(
            alias,
            [order for order in orders if str(order.get("storeAlias") or "") == alias],
            [issue for issue in issues if str(issue.get("storeAlias") or "") == alias],
        )
        for alias in aliases
    ]
    global_blocking_count = sum(
        bool(issue.get("blocking")) and not issue.get("storeAlias")
        for issue in issues
    )
    if not summaries:
        empty = _empty_store_summary("ALL")
        if global_blocking_count:
            empty["status"] = "PARTIAL"
            empty["issueCount"] = global_blocking_count
        return empty

    count_fields = (
        "orderCount",
        "confirmedOrderCount",
        "openItemOrderCount",
        "completedCount",
        "returnedCount",
        "correctedCount",
        "unresolvedOrderCount",
        "issueCount",
    )
    money_fields = (
        "salesAmountGCny",
        "netTurnoverCny",
        "forPayCny",
        "purchaseCostCny",
        "actualCommissionCCny",
        "wbCommissionCny",
        "acquiringFeeCny",
        "paymentFeeCny",
        "accountIncrementCny",
        "forwardOperationsCny",
        "managedOperationsCny",
        "combinedAdvertisingAndReturnPoolCny",
        "combinedPoolCny",
        "totalPolicyReserveCny",
        "returnRiskReserveCny",
        "promotionReserveCny",
        "returnLogisticsCny",
        "advertisingProvisionCny",
        "excessReturnLossCny",
        "ruleWithdrawableCny",
        "actualAdvertisingSpendCny",
        "advertisingSettlementDeductionCny",
        "otherPlatformCostCny",
        "strictOwnerProfitCny",
        "ownerProfitCny",
    )
    result: dict[str, Any] = {
        "storeAlias": "ALL",
        "status": (
            "PARTIAL"
            if global_blocking_count
            or any(summary.get("status") == "PARTIAL" for summary in summaries)
            else "FINAL"
        ),
    }
    for field in count_fields:
        result[field] = sum(int(summary.get(field) or 0) for summary in summaries)
    result["issueCount"] += global_blocking_count
    for field in money_fields:
        values = [summary.get(field) for summary in summaries]
        result[field] = (
            None
            if any(value is None for value in values)
            else _money(sum((_decimal(value) for value in values), _ZERO))
        )
    result["netQuantity"] = _quantity(
        sum((_decimal(summary.get("netQuantity")) for summary in summaries), _ZERO)
    )
    return result


def _advertising_accrued_at(
    snapshot: Mapping[str, Any],
    report: Mapping[str, Any],
) -> datetime:
    fallback = _parse_datetime(report.get("period_end"))
    if snapshot.get("periodRecognition") == "PRIOR_PERIOD_CORRECTION":
        if fallback is None:
            raise FinanceServiceError(
                f"Report {report.get('report_id')} has no usable correction timestamp"
            )
        return fallback
    completed = _parse_datetime(snapshot.get("completedAt"))
    if completed is not None:
        return completed
    if fallback is None:
        raise FinanceServiceError(
            f"Report {report.get('report_id')} has no usable advertising accrual timestamp"
        )
    return fallback


def _snapshot_is_cash_allocatable(snapshot: Mapping[str, Any]) -> bool:
    """Return true only for settled rows that can safely drive cash allocation."""

    required = (
        "accountIncrementCny",
        "advertisingProvisionCny",
        "ruleWithdrawableCny",
    )
    return (
        snapshot.get("evidenceStatus") == "COMPLETE"
        and snapshot.get("cashReconciliationStatus") == "PASS"
        and all(snapshot.get(field) is not None for field in required)
    )


def _report_version_sort_key(report: Mapping[str, Any]) -> tuple[int, str, str]:
    raw_version = str(report.get("report_version") or "")
    version = (
        int(raw_version[1:])
        if raw_version.startswith("v") and raw_version[1:].isdigit()
        else -1
    )
    return (
        version,
        str(report.get("generated_at") or ""),
        str(report.get("report_id") or ""),
    )


def _report_recency_key(report: Mapping[str, Any]) -> tuple[str, int, int, str]:
    """Order report versions by creation time, including PENDING candidates."""

    status_rank = {
        # A pending candidate created in the same normalized second as an
        # earlier final row must not be hidden by the final row.  Pending
        # candidates are idempotent and harmless to recheck if a final row was
        # actually written first; losing the candidate could strand a late
        # correction indefinitely.
        "PENDING_DATA": 2,
        "FINAL": 1,
        "RESTATED": 3,
    }.get(str(report.get("report_status") or ""), -1)
    version_rank = _report_version_sort_key(report)[0]
    return (
        str(report.get("generated_at") or ""),
        status_rank,
        version_rank,
        str(report.get("report_id") or ""),
    )


def _empty_store_summary(store_alias: str) -> dict[str, Any]:
    return {
        "storeAlias": store_alias,
        "status": "FINAL",
        "orderCount": 0,
        "confirmedOrderCount": 0,
        "openItemOrderCount": 0,
        "completedCount": 0,
        "returnedCount": 0,
        "correctedCount": 0,
        "netQuantity": "0",
        "salesAmountGCny": "0.00",
        "netTurnoverCny": "0.00",
        "forPayCny": "0.00",
        "purchaseCostCny": "0.00",
        "actualCommissionCCny": "0.00",
        "wbCommissionCny": "0.00",
        "acquiringFeeCny": "0.00",
        "paymentFeeCny": "0.00",
        "accountIncrementCny": "0.00",
        "forwardOperationsCny": "0.00",
        "managedOperationsCny": "0.00",
        "combinedAdvertisingAndReturnPoolCny": "0.00",
        "combinedPoolCny": "0.00",
        "totalPolicyReserveCny": "0.00",
        "returnRiskReserveCny": "0.00",
        "promotionReserveCny": "0.00",
        "returnLogisticsCny": "0.00",
        "advertisingProvisionCny": "0.00",
        "excessReturnLossCny": "0.00",
        "ruleWithdrawableCny": "0.00",
        "actualAdvertisingSpendCny": "0.00",
        "advertisingSettlementDeductionCny": "0.00",
        "otherPlatformCostCny": "0.00",
        "strictOwnerProfitCny": "0.00",
        "ownerProfitCny": "0.00",
        "unresolvedOrderCount": 0,
        "issueCount": 0,
    }


def _period_start(day: date) -> str:
    return datetime.combine(day, time.min, tzinfo=MOSCOW).isoformat()


def _period_end_exclusive(day: date) -> str:
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=MOSCOW).isoformat()


def _period_end_inclusive(day: date) -> str:
    return datetime.combine(day, time.max, tzinfo=MOSCOW).isoformat()


def _decimal(value: Any) -> Decimal:
    return _ZERO if value in (None, "") else Decimal(str(value))


def _strict_non_negative_money(value: Any, label: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise FinanceServiceError(f"{label} is missing or invalid")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinanceServiceError(f"{label} is missing or invalid") from exc
    if not amount.is_finite() or amount < _ZERO:
        raise FinanceServiceError(f"{label} is missing or invalid")
    quantized = amount.quantize(_CENT)
    if quantized != amount:
        raise FinanceServiceError(f"{label} has unsupported sub-cent precision")
    return quantized


def _exact_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or str(parsed) != str(value).strip():
        return None
    return parsed


def _exact_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise FinanceServiceError(f"{label} is missing or invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise FinanceServiceError(f"{label} is missing or invalid") from exc
    if str(parsed) != str(value).strip():
        raise FinanceServiceError(f"{label} is missing or invalid")
    return parsed


def _campaign_status_from_known_shapes(data: Any, campaign_id: int) -> int:
    adverts = data.get("adverts") if isinstance(data, Mapping) else None
    if not isinstance(adverts, list) or not adverts:
        raise FinanceServiceError(
            "WB campaign preflight returned an unknown shape; live deposit is blocked"
        )
    matches: list[int] = []
    for item in adverts:
        if not isinstance(item, Mapping):
            raise FinanceServiceError("WB campaign preflight contains an invalid row")
        if "advert_list" in item:
            rows = item.get("advert_list")
            if not isinstance(rows, list):
                raise FinanceServiceError("WB grouped campaign list is invalid")
            group_status = _exact_int(item.get("status"), "campaign group status")
            for row in rows:
                if not isinstance(row, Mapping):
                    raise FinanceServiceError("WB grouped campaign row is invalid")
                if _exact_positive_int(row.get("advertId")) == campaign_id:
                    matches.append(group_status)
            continue
        if "id" in item:
            if _exact_positive_int(item.get("id")) == campaign_id:
                matches.append(
                    _exact_int(
                        item.get("status", item.get("advertStatus")),
                        "campaign status",
                    )
                )
            continue
        raise FinanceServiceError(
            "WB campaign preflight returned an unsupported campaign row shape"
        )
    if len(matches) != 1:
        raise FinanceServiceError(
            "Campaign is not uniquely visible to the approved store token; "
            "ownership/permission cannot be confirmed"
        )
    return matches[0]


def _money(value: Any) -> str:
    return format(_decimal(value).quantize(Decimal("0.01")), "f")


_REPORT_VARIANCE_METRICS = (
    ("confirmedOrderCount", "已确认订单数"),
    ("salesAmountGCny", "已确认营业额 G"),
    ("managedOperationsCny", "代运营费 2%"),
    ("combinedAdvertisingAndReturnPoolCny", "政策计提合计"),
    ("strictOwnerProfitCny", "已确认严格利润"),
)


def _build_previous_report_bridge(
    previous_report: Mapping[str, Any],
    *,
    current_orders: Sequence[Mapping[str, Any]],
    current_totals: Mapping[str, Any],
    current_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an auditable previous-to-current bridge without inventing values.

    Order-set movements, corrections on orders confirmed in both versions,
    and store/period aggregate recalculation are separate components.  Missing
    legacy fields propagate as ``None`` and mark the bridge ``PENDING_DATA``;
    they are never silently treated as zero.
    """

    raw_previous_orders = previous_report.get("orders")
    compatibility_issues: list[str] = []
    if not isinstance(raw_previous_orders, list):
        compatibility_issues.append("上一版缺少可核对的订单明细")
        raw_previous_orders = []
    previous_orders = [dict(value) for value in raw_previous_orders if isinstance(value, Mapping)]
    if len(previous_orders) != len(raw_previous_orders):
        compatibility_issues.append("上一版订单明细包含无效记录")
    normalized_current_orders = [
        dict(value) for value in current_orders if isinstance(value, Mapping)
    ]
    if len(normalized_current_orders) != len(current_orders):
        compatibility_issues.append("本版订单明细包含无效记录")
    previous_confirmed = {
        _report_order_identity(row): row
        for row in previous_orders
        if _report_order_is_confirmed(row)
    }
    current_confirmed = {
        _report_order_identity(row): dict(row)
        for row in normalized_current_orders
        if _report_order_is_confirmed(row)
    }
    if any(not key[1] for key in previous_confirmed):
        compatibility_issues.append("上一版存在缺少订单键的确认记录")
    if any(not key[1] for key in current_confirmed):
        compatibility_issues.append("本版存在缺少订单键的确认记录")
    added_keys = sorted(set(current_confirmed) - set(previous_confirmed))
    removed_keys = sorted(set(previous_confirmed) - set(current_confirmed))
    retained_keys = sorted(set(previous_confirmed) & set(current_confirmed))
    previous_totals = (
        dict(previous_report.get("totals") or {})
        if isinstance(previous_report.get("totals"), Mapping)
        else {}
    )
    if not previous_totals:
        compatibility_issues.append("上一版缺少可核对的汇总金额")
    previous_policy = (
        dict(previous_report.get("policy") or {})
        if isinstance(previous_report.get("policy"), Mapping)
        else {}
    )
    current_policy_dict = dict(current_policy or {})
    basis_changes: list[str] = []
    for key, label in (
        ("reportingPolicyId", "财务策略"),
        ("pricingPolicyId", "价格策略"),
    ):
        old_value = str(previous_policy.get(key) or "")
        new_value = str(current_policy_dict.get(key) or "")
        if old_value and new_value and old_value != new_value:
            basis_changes.append(f"{label}：{old_value} → {new_value}")
    rows: list[dict[str, Any]] = []
    for metric, label in _REPORT_VARIANCE_METRICS:
        if metric == "confirmedOrderCount":
            previous_value = _optional_report_decimal(
                previous_totals.get(metric)
            )
            current_value = _optional_report_decimal(current_totals.get(metric))
            if previous_value is None:
                previous_value = Decimal(len(previous_confirmed))
                compatibility_issues.append("上一版确认订单数由订单明细推导")
            if current_value is None:
                current_value = Decimal(len(current_confirmed))
                compatibility_issues.append("本版确认订单数由订单明细推导")
        else:
            previous_value = _report_total_decimal(previous_totals, metric)
            current_value = _report_total_decimal(current_totals, metric)
        newly_confirmed = _sum_report_order_metric(
            (current_confirmed[key] for key in added_keys), metric
        )
        removed_total = _sum_report_order_metric(
            (previous_confirmed[key] for key in removed_keys), metric
        )
        removed_from_confirmed = (
            None if removed_total is None else -removed_total
        )
        confirmed_corrections = _sum_retained_order_corrections(
            previous_confirmed,
            current_confirmed,
            retained_keys,
            metric,
        )
        components = (
            previous_value,
            current_value,
            newly_confirmed,
            removed_from_confirmed,
            confirmed_corrections,
        )
        if any(value is None for value in components):
            delta = None if previous_value is None or current_value is None else current_value - previous_value
            aggregate_recalculation = None
            row_status = "PENDING_DATA"
        else:
            delta = current_value - previous_value
            aggregate_recalculation = (
                current_value
                - previous_value
                - newly_confirmed
                - removed_from_confirmed
                - confirmed_corrections
            )
            row_status = "COMPLETE"

        # Sales G, the 2% fee, and counts are additive.  Reconcile both
        # published totals independently to their own confirmed-order sets;
        # a mismatch is a real data-quality issue, not an "other correction".
        reconciliation_issues: list[str] = []
        if metric in {
            "confirmedOrderCount",
            "salesAmountGCny",
            "managedOperationsCny",
        }:
            previous_order_total = _sum_report_order_metric(
                previous_confirmed.values(), metric
            )
            current_order_total = _sum_report_order_metric(
                current_confirmed.values(), metric
            )
            if not _report_values_equal(previous_value, previous_order_total, metric):
                reconciliation_issues.append("上一版汇总与确认订单明细不一致")
            if not _report_values_equal(current_value, current_order_total, metric):
                reconciliation_issues.append("本版汇总与确认订单明细不一致")
            if reconciliation_issues:
                row_status = "PENDING_DATA"
                compatibility_issues.extend(
                    f"{label}：{issue}" for issue in reconciliation_issues
                )
        reasons = []
        if added_keys:
            reasons.append(f"{len(added_keys)}笔新转确认")
        if removed_keys:
            reasons.append(f"{len(removed_keys)}笔移出确认")
        if confirmed_corrections not in (None, _ZERO):
            reasons.append("留存确认订单金额更正")
        if aggregate_recalculation not in (None, _ZERO):
            reasons.append(
                "店级政策计提/利润重算影响"
                if metric
                in {
                    "combinedAdvertisingAndReturnPoolCny",
                    "strictOwnerProfitCny",
                }
                else "汇总与订单变动存在未归因差额"
            )
        if reconciliation_issues:
            reasons.append("；".join(reconciliation_issues))
        if any(value is None for value in components):
            reasons.append("缺少同口径字段，精确归因待定")
        rows.append(
            {
                "metric": metric,
                "label": label,
                "status": row_status,
                "previous": _format_report_bridge_value(metric, previous_value),
                "newlyConfirmed": _format_report_bridge_value(
                    metric, newly_confirmed
                ),
                "removedFromConfirmed": _format_report_bridge_value(
                    metric, removed_from_confirmed
                ),
                "confirmedCorrections": _format_report_bridge_value(
                    metric, confirmed_corrections
                ),
                "aggregateRecalculation": _format_report_bridge_value(
                    metric, aggregate_recalculation
                ),
                "current": _format_report_bridge_value(metric, current_value),
                "delta": _format_report_bridge_value(metric, delta),
                "reason": "；".join(reasons) if reasons else "无变化",
                "reconciliationIssues": reconciliation_issues,
            }
        )
    compatibility_issues = list(dict.fromkeys(compatibility_issues))
    return {
        "schemaVersion": "finance-report-variance-v2",
        "status": (
            "PENDING_DATA"
            if compatibility_issues
            or any(row.get("status") != "COMPLETE" for row in rows)
            else "COMPLETE"
        ),
        "compatibilityIssues": compatibility_issues,
        "basisChanges": basis_changes,
        "reportId": previous_report.get("reportId"),
        "reportStatus": previous_report.get("status"),
        "generatedAt": previous_report.get("generatedAt"),
        "period": (
            dict(previous_report.get("period") or {})
            if isinstance(previous_report.get("period"), Mapping)
            else {}
        ),
        "policy": previous_policy,
        "totals": previous_totals,
        "newlyConfirmedOrders": [
            {"storeAlias": key[0], "orderKey": key[1]} for key in added_keys
        ],
        "removedFromConfirmedOrders": [
            {"storeAlias": key[0], "orderKey": key[1]} for key in removed_keys
        ],
        "varianceRows": rows,
    }


def _report_order_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    return (str(row.get("storeAlias") or ""), str(row.get("orderKey") or ""))


def _report_order_is_confirmed(row: Mapping[str, Any]) -> bool:
    cash_status = str(row.get("cashReconciliationStatus") or "").upper()
    return (
        str(row.get("evidenceStatus") or "").upper() == "COMPLETE"
        and cash_status == "PASS"
        and row.get("strictOwnerProfitCny", row.get("ownerProfitCny")) is not None
    )


def _report_metric_value(row: Mapping[str, Any], metric: str) -> Any:
    if metric == "confirmedOrderCount":
        return 1
    return row.get(metric)


def _optional_report_decimal(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _report_total_decimal(totals: Mapping[str, Any], metric: str) -> Decimal | None:
    return _optional_report_decimal(totals.get(metric))


def _sum_report_order_metric(
    rows: Iterable[Mapping[str, Any]], metric: str
) -> Decimal | None:
    total = _ZERO
    for row in rows:
        value = _optional_report_decimal(_report_metric_value(row, metric))
        if value is None:
            return None
        total += value
    return total if metric == "confirmedOrderCount" else total.quantize(_CENT)


def _sum_retained_order_corrections(
    previous_confirmed: Mapping[tuple[str, str], Mapping[str, Any]],
    current_confirmed: Mapping[tuple[str, str], Mapping[str, Any]],
    retained_keys: Sequence[tuple[str, str]],
    metric: str,
) -> Decimal | None:
    if metric == "confirmedOrderCount":
        return _ZERO
    total = _ZERO
    for key in retained_keys:
        previous_value = _optional_report_decimal(
            _report_metric_value(previous_confirmed[key], metric)
        )
        current_value = _optional_report_decimal(
            _report_metric_value(current_confirmed[key], metric)
        )
        if previous_value is None or current_value is None:
            return None
        total += current_value - previous_value
    return total.quantize(_CENT)


def _report_values_equal(
    left: Decimal | None,
    right: Decimal | None,
    metric: str,
) -> bool:
    if left is None or right is None:
        return left is right
    tolerance = _ZERO if metric == "confirmedOrderCount" else _CENT
    return abs(left - right) < tolerance if tolerance else left == right


def _format_report_bridge_value(metric: str, value: Decimal | None) -> int | str | None:
    if value is None:
        return None
    if metric == "confirmedOrderCount":
        return int(value)
    return _money(value)


def _quantity(value: Any) -> str:
    rendered = format(_decimal(value), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _dedupe_blockers(values: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected = {}
    for item in values:
        value = dict(item)
        key = (
            str(value.get("code") or "BLOCKER"),
            str(value.get("storeAlias") or ""),
            str(value.get("orderKey") or ""),
            str(value.get("message") or ""),
        )
        selected[key] = value
    return list(selected.values())
