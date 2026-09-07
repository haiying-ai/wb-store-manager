#!/usr/bin/env python3
"""Create and update resumable marketplace workflow state files."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


FLOWS = {
    "daily": ["collect", "analyze", "verify", "report", "archive"],
    "listing": ["sync", "confirm", "pricing", "media", "validate", "approve", "publish", "readback", "buyer_audit", "archive"],
    "media": ["facts", "brief", "generate", "review", "approve", "upload", "readback", "archive"],
    "review": ["collect", "compare", "diagnose", "recommend", "report", "archive"],
    "finance": ["collect", "backfill", "reconcile", "calculate", "verify", "report", "archive"],
    "funding": ["preflight", "plan", "approve", "execute", "readback", "archive"],
    "advertising": ["preflight", "plan", "approve", "execute", "readback", "archive"],
    "fulfillment": ["collect", "cancel_cleanup", "table_sync", "approve", "prepare", "wait_imei", "sync_imei", "labels", "archive"],
}
STATUSES = {"pending", "in_progress", "waiting_approval", "passed", "blocked"}
APPROVAL_STAGES = {"approve"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_state(state: dict) -> list[str]:
    errors: list[str] = []
    flow = state.get("flow")
    if flow not in FLOWS:
        return [f"unknown flow: {flow!r}"]
    expected = FLOWS[flow]
    stages = state.get("stages", {})
    if list(stages) != expected:
        errors.append("stage order does not match the selected flow")
    incomplete_seen = False
    for name in expected:
        item = stages.get(name, {})
        status = item.get("status")
        if status not in STATUSES:
            errors.append(f"{name}: invalid status {status!r}")
            incomplete_seen = True
            continue
        if status == "passed" and incomplete_seen:
            errors.append(f"{name}: cannot pass before an earlier stage")
        elif flow == "fulfillment" and status in {"in_progress", "waiting_approval"} and incomplete_seen:
            errors.append(f"{name}: cannot start before an earlier stage passes")
        if status != "passed":
            incomplete_seen = True
        if name in APPROVAL_STAGES and status == "passed" and not item.get("approved_by"):
            errors.append(f"{name}: approved_by is required")
    return errors


def command_new(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if output.exists() and not args.force:
        raise SystemExit(f"state already exists: {output}")
    timestamp = now()
    state = {
        "schema_version": 1,
        "task_id": args.task_id,
        "flow": args.flow,
        "subject": args.subject,
        "store": args.store,
        "created_at": timestamp,
        "updated_at": timestamp,
        "stages": {
            name: {"status": "pending", "evidence": [], "blockers": [], "updated_at": timestamp}
            for name in FLOWS[args.flow]
        },
    }
    write(output, state)
    print(output)
    return 0


def command_set(args: argparse.Namespace) -> int:
    path = Path(args.file)
    state = read(path)
    if args.stage not in state.get("stages", {}):
        raise SystemExit(f"stage {args.stage!r} is not part of flow {state.get('flow')!r}")
    item = state["stages"][args.stage]
    item["status"] = args.status
    if args.evidence:
        item["evidence"] = list(dict.fromkeys(item.get("evidence", []) + args.evidence))
    if args.blocker:
        item["blockers"] = list(dict.fromkeys(item.get("blockers", []) + args.blocker))
    if args.approved_by:
        item["approved_by"] = args.approved_by
    item["updated_at"] = now()
    state["updated_at"] = item["updated_at"]
    errors = validate_state(state)
    if errors:
        raise SystemExit("invalid transition:\n- " + "\n- ".join(errors))
    write(path, state)
    print(path)
    return 0


def command_validate(args: argparse.Namespace) -> int:
    errors = validate_state(read(Path(args.file)))
    if errors:
        print("INVALID\n- " + "\n- ".join(errors))
        return 1
    print("OK")
    return 0


def command_show(args: argparse.Namespace) -> int:
    print(json.dumps(read(Path(args.file)), ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new")
    new.add_argument("--flow", choices=FLOWS, required=True)
    new.add_argument("--task-id", required=True)
    new.add_argument("--subject", required=True)
    new.add_argument("--store", default="default")
    new.add_argument("--output", required=True)
    new.add_argument("--force", action="store_true")
    new.set_defaults(func=command_new)
    update = commands.add_parser("set")
    update.add_argument("--file", required=True)
    update.add_argument("--stage", required=True)
    update.add_argument("--status", choices=STATUSES, required=True)
    update.add_argument("--evidence", action="append")
    update.add_argument("--blocker", action="append")
    update.add_argument("--approved-by")
    update.set_defaults(func=command_set)
    check = commands.add_parser("validate")
    check.add_argument("--file", required=True)
    check.set_defaults(func=command_validate)
    show = commands.add_parser("show")
    show.add_argument("--file", required=True)
    show.set_defaults(func=command_show)
    return root


if __name__ == "__main__":
    args = parser().parse_args()
    raise SystemExit(args.func(args))
