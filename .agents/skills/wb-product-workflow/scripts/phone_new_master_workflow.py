#!/usr/bin/env python3
"""Cross-session state and master lock for new WB phone-template tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


EXACT_MODE = "EXACT_MASTER_TEST"
DESIGN_MODE = "DESIGN_SYNTHESIS"
MODES = {EXACT_MODE, DESIGN_MODE}
EXACT_GENERATION_MODE = "DIRECT_FULL_TEXT_IMAGEGEN"
DESIGN_GENERATION_MODE = "DIRECT_FULL_TEXT_IMAGEGEN"
DIRECTION_EXPLORATION_STAGE = "DIRECTION_EXPLORATION"
FULL_PACK_STAGE = "FULL_PACK"
DESIGN_STAGES = {DIRECTION_EXPLORATION_STAGE, FULL_PACK_STAGE}
GENERATION_MODES = {
    EXACT_MODE: EXACT_GENERATION_MODE,
    DESIGN_MODE: DESIGN_GENERATION_MODE,
}
# Backward-compatible aliases for callers/tests that imported the old constants.
MODE = EXACT_MODE
GENERATION_MODE = EXACT_GENERATION_MODE
DEFAULT_POINTER = Path("data/template_registry/phone_new_master/active_test.json")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def workspace_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    cwd = Path.cwd().resolve()
    if (cwd / ".agents" / "skills" / "wb-product-workflow").exists():
        return cwd
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".agents" / "skills" / "wb-product-workflow").exists():
            return candidate
    raise SystemExit("Cannot locate WB workspace root; pass --workspace-root.")


def resolved(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path.resolve())


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {path}: {exc}") from exc


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def natural_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def pointer_path(root: Path, supplied: str | None) -> Path:
    return resolved(root, supplied or DEFAULT_POINTER)


def validate_payload(root: Path, pointer: dict) -> list[str]:
    errors: list[str] = []
    required = {
        "schemaVersion",
        "taskId",
        "mode",
        "generationMode",
        "status",
        "cohortPath",
        "workflowStatePath",
        "pilotVendorCode",
        "generationAuthorized",
        "liveUploadAuthorized",
    }
    missing = sorted(required - pointer.keys())
    if missing:
        errors.append("missing pointer fields: " + ", ".join(missing))
        return errors
    mode = pointer["mode"]
    if mode not in MODES:
        errors.append(f"mode must be one of {sorted(MODES)}")
    expected_generation_mode = GENERATION_MODES.get(mode)
    if expected_generation_mode and pointer["generationMode"] != expected_generation_mode:
        errors.append(f"generationMode must be {expected_generation_mode} for {mode}")
    if pointer["liveUploadAuthorized"]:
        errors.append("new-master test cannot authorize live upload")

    try:
        cohort_path = resolved(root, pointer["cohortPath"])
        workflow_path = resolved(root, pointer["workflowStatePath"])
        cohort = read_json(cohort_path)
        workflow = read_json(workflow_path)
        if cohort.get("taskId") != pointer["taskId"]:
            errors.append("cohort taskId does not match pointer")
        if workflow.get("task_id") != pointer["taskId"]:
            errors.append("workflow task_id does not match pointer")
        pilots = [sku for sku in cohort.get("skus", []) if sku.get("role") == "pilot"]
        if len(pilots) != 1:
            errors.append("cohort must contain exactly one role=pilot SKU")
        elif pilots[0].get("vendorCode") != pointer["pilotVendorCode"]:
            errors.append("pointer pilotVendorCode does not match cohort pilot")
    except ValueError as exc:
        errors.append(str(exc))

    if mode == EXACT_MODE:
        lock_value = pointer.get("masterLockPath")
        if pointer["generationAuthorized"]:
            if not lock_value:
                errors.append("generationAuthorized requires masterLockPath")
            else:
                try:
                    lock = read_json(resolved(root, lock_value))
                    if lock.get("sourceType") != "USER_COMPLETE_SET":
                        errors.append("master sourceType must be USER_COMPLETE_SET")
                    if not lock.get("approvedBy"):
                        errors.append("authorized master lock requires approvedBy")
                    if not lock.get("pages"):
                        errors.append("authorized master lock has no pages")
                except ValueError as exc:
                    errors.append(str(exc))
        elif pointer["status"] not in {"WAITING_FOR_USER_MASTER", "WAITING_MASTER_APPROVAL", "PAUSED"}:
            errors.append("unauthorized exact-master pointer has an invalid waiting status")
    elif mode == DESIGN_MODE:
        design_lock_value = pointer.get("designLockPath")
        if pointer.get("masterLockPath"):
            errors.append("design-synthesis pointer cannot carry masterLockPath")
        if pointer["generationAuthorized"]:
            if not design_lock_value:
                errors.append("generationAuthorized requires designLockPath")
            else:
                try:
                    lock = read_json(resolved(root, design_lock_value))
                    if lock.get("sourceType") != "DESIGN_BRIEF":
                        errors.append("design sourceType must be DESIGN_BRIEF")
                    if not lock.get("approvedBy"):
                        errors.append("authorized design lock requires approvedBy")
                    if not lock.get("briefPath") or not lock.get("contractPath"):
                        errors.append("authorized design lock requires briefPath and contractPath")
                    if int(lock.get("expectedPages", 0)) < 1:
                        errors.append("authorized design lock requires expectedPages >= 1")
                    if lock.get("designStage", FULL_PACK_STAGE) not in DESIGN_STAGES:
                        errors.append(f"designStage must be one of {sorted(DESIGN_STAGES)}")
                except ValueError as exc:
                    errors.append(str(exc))
        elif pointer["status"] not in {
            "WAITING_DESIGN_BRIEF",
            "WAITING_DESIGN_APPROVAL",
            "WAITING_DIRECTION_SELECTION",
            "PAUSED",
        }:
            errors.append("unauthorized design-synthesis pointer has an invalid waiting status")
        elif pointer["status"] == "WAITING_DIRECTION_SELECTION":
            if not design_lock_value:
                errors.append("WAITING_DIRECTION_SELECTION requires designLockPath")
            else:
                try:
                    lock = read_json(resolved(root, design_lock_value))
                    if lock.get("designStage") != DIRECTION_EXPLORATION_STAGE:
                        errors.append("WAITING_DIRECTION_SELECTION requires a DIRECTION_EXPLORATION lock")
                    if not lock.get("explorationReportPath") or not lock.get("candidateDirectory"):
                        errors.append("completed direction exploration requires report and candidate directory")
                except ValueError as exc:
                    errors.append(str(exc))
    return errors


def cmd_init(args: argparse.Namespace) -> int:
    root = workspace_root(args.workspace_root)
    target = pointer_path(root, args.pointer)
    if target.exists():
        existing = read_json(target)
        if existing.get("taskId") != args.task_id and not args.replace:
            raise SystemExit("Active new-master task exists; explicit --replace is required.")
    cohort = read_json(resolved(root, args.cohort))
    pilots = [sku for sku in cohort.get("skus", []) if sku.get("role") == "pilot"]
    if len(pilots) != 1:
        raise SystemExit("Cohort must contain exactly one role=pilot SKU.")
    if pilots[0].get("vendorCode") != args.pilot_vendor_code:
        raise SystemExit("Requested pilot does not match the cohort role=pilot SKU.")
    mode = args.mode
    payload = {
        "schemaVersion": 1,
        "taskId": args.task_id,
        "mode": mode,
        "generationMode": GENERATION_MODES[mode],
        "status": "WAITING_FOR_USER_MASTER" if mode == EXACT_MODE else "WAITING_DESIGN_BRIEF",
        "cohortPath": rel(root, resolved(root, args.cohort)),
        "workflowStatePath": rel(root, resolved(root, args.workflow_state)),
        "pilotVendorCode": args.pilot_vendor_code,
        "outputRoot": rel(root, resolved(root, args.output_root)),
        "masterLockPath": None,
        "designLockPath": None,
        "generationAuthorized": False,
        "liveUploadAuthorized": False,
        "updatedAt": now_iso(),
    }
    errors = validate_payload(root, payload)
    if errors:
        raise SystemExit("; ".join(errors))
    write_json(target, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_register_master(args: argparse.Namespace) -> int:
    root = workspace_root(args.workspace_root)
    target = pointer_path(root, args.pointer)
    pointer = read_json(target)
    if pointer.get("mode") != EXACT_MODE:
        raise SystemExit("register-master requires EXACT_MASTER_TEST mode; promote an approved design first.")
    master_dir = resolved(root, args.master_dir)
    if not master_dir.is_dir():
        raise SystemExit(f"Master directory does not exist: {master_dir}")
    images = sorted(
        [path for path in master_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES],
        key=natural_key,
    )
    if not images:
        raise SystemExit("Master directory contains no supported images.")
    pages = []
    dimensions = set()
    for index, image_path in enumerate(images, start=1):
        with Image.open(image_path) as image:
            size = [int(image.width), int(image.height)]
        dimensions.add(tuple(size))
        pages.append({"page": index, "path": rel(root, image_path), "dimensions": size, "sha256": digest(image_path)})
    if len(dimensions) != 1:
        raise SystemExit("All master pages must use identical canvas dimensions.")
    if args.expected_pages and len(pages) != args.expected_pages:
        raise SystemExit(f"Expected {args.expected_pages} pages, found {len(pages)}.")
    lock_path = resolved(
        root,
        args.lock_path or f"data/template_registry/phone_new_master/locks/{pointer['taskId']}_master_lock.json",
    )
    lock = {
        "schemaVersion": 1,
        "taskId": pointer["taskId"],
        "sourceType": "USER_COMPLETE_SET",
        "masterDirectory": rel(root, master_dir),
        "pageCount": len(pages),
        "canvasDimensions": list(next(iter(dimensions))),
        "pages": pages,
        "immutableContractPath": args.contract_path,
        "approvedBy": None,
        "registeredAt": now_iso(),
    }
    write_json(lock_path, lock)
    pointer.update(
        {
            "status": "WAITING_MASTER_APPROVAL",
            "masterLockPath": rel(root, lock_path),
            "generationAuthorized": False,
            "updatedAt": now_iso(),
        }
    )
    write_json(target, pointer)
    print(json.dumps(lock, ensure_ascii=False, indent=2))
    return 0


def cmd_approve_master(args: argparse.Namespace) -> int:
    root = workspace_root(args.workspace_root)
    target = pointer_path(root, args.pointer)
    pointer = read_json(target)
    if pointer.get("mode") != EXACT_MODE:
        raise SystemExit("approve-master requires EXACT_MASTER_TEST mode.")
    if not pointer.get("masterLockPath"):
        raise SystemExit("No registered master to approve.")
    lock_path = resolved(root, pointer["masterLockPath"])
    lock = read_json(lock_path)
    lock["approvedBy"] = args.approved_by
    lock["approvedAt"] = now_iso()
    write_json(lock_path, lock)
    pointer.update({"status": "MASTER_LOCKED", "generationAuthorized": True, "updatedAt": now_iso()})
    errors = validate_payload(root, pointer)
    if errors:
        raise SystemExit("; ".join(errors))
    write_json(target, pointer)
    print(json.dumps(pointer, ensure_ascii=False, indent=2))
    return 0


def cmd_register_design(args: argparse.Namespace) -> int:
    root = workspace_root(args.workspace_root)
    target = pointer_path(root, args.pointer)
    pointer = read_json(target)
    if pointer.get("mode") != DESIGN_MODE:
        raise SystemExit("register-design requires DESIGN_SYNTHESIS mode.")
    brief_path = resolved(root, args.brief_path)
    contract_path = resolved(root, args.contract_path)
    if not brief_path.is_file():
        raise SystemExit(f"Design brief does not exist: {brief_path}")
    if not contract_path.is_file():
        raise SystemExit(f"Design contract does not exist: {contract_path}")
    lock_path = resolved(
        root,
        args.lock_path or f"data/template_registry/phone_new_master/locks/{pointer['taskId']}_design_lock.json",
    )
    lock = {
        "schemaVersion": 1,
        "taskId": pointer["taskId"],
        "sourceType": "DESIGN_BRIEF",
        "templateId": args.template_id,
        "designStage": args.design_stage,
        "exampleCases": args.example_case,
        "briefPath": rel(root, brief_path),
        "contractPath": rel(root, contract_path),
        "expectedPages": args.expected_pages,
        "approvedBy": None,
        "registeredAt": now_iso(),
    }
    write_json(lock_path, lock)
    pointer.update(
        {
            "generationMode": DESIGN_GENERATION_MODE,
            "status": "WAITING_DESIGN_APPROVAL",
            "designLockPath": rel(root, lock_path),
            "generationAuthorized": False,
            "updatedAt": now_iso(),
        }
    )
    errors = validate_payload(root, pointer)
    if errors:
        raise SystemExit("; ".join(errors))
    write_json(target, pointer)
    print(json.dumps(lock, ensure_ascii=False, indent=2))
    return 0


def cmd_approve_design(args: argparse.Namespace) -> int:
    root = workspace_root(args.workspace_root)
    target = pointer_path(root, args.pointer)
    pointer = read_json(target)
    if pointer.get("mode") != DESIGN_MODE:
        raise SystemExit("approve-design requires DESIGN_SYNTHESIS mode.")
    if not pointer.get("designLockPath"):
        raise SystemExit("No registered design brief to approve.")
    lock_path = resolved(root, pointer["designLockPath"])
    lock = read_json(lock_path)
    lock["approvedBy"] = args.approved_by
    lock["approvedAt"] = now_iso()
    write_json(lock_path, lock)
    pointer.update({"status": "DESIGN_AUTHORIZED", "generationAuthorized": True, "updatedAt": now_iso()})
    errors = validate_payload(root, pointer)
    if errors:
        raise SystemExit("; ".join(errors))
    write_json(target, pointer)
    print(json.dumps(pointer, ensure_ascii=False, indent=2))
    return 0


def cmd_complete_exploration(args: argparse.Namespace) -> int:
    root = workspace_root(args.workspace_root)
    target = pointer_path(root, args.pointer)
    pointer = read_json(target)
    if pointer.get("mode") != DESIGN_MODE or pointer.get("status") != "DESIGN_AUTHORIZED":
        raise SystemExit("complete-exploration requires an authorized DESIGN_SYNTHESIS task.")
    if not pointer.get("designLockPath"):
        raise SystemExit("No authorized design lock exists.")
    lock_path = resolved(root, pointer["designLockPath"])
    lock = read_json(lock_path)
    if lock.get("designStage") != DIRECTION_EXPLORATION_STAGE:
        raise SystemExit("complete-exploration requires a DIRECTION_EXPLORATION lock.")
    report_path = resolved(root, args.report_path)
    candidate_dir = resolved(root, args.candidate_dir)
    if not report_path.is_file():
        raise SystemExit(f"Exploration report does not exist: {report_path}")
    if not candidate_dir.is_dir():
        raise SystemExit(f"Candidate directory does not exist: {candidate_dir}")
    images = sorted(
        [path for path in candidate_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES],
        key=natural_key,
    )
    expected = int(lock.get("expectedPages", 0))
    if len(images) != expected:
        raise SystemExit(f"Expected {expected} exploration candidates, found {len(images)}.")
    candidates = []
    dimensions = set()
    for image_path in images:
        with Image.open(image_path) as image:
            size = [int(image.width), int(image.height)]
        dimensions.add(tuple(size))
        candidates.append({"path": rel(root, image_path), "dimensions": size, "sha256": digest(image_path)})
    if len(dimensions) != 1:
        raise SystemExit("All exploration candidates must use identical canvas dimensions.")
    lock.update(
        {
            "explorationReportPath": rel(root, report_path),
            "candidateDirectory": rel(root, candidate_dir),
            "candidates": candidates,
            "explorationCompletedAt": now_iso(),
        }
    )
    write_json(lock_path, lock)
    pointer.update(
        {
            "status": "WAITING_DIRECTION_SELECTION",
            "generationAuthorized": False,
            "updatedAt": now_iso(),
        }
    )
    errors = validate_payload(root, pointer)
    if errors:
        raise SystemExit("; ".join(errors))
    write_json(target, pointer)
    print(json.dumps(pointer, ensure_ascii=False, indent=2))
    return 0


def cmd_select_direction(args: argparse.Namespace) -> int:
    root = workspace_root(args.workspace_root)
    target = pointer_path(root, args.pointer)
    pointer = read_json(target)
    if pointer.get("mode") != DESIGN_MODE or pointer.get("status") != "WAITING_DIRECTION_SELECTION":
        raise SystemExit("select-direction requires WAITING_DIRECTION_SELECTION.")
    candidate_path = resolved(root, args.candidate_path)
    if not candidate_path.is_file() or candidate_path.suffix.lower() not in IMAGE_SUFFIXES:
        raise SystemExit(f"Selected direction candidate does not exist: {candidate_path}")
    selection_path = resolved(
        root,
        args.selection_path
        or f"data/template_registry/phone_new_master/selections/{pointer['taskId']}_direction_selection.json",
    )
    selection = {
        "schemaVersion": 1,
        "taskId": pointer["taskId"],
        "directionId": args.direction_id,
        "candidatePath": rel(root, candidate_path),
        "selectedBy": args.selected_by,
        "selectedAt": now_iso(),
    }
    write_json(selection_path, selection)
    previous_lock = pointer.get("designLockPath")
    pointer.update(
        {
            "status": "WAITING_DESIGN_BRIEF",
            "lastExplorationLockPath": previous_lock,
            "designLockPath": None,
            "directionSelectionPath": rel(root, selection_path),
            "generationAuthorized": False,
            "updatedAt": now_iso(),
        }
    )
    errors = validate_payload(root, pointer)
    if errors:
        raise SystemExit("; ".join(errors))
    write_json(target, pointer)
    print(json.dumps(selection, ensure_ascii=False, indent=2))
    return 0


def cmd_promote_design(args: argparse.Namespace) -> int:
    root = workspace_root(args.workspace_root)
    target = pointer_path(root, args.pointer)
    pointer = read_json(target)
    if pointer.get("mode") != DESIGN_MODE or pointer.get("status") != "DESIGN_AUTHORIZED":
        raise SystemExit("promote-design requires an authorized DESIGN_SYNTHESIS task.")
    if not pointer.get("designLockPath"):
        raise SystemExit("promote-design requires an authorized design lock.")
    design_lock = read_json(resolved(root, pointer["designLockPath"]))
    if design_lock.get("designStage", FULL_PACK_STAGE) != FULL_PACK_STAGE:
        raise SystemExit("Direction exploration cannot be promoted; select a direction and approve a FULL_PACK contract.")
    master_dir = resolved(root, args.master_dir)
    if not master_dir.is_dir():
        raise SystemExit(f"Design output directory does not exist: {master_dir}")
    images = sorted(
        [path for path in master_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES],
        key=natural_key,
    )
    if len(images) != args.expected_pages:
        raise SystemExit(f"Expected {args.expected_pages} design pages, found {len(images)}.")
    pages = []
    dimensions = set()
    for index, image_path in enumerate(images, start=1):
        with Image.open(image_path) as image:
            size = [int(image.width), int(image.height)]
        dimensions.add(tuple(size))
        pages.append({"page": index, "path": rel(root, image_path), "dimensions": size, "sha256": digest(image_path)})
    if len(dimensions) != 1:
        raise SystemExit("All promoted design pages must use identical canvas dimensions.")
    lock_path = resolved(
        root,
        args.lock_path or f"data/template_registry/phone_new_master/locks/{pointer['taskId']}_master_lock.json",
    )
    lock = {
        "schemaVersion": 1,
        "taskId": pointer["taskId"],
        "sourceType": "USER_COMPLETE_SET",
        "origin": "APPROVED_DESIGN_SYNTHESIS",
        "masterDirectory": rel(root, master_dir),
        "pageCount": len(pages),
        "canvasDimensions": list(next(iter(dimensions))),
        "pages": pages,
        "immutableContractPath": args.contract_path,
        "approvedBy": None,
        "registeredAt": now_iso(),
    }
    write_json(lock_path, lock)
    pointer.update(
        {
            "mode": EXACT_MODE,
            "generationMode": EXACT_GENERATION_MODE,
            "status": "WAITING_MASTER_APPROVAL",
            "masterLockPath": rel(root, lock_path),
            "designLockPath": pointer.get("designLockPath"),
            "generationAuthorized": False,
            "updatedAt": now_iso(),
        }
    )
    errors = validate_payload(root, pointer)
    if errors:
        raise SystemExit("; ".join(errors))
    write_json(target, pointer)
    print(json.dumps(lock, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = workspace_root(args.workspace_root)
    target = pointer_path(root, args.pointer)
    pointer = read_json(target)
    design_stage = None
    if pointer.get("designLockPath"):
        try:
            design_stage = read_json(resolved(root, pointer["designLockPath"])).get("designStage", FULL_PACK_STAGE)
        except ValueError:
            design_stage = None
    result = {
        "pointer": rel(root, target),
        "taskId": pointer.get("taskId"),
        "mode": pointer.get("mode"),
        "generationMode": pointer.get("generationMode"),
        "status": pointer.get("status"),
        "pilotVendorCode": pointer.get("pilotVendorCode"),
        "generationAuthorized": pointer.get("generationAuthorized"),
        "liveUploadAuthorized": pointer.get("liveUploadAuthorized"),
        "masterLockPath": pointer.get("masterLockPath"),
        "designStage": design_stage,
        "directionSelectionPath": pointer.get("directionSelectionPath"),
        "outputRoot": pointer.get("outputRoot"),
        "updatedAt": pointer.get("updatedAt"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    root = workspace_root(args.workspace_root)
    target = pointer_path(root, args.pointer)
    pointer = read_json(target)
    errors = validate_payload(root, pointer)
    result = {"valid": not errors, "pointer": rel(root, target), "errors": errors}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--workspace-root")
    result.add_argument("--pointer")
    commands = result.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("--task-id", required=True)
    init.add_argument("--mode", choices=sorted(MODES), default=EXACT_MODE)
    init.add_argument("--cohort", required=True)
    init.add_argument("--workflow-state", required=True)
    init.add_argument("--pilot-vendor-code", required=True)
    init.add_argument("--output-root", required=True)
    init.add_argument("--replace", action="store_true")
    init.set_defaults(func=cmd_init)

    register = commands.add_parser("register-master")
    register.add_argument("--master-dir", required=True)
    register.add_argument("--expected-pages", type=int, default=8)
    register.add_argument("--contract-path", required=True)
    register.add_argument("--lock-path")
    register.set_defaults(func=cmd_register_master)

    approve = commands.add_parser("approve-master")
    approve.add_argument("--approved-by", required=True)
    approve.set_defaults(func=cmd_approve_master)

    register_design = commands.add_parser("register-design")
    register_design.add_argument("--brief-path", required=True)
    register_design.add_argument("--contract-path", required=True)
    register_design.add_argument("--template-id", required=True)
    register_design.add_argument("--example-case", action="append", default=[])
    register_design.add_argument("--expected-pages", type=int, default=8)
    register_design.add_argument("--design-stage", choices=sorted(DESIGN_STAGES), default=FULL_PACK_STAGE)
    register_design.add_argument("--lock-path")
    register_design.set_defaults(func=cmd_register_design)

    approve_design = commands.add_parser("approve-design")
    approve_design.add_argument("--approved-by", required=True)
    approve_design.set_defaults(func=cmd_approve_design)

    complete_exploration = commands.add_parser("complete-exploration")
    complete_exploration.add_argument("--candidate-dir", required=True)
    complete_exploration.add_argument("--report-path", required=True)
    complete_exploration.set_defaults(func=cmd_complete_exploration)

    select_direction = commands.add_parser("select-direction")
    select_direction.add_argument("--direction-id", required=True)
    select_direction.add_argument("--candidate-path", required=True)
    select_direction.add_argument("--selected-by", required=True)
    select_direction.add_argument("--selection-path")
    select_direction.set_defaults(func=cmd_select_direction)

    promote_design = commands.add_parser("promote-design")
    promote_design.add_argument("--master-dir", required=True)
    promote_design.add_argument("--expected-pages", type=int, default=8)
    promote_design.add_argument("--contract-path", required=True)
    promote_design.add_argument("--lock-path")
    promote_design.set_defaults(func=cmd_promote_design)

    status = commands.add_parser("status")
    status.set_defaults(func=cmd_status)
    validate = commands.add_parser("validate")
    validate.set_defaults(func=cmd_validate)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except ValueError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
