from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wb_manager.client import WbClient
from wb_manager.config import load_settings
from tools.tag_zero_stock_cards import fetch_cards


ROOT = Path("E:/CodexWorks/WB/data/template_registry/phone_v1")


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture an immutable final V1 phone image-template snapshot from a live WB card."
    )
    parser.add_argument("--anchor-nmid", required=True)
    parser.add_argument("--vendor-code", required=True)
    parser.add_argument("--template-family", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--expected-count", required=True, type=int)
    parser.add_argument("--approved-by", required=True)
    return parser.parse_args()


def get_live_card(nm_id: str, vendor_code: str) -> dict:
    settings = load_settings()
    client = WbClient(settings.token, verify_tls=settings.verify_tls)
    card = next(
        (item for item in fetch_cards(client) if str(item.get("nmID")) == nm_id),
        None,
    )
    if card is None:
        raise RuntimeError(f"Anchor nmID {nm_id} is unavailable")
    if card.get("vendorCode") != vendor_code:
        raise RuntimeError(
            f"Anchor vendorCode mismatch: {card.get('vendorCode')} != {vendor_code}"
        )
    return card


def get_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "wb-store-manager/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def main() -> int:
    args = parse_args()
    card = get_live_card(args.anchor_nmid, args.vendor_code)
    photos = card.get("photos") or []
    if len(photos) != args.expected_count:
        raise RuntimeError(
            f"Expected {args.expected_count} photos, got {len(photos)} for {args.anchor_nmid}"
        )

    version_dir = ROOT / args.template_family / "versions" / args.version
    if version_dir.exists():
        raise RuntimeError(f"Refusing to overwrite immutable version: {version_dir}")
    image_dir = version_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=False)
    entries: list[dict] = []
    for position, photo in enumerate(photos, start=1):
        url = photo.get("big")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise RuntimeError(f"Position {position} lacks a valid source URL")
        payload = get_bytes(url)
        with Image.open(BytesIO(payload)) as image:
            width, height = image.size
            image_format = image.format
        if width < 600 or height < 800:
            raise RuntimeError(f"Position {position} is too small: {width}x{height}")
        filename = f"{position:02d}.webp"
        (image_dir / filename).write_bytes(payload)
        entries.append(
            {
                "position": position,
                "file": f"versions/{args.version}/images/{filename}",
                "sourceUrl": url,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
                "width": width,
                "height": height,
                "format": image_format,
            }
        )

    manifest = {
        "schemaVersion": 1,
        "templateFamily": args.template_family,
        "version": args.version,
        "status": "active",
        "approvedBy": args.approved_by,
        "capturedAt": now(),
        "templateSource": "live_anchor_snapshot",
        "anchors": [
            {
                "nmID": int(args.anchor_nmid),
                "vendorCode": card["vendorCode"],
                "title": card.get("title"),
                "photoCount": len(photos),
                "updatedAt": card.get("updatedAt"),
            }
        ],
        "imageCount": len(entries),
        "images": entries,
        "visualReview": {
            "status": "pending_manual_review",
            "required": ["full_size", "marketplace_thumbnail", "image_order"],
        },
        "fallbackPolicy": "Use only this exact approved family snapshot when the registered live anchor is unavailable or incomplete.",
        "upgradePolicy": "A changed live source creates a pending version and requires user approval before activation.",
    }
    (version_dir / "template_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    current_dir = ROOT / args.template_family / "current"
    current_dir.mkdir(parents=True, exist_ok=True)
    (current_dir / "template_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "SNAPSHOT_CREATED_PENDING_VISUAL_REVIEW",
                "templateFamily": args.template_family,
                "version": args.version,
                "anchorNmID": args.anchor_nmid,
                "imageCount": len(entries),
                "root": str(ROOT / args.template_family),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
