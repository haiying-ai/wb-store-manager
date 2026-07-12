from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import CategoryPaths, timestamped_batch_dir
from .sku_alias import canonical_sku


REQUIRED_DETAIL_FIELDS = [
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
]


FIELD_LABEL_TRANSLATIONS = {
    "brand": ("品牌", "Бренд"),
    "title": ("商品名称", "Наименование"),
    "description": ("商品描述", "Описание"),
    "model": ("型号", "Модель"),
    "color": ("颜色", "Цвет"),
    "specs": ("规格参数", "Характеристики"),
    "packageContents": ("包装清单", "Комплектация"),
    "material": ("材质", "Материал"),
    "productDimensions": ("产品尺寸", "Размер товара"),
    "packageDimensions": ("包装尺寸", "Размер упаковки"),
    "certifications": ("认证", "Сертификаты"),
    "warehouse": ("仓库/发货仓", "Склад / место отгрузки"),
    "barcode": ("条码", "Штрихкод"),
    "wbSubject": ("WB类目", "Категория WB"),
    "wbCharacteristics": ("WB商品特性", "Характеристики WB"),
}


KNOWN_VALUE_TRANSLATIONS = {
    "white": "белый",
    "silver": "серебристый",
    "blue": "синий",
    "China": "Китай",
    "强脉冲光脱毛仪": "IPL фотоэпилятор",
    "Фотоэпилятор IPL": "Фотоэпилятор IPL",
    "Фотоэпиляторы": "Фотоэпиляторы",
    "IPL光子脱毛仪": "IPL фотоэпилятор",
    "Plug-in type": "питание от сети",
    "PC": "PC-пластик",
    "99W integrated lamp head": "встроенная лампа 99 Вт",
    "America / Europe / Britain / Japan": "США / Европа / Великобритания / Япония",
    "脱毛嫩肤一体灯头": "насадка для эпиляции и омоложения кожи",
    "可调节出光模式": "регулируемый режим вспышек",
    "3个档位": "3 уровня мощности",
    "3种模式": "3 режима",
    "皮肤表层": "поверхность кожи",
    "3个档位、3种模式": "3 уровня мощности, 3 режима",
    "手持操作方便": "удобное ручное использование",
    "定制灯管": "индивидуальная лампа",
    "24V 1.5A 37W": "24 В 1,5 А 37 Вт",
    "5.2 cm²": "5,2 см²",
    "183 x 63 x 38 mm": "183 x 63 x 38 мм",
    "22.5 x 22.5 x 6 cm": "22,5 x 22,5 x 6 см",
    "FCC": "FCC",
    "CE": "CE",
    "ETL": "ETL",
    "EMC": "EMC",
    "UKCA": "UKCA",
}


def create_product_review(paths: CategoryPaths, sku: str, *, stamp: str | None = None) -> Path:
    sku = canonical_sku(sku, category=paths.category)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    batch = timestamped_batch_dir(paths, sku, stamp)
    batch.mkdir(parents=True, exist_ok=True)
    facts = _read_json(paths.kb / "product_facts.json", default={"products": {}})
    product = _find_product(facts, sku)
    review = _build_review(sku, product)
    bilingual_mapping = _build_bilingual_mapping(review, product)
    review["bilingualMapping"] = bilingual_mapping
    (batch / "product_detail_review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    (batch / "product_detail_review.md").write_text(_review_markdown(review), encoding="utf-8")
    (batch / "bilingual_mapping.json").write_text(json.dumps(bilingual_mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    (batch / "bilingual_mapping.md").write_text(_bilingual_mapping_markdown(bilingual_mapping), encoding="utf-8")
    (batch / "missing_facts.md").write_text(_missing_markdown(review), encoding="utf-8")
    return batch


def _build_review(sku: str, product: dict[str, Any]) -> dict[str, Any]:
    fields = product.get("fields", {})
    pricing = product.get("pricing", {})
    known = {
        "sku": sku,
        "sourceFolder": product.get("sourceFolder"),
        "imageCount": len(product.get("images", [])),
        "plannedSalePriceRub": pricing.get("plannedSalePriceRub"),
    }
    missing = [field for field in REQUIRED_DETAIL_FIELDS if not fields.get(field)]
    return {
        "sku": sku,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "status": "blocked" if missing else "ready_for_pricing",
        "knownFacts": known,
        "detailFields": fields,
        "missingRequiredFields": missing,
        "humanSupplementTemplate": {field: "" for field in missing},
    }


def _build_bilingual_mapping(review: dict[str, Any], product: dict[str, Any]) -> dict[str, Any]:
    fields = review.get("detailFields", {})
    image_text_by_path = fields.get("imageTextMappings") or {}
    field_mappings = []
    for field in REQUIRED_DETAIL_FIELDS:
        label_zh, label_ru = FIELD_LABEL_TRANSLATIONS[field]
        value = fields.get(field, "")
        field_mappings.append(
            {
                "field": field,
                "labelZh": label_zh,
                "labelRu": label_ru,
                "value": value,
                "valueZh": _value_to_text(value),
                "valueRu": _translate_known_value(value),
                "status": "prefilled" if value not in ("", None, {}, []) else "needs_confirmation",
            }
        )

    image_mappings = []
    for image in product.get("images", []):
        extracted = image_text_by_path.get(image, {})
        image_mappings.append(
            {
                "imagePath": image,
                "imageTextZh": extracted.get("imageTextZh", ""),
                "imageTextRu": extracted.get("imageTextRu", ""),
                "status": extracted.get("status", "needs_image_text_review"),
                "note": extracted.get("note", "逐图读取全部可见文字，并在上线前确认中文/俄文对应关系。"),
            }
        )
    return {
        "sku": review["sku"],
        "generatedAt": review["generatedAt"],
        "requirement": "所有商品确认包必须包含字段中俄映射和图片可见文字中俄映射。",
        "fieldMappings": field_mappings,
        "imageTextMappings": image_mappings,
    }


def _value_to_text(value: Any) -> str:
    if value in ("", None):
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _translate_known_value(value: Any) -> str:
    if isinstance(value, str):
        return KNOWN_VALUE_TRANSLATIONS.get(value, "")
    if isinstance(value, list):
        translated = [KNOWN_VALUE_TRANSLATIONS.get(str(item), "") for item in value]
        return "; ".join(item for item in translated if item)
    if isinstance(value, dict):
        translated: dict[str, Any] = {}
        for key, item in value.items():
            translated[key] = _translate_known_value(item)
        return json.dumps(translated, ensure_ascii=False)
    return ""


def _review_markdown(review: dict[str, Any]) -> str:
    lines = [f"# Product Detail Review - {review['sku']}", "", f"Status: {review['status']}", ""]
    lines.append("## Known Facts")
    for key, value in review["knownFacts"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Missing Required Fields"])
    if review["missingRequiredFields"]:
        for field in review["missingRequiredFields"]:
            lines.append(f"- {field}")
    else:
        lines.append("- None")
    lines.extend(["", "## Human Supplement Template"])
    for field in review["humanSupplementTemplate"]:
        lines.append(f"- {field}: ")
    lines.extend(["", "## Bilingual Mapping"])
    lines.append("- Field mapping: `bilingual_mapping.json` / `bilingual_mapping.md`")
    lines.append("- Image text mapping: included per source image; every visible phrase must be confirmed before listing.")
    return "\n".join(lines) + "\n"


def _bilingual_mapping_markdown(mapping: dict[str, Any]) -> str:
    lines = [f"# 中俄双语映射 / Bilingual Mapping - {mapping['sku']}", ""]
    lines.append("所有商品确认包必须包含字段中俄映射，并逐图记录图片内可见文字的中文/俄文对应关系。")
    lines.append("Все пакеты подтверждения товара должны содержать китайско-русскую таблицу полей и текстов на изображениях.")
    lines.extend(["", "## 字段映射 / Field Mapping", ""])
    lines.append("| 字段 | 中文字段名 | Русское название | 原始值 | 中文确认值 | Русское значение | 状态 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for item in mapping["fieldMappings"]:
        value = _value_to_text(item.get("value", "")).replace("\n", " ")
        value_zh = item.get("valueZh", "").replace("\n", " ")
        value_ru = item.get("valueRu", "").replace("\n", " ")
        lines.append(f"| {item['field']} | {item['labelZh']} | {item['labelRu']} | {value} | {value_zh} | {value_ru} | {item['status']} |")
    lines.extend(["", "## 图片文字映射 / Image Text Mapping", ""])
    if not mapping["imageTextMappings"]:
        lines.append("- No source images detected.")
    for item in mapping["imageTextMappings"]:
        zh = item.get("imageTextZh") or "待逐图识别"
        ru = item.get("imageTextRu") or "待翻译确认"
        lines.append(f"- {item['imagePath']}: 中文文字 = `{zh}`, 俄文文字 = `{ru}`, 状态 = {item['status']}")
    return "\n".join(lines) + "\n"


def _missing_markdown(review: dict[str, Any]) -> str:
    lines = [f"# Missing Facts - {review['sku']}", ""]
    if not review["missingRequiredFields"]:
        return "\n".join(lines + ["No required gaps detected.", ""])
    lines.append("Fill these before moving the SKU to online listing materials:")
    lines.extend(f"- {field}" for field in review["missingRequiredFields"])
    lines.append("")
    return "\n".join(lines)


def _find_product(facts: dict[str, Any], sku: str) -> dict[str, Any]:
    wanted = sku.upper()
    for key, product in facts.get("products", {}).items():
        if key.upper() == wanted or str(product.get("sku", "")).upper() == wanted:
            return product
    return {"sku": sku, "fields": {}, "pricing": {}, "images": []}


def _read_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))
