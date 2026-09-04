#!/usr/bin/env python3
"""Validate normalized private evidence and print a union-only dry run; no API writes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def identity(value: object) -> dict:
    require(isinstance(value, dict), "identity must be an object")
    require(text(value.get("store")), "store is required")
    for key in ("advert_id", "nm_id"):
        require(type(value.get(key)) is int and value[key] > 0, f"invalid {key}")
    return {key: value[key] for key in ("store", "advert_id", "nm_id")}


def build_plan(document: dict) -> dict:
    require(isinstance(document, dict), "input must be an object")
    target = identity(document.get("target"))
    snapshot = document.get("snapshot")
    require(isinstance(snapshot, dict), "snapshot is required")
    require(identity(snapshot.get("target")) == target, "snapshot identity mismatch")
    require(snapshot.get("status") == "ok", "source is not successful")
    require(snapshot.get("complete") is True, "source is incomplete")
    require(text(snapshot.get("retrieved_at")), "source timestamp is required")
    require(text(snapshot.get("evidence")), "source evidence is required")
    existing = snapshot.get("norm_queries")
    require(isinstance(existing, list) and all(text(q) for q in existing),
            "explicit existing list is required; unknown is not empty")
    decisions = document.get("decisions")
    require(isinstance(decisions, list), "reviewed decisions list is required")
    merged = list(dict.fromkeys(existing))
    additions = []
    seen = set()
    for row in decisions:
        require(isinstance(row, dict), "decision must be an object")
        require(identity(row.get("target")) == target, "decision identity mismatch")
        query = row.get("query")
        require(text(query), "exact query is required")
        require(query not in seen, "duplicate or conflicting query decisions")
        seen.add(query)
        require(row.get("classification") in {
            "relevant", "wrong_model", "accessory", "broad_or_uncertain"
        }, "unknown classification")
        require(row.get("action") in {"keep", "exclude"}, "unknown action")
        require(text(row.get("reason")) and text(row.get("evidence")),
                "classification reason and evidence are required")
        if row["action"] == "keep":
            continue
        require(row["classification"] in {"wrong_model", "accessory"},
                "only reviewed mismatches or accessory intent can be excluded")
        require(row.get("approved") is True and text(row.get("approval_ref")),
                "exclusion needs explicit scope approval")
        if query not in merged:
            merged.append(query)
            additions.append(query)
    return {
        "dry_run": True,
        "target": target,
        "snapshot_evidence": snapshot["evidence"],
        "snapshot_retrieved_at": snapshot["retrieved_at"],
        "requires_fresh_read_before_write": True,
        "added": additions,
        "removed": [],
        "write_required": bool(additions),
        "payload": ({"advert_id": target["advert_id"], "nm_id": target["nm_id"],
                     "norm_queries": merged} if additions else None),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="private JSON with target, snapshot and reviewed decisions")
    args = parser.parse_args()
    try:
        plan = build_plan(json.loads(args.input.read_text(encoding="utf-8-sig")))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"BLOCKED: {exc}\n")
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
