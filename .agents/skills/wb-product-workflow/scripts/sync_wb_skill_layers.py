#!/usr/bin/env python3
"""Copy or verify the project WB skill against the user-level skill package."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


DEFAULT_PROJECT = Path(r"E:\CodexWorks\WB\.agents\skills\wb-product-workflow")
DEFAULT_USER = Path.home() / ".agents" / "skills" / "wb-product-workflow"
IGNORED_PARTS = {"__pycache__", ".pytest_cache"}


def files(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and not any(part in IGNORED_PARTS for part in path.parts)
    }


def digest(path: Path) -> str:
    value = hashlib.sha256()
    value.update(path.read_bytes())
    return value.hexdigest()


def compare(project: Path, user: Path) -> dict:
    project_files = files(project)
    user_files = files(user) if user.exists() else {}
    missing = sorted(set(project_files) - set(user_files))
    mismatched = sorted(
        name for name in set(project_files) & set(user_files) if digest(project_files[name]) != digest(user_files[name])
    )
    return {
        "project": str(project),
        "user": str(user),
        "projectFileCount": len(project_files),
        "missingFromUser": missing,
        "hashMismatches": mismatched,
        "userOnly": sorted(set(user_files) - set(project_files)),
        "equivalent": not missing and not mismatched,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "sync"))
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--user", type=Path, default=DEFAULT_USER)
    args = parser.parse_args()
    project = args.project.resolve()
    user = args.user.resolve()
    if args.command == "sync":
        user.mkdir(parents=True, exist_ok=True)
        for name, source in files(project).items():
            destination = user / Path(name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    result = compare(project, user)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["equivalent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
