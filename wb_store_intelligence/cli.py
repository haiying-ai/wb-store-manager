from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from .config import StoreRegistry, load_token_file
from .collector import BaselineCollector
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
    collect.add_argument("--token-file", action="append", required=True)
    collect.add_argument("--output", default="data/store_intelligence/snapshots")
    collect.add_argument("--days", type=int, default=7)

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
        for token_file in args.token_file:
            load_token_file(token_file, override=True)
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

    try:
        rule = ReadOnlyPolicy().authorize(args.method, args.url)
    except SafetyViolation as exc:
        print(json.dumps({"allowed": False, "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"allowed": True, "endpoint": rule.name}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
