from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from .client import WbClient
from .endpoints import ApiDomain
from .paths import CategoryPaths
from .sku_alias import canonical_sku


def validate_listing(paths: CategoryPaths, sku: str) -> dict[str, Any]:
    sku = canonical_sku(sku, category=paths.category)
    sku_listing = paths.listing / sku
    issues: list[str] = []
    manifest_path = sku_listing / "listing_manifest.json"
    pricing_path = sku_listing / "pricing_review.json"
    detail_path = sku_listing / "product_detail_review.json"
    images_dir = sku_listing / "images"

    manifest = _read_json(manifest_path, default=None)
    pricing = _read_json(pricing_path, default=None)
    detail = _read_json(detail_path, default=None)
    if manifest is None:
        issues.append("Missing listing_manifest.json")
    if pricing is None:
        issues.append("Missing pricing_review.json")
    elif pricing.get("status") != "ready" or not pricing.get("result", {}).get("passesGate"):
        issues.append("Pricing gate has not passed")
    if detail is None:
        issues.append("Missing product_detail_review.json")
    elif detail.get("missingRequiredFields"):
        issues.append("Product detail review still has missing fields")
    if not images_dir.exists() or not any(path.is_file() for path in images_dir.iterdir()):
        issues.append("No confirmed listing images found")

    report = {
        "sku": sku,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "status": "ready" if not issues else "blocked",
        "issues": issues,
        "paths": {
            "listing": str(sku_listing),
            "manifest": str(manifest_path),
            "pricing": str(pricing_path),
            "detail": str(detail_path),
            "images": str(images_dir),
        },
    }
    sku_listing.mkdir(parents=True, exist_ok=True)
    (sku_listing / "listing_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def publish_listing(paths: CategoryPaths, sku: str, *, dry_run: bool = True, confirm_live: bool = False, client: WbClient | None = None) -> dict[str, Any]:
    sku = canonical_sku(sku, category=paths.category)
    validation = validate_listing(paths, sku)
    if validation["status"] != "ready":
        result = {"sku": sku, "status": "blocked", "validation": validation, "live": False}
        _audit(paths, sku, "publish", result)
        return result
    if dry_run or not confirm_live:
        result = {"sku": sku, "status": "dry_run", "validation": validation, "live": False}
        _audit(paths, sku, "publish", result)
        return result
    if client is None:
        raise ValueError("client is required for live publish")
    listing_dir = paths.listing / sku
    manifest = _read_json(listing_dir / "listing_manifest.json", default={})
    payload = build_cards_upload_payload(manifest)
    (listing_dir / "wb_cards_upload_payload.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    response = client.request(
        "POST",
        f"{ApiDomain.CONTENT}/content/v2/cards/upload",
        body=payload,
    )
    result = {
        "sku": sku,
        "status": "submitted" if response.ok else "wb_error",
        "validation": validation,
        "live": response.ok,
        "operation": "content.v2.cards.upload",
        "requestPath": str(listing_dir / "wb_cards_upload_payload.json"),
        "response": {
            "status": response.status,
            "ok": response.ok,
            "data": response.data,
        },
        "nextSteps": [
            "Check WB card errors and card list for the created nmID.",
            "Upload media after nmID is available.",
            "Set price/discount after nmID is available.",
            "Set stock only after final human confirmation.",
        ],
    }
    _audit(paths, sku, "publish", result)
    return result


def build_cards_upload_payload(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    product = manifest.get("product") or {}
    pricing = manifest.get("pricing") or {}
    subject = product.get("wbSubject") or {}
    subject_id = subject.get("subjectID")
    if not subject_id:
        raise ValueError("listing_manifest.json is missing product.wbSubject.subjectID")

    vendor_code = manifest.get("sku") or product.get("model")
    barcode = product.get("barcode")
    if not vendor_code:
        raise ValueError("listing_manifest.json is missing sku/vendorCode")
    if not barcode:
        raise ValueError("listing_manifest.json is missing product.barcode")

    dimensions = _parse_dimensions_cm(product.get("packageDimensions"))
    package_weight = _package_weight_kg(product)
    variant: dict[str, Any] = {
        "vendorCode": str(vendor_code),
        "title": _required_text(product, "titleRu", "title"),
        "description": _required_text(product, "description"),
        "brand": _required_text(product, "brand"),
        "dimensions": {
            "length": dimensions["length"],
            "width": dimensions["width"],
            "height": dimensions["height"],
            "weightBrutto": package_weight,
        },
        "sizes": [
            {
                "techSize": "0",
                "wbSize": "",
                "price": _price_rub(pricing),
                "skus": [str(barcode)],
            }
        ],
    }
    characteristics = _characteristics_list(product.get("wbCharacteristics") or {})
    if characteristics:
        variant["characteristics"] = characteristics
    return [{"subjectID": int(subject_id), "variants": [variant]}]


def _required_text(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(f"listing_manifest.json is missing required text field: {'/'.join(keys)}")


def _parse_dimensions_cm(value: Any) -> dict[str, float]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("listing_manifest.json is missing product.packageDimensions")
    normalized = value.lower().replace("×", "x").replace("*", "x")
    parts = [part.strip().replace(",", ".") for part in normalized.replace("cm", "").split("x")]
    numbers = [float(part) for part in parts if part]
    if len(numbers) != 3:
        raise ValueError(f"Cannot parse packageDimensions as L x W x H cm: {value}")
    return {"length": math.ceil(numbers[0]), "width": math.ceil(numbers[1]), "height": math.ceil(numbers[2])}


def _package_weight_kg(product: dict[str, Any]) -> float:
    value = product.get("packageWeightKg")
    if value in (None, ""):
        specs = product.get("specs") if isinstance(product.get("specs"), dict) else {}
        value = specs.get("packageWeightKg")
    if value in (None, ""):
        raise ValueError("listing_manifest.json is missing product.packageWeightKg")
    return round(float(value), 3)


def _characteristics_list(values: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for key, value in values.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(key, str) and key.isdigit():
            result.append({"id": int(key), "value": value})
        elif isinstance(value, dict) and "id" in value and "value" in value:
            result.append({"id": int(value["id"]), "value": value["value"]})
    return result


def _price_rub(pricing: dict[str, Any]) -> int:
    value = pricing.get("plannedSalePriceRub")
    if value in (None, ""):
        raise ValueError("listing_manifest.json is missing pricing.plannedSalePriceRub")
    return int(round(float(value)))


def _audit(paths: CategoryPaths, sku: str, action: str, payload: dict[str, Any]) -> None:
    folder = paths.listing / sku
    folder.mkdir(parents=True, exist_ok=True)
    event = {"time": datetime.now().isoformat(timespec="seconds"), "action": action, "payload": payload}
    with (folder / "audit_log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _read_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))
