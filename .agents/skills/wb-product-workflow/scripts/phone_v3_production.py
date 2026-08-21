#!/usr/bin/env python3
"""Deterministic V3 phone-image production state, regression, and progress tool."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import importlib.util
import json
import os
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from PIL import Image


ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MANIFEST = ROOT / "data/analysis/phone_v3_original_card_batch_20260808/review/v3_121_sku_8page_overview_20260820/manifest.json"
DEFAULT_TEMPLATE = ROOT / "data/template_registry/phone_v3/versions/v3_20260821_deterministic_production"
DEFAULT_RUN_DIR = ROOT / "data/operations/phone-v3-production-current"
ACTIVE_POINTER = ROOT / "data/template_registry/phone_v3/active_production.json"
CLAIM_SOURCE = ROOT / "data/template_registry/phone_v3/versions/v3_20260807_benchmark_c_trust_reference/template_manifest.json"
PAGE_SCORE_FLOORS = {1: 93, 2: 91, 3: 92, 4: 91, 5: 89, 6: 91, 7: 90, 8: 90}
TERMINAL_PASS = {"REGRESSION_PASS", "APPROVED"}
MARK_STATUSES = {"LOCKED", "ARCHIVED", "REVIEW_REQUIRED", "APPROVED", "REJECTED", "BLOCKED"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve())


def sync_active_pointer(run_dir: Path, run_status: str, template_version: str) -> None:
    write_json(
        ACTIVE_POINTER,
        {
            "templateFamily": "phone_v3",
            "templateVersion": template_version,
            "runDir": display_path(run_dir),
            "runStatus": run_status,
            "statusCommand": f"python .agents/skills/wb-product-workflow/scripts/phone_v3_production.py status --run-dir \"{run_dir}\"",
            "dashboard": display_path(run_dir / "dashboard.html"),
            "updatedAt": now_iso(),
            "liveUploadApproved": False,
        },
    )


def parse_capacity_gb(value: str) -> int:
    match = re.search(r"(\d+)", str(value))
    if not match:
        raise ValueError(f"Cannot parse capacity: {value!r}")
    return int(match.group(1))


def brand_for_model(model: str) -> str:
    if model.lower().startswith("iphone"):
        return "Apple"
    if model.lower().startswith("samsung"):
        return "Samsung"
    return "UNRESOLVED"


def connect(run_dir: Path) -> sqlite3.Connection:
    db = sqlite3.connect(run_dir / "progress.sqlite3")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def event(db: sqlite3.Connection, level: str, event_type: str, message: str, task_id: int | None = None) -> None:
    db.execute(
        "INSERT INTO events(created_at, level, event_type, task_id, message) VALUES(?,?,?,?,?)",
        (now_iso(), level, event_type, task_id, message),
    )


def create_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE runs(
          run_id TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          manifest_path TEXT NOT NULL,
          manifest_sha256 TEXT NOT NULL,
          template_version TEXT NOT NULL,
          template_lock_sha256 TEXT NOT NULL,
          sku_count INTEGER NOT NULL,
          task_count INTEGER NOT NULL,
          approved_by TEXT,
          approval_scope TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE skus(
          vendor_code TEXT PRIMARY KEY,
          brand TEXT NOT NULL,
          model TEXT NOT NULL,
          capacity_gb INTEGER NOT NULL,
          color TEXT NOT NULL,
          claim_profile_id TEXT NOT NULL,
          fact_status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          vendor_code TEXT NOT NULL REFERENCES skus(vendor_code),
          page INTEGER NOT NULL,
          production_unit TEXT NOT NULL,
          source_path TEXT NOT NULL,
          expected_sha256 TEXT NOT NULL,
          source_width INTEGER NOT NULL,
          source_height INTEGER NOT NULL,
          source_mode TEXT NOT NULL,
          output_path TEXT,
          output_sha256 TEXT,
          status TEXT NOT NULL,
          automated_qa TEXT NOT NULL,
          visual_qa TEXT NOT NULL,
          score REAL,
          attempts INTEGER NOT NULL DEFAULT 0,
          note TEXT,
          updated_at TEXT NOT NULL,
          UNIQUE(vendor_code, page)
        );
        CREATE INDEX idx_tasks_status ON tasks(status);
        CREATE INDEX idx_tasks_page ON tasks(page);
        CREATE TABLE approvals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          scope TEXT NOT NULL,
          status TEXT NOT NULL,
          approved_by TEXT NOT NULL,
          note TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT NOT NULL,
          level TEXT NOT NULL,
          event_type TEXT NOT NULL,
          task_id INTEGER REFERENCES tasks(id),
          message TEXT NOT NULL
        );
        """
    )


def build_claim_registry(rows: list[dict[str, Any]]) -> dict[str, Any]:
    source = load_json(CLAIM_SOURCE)
    ru_claims = source["ownerClaimConfirmation"]["claims"]
    ids = [
        "new_only_activated",
        "no_extended_use",
        "factory_components_not_replaced",
        "battery_max_capacity_100",
        "return_30_days_confirmed_defect",
        "seller_warranty_1_year",
        "included_box_cable_adapter_case",
    ]
    page_scope = {
        "new_only_activated": [1, 2, 6],
        "no_extended_use": [2, 6],
        "factory_components_not_replaced": [1, 3, 6],
        "battery_max_capacity_100": [1, 3, 4],
        "return_30_days_confirmed_defect": [1, 5],
        "seller_warranty_1_year": [1, 5],
        "included_box_cable_adapter_case": [4],
    }
    claims = []
    for claim_id, text_ru in zip(ids, ru_claims):
        claims.append(
            {
                "claimId": claim_id,
                "textRu": text_ru,
                "status": "OWNER_CONFIRMED",
                "confirmedAt": source["ownerClaimConfirmation"]["confirmedAt"],
                "source": display_path(CLAIM_SOURCE),
                "scope": "all SKUs in this run; user stated same source and channel as benchmark C",
                "allowedPages": page_scope[claim_id],
                "requiresExactSkuMapping": True,
            }
        )
    return {
        "schemaVersion": 1,
        "profileId": "phone_v3_owner_confirmed_20260807",
        "skuCount": len(rows),
        "claims": claims,
        "prohibitedClaims": [
            "Apple official warranty",
            "Apple official store",
            "unconfirmed gifts",
            "unconfirmed certificates",
            "unconfirmed dispatch promise",
        ],
        "liveUseRule": "Resolve current WB fields and exact SKU identity before any payload; this registry is not live-write approval.",
    }


def write_claim_files(run_dir: Path, registry: dict[str, Any]) -> None:
    write_json(run_dir / "claim_registry.json", registry)
    with (run_dir / "claim_registry.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["claim_id", "text_ru", "status", "confirmed_at", "source", "scope", "allowed_pages"],
        )
        writer.writeheader()
        for item in registry["claims"]:
            writer.writerow(
                {
                    "claim_id": item["claimId"],
                    "text_ru": item["textRu"],
                    "status": item["status"],
                    "confirmed_at": item["confirmedAt"],
                    "source": item["source"],
                    "scope": item["scope"],
                    "allowed_pages": ",".join(str(page) for page in item["allowedPages"]),
                }
            )


def fact_record(row: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    return {
        "vendor_code": row["vendorCode"],
        "brand": brand_for_model(row["model"]),
        "model": row["model"],
        "capacity_gb": parse_capacity_gb(row["capacity"]),
        "color": row["color"],
        "condition_ru": "новый, только активирован",
        "claim_profile_id": "phone_v3_owner_confirmed_20260807",
        "page01_mode": "controlled_fields_on_approved_sku_scene",
        "pages02_08_mode": "atomic_model_color_page_reuse",
        "fact_status": "CONFIRMED_FROM_121_MANIFEST",
        "technical_fact_source": "approved atomic model-color pages and listing payload; do not infer missing live characteristics",
        "source_manifest": display_path(manifest_path),
        "live_write_approved": False,
    }


def write_fact_files(run_dir: Path, facts: list[dict[str, Any]]) -> None:
    fields = list(facts[0].keys())
    with (run_dir / "sku_facts.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(facts)
    with (run_dir / "sku_facts.jsonl").open("w", encoding="utf-8") as stream:
        for item in facts:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")


def build_template_lock(template_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_json(template_dir / "template_manifest.json")
    source_master = ROOT / manifest["sourceVisualMaster"]
    files = [
        template_dir / "template_manifest.json",
        template_dir / "page01/layout.json",
        template_dir / "page01/font_manifest.json",
        template_dir / "pages02_08/contract.json",
        ROOT / "tools/render_v3_layered_page01.py",
        Path(__file__).resolve(),
    ]
    files.extend(source_master / "images" / f"{page:02d}.png" for page in range(1, 9))
    font_manifest = load_json(template_dir / "page01/font_manifest.json")
    files.extend(Path(spec["path"]) for spec in font_manifest.values() if isinstance(spec, dict) and "path" in spec)
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Template lock inputs missing: {missing}")
    return {
        "schemaVersion": 1,
        "templateFamily": "phone_v3",
        "templateVersion": manifest["version"],
        "status": "LOCKED_FOR_LOCAL_REGRESSION",
        "liveUploadApproved": False,
        "lockedAt": now_iso(),
        "inputManifest": display_path(manifest_path),
        "inputManifestSha256": sha256(manifest_path),
        "files": [
            {"path": display_path(path), "sha256": sha256(path), "bytes": path.stat().st_size}
            for path in files
        ],
        "pageModes": manifest["productionModes"],
        "hardGates": [
            "template hash must match",
            "SKU fact and claim profile must resolve",
            "page01 product locked region must have zero changed pixels",
            "pages02-08 must use byte-identical approved model-color pages",
            "full-size and 320px visual review is mandatory for new assets",
            "no live WB action without separate exact-scope approval and readback",
        ],
    }


def inspect_image(path: Path) -> tuple[int, int, str]:
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        return image.width, image.height, image.mode


def model_color_violations(rows: list[dict[str, Any]], page_hashes: dict[tuple[str, int], str]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, str, str], set[str]] = defaultdict(set)
    for row in rows:
        for page in range(2, 9):
            grouped[(page, row["model"], row["color"])].add(page_hashes[(row["vendorCode"], page)])
    return [
        {"page": key[0], "model": key[1], "color": key[2], "hashCount": len(values)}
        for key, values in grouped.items()
        if len(values) != 1
    ]


def init_run(args: argparse.Namespace) -> None:
    manifest_path = Path(args.manifest).resolve()
    template_dir = Path(args.template_dir).resolve()
    run_dir = Path(args.run_dir).resolve()
    db_path = run_dir / "progress.sqlite3"
    if db_path.exists():
        raise FileExistsError(f"Run already exists; use status/regress/validate instead: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_json(manifest_path)
    rows = manifest["rows"]
    if not rows:
        raise ValueError("Manifest has no rows")
    if any(len(row.get("pages", [])) != 8 for row in rows):
        raise ValueError("Every V3 SKU must resolve exactly 8 pages")
    vendor_codes = [row["vendorCode"] for row in rows]
    if len(vendor_codes) != len(set(vendor_codes)):
        raise ValueError("Duplicate vendorCode in manifest")

    facts = [fact_record(row, manifest_path) for row in rows]
    write_fact_files(run_dir, facts)
    claim_registry = build_claim_registry(rows)
    write_claim_files(run_dir, claim_registry)
    template_lock = build_template_lock(template_dir, manifest_path)
    write_json(run_dir / "template_lock.json", template_lock)
    lock_hash = sha256(run_dir / "template_lock.json")

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    create_schema(db)
    run_id = args.run_id or f"phone-v3-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    timestamp = now_iso()
    task_count = len(rows) * 8
    db.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            "INITIALIZING",
            display_path(manifest_path),
            sha256(manifest_path),
            load_json(template_dir / "template_manifest.json")["version"],
            lock_hash,
            len(rows),
            task_count,
            args.approved_by,
            "local template and no-generation regression only",
            timestamp,
            timestamp,
        ),
    )
    if args.approved_by:
        db.execute(
            "INSERT INTO approvals(scope,status,approved_by,note,created_at) VALUES(?,?,?,?,?)",
            (
                "local template and no-generation regression",
                "APPROVED",
                args.approved_by,
                "Does not approve image generation, WB upload, or any live marketplace mutation.",
                timestamp,
            ),
        )
    for fact in facts:
        db.execute(
            "INSERT INTO skus VALUES(?,?,?,?,?,?,?,?)",
            (
                fact["vendor_code"],
                fact["brand"],
                fact["model"],
                fact["capacity_gb"],
                fact["color"],
                fact["claim_profile_id"],
                fact["fact_status"],
                timestamp,
            ),
        )

    golden_path = run_dir / "golden_assets.jsonl"
    page_hashes: dict[tuple[str, int], str] = {}
    with golden_path.open("w", encoding="utf-8") as golden:
        completed = 0
        for row in rows:
            for page in range(1, 9):
                source = Path(row["pages"][page - 1]).resolve()
                if not source.is_file():
                    raise FileNotFoundError(source)
                width, height, mode = inspect_image(source)
                file_hash = sha256(source)
                page_hashes[(row["vendorCode"], page)] = file_hash
                production_unit = "sku" if page == 1 else "model_color"
                record = {
                    "vendorCode": row["vendorCode"],
                    "model": row["model"],
                    "capacity": row["capacity"],
                    "color": row["color"],
                    "page": page,
                    "productionUnit": production_unit,
                    "sourcePath": str(source),
                    "sha256": file_hash,
                    "width": width,
                    "height": height,
                    "mode": mode,
                }
                golden.write(json.dumps(record, ensure_ascii=False) + "\n")
                db.execute(
                    """INSERT INTO tasks(
                    vendor_code,page,production_unit,source_path,expected_sha256,
                    source_width,source_height,source_mode,status,automated_qa,visual_qa,note,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["vendorCode"],
                        page,
                        production_unit,
                        str(source),
                        file_hash,
                        width,
                        height,
                        mode,
                        "LOCKED",
                        "PENDING",
                        "GOLDEN_SNAPSHOT",
                        "No-generation regression will resolve this approved source byte-for-byte.",
                        timestamp,
                    ),
                )
                completed += 1
                if completed % 100 == 0:
                    db.commit()
                    print(f"locked {completed}/{task_count}", flush=True)

    violations = model_color_violations(rows, page_hashes)
    if violations:
        write_json(run_dir / "model_color_hash_violations.json", violations)
        raise ValueError(f"Model-color atomic reuse conflict: {len(violations)}")

    pipeline_config = {
        "schemaVersion": 1,
        "runId": run_id,
        "runDir": display_path(run_dir),
        "manifest": display_path(manifest_path),
        "manifestSha256": sha256(manifest_path),
        "templateDir": display_path(template_dir),
        "templateVersion": load_json(template_dir / "template_manifest.json")["version"],
        "templateLockSha256": lock_hash,
        "skuCount": len(rows),
        "taskCount": task_count,
        "pageModes": {"01": "sku controlled-fields / golden atomic regression", "02-08": "model-color atomic reuse"},
        "scoreFloors": {str(page): score for page, score in PAGE_SCORE_FLOORS.items()},
        "status": "LOCKED",
        "liveUploadApproved": False,
        "createdAt": timestamp,
    }
    write_json(run_dir / "pipeline_config.json", pipeline_config)
    db.execute("UPDATE runs SET status=?,updated_at=?", ("LOCKED", now_iso()))
    event(db, "INFO", "RUN_INITIALIZED", f"Locked {len(rows)} SKUs and {task_count} page assignments")
    db.commit()
    db.close()
    if not args.no_set_active:
        sync_active_pointer(run_dir, "LOCKED", pipeline_config["templateVersion"])
    refresh_dashboard(run_dir)
    print(json.dumps({"status": "LOCKED", "runDir": str(run_dir), "skus": len(rows), "tasks": task_count}, ensure_ascii=False))


def regression(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    db = connect(run_dir)
    run = db.execute("SELECT * FROM runs").fetchone()
    if not run:
        raise ValueError("Missing run row")
    if sha256(run_dir / "template_lock.json") != run["template_lock_sha256"]:
        raise ValueError("Template lock changed; create a new run instead of continuing")
    db.execute("UPDATE runs SET status=?,updated_at=?", ("REGRESSION_RUNNING", now_iso()))
    event(db, "INFO", "REGRESSION_STARTED", "No-generation golden regression started")
    db.commit()
    tasks = db.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    failures = []
    for index, task in enumerate(tasks, 1):
        source = Path(task["source_path"])
        reason = None
        current_hash = None
        try:
            width, height, mode = inspect_image(source)
            current_hash = sha256(source)
            if (width, height) != (task["source_width"], task["source_height"]):
                reason = f"dimension drift: {width}x{height}"
            elif mode != task["source_mode"]:
                reason = f"mode drift: {mode}"
            elif current_hash != task["expected_sha256"]:
                reason = "SHA-256 drift"
        except Exception as exc:  # preserve exact blocker
            reason = f"unreadable: {exc}"
        if reason:
            status = "BLOCKED"
            automated_qa = "FAIL"
            failures.append({"vendorCode": task["vendor_code"], "page": task["page"], "reason": reason})
            event(db, "ERROR", "REGRESSION_BLOCKED", f"{task['vendor_code']} page {task['page']:02d}: {reason}", task["id"])
        else:
            status = "REGRESSION_PASS"
            automated_qa = "PASS"
        db.execute(
            """UPDATE tasks SET output_path=?,output_sha256=?,status=?,automated_qa=?,
            visual_qa=?,attempts=attempts+1,note=?,updated_at=? WHERE id=?""",
            (
                str(source),
                current_hash,
                status,
                automated_qa,
                "GOLDEN_HASH_INHERITED",
                "Byte-identical golden reuse; no generation, overlay, text rendering, or image mutation.",
                now_iso(),
                task["id"],
            ),
        )
        if index % 25 == 0 or index == len(tasks):
            db.commit()
            refresh_dashboard(run_dir, db=db)
            print(f"regressed {index}/{len(tasks)}", flush=True)

    final_status = "REGRESSION_PASS_REVIEW_REQUIRED" if not failures else "BLOCKED"
    db.execute("UPDATE runs SET status=?,updated_at=?", (final_status, now_iso()))
    event(
        db,
        "INFO" if not failures else "ERROR",
        "REGRESSION_FINISHED",
        f"{len(tasks) - len(failures)}/{len(tasks)} assignments passed; failures={len(failures)}",
    )
    db.commit()
    report = {
        "schemaVersion": 1,
        "runId": run["run_id"],
        "status": final_status,
        "mode": "NO_GENERATION_GOLDEN_REUSE",
        "skuCount": run["sku_count"],
        "pageAssignmentCount": len(tasks),
        "passCount": len(tasks) - len(failures),
        "failureCount": len(failures),
        "failures": failures,
        "templateLockSha256": run["template_lock_sha256"],
        "conclusion": "Hash identity proves pipeline resolution matches the frozen current batch; it does not authorize WB upload.",
        "completedAt": now_iso(),
    }
    write_json(run_dir / "regression_report.json", report)
    refresh_dashboard(run_dir, db=db)
    sync_active_pointer(run_dir, final_status, run["template_version"])
    db.close()
    print(json.dumps(report, ensure_ascii=False))


def dashboard_data(db: sqlite3.Connection) -> dict[str, Any]:
    run = dict(db.execute("SELECT * FROM runs").fetchone())
    status_counts = {row["status"]: row["count"] for row in db.execute("SELECT status,COUNT(*) count FROM tasks GROUP BY status")}
    page_rows = db.execute(
        "SELECT page,status,COUNT(*) count FROM tasks GROUP BY page,status ORDER BY page,status"
    ).fetchall()
    pages: dict[str, dict[str, int]] = defaultdict(dict)
    for row in page_rows:
        pages[f"{row['page']:02d}"][row["status"]] = row["count"]
    passed = sum(status_counts.get(status, 0) for status in TERMINAL_PASS)
    events = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 20")]
    blockers = [
        dict(row)
        for row in db.execute(
            "SELECT vendor_code,page,status,note,updated_at FROM tasks WHERE status IN ('BLOCKED','REJECTED') ORDER BY updated_at DESC LIMIT 30"
        )
    ]
    return {
        "run": run,
        "progress": {
            "passed": passed,
            "total": run["task_count"],
            "percent": round(100 * passed / run["task_count"], 2) if run["task_count"] else 0,
            "statusCounts": status_counts,
            "pages": pages,
        },
        "blockers": blockers,
        "events": events,
        "updatedAt": now_iso(),
    }


def refresh_dashboard(run_dir: Path, db: sqlite3.Connection | None = None) -> dict[str, Any]:
    own_db = db is None
    if own_db:
        db = connect(run_dir)
    assert db is not None
    data = dashboard_data(db)
    write_json(run_dir / "progress.json", data)
    run = data["run"]
    progress = data["progress"]
    status_rows = "".join(
        f"<tr><td>{html.escape(status)}</td><td>{count}</td></tr>"
        for status, count in sorted(progress["statusCounts"].items())
    )
    page_rows = "".join(
        f"<tr><td>{page}</td><td>{sum(counts.values())}</td><td>{counts.get('REGRESSION_PASS',0)+counts.get('APPROVED',0)}</td><td>{html.escape(json.dumps(counts,ensure_ascii=False))}</td></tr>"
        for page, counts in progress["pages"].items()
    )
    blocker_rows = "".join(
        f"<tr><td>{html.escape(item['vendor_code'])}</td><td>{item['page']:02d}</td><td>{html.escape(item['status'])}</td><td>{html.escape(item.get('note') or '')}</td></tr>"
        for item in data["blockers"]
    ) or "<tr><td colspan='4'>无阻断项</td></tr>"
    event_rows = "".join(
        f"<tr><td>{html.escape(item['created_at'])}</td><td>{html.escape(item['level'])}</td><td>{html.escape(item['event_type'])}</td><td>{html.escape(item['message'])}</td></tr>"
        for item in data["events"]
    )
    html_text = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta http-equiv='refresh' content='5'>
<title>V3 SKU生产进度</title><style>
body{{font-family:Arial,'Microsoft YaHei',sans-serif;margin:24px;background:#f6f3ef;color:#17131f}}
.grid{{display:grid;grid-template-columns:repeat(4,minmax(160px,1fr));gap:12px}}.card{{background:white;border:1px solid #d8cfee;border-radius:12px;padding:16px}}
.bar{{height:18px;background:#e7e0f4;border-radius:9px;overflow:hidden}}.bar span{{display:block;height:100%;background:#4b1dac;width:{progress['percent']}%}}
table{{width:100%;border-collapse:collapse;background:white;margin:14px 0}}th,td{{padding:8px;border:1px solid #ddd;text-align:left}}th{{background:#eee7fa}}
code{{background:#eee;padding:2px 5px}}h1,h2{{color:#351275}}</style></head><body>
<h1>V3 SKU确定性生产进度</h1><div class='grid'>
<div class='card'><b>Run</b><br>{html.escape(run['run_id'])}</div><div class='card'><b>状态</b><br>{html.escape(run['status'])}</div>
<div class='card'><b>SKU</b><br>{run['sku_count']}</div><div class='card'><b>页面任务</b><br>{progress['passed']} / {progress['total']}</div></div>
<h2>总进度 {progress['percent']}%</h2><div class='bar'><span></span></div><p>最后刷新：{html.escape(data['updatedAt'])}（页面每5秒自动刷新）</p>
<h2>各页进度</h2><table><tr><th>页面</th><th>任务</th><th>通过</th><th>状态明细</th></tr>{page_rows}</table>
<h2>任务状态</h2><table><tr><th>状态</th><th>数量</th></tr>{status_rows}</table>
<h2>阻断项</h2><table><tr><th>SKU</th><th>页</th><th>状态</th><th>说明</th></tr>{blocker_rows}</table>
<h2>最近事件</h2><table><tr><th>时间</th><th>级别</th><th>事件</th><th>说明</th></tr>{event_rows}</table>
<p>本看板只反映本地生产状态；不代表WB已上传或已通过线上读回。</p></body></html>"""
    (run_dir / "dashboard.html").write_text(html_text, encoding="utf-8")
    markdown = [
        "# V3 SKU 生产进度",
        "",
        f"- Run: `{run['run_id']}`",
        f"- 状态: `{run['status']}`",
        f"- SKU: {run['sku_count']}",
        f"- 页面任务: {progress['passed']} / {progress['total']} ({progress['percent']}%)",
        f"- 最后更新: {data['updatedAt']}",
        f"- 阻断项: {len(data['blockers'])}",
        "",
        "本状态只表示本地生产与QA，不表示WB上传完成。",
    ]
    (run_dir / "progress.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    if own_db:
        db.close()
    return data


def status_command(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    data = refresh_dashboard(run_dir)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def next_command(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    db = connect(run_dir)
    placeholders = ",".join("?" for _ in TERMINAL_PASS)
    rows = db.execute(
        f"SELECT vendor_code,page,production_unit,status,source_path,note FROM tasks WHERE status NOT IN ({placeholders}) ORDER BY page,vendor_code LIMIT ?",
        (*sorted(TERMINAL_PASS), args.limit),
    ).fetchall()
    db.close()
    print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))


def mark_command(args: argparse.Namespace) -> None:
    if args.status not in MARK_STATUSES:
        raise ValueError(f"Unsupported status: {args.status}")
    run_dir = Path(args.run_dir).resolve()
    db = connect(run_dir)
    task = db.execute(
        "SELECT * FROM tasks WHERE vendor_code=? AND page=?",
        (args.vendor_code, args.page),
    ).fetchone()
    if not task:
        raise ValueError("Task not found")
    asset_path = Path(args.asset_path).resolve() if args.asset_path else None
    output_hash = None
    if asset_path:
        width, height, _ = inspect_image(asset_path)
        if (width, height) != (900, 1200):
            raise ValueError(f"Asset must be 900x1200, got {width}x{height}")
        output_hash = sha256(asset_path)
    if args.status == "APPROVED":
        if not args.approved_by:
            raise ValueError("APPROVED requires --approved-by")
        if not asset_path:
            raise ValueError("APPROVED requires --asset-path")
        floor = PAGE_SCORE_FLOORS[args.page]
        if args.score is None or args.score < floor:
            raise ValueError(f"Page {args.page:02d} requires score >= {floor}")
        db.execute(
            "INSERT INTO approvals(scope,status,approved_by,note,created_at) VALUES(?,?,?,?,?)",
            (
                f"{args.vendor_code} page {args.page:02d}",
                "APPROVED",
                args.approved_by,
                args.note,
                now_iso(),
            ),
        )
    db.execute(
        """UPDATE tasks SET output_path=COALESCE(?,output_path),output_sha256=COALESCE(?,output_sha256),
        status=?,visual_qa=?,score=COALESCE(?,score),attempts=attempts+1,note=?,updated_at=? WHERE id=?""",
        (
            str(asset_path) if asset_path else None,
            output_hash,
            args.status,
            "MANUAL_APPROVED" if args.status == "APPROVED" else "REVIEW_REQUIRED",
            args.score,
            args.note,
            now_iso(),
            task["id"],
        ),
    )
    event(db, "INFO", "TASK_MARKED", f"{args.vendor_code} page {args.page:02d} -> {args.status}", task["id"])
    db.commit()
    refresh_dashboard(run_dir, db=db)
    db.close()
    print(json.dumps({"vendorCode": args.vendor_code, "page": args.page, "status": args.status}, ensure_ascii=False))


def render_page01_command(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    source_scene = Path(args.source_scene).resolve()
    if not source_scene.is_file():
        raise FileNotFoundError(source_scene)
    db = connect(run_dir)
    task = db.execute(
        """SELECT tasks.*,skus.model,skus.capacity_gb FROM tasks
        JOIN skus ON skus.vendor_code=tasks.vendor_code
        WHERE tasks.vendor_code=? AND tasks.page=1""",
        (args.vendor_code,),
    ).fetchone()
    if not task:
        raise ValueError(f"Page01 task not found: {args.vendor_code}")
    config = load_json(run_dir / "pipeline_config.json")
    template_dir = ROOT / config["templateDir"]
    renderer_path = ROOT / "tools/render_v3_layered_page01.py"
    spec = importlib.util.spec_from_file_location("phone_v3_page01_renderer", renderer_path)
    if spec is None or spec.loader is None:
        raise ImportError(renderer_path)
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    layout = load_json(template_dir / "page01/layout.json")
    fonts = load_json(template_dir / "page01/font_manifest.json")
    master = Image.open(ROOT / layout["locked_master"]).convert("RGB")
    output_dir = Path(args.output_dir).resolve() if args.output_dir else run_dir / "candidates/page01"
    case = {
        "vendor_code": args.vendor_code,
        "title": task["model"],
        "capacity": f"{task['capacity_gb']} ГБ",
        "source": str(source_scene),
    }
    result = renderer.render_case(case, layout, fonts, master, output_dir)
    rebuilt = ROOT / result["rebuilt"] if not Path(result["rebuilt"]).is_absolute() else Path(result["rebuilt"])
    task_status = "ARCHIVED" if result["qa"] == "PASS" else "BLOCKED"
    db.execute(
        """UPDATE tasks SET output_path=?,output_sha256=?,status=?,automated_qa=?,visual_qa=?,
        attempts=attempts+1,note=?,updated_at=? WHERE id=?""",
        (
            str(rebuilt),
            sha256(rebuilt),
            task_status,
            result["qa"],
            "REVIEW_REQUIRED",
            f"page01 deterministic render; outside={result['changed_pixels_outside_editable_regions']}; product={result['changed_pixels_in_product_locked_region']}",
            now_iso(),
            task["id"],
        ),
    )
    event(db, "INFO", "PAGE01_RENDERED", f"{args.vendor_code} -> {task_status}", task["id"])
    db.commit()
    refresh_dashboard(run_dir, db=db)
    db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def approve_run(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    db = connect(run_dir)
    blockers = db.execute("SELECT COUNT(*) FROM tasks WHERE status NOT IN ('REGRESSION_PASS','APPROVED')").fetchone()[0]
    if blockers:
        raise ValueError(f"Cannot approve run; non-passing tasks: {blockers}")
    timestamp = now_iso()
    db.execute(
        "INSERT INTO approvals(scope,status,approved_by,note,created_at) VALUES(?,?,?,?,?)",
        ("local deterministic V3 production standard", "APPROVED", args.approved_by, args.note, timestamp),
    )
    db.execute(
        "UPDATE runs SET status=?,approved_by=?,approval_scope=?,updated_at=?",
        ("APPROVED_FOR_LOCAL_PRODUCTION", args.approved_by, "local production only; no WB mutation", timestamp),
    )
    event(db, "INFO", "RUN_APPROVED", f"Local production approved by {args.approved_by}")
    db.commit()
    refresh_dashboard(run_dir, db=db)
    sync_active_pointer(run_dir, "APPROVED_FOR_LOCAL_PRODUCTION", db.execute("SELECT template_version FROM runs").fetchone()[0])
    db.close()
    print(json.dumps({"status": "APPROVED_FOR_LOCAL_PRODUCTION", "approvedBy": args.approved_by}, ensure_ascii=False))


def validate_command(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    required = [
        "pipeline_config.json",
        "template_lock.json",
        "sku_facts.csv",
        "sku_facts.jsonl",
        "claim_registry.json",
        "claim_registry.csv",
        "golden_assets.jsonl",
        "progress.sqlite3",
        "progress.json",
        "dashboard.html",
    ]
    missing = [name for name in required if not (run_dir / name).is_file()]
    config = load_json(run_dir / "pipeline_config.json") if not missing else {}
    db = connect(run_dir) if not missing else None
    checks = {}
    if db:
        run = db.execute("SELECT * FROM runs").fetchone()
        lock = load_json(run_dir / "template_lock.json")
        locked_file_checks = []
        for item in lock["files"]:
            locked_path = Path(item["path"])
            if not locked_path.is_absolute():
                locked_path = ROOT / locked_path
            locked_file_checks.append(locked_path.is_file() and sha256(locked_path) == item["sha256"])
        checks = {
            "template_lock_hash": sha256(run_dir / "template_lock.json") == run["template_lock_sha256"],
            "locked_files_current": all(locked_file_checks),
            "sku_count": db.execute("SELECT COUNT(*) FROM skus").fetchone()[0] == run["sku_count"],
            "task_count": db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == run["task_count"],
            "facts_count": sum(1 for _ in (run_dir / "sku_facts.jsonl").open(encoding="utf-8")) == run["sku_count"],
            "golden_count": sum(1 for _ in (run_dir / "golden_assets.jsonl").open(encoding="utf-8")) == run["task_count"],
            "manifest_hash": sha256(ROOT / config["manifest"]) == run["manifest_sha256"],
        }
        db.close()
    passed = not missing and all(checks.values())
    result = {"status": "PASS" if passed else "FAIL", "missing": missing, "checks": checks}
    write_json(run_dir / "validation_report.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not passed:
        raise SystemExit(1)


def serve_command(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    refresh_dashboard(run_dir)
    handler = partial(SimpleHTTPRequestHandler, directory=str(run_dir))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Dashboard: http://{args.host}:{args.port}/dashboard.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Freeze template, facts, claims, golden hashes, DB, and dashboard")
    init.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    init.add_argument("--template-dir", default=str(DEFAULT_TEMPLATE))
    init.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    init.add_argument("--run-id")
    init.add_argument("--approved-by")
    init.add_argument("--no-set-active", action="store_true")
    init.set_defaults(func=init_run)

    regress = sub.add_parser("regress", help="Run 121-SKU no-generation golden regression")
    regress.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    regress.set_defaults(func=regression)

    status = sub.add_parser("status", help="Refresh and print progress")
    status.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    status.set_defaults(func=status_command)

    next_parser = sub.add_parser("next", help="Return the next non-passing tasks")
    next_parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    next_parser.add_argument("--limit", type=int, default=20)
    next_parser.set_defaults(func=next_command)

    mark = sub.add_parser("mark", help="Record an archived/reviewed/approved/rejected task")
    mark.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    mark.add_argument("--vendor-code", required=True)
    mark.add_argument("--page", type=int, choices=range(1, 9), required=True)
    mark.add_argument("--status", required=True)
    mark.add_argument("--asset-path")
    mark.add_argument("--score", type=float)
    mark.add_argument("--approved-by")
    mark.add_argument("--note", default="")
    mark.set_defaults(func=mark_command)

    render_page01 = sub.add_parser("render-page01", help="Render one fixed-geometry page01 candidate from run facts")
    render_page01.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    render_page01.add_argument("--vendor-code", required=True)
    render_page01.add_argument("--source-scene", required=True)
    render_page01.add_argument("--output-dir")
    render_page01.set_defaults(func=render_page01_command)

    approve = sub.add_parser("approve-run", help="Approve a fully passing run for local production")
    approve.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--note", default="")
    approve.set_defaults(func=approve_run)

    validate = sub.add_parser("validate", help="Validate production artifacts and counts")
    validate.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    validate.set_defaults(func=validate_command)

    serve = sub.add_parser("serve", help="Serve the auto-refresh progress dashboard")
    serve.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=serve_command)
    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
