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
