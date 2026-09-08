from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def add_finance_parser(subparsers: Any) -> None:
    finance = subparsers.add_parser(
        "finance",
        help="Multi-store WB finance ledger, reports, and approved advertising funding",
    )
    commands = finance.add_subparsers(dest="finance_command", required=True)

    bootstrap = commands.add_parser(
        "bootstrap", help="Backfill every enabled store from opening/history start through yesterday"
    )
    _add_runtime_paths(bootstrap)
    bootstrap.add_argument(
        "--through", default="yesterday", help="Inclusive finance date, YYYY-MM-DD or yesterday"
    )
    bootstrap.add_argument(
        "--max-store-workers",
        type=int,
        default=2,
        help=(
            "Maximum concurrent store API readers. Stores sharing a token are "
            "always serialized; use 1 if the platform applies a global limit."
        ),
    )

    daily = commands.add_parser(
        "daily",
        help="Repair/sync through the cutoff, close due reports, and append the canonical workbook",
    )
    _add_runtime_paths(daily)
    daily.add_argument(
        "--through", default="yesterday", help="Inclusive finance date, YYYY-MM-DD or yesterday"
    )
    daily.add_argument(
        "--skip-workbook",
        action="store_true",
        help="Skip the canonical iterative workbook append (diagnostic/tests only)",
    )
    daily.add_argument(
        "--correction-window-days",
        type=int,
        default=None,
        help=(
            "Override the daily historical correction window.  By default, "
            "ordinary days use 30 days and Monday/month-start use 120 days."
        ),
    )
    daily.add_argument(
        "--max-store-workers",
        type=int,
        default=2,
        help=(
            "Maximum concurrent store API readers. Stores sharing a token are "
            "always serialized; use 1 if the platform applies a global limit."
        ),
    )

    account_refresh = commands.add_parser(
        "account-refresh",
        help="Refresh persistent running accounts from local evidence, without platform writes",
    )
    _add_runtime_paths(account_refresh)
    account_refresh.add_argument("--through", default="yesterday", help="Inclusive complete-day cutoff")

    account_report = commands.add_parser(
        "account-report",
        help="Create the fixed per-store and consolidated running-account report",
    )
    _add_runtime_paths(account_report)
    account_report.add_argument("--period", choices=("weekly", "monthly", "as-of"), required=True)
    account_report.add_argument("--from", dest="period_start", default=None, help="Optional inclusive start date")
    account_report.add_argument(
        "--through", default="yesterday",
        help="Inclusive cutoff; weekly must end Sunday, monthly must end on the final calendar day",
    )

    workbook_sync = commands.add_parser(
        "workbook-sync",
        help="Append immutable finance report artifacts to the configured canonical workbook",
    )
    _add_runtime_paths(workbook_sync, output=False)
    workbook_sync.add_argument(
        "--through", default="yesterday", help="Only include report periods through this date"
    )
    workbook_sync.add_argument(
        "--report",
        action="append",
        default=[],
        help="Specific report JSON artifact; repeatable. Omit to discover latest artifacts.",
    )
    workbook_sync.add_argument("--canonical", default=None, help="Override canonical XLSX path")
    workbook_sync.add_argument("--report-root", default=None, help="Override report artifact root")
    workbook_sync.add_argument(
        "--debt-json",
        default=None,
        help="Optional advertising-debt snapshot JSON (for manual withdrawal reconciliation)",
    )

    cost_import = commands.add_parser(
        "cost-import", help="Append effective-dated purchase costs from an approved quote"
    )
    _add_runtime_paths(cost_import, output=False)
    cost_import.add_argument("--quote", required=True, help="Approved JSON/CSV/TSV/XLSX quote")
    cost_import.add_argument(
        "--effective-from",
        required=True,
        help="Moscow-effective date/time; a date means 00:00 Europe/Moscow",
    )
    cost_import.add_argument("--approved-by", required=True, help="Person confirming this cost version")
    cost_import.add_argument(
        "--source-currency",
        choices=("RUB", "CNY"),
        required=True,
        help="Currency of the selected source-cost column",
    )
    cost_import.add_argument(
        "--rub-per-cny",
        help="Required for RUB source costs; optional cross-check when CNY and RUB evidence coexist",
    )

    report = commands.add_parser(
        "report", help="Create or restate a bootstrap, weekly, or monthly report"
    )
    _add_runtime_paths(report)
    report.add_argument("--kind", choices=("bootstrap", "weekly", "monthly"), required=True)
    report.add_argument(
        "--period",
        required=True,
        help="auto, YYYY-MM-DD:YYYY-MM-DD, ISO week (2026-W34), or month (2026-08)",
    )
    report.add_argument(
        "--reason", help="Required explanation when the same period is restated"
    )

    funds_refresh = commands.add_parser(
        "funds-refresh", help="Fetch financial sources and refresh daily funding without generating reports"
    )
    _add_runtime_paths(funds_refresh)
    funds_refresh.add_argument("--through", default="yesterday")
    funds_refresh.add_argument("--correction-window-days", type=int, default=30)

    funds = commands.add_parser(
        "funds-plan", help="Create a plan-only withdrawal and advertising-reserve calculation"
    )
    _add_runtime_paths(funds)
    funds.add_argument(
        "--report-id",
        default=None,
        help="Optional legacy weekly report ID; omit to use the confirmed daily account",
    )
    funds.add_argument("--through", default="yesterday", help="Complete financial day for the daily funding basis")

    deposit_plan = commands.add_parser(
        "ads-deposit-plan",
        help="Create one immutable balance-funded campaign-budget allocation plan from current read-only evidence",
    )
    _add_runtime_paths(deposit_plan)
    deposit_plan.add_argument(
        "--funds-plan",
        required=True,
        help="Exact daily or legacy weekly funding-plan JSON produced by finance funds-plan",
    )
    deposit_plan.add_argument(
        "--store",
        required=True,
        help="One enabled store alias; plans never span stores",
    )
    deposit_plan.add_argument(
        "--campaign",
        required=True,
        type=int,
        help="One WB advertising campaign ID",
    )
    deposit_plan.add_argument(
        "--amount-cny",
        required=True,
        type=int,
        help="Whole-CNY amount; funding source is forced to WB balance (type=1)",
    )
    deposit_plan.add_argument(
        "--approved-by",
        required=True,
        help="Approver for this exact store, campaign and amount",
    )
    deposit_plan.add_argument("--cash-only", action="store_true", help="Explicitly disable promotional participation in a new daily plan")

    deposit = commands.add_parser(
        "ads-deposit", help="Execute one explicitly approved balance-funded campaign-budget allocation plan"
    )
    _add_runtime_paths(deposit, output=False)
    deposit.add_argument("--plan", required=True, help="Exact immutable campaign-budget allocation plan JSON")
    deposit.add_argument("--approved-by", required=True, help="Approver name recorded in the action log")
    deposit.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required for the live campaign-budget write; otherwise validate only",
    )

    withdrawal_record = commands.add_parser(
        "withdrawal-record",
        help=(
            "Record a human-executed withdrawal; use settlement_balance when "
            "advertising is paid from WB balance and is unrelated to withdrawal"
        ),
    )
    _add_runtime_paths(withdrawal_record, output=False)
    withdrawal_record.add_argument("--store", required=True, help="Enabled store alias")
    withdrawal_record.add_argument(
        "--record-key",
        required=True,
        help="Stable user/platform withdrawal reference; reusing it is idempotent",
    )
    withdrawal_record.add_argument("--amount-cny", required=True, help="Actual withdrawn amount")
    withdrawal_record.add_argument(
        "--withdrawn-at", required=True, help="Timezone-aware withdrawal timestamp"
    )
    withdrawal_record.add_argument(
        "--ad-fee-due-cny",
        default=None,
        help=(
            "Historical advertising fee that should have been retained. Omit for "
            "settlement_balance; otherwise omission means unknown/PENDING_DATA"
        ),
    )
    withdrawal_record.add_argument(
        "--ad-fee-withheld-cny",
        default=None,
        help=(
            "Historical advertising fee actually withheld. Omit for "
            "settlement_balance; otherwise omission means unknown/PENDING_DATA"
        ),
    )
    withdrawal_record.add_argument(
        "--ad-fee-status",
        choices=(
            "deducted",
            "not_deducted",
            "partial",
            "unknown",
            "settlement_balance",
        ),
        default=None,
        help=(
            "Use settlement_balance for the current model: due/withheld/debt are "
            "zero and platform advertising cycle deductions are reconciled separately"
        ),
    )
    withdrawal_record.add_argument("--settled-at", help="Optional portal settlement timestamp")
    withdrawal_record.add_argument("--external-reference", help="Portal withdrawal ID")
    withdrawal_record.add_argument(
        "--evidence-json",
        default=None,
        help="Optional JSON object containing screenshot/portal evidence metadata",
    )

    debt_status = commands.add_parser(
        "ads-debt-status",
        help="Show historical advertising-fee debt and FIFO evidence",
    )
    _add_runtime_paths(debt_status, output=False)
    debt_status.add_argument("--store", default=None, help="One store alias; omit for all stores")
    debt_status.add_argument("--as-of", default=None, help="Optional timezone-aware cutoff")

    debt_reconcile = commands.add_parser(
        "ads-debt-reconcile",
        aliases=("ads-payment-reconcile",),
        help=(
            "Import synced WB advertising payment readbacks and settle historical "
            "debt (read-only)"
        ),
    )
    _add_runtime_paths(debt_reconcile, output=False)
    debt_reconcile.add_argument("--store", required=True, help="Enabled store alias")
    debt_reconcile.add_argument("--from", dest="period_start", default=None)
    debt_reconcile.add_argument("--through", dest="period_end", default=None)


def dispatch_finance(args: argparse.Namespace) -> int:
    # Import lazily so ordinary listing/pricing commands do not load SQLite or
    # optional workbook tooling.
    from .finance_service import FinanceService

    service = FinanceService(
        db_path=Path(args.finance_db),
        registry_path=Path(args.store_registry),
        output_root=Path(args.finance_output) if getattr(args, "finance_output", None) else None,
    )
    try:
        command = args.finance_command
        if command == "bootstrap":
            # The constructor default remains available to library callers;
            # the CLI exposes an explicit bounded-concurrency override.
            service.max_store_workers = max(
                1, int(getattr(args, "max_store_workers", service.max_store_workers))
            )
            result = service.bootstrap(through=args.through)
        elif command == "daily":
            result = service.daily(
                through=args.through,
                sync_workbook=not bool(getattr(args, "skip_workbook", False)),
                correction_window_days=getattr(args, "correction_window_days", None),
                max_store_workers=getattr(args, "max_store_workers", None),
            )
        elif command == "account-refresh":
            result = service.account_refresh(through=args.through)
        elif command == "account-report":
            result = service.account_report(
                period=args.period, period_start=args.period_start, through=args.through
            )
        elif command == "workbook-sync":
            from .finance_workbook import sync_canonical_workbook

            debt_snapshot = None
            if args.debt_json:
                debt_snapshot = json.loads(Path(args.debt_json).read_text(encoding="utf-8-sig"))
                if not isinstance(debt_snapshot, dict):
                    raise ValueError("--debt-json must contain a JSON object")
            result = sync_canonical_workbook(
                report_paths=args.report,
                through=args.through,
                canonical=args.canonical,
                report_root=args.report_root,
                registry=Path(args.store_registry),
                debt_snapshot=debt_snapshot,
                repo_root=Path(__file__).resolve().parents[1],
            )
        elif command == "cost-import":
            result = service.cost_import(
                quote=Path(args.quote),
                effective_from=args.effective_from,
                approved_by=args.approved_by,
                rub_per_cny=args.rub_per_cny,
                source_currency=args.source_currency,
            )
        elif command == "report":
            result = service.report(kind=args.kind, period=args.period, reason=args.reason)
        elif command == "funds-refresh":
            result = service.funds_refresh(through=args.through, correction_window_days=args.correction_window_days)
        elif command == "funds-plan":
            result = service.funds_plan(report_id=args.report_id, through=args.through)
        elif command == "ads-deposit-plan":
            result = service.ads_deposit_plan(
                funds_plan_path=Path(args.funds_plan),
                store_alias=args.store,
                campaign=args.campaign,
                amount_cny=args.amount_cny,
                approved_by=args.approved_by,
                use_cashback=not args.cash_only,
            )
        elif command == "ads-deposit":
            result = service.ads_deposit(
                plan_path=Path(args.plan),
                approved_by=args.approved_by,
                confirm_live=args.confirm_live,
            )
        elif command == "withdrawal-record":
            evidence = None
            if args.evidence_json:
                try:
                    evidence = json.loads(args.evidence_json)
                except json.JSONDecodeError as exc:
                    raise ValueError("--evidence-json must be a JSON object") from exc
                if not isinstance(evidence, dict):
                    raise ValueError("--evidence-json must be a JSON object")
            result = service.withdrawal_record(
                store_alias=args.store,
                record_key=args.record_key,
                amount_cny=args.amount_cny,
                withdrawn_at=args.withdrawn_at,
                advertising_fee_due_cny=args.ad_fee_due_cny,
                advertising_fee_withheld_cny=args.ad_fee_withheld_cny,
                advertising_fee_status=args.ad_fee_status,
                settled_at=args.settled_at,
                external_reference=args.external_reference,
                evidence=evidence,
            )
        elif command == "ads-debt-status":
            result = service.advertising_debt_status(
                store_alias=args.store,
                as_of=args.as_of,
            )
        elif command in {"ads-debt-reconcile", "ads-payment-reconcile"}:
            result = service.reconcile_advertising_debt(
                store_alias=args.store,
                period_start=args.period_start,
                period_end=args.period_end,
            )
        else:  # argparse already prevents this, but keep the dispatch fail-closed.
            raise ValueError(f"Unsupported finance command: {command}")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
        return 1 if result.get("status") in {"FAILED", "PARTIAL_FAILURE"} else 0
    finally:
        service.close()


def _add_runtime_paths(parser: argparse.ArgumentParser, *, output: bool = True) -> None:
    parser.add_argument(
        "--finance-db",
        default="data/finance/wb_finance.sqlite3",
        help="Local SQLite finance ledger",
    )
    parser.add_argument(
        "--store-registry",
        default="config/finance/stores.json",
        help="Credential-free multi-store registry",
    )
    if output:
        parser.add_argument(
            "--finance-output",
            default=None,
            help="Report/plan output root; defaults to the configured WB finance archive",
        )


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
