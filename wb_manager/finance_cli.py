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

    daily = commands.add_parser(
        "daily", help="Repair missing periods, sync through yesterday, and close due reports"
    )
    _add_runtime_paths(daily)
    daily.add_argument(
        "--through", default="yesterday", help="Inclusive finance date, YYYY-MM-DD or yesterday"
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
        "--rub-per-cny",
        help="RUB per CNY; defaults to the current approved financial policy",
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

    funds = commands.add_parser(
        "funds-plan", help="Create a plan-only withdrawal and advertising-reserve calculation"
    )
    _add_runtime_paths(funds)
    funds.add_argument("--report-id", required=True, help="A FINAL or RESTATED weekly report ID")

    deposit = commands.add_parser(
        "ads-deposit", help="Execute one explicitly approved campaign deposit plan"
    )
    _add_runtime_paths(deposit, output=False)
    deposit.add_argument("--plan", required=True, help="Exact immutable advertising deposit plan JSON")
    deposit.add_argument("--approved-by", required=True, help="Approver name recorded in the action log")
    deposit.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required for the live campaign-budget write; otherwise validate only",
    )

    withdrawal = commands.add_parser(
        "withdrawal-record",
        help="Record a human-executed withdrawal without moving money",
    )
    _add_runtime_paths(withdrawal, output=False)
    withdrawal.add_argument("--store", required=True, help="Enabled store alias")
    withdrawal.add_argument("--record-key", required=True, help="Stable idempotency key")
    withdrawal.add_argument("--amount-cny", required=True, help="Actual withdrawal amount")
    withdrawal.add_argument(
        "--withdrawn-at", required=True, help="Timezone-aware withdrawal timestamp"
    )
    withdrawal.add_argument("--ad-fee-due-cny", default=None)
    withdrawal.add_argument("--ad-fee-withheld-cny", default=None)
    withdrawal.add_argument(
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
            "Post-2026-09-05 withdrawals default to settlement_balance; "
            "due/withheld/debt are then zero"
        ),
    )
    withdrawal.add_argument("--settled-at")
    withdrawal.add_argument("--external-reference")
    withdrawal.add_argument("--evidence-json", default=None)

    debt_status = commands.add_parser(
        "ads-debt-status", help="Show legacy withdrawal advertising-fee debt"
    )
    _add_runtime_paths(debt_status, output=False)
    debt_status.add_argument("--store", default=None)
    debt_status.add_argument("--as-of", default=None)

    debt_reconcile = commands.add_parser(
        "ads-debt-reconcile",
        aliases=("ads-payment-reconcile",),
        help="Import synced advertising-payment readbacks and reconcile legacy debt",
    )
    _add_runtime_paths(debt_reconcile, output=False)
    debt_reconcile.add_argument("--store", required=True)
    debt_reconcile.add_argument(
        "--from",
        dest="period_start",
        default=None,
        help="Financial date (YYYY-MM-DD) or exact ISO datetime",
    )
    debt_reconcile.add_argument(
        "--through",
        dest="period_end",
        default=None,
        help=(
            "Inclusive financial date (YYYY-MM-DD); an ISO datetime is an "
            "exclusive boundary"
        ),
    )


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
            result = service.bootstrap(through=args.through)
        elif command == "daily":
            result = service.daily(through=args.through)
        elif command == "cost-import":
            result = service.cost_import(
                quote=Path(args.quote),
                effective_from=args.effective_from,
                approved_by=args.approved_by,
                rub_per_cny=args.rub_per_cny,
            )
        elif command == "report":
            result = service.report(kind=args.kind, period=args.period, reason=args.reason)
        elif command == "funds-plan":
            result = service.funds_plan(report_id=args.report_id)
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
                store_alias=args.store, as_of=args.as_of
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
        return 0
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
