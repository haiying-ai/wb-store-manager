from __future__ import annotations

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


ANCHOR_NM_ID = "1308234785"
EXPECTED_VENDOR_CODE = "IPHONE 15-256GB-BLACK-BV5"
VERSION = "v2_20260804_iphone15_256gb_black"
ROOT = Path("E:/CodexWorks/WB/data/template_registry/phone_v2")


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_anchor() -> dict:
    settings = load_settings()
    client = WbClient(settings.token, verify_tls=settings.verify_tls)
    card = next(
        (item for item in fetch_cards(client) if str(item.get("nmID")) == ANCHOR_NM_ID),
        None,
    )
    if card is None:
        raise RuntimeError(f"Anchor nmID {ANCHOR_NM_ID} is not available")
    if card.get("vendorCode") != EXPECTED_VENDOR_CODE:
        raise RuntimeError(
            f"Anchor vendorCode mismatch: {card.get('vendorCode')} != {EXPECTED_VENDOR_CODE}"
        )
    photos = card.get("photos") or []
    if len(photos) != 8:
        raise RuntimeError(f"Anchor photo count must be 8; got {len(photos)}")
    return card


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "wb-store-manager/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def main() -> int:
    card = read_anchor()
    version_dir = ROOT / "versions" / VERSION
    image_dir = version_dir / "images"
    if version_dir.exists():
        raise RuntimeError(f"Refusing to overwrite immutable version: {version_dir}")
    image_dir.mkdir(parents=True, exist_ok=False)

    entries: list[dict] = []
    try:
        for index, photo in enumerate(card["photos"], start=1):
            url = photo.get("big")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise RuntimeError(f"Image {index} has no valid original URL")
            payload = download(url)
            with Image.open(BytesIO(payload)) as image:
                width, height = image.size
                image_format = image.format
            if width < 600 or height < 800:
                raise RuntimeError(f"Image {index} is unexpectedly small: {width}x{height}")
            filename = f"{index:02d}.webp"
            (image_dir / filename).write_bytes(payload)
            entries.append(
                {
                    "position": index,
                    "file": f"versions/{VERSION}/images/{filename}",
                    "sourceUrl": url,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                    "width": width,
                    "height": height,
                    "format": image_format,
                }
            )
    except Exception:
        # Preserve no partial template as an active or reusable snapshot.
        for child in sorted(version_dir.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        version_dir.rmdir()
        raise

    manifest = {
        "schemaVersion": 1,
        "templateFamily": "phone_v2",
        "version": VERSION,
        "status": "active",
        "approvedBy": "user-confirmed V2 template conversation",
        "approvedAt": "2026-08-04",
        "capturedAt": utc_now(),
        "templateSource": "live_anchor_snapshot",
        "anchors": [
            {
                "nmID": int(ANCHOR_NM_ID),
                "vendorCode": card["vendorCode"],
                "title": card.get("title"),
                "photoCount": len(card["photos"]),
            }
        ],
        "imageCount": len(entries),
        "images": entries,
        "visualReview": {
            "status": "approved_reference",
            "basis": "User-confirmed live V2 iPhone 15 256GB black template",
        },
        "fallbackPolicy": "Use this exact snapshot only if every live anchor is unavailable or incomplete; report templateSource=fallback_snapshot.",
        "upgradePolicy": "A differing live anchor creates a pending version and requires user approval before activation.",
    }
    (version_dir / "template_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    current_dir = ROOT / "current"
    current_dir.mkdir(parents=True, exist_ok=True)
    (current_dir / "template_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "ACTIVE_SNAPSHOT_CREATED",
                "version": VERSION,
                "anchorNmID": ANCHOR_NM_ID,
                "vendorCode": card["vendorCode"],
                "imageCount": len(entries),
                "root": str(ROOT),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
