from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .kb import IMAGE_EXTS
from .paths import CategoryPaths, timestamped_batch_dir
from .sku_alias import canonical_sku


PROMPT_TYPES = ["效果图", "功能卖点图", "对比图", "拆分构成图", "规格参数图", "使用场景图", "包装/配件图"]


def create_main_image_workspace(paths: CategoryPaths, sku: str, *, stamp: str | None = None) -> Path:
    sku = canonical_sku(sku, category=paths.category)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    batch = timestamped_batch_dir(paths, sku, stamp)
    for folder in ("main/raw", "main/preview", "main/final", "external_inbox"):
        (batch / folder).mkdir(parents=True, exist_ok=True)
    review = batch / "main" / "main_image_review.md"
    review.write_text(
        f"# Main Image Review - {sku}\n\nStatus: pending\n\nPut candidate main images in `main/raw`, previews in `main/preview`, and confirmed files in `main/final`.\n",
        encoding="utf-8",
    )
    return batch


def create_image_prompts(paths: CategoryPaths, sku: str, *, batch: Path | None = None) -> Path:
    sku = canonical_sku(sku, category=paths.category)
    batch = batch or _latest_or_new_batch(paths, sku)
    prompts_dir = batch / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    prompts = [
        {
            "type": prompt_type,
            "status": "pending_user_confirmation",
            "prompt": f"为 {sku} 生成{prompt_type}：使用知识库中已确认的颜色、规格、材质、卖点和 WB 俄语电商风格；不得捏造未确认参数，不得使用未确认颜色。",
        }
        for prompt_type in PROMPT_TYPES
    ]
    (prompts_dir / "image_prompts.json").write_text(json.dumps({"sku": sku, "prompts": prompts}, ensure_ascii=False, indent=2), encoding="utf-8")
    (prompts_dir / "image_prompts.md").write_text(_prompts_markdown(sku, prompts), encoding="utf-8")
    (batch / "external_inbox").mkdir(parents=True, exist_ok=True)
    return batch


def inspect_external_inbox(paths: CategoryPaths, sku: str, *, batch: Path | None = None) -> Path:
    sku = canonical_sku(sku, category=paths.category)
    batch = batch or _latest_or_new_batch(paths, sku)
    inbox = batch / "external_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    files = []
    for path in sorted(inbox.iterdir()) if inbox.exists() else []:
        if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
            files.append({"path": str(path), "name": path.name, "bytes": path.stat().st_size, "status": "needs_visual_review"})
    report = {
        "sku": sku,
        "batch": str(batch),
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "imageCount": len(files),
        "images": files,
        "checks": ["dimensions", "clarity", "language", "sku_consistency", "main_image_compliance", "wb_fit"],
    }
    (batch / "external_inbox_review.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (batch / "external_inbox_review.md").write_text(_inbox_markdown(report), encoding="utf-8")
    return batch


def _latest_or_new_batch(paths: CategoryPaths, sku: str) -> Path:
    sku_dir = paths.generated / sku
    if sku_dir.exists():
        batches = sorted([p for p in sku_dir.iterdir() if p.is_dir()])
        if batches:
            return batches[-1]
    return create_main_image_workspace(paths, sku)


def _prompts_markdown(sku: str, prompts: list[dict[str, Any]]) -> str:
    lines = [f"# Image Prompts - {sku}", "", "Status: pending user confirmation", ""]
    for item in prompts:
        lines.append(f"## {item['type']}")
        lines.append(item["prompt"])
        lines.append("")
    return "\n".join(lines)


def _inbox_markdown(report: dict[str, Any]) -> str:
    lines = [f"# External Inbox Review - {report['sku']}", "", f"Images: {report['imageCount']}", ""]
    for item in report["images"]:
        lines.append(f"- {item['name']}: {item['status']}, {item['bytes']} bytes")
    lines.append("")
    return "\n".join(lines)
