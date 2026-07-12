from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .paths import CategoryPaths, ensure_category_dirs
from .sku_alias import canonical_sku


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
DOC_EXTS = {".txt", ".md", ".docx", ".pptx", ".xlsx", ".xlsm"}
ARCHIVE_EXTS = {".zip"}
IGNORED_SOURCE_PREFIXES = ("~$",)


@dataclass(frozen=True)
class ScanResult:
    manifest: dict[str, Any]
    asset_index: dict[str, Any]
    product_facts: dict[str, Any]
    changes: dict[str, list[dict[str, Any]]]


def sync_knowledge_base(paths: CategoryPaths) -> ScanResult:
    ensure_category_dirs(paths)
    previous = _read_json(paths.kb / "source_manifest.json", default={"files": []})
    manifest = _build_manifest(paths.source)
    changes = _diff_manifest(previous, manifest)
    asset_index = _build_asset_index(manifest)
    product_facts = _extract_product_facts(paths.source, manifest)
    _apply_manual_overrides(product_facts, paths.kb / "manual_overrides.json")
    missing = _missing_facts_markdown(product_facts)

    _write_json(paths.kb / "source_manifest.json", manifest)
    _write_json(paths.kb / "asset_index.json", asset_index)
    _write_json(paths.kb / "product_facts.json", product_facts)
    (paths.kb / "missing_facts.md").write_text(missing, encoding="utf-8")
    _append_change_log(paths.kb / "change_log.md", changes)
    return ScanResult(manifest, asset_index, product_facts, changes)


def _apply_manual_overrides(product_facts: dict[str, Any], path: Path) -> None:
    if not path.exists():
        return
    overrides = _read_json(path, default={})
    for sku, override in (overrides.get("products") or {}).items():
        product = _find_product_for_override(product_facts, sku)
        if product is None:
            product_facts.setdefault("products", {})[sku] = {
                "sku": sku,
                "sourceFolder": override.get("sourceFolder", sku),
                "images": [],
                "fields": {},
                "missingRequired": [],
            }
            product = product_facts["products"][sku]
        product.setdefault("fields", {}).update(override.get("fields") or {})
        product.setdefault("pricing", {}).update(override.get("pricing") or {})
        if override.get("notes"):
            product.setdefault("manualNotes", []).extend(override["notes"])
        product.setdefault("sources", []).append({"field": "manualOverrides", "path": str(path)})


def _find_product_for_override(product_facts: dict[str, Any], sku: str) -> dict[str, Any] | None:
    wanted = sku.upper()
    for key, product in product_facts.get("products", {}).items():
        if key.upper() == wanted or str(product.get("sku", "")).upper() == wanted:
            return product
    return None


def _build_manifest(source: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    if source.exists():
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            if path.name.startswith(IGNORED_SOURCE_PREFIXES):
                continue
            stat = path.stat()
            rel = path.relative_to(source).as_posix()
            files.append(
                {
                    "path": rel,
                    "absolutePath": str(path),
                    "extension": path.suffix.lower(),
                    "bytes": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    "sha256": _sha256(path),
                    "kind": _kind(path.suffix.lower()),
                }
            )
    return {
        "sourceRoot": str(source),
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "files": files,
    }


def _build_asset_index(manifest: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in manifest.get("files", []):
        rel = Path(item["path"])
        group = rel.parts[0] if len(rel.parts) > 1 else "_root"
        groups.setdefault(group, []).append(item)
    return {
        "generatedAt": manifest["generatedAt"],
        "groups": groups,
        "counts": {
            "files": len(manifest.get("files", [])),
            "images": sum(1 for f in manifest.get("files", []) if f.get("kind") == "image"),
            "documents": sum(1 for f in manifest.get("files", []) if f.get("kind") == "document"),
            "archives": sum(1 for f in manifest.get("files", []) if f.get("kind") == "archive"),
        },
    }


def _extract_product_facts(source: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "generatedAt": manifest["generatedAt"],
        "products": {},
        "sourceNotes": [],
        "conflicts": [],
    }
    sku_names = _candidate_skus(manifest)
    for sku in sku_names:
        facts["products"][sku] = {
            "sku": _canonical_sku(sku),
            "sourceFolder": sku,
            "images": [f["path"] for f in manifest.get("files", []) if f["path"].startswith(f"{sku}/") and f["kind"] == "image"],
            "fields": {},
            "missingRequired": [],
        }

    for item in manifest.get("files", []):
        ext = item.get("extension")
        path = source / item["path"]
        if ext in {".txt", ".md"}:
            facts["sourceNotes"].append({"path": item["path"], "text": _read_text_guess(path)})
        elif ext in {".docx", ".pptx"}:
            text = _extract_office_text(path)
            if text:
                facts["sourceNotes"].append({"path": item["path"], "text": text[:6000]})
        elif ext in {".xlsx", ".xlsm"}:
            facts["sourceNotes"].append({"path": item["path"], "note": "Workbook detected; run product review to inspect SKU rows if needed."})

    _infer_prices_from_notes(facts)
    return facts


def _candidate_skus(manifest: dict[str, Any]) -> list[str]:
    names: set[str] = set()
    for item in manifest.get("files", []):
        parts = Path(item["path"]).parts
        if len(parts) > 1 and item.get("kind") == "image":
            names.add(parts[0])
    return sorted(names)


def _canonical_sku(name: str) -> str:
    return canonical_sku(name)


def _infer_prices_from_notes(facts: dict[str, Any]) -> None:
    for note in facts.get("sourceNotes", []):
        text = note.get("text", "")
        for sku, price in re.findall(r"([A-Za-z]{1,4}\d{2,4}[A-Za-z]?)\s*[-:：—]+\s*(\d+(?:\.\d+)?)", text):
            canonical = _canonical_sku(sku.upper())
            for product in facts["products"].values():
                if product["sku"].upper() == canonical.upper() or product["sourceFolder"].upper() == sku.upper():
                    product.setdefault("pricing", {})["plannedSalePriceRub"] = float(price)
                    product.setdefault("sources", []).append({"field": "plannedSalePriceRub", "path": note.get("path")})


def _missing_facts_markdown(product_facts: dict[str, Any]) -> str:
    required = [
        "brand",
        "title",
        "description",
        "model",
        "color",
        "specs",
        "packageContents",
        "material",
        "productDimensions",
        "packageDimensions",
        "certifications",
        "warehouse",
        "barcode",
        "wbSubject",
        "wbCharacteristics",
        "cost",
        "freight",
        "commissionRate",
        "targetNetMarginRate",
    ]
    lines = ["# Missing Facts", ""]
    for name, product in product_facts.get("products", {}).items():
        fields = product.get("fields", {})
        pricing = product.get("pricing", {})
        missing = [field for field in required if field not in fields and field not in pricing]
        product["missingRequired"] = missing
        lines.append(f"## {product.get('sku', name)}")
        if missing:
            for field in missing:
                lines.append(f"- {field}")
        else:
            lines.append("- No required gaps detected.")
        lines.append("")
    return "\n".join(lines)


def _diff_manifest(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    old = {item["path"]: item for item in previous.get("files", [])}
    new = {item["path"]: item for item in current.get("files", [])}
    added = [new[path] for path in sorted(new.keys() - old.keys())]
    removed = [old[path] for path in sorted(old.keys() - new.keys())]
    changed = [new[path] for path in sorted(new.keys() & old.keys()) if new[path].get("sha256") != old[path].get("sha256")]
    return {"added": added, "changed": changed, "removed": removed}


def _append_change_log(path: Path, changes: dict[str, list[dict[str, Any]]]) -> None:
    total = sum(len(v) for v in changes.values())
    if not path.exists():
        path.write_text("# Knowledge Base Change Log\n\n", encoding="utf-8")
    lines = [f"## {datetime.now().isoformat(timespec='seconds')}", "", f"- Total changes: {total}"]
    for key in ("added", "changed", "removed"):
        lines.append(f"- {key}: {len(changes[key])}")
    lines.append("")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _kind(ext: str) -> str:
    if ext in IMAGE_EXTS:
        return "image"
    if ext in DOC_EXTS:
        return "document"
    if ext in ARCHIVE_EXTS:
        return "archive"
    return "other"


def _read_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_text_guess(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_office_text(path: Path) -> str:
    if not zipfile.is_zipfile(path):
        return ""
    chunks: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if not name.endswith(".xml"):
                continue
            if not (name.startswith("word/") or name.startswith("ppt/slides/")):
                continue
            try:
                root = ElementTree.fromstring(zf.read(name))
            except ElementTree.ParseError:
                continue
            for elem in root.iter():
                if elem.text and elem.text.strip():
                    chunks.append(elem.text.strip())
    return "\n".join(chunks)
