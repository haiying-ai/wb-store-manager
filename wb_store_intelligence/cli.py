from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from .client import ReadOnlyWbClient
from .config import StoreRegistry, load_token_file
from .collector import BaselineCollector, OrdersCollector
from .analyzer import build_report
from .policy import ReadOnlyPolicy, SafetyViolation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WB read-only store intelligence safety tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-config")
    validate.add_argument("--config", required=True)
    validate.add_argument("--token-file", action="append")

    check = subparsers.add_parser("policy-check")
    check.add_argument("--method", required=True)
    check.add_argument("--url", required=True)

    collect = subparsers.add_parser("collect-baseline")
    collect.add_argument("--config", required=True)
    collect.add_argument("--token-file", action="append")
    collect.add_argument("--output", default="data/store_intelligence/snapshots")
    collect.add_argument("--days", type=int, default=7)
    collect.add_argument("--owner-workflow", action="store_true")

    orders = subparsers.add_parser("collect-orders")
    orders.add_argument("--config", required=True)
    orders.add_argument("--token-file", action="append")
    orders.add_argument("--owner-workflow", action="store_true")
    orders.add_argument("--output", default="data/store_intelligence/snapshots")
    orders.add_argument("--date-from", required=True)

    promotions = subparsers.add_parser("collect-promotion-status")
    promotions.add_argument("--config", required=True)
    promotions.add_argument("--token-file", action="append", required=True)
    promotions.add_argument("--store", required=True)
    promotions.add_argument("--promotion-id", action="append", type=int)
    promotions.add_argument("--output", default="data/store_intelligence/snapshots")

    analyze = subparsers.add_parser("analyze-three-stores")
    analyze.add_argument("--snapshot", required=True)
    analyze.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-config":
        registry = StoreRegistry.from_file(args.config)
        tokens_ready = None
        if args.token_file:
            loaded: set[str] = set()
            for token_file in args.token_file:
                loaded.update(load_token_file(token_file, override=True))
            missing = [s.alias for s in registry.stores if s.token_env not in loaded]
            if missing:
                raise RuntimeError(
                    "Token file is missing configured stores: " + ", ".join(missing)
                )
            tokens_ready = True
        print(
            json.dumps(
                {
                    "valid": True,
                    "tokens_ready": tokens_ready,
                    "stores": [
                        {"alias": s.alias, "role": s.role, "token_env": s.token_env}
                        for s in registry.stores
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "collect-baseline":
        if not 1 <= args.days <= 31:
            raise ValueError("--days must be between 1 and 31")
        registry = StoreRegistry.from_file(args.config)
        for token_file in args.token_file or []:
            load_token_file(token_file, override=True)
        if args.owner_workflow:
            import os
            from wb_manager.config import load_settings

            owner_stores = [store for store in registry.stores if store.role == "owner_readonly"]
            if len(owner_stores) != len(registry.stores):
                raise RuntimeError("--owner-workflow accepts owner_readonly stores only")
            os.environ["WB_API_TOKEN"] = load_settings().token
        elif not args.token_file:
            raise RuntimeError("--token-file is required unless --owner-workflow is used")
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_root = Path(args.output).resolve() / run_id
        summary: dict[str, object] = {"runId": run_id, "stores": {}}
        for store in registry.stores:
            collector = BaselineCollector(store, output_root / store.alias)
            statuses = collector.collect(days=args.days)
            summary["stores"][store.alias] = [
                {
                    "source": item.source,
                    "status": item.status,
                    "ok": item.ok,
                    "records": item.records,
                    "error": item.error,
                }
                for item in statuses
            ]
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    if args.command == "collect-orders":
        registry = StoreRegistry.from_file(args.config)
        for token_file in args.token_file or []:
            load_token_file(token_file, override=True)
        if args.owner_workflow:
            import os
            from wb_manager.config import load_settings

            owner_stores = [store for store in registry.stores if store.role == "owner_readonly"]
            if len(owner_stores) != len(registry.stores):
                raise RuntimeError("--owner-workflow accepts owner_readonly stores only")
            os.environ["WB_API_TOKEN"] = load_settings().token
        elif not args.token_file:
            raise RuntimeError("--token-file is required unless --owner-workflow is used")
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_root = Path(args.output).resolve() / run_id
        summary: dict[str, object] = {"runId": run_id, "dateFrom": args.date_from, "stores": {}}
        for store in registry.stores:
            status = OrdersCollector(store, output_root / store.alias).collect(
                date_from=args.date_from
            )
            summary["stores"][store.alias] = status.__dict__
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "orders-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    if args.command == "analyze-three-stores":
        report, metrics = build_report(
            Path(args.snapshot).resolve(), Path(args.output).resolve()
        )
        print(
            json.dumps(
                {"report": str(report), "metrics": str(metrics)}, ensure_ascii=False
            )
        )
        return 0

    if args.command == "collect-promotion-status":
        registry = StoreRegistry.from_file(args.config)
        for token_file in args.token_file:
            load_token_file(token_file, override=True)
        store = registry.get(args.store)
        client = ReadOnlyWbClient(store)
        def safe_get(url: str):
            try:
                return client.request("GET", url), None
            except Exception as exc:
                return None, {"status": None, "errorType": type(exc).__name__}

        now = datetime.now().astimezone()
        calendar_url = (
            "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions?"
            + urlencode(
                {
                    "startDateTime": now.strftime("%Y-%m-%dT00:00:00Z"),
                    "endDateTime": now.strftime("%Y-%m-%dT23:59:59Z"),
                    "allPromo": "true",
                    "limit": 1000,
                    "offset": 0,
                }
            )
        )
        calendar, calendar_error = safe_get(calendar_url)
        promotions = (
            ((calendar.data or {}).get("data") or {}).get("promotions") or []
            if calendar is not None
            else []
        )
        promotion_ids = [item.get("id") for item in promotions if item.get("id")]
        if args.promotion_id:
            promotion_ids = list(dict.fromkeys(args.promotion_id))
        details = details_error = None
        if promotion_ids:
            time.sleep(0.7)
            details, details_error = safe_get(
                "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions/details?"
                + urlencode([("promotionIDs", item) for item in promotion_ids])
            )
        entries = []
        for promotion_id in promotion_ids:
            base = "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions/nomenclatures?"
            requested = {"promotionID": promotion_id, "limit": 1000, "offset": 0}
            time.sleep(0.7)
            active, active_error = safe_get(base + urlencode({**requested, "inAction": "true"}))
            time.sleep(0.7)
            eligible, eligible_error = safe_get(base + urlencode({**requested, "inAction": "false"}))
            active_rows = ((active.data or {}).get("data") or {}).get("nomenclatures") or [] if active is not None else []
            eligible_rows = ((eligible.data or {}).get("data") or {}).get("nomenclatures") or [] if eligible is not None else []
            entries.append(
                {
                    "promotionID": promotion_id,
                    "activeStatus": None if active is None else active.status,
                    "eligibleStatus": None if eligible is None else eligible.status,
                    "activeError": active_error,
                    "eligibleError": eligible_error,
                    "activeCount": len(active_rows),
                    "eligibleCount": len(eligible_rows),
                    "active": active_rows,
                    "eligible": eligible_rows,
                }
            )
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path(args.output).resolve() / store.alias / run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact = output_dir / "promotion_status.json"
        payload = {
            "store": store.alias,
            "collectedAt": datetime.now().astimezone().isoformat(),
            "calendar": {"status": None if calendar is None else calendar.status, "error": calendar_error, "data": promotions},
            "details": None if details is None else {"status": details.status, "data": details.data, "error": details_error},
            "promotions": entries,
        }
        artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"store": store.alias, "artifact": str(artifact), "calendarStatus": None if calendar is None else calendar.status, "detailsStatus": None if details is None else details.status, "promotions": [{key: item[key] for key in ("promotionID", "activeStatus", "eligibleStatus", "activeCount", "eligibleCount")} for item in entries]}, ensure_ascii=False))
        return 0

    try:
        rule = ReadOnlyPolicy().authorize(args.method, args.url)
    except SafetyViolation as exc:
        print(json.dumps({"allowed": False, "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"allowed": True, "endpoint": rule.name}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
