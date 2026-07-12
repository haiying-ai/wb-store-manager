from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .paths import CategoryPaths
from .reporting import build_summary


def write_daily_report(paths: CategoryPaths, *, day: date | None = None, snapshot: dict[str, Any] | None = None) -> Path:
    day = day or date.today()
    out = paths.ops / day.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot or {"generatedAt": datetime.now().isoformat(timespec="seconds"), "note": "No live WB snapshot supplied."}
    (out / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_metrics_csv(out / "metrics.csv", snapshot)
    (out / "daily_report.md").write_text(_daily_markdown(paths.category, day, snapshot), encoding="utf-8")
    return out


def _write_metrics_csv(path: Path, snapshot: dict[str, Any]) -> None:
    rows = [
        ("product_cards", _count(snapshot.get("content", {}), "cards")),
        ("orders", _count(snapshot.get("statistics", {}), "orders")),
        ("sales", _count(snapshot.get("statistics", {}), "sales")),
        ("stocks", _count(snapshot.get("statistics", {}), "stocks")),
        ("unanswered_feedbacks", _count(snapshot.get("feedbacks", {}), "feedbacksUnanswered")),
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        writer.writerows(rows)


def _daily_markdown(category: str, day: date, snapshot: dict[str, Any]) -> str:
    lines = [f"# WB Daily Report - {category} - {day.isoformat()}", ""]
    if "content" in snapshot:
        lines.append("## Store Summary")
        lines.append("```")
        lines.append(build_summary(snapshot))
        lines.append("```")
    else:
        lines.append("No live WB snapshot supplied. Run `python -m wb_manager snapshot` with WB_API_TOKEN, then regenerate if live metrics are needed.")
    lines.extend(["", "## Profit Watch", "- Compare active prices against each SKU's minimum acceptable price before promotion changes.", "- Views/add-to-cart metrics depend on WB API permissions; missing data should be treated as unavailable, not zero.", ""])
    return "\n".join(lines)


def _count(parent: dict[str, Any], key: str) -> Any:
    value = parent.get(key, {})
    if isinstance(value, dict):
        return value.get("count", "")
    return ""
