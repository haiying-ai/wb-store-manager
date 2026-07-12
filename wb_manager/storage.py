from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


SNAPSHOT_DIR = Path("data/snapshots")


def write_snapshot(data: dict[str, Any], *, prefix: str = "snapshot") -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = SNAPSHOT_DIR / f"{prefix}-{stamp}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def latest_snapshot() -> Path | None:
    if not SNAPSHOT_DIR.exists():
        return None
    files = sorted(SNAPSHOT_DIR.glob("snapshot-*.json"))
    return files[-1] if files else None


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
