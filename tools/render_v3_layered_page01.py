from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from statistics import median

from PIL import Image, ImageChops, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def row_background_color(
    image: Image.Image,
    y: int,
    zones: list[tuple[int, int]],
    background_kind: str,
) -> tuple[int, int, int]:
    pixels = []
    for x0, x1 in zones:
        pixels.extend(image.crop((x0, y, x1, y + 1)).getdata())
    if background_kind == "light":
        candidates = [p for p in pixels if min(p) > 140]
    elif background_kind == "purple":
        candidates = [p for p in pixels if p[2] > 80 and p[2] > p[0] + 20 and max(p) < 225]
    else:
        raise ValueError(f"Unknown background kind: {background_kind}")
    if not candidates:
        raise ValueError(f"Cannot sample {background_kind} background at row {y}")
    return tuple(round(median(p[channel] for p in candidates)) for channel in range(3))


def clear_with_row_colors(
    image: Image.Image,
    rect: list[int],
    sample_zones: list[tuple[int, int]],
    background_kind: str,
) -> None:
    draw = ImageDraw.Draw(image)
    x0, y0, x1, y1 = rect
    for y in range(y0, y1):
        draw.line(
            (x0, y, x1 - 1, y),
            fill=row_background_color(image, y, sample_zones, background_kind),
        )


def fit_font(spec: dict, text: str, target: list[int]) -> tuple[ImageFont.FreeTypeFont, int, tuple[int, int, int, int]]:
    font_path = Path(spec["path"])
    if not font_path.is_file():
        raise FileNotFoundError(f"Locked font is missing; fallback is forbidden: {font_path}")
    max_w = target[2] - target[0]
    max_h = target[3] - target[1]
    for size in range(int(spec["preferred_size"]), int(spec["minimum_size"]) - 1, -1):
        font = ImageFont.truetype(str(font_path), size)
        bbox = font.getbbox(text)
        if bbox[2] - bbox[0] <= max_w and bbox[3] - bbox[1] <= max_h:
            return font, size, bbox
    raise ValueError(f"BLOCKED_TEXT_FIT: {text!r} does not fit {target}")


def draw_centered_text(
    image: Image.Image,
    text: str,
    target: list[int],
    font_spec: dict,
) -> dict:
    font, size, bbox = fit_font(font_spec, text, target)
    glyph_w = bbox[2] - bbox[0]
    glyph_h = bbox[3] - bbox[1]
    cx = (target[0] + target[2]) / 2
    cy = (target[1] + target[3]) / 2
    x = round(cx - glyph_w / 2 - bbox[0])
    y = round(cy - glyph_h / 2 - bbox[1])
    ImageDraw.Draw(image).text((x, y), text, font=font, fill=tuple(font_spec["fill"]))
    actual = [x + bbox[0], y + bbox[1], x + bbox[2], y + bbox[3]]
    inside = actual[0] >= target[0] and actual[1] >= target[1] and actual[2] <= target[2] and actual[3] <= target[3]
    return {"font_size": size, "actual_bbox": actual, "target_bbox": target, "inside": inside}


def badge_row_color(reference: Image.Image, y: int, bbox: list[int]) -> tuple[int, int, int]:
    x0, _, x1, _ = bbox
    samples = list(reference.crop((x0, y, x1, y + 1)).getdata())
    purple = [p for p in samples if p[2] > 90 and p[0] < 130 and p[1] < 100]
    if not purple:
        raise ValueError(f"Cannot sample locked badge color at row {y}")
    return Counter(purple).most_common(1)[0][0]


def draw_fixed_badge(image: Image.Image, reference: Image.Image, badge: dict) -> None:
    bbox = badge["bbox"]
    x0, y0, x1, y1 = bbox
    layer = Image.new("RGB", (x1 - x0, y1 - y0))
    layer_draw = ImageDraw.Draw(layer)
    for y in range(y0, y1):
        layer_draw.line((0, y - y0, x1 - x0, y - y0), fill=badge_row_color(reference, y, bbox))
    mask = Image.new("L", layer.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, layer.width - 1, layer.height - 1), radius=badge["radius"], fill=255)
    image.paste(layer, (x0, y0), mask)


def allowed_mask(size: tuple[int, int], rects: list[list[int]]) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for rect in rects:
        draw.rectangle((rect[0], rect[1], rect[2] - 1, rect[3] - 1), fill=255)
    return mask


def diff_image(original: Image.Image, rebuilt: Image.Image) -> Image.Image:
    delta = ImageChops.difference(original, rebuilt).convert("RGB")
    boosted = delta.point(lambda v: min(255, v * 5))
    canvas = Image.new("RGB", original.size, "white")
    changed = delta.convert("L").point(lambda v: 255 if v else 0)
    red = Image.new("RGB", original.size, (235, 35, 70))
    marked = Image.blend(boosted, red, 0.28)
    canvas.paste(marked, mask=changed)
    return canvas


def render_case(case: dict, layout: dict, fonts: dict, master: Image.Image, out_dir: Path) -> dict:
    source_path = ROOT / case["source"]
    original = Image.open(source_path).convert("RGB")
    if list(original.size) != layout["canvas"]:
        raise ValueError(f"Unexpected source size for {case['vendor_code']}: {original.size}")
    rebuilt = original.copy()
    edit = layout["editable_regions"]

    clear_with_row_colors(rebuilt, edit["title_clear"], [(18, 120), (780, 882)], "light")
    clear_with_row_colors(rebuilt, edit["capacity_clear"], [(18, 150), (750, 882)], "light")
    draw_fixed_badge(rebuilt, master, layout["capacity_badge"])
    clear_with_row_colors(rebuilt, edit["status_text_clear"], [(18, 80), (820, 882)], "purple")

    metrics = {
        "title": draw_centered_text(rebuilt, case["title"], layout["text_targets"]["title"], fonts["title"]),
        "capacity": draw_centered_text(rebuilt, case["capacity"], layout["text_targets"]["capacity"], fonts["capacity"]),
        "status": draw_centered_text(rebuilt, layout["fixed_status"], layout["text_targets"]["status"], fonts["status"]),
    }

    allowed_rects = list(edit.values())
    allowed = allowed_mask(original.size, allowed_rects)
    delta = ImageChops.difference(original, rebuilt).convert("L")
    changed = delta.point(lambda v: 255 if v else 0)
    outside = ImageChops.multiply(changed, ImageChops.invert(allowed))
    changed_total = sum(1 for p in changed.getdata() if p)
    changed_outside = sum(1 for p in outside.getdata() if p)
    product_y = int(layout["product_lock_from_y"])
    product_delta = ImageChops.difference(
        original.crop((0, product_y, original.width, original.height)),
        rebuilt.crop((0, product_y, rebuilt.width, rebuilt.height)),
    ).convert("L")
    product_changed = sum(1 for p in product_delta.getdata() if p)

    slug = case["vendor_code"].lower().replace(" ", "_").replace("/", "_")
    case_dir = out_dir / slug
    case_dir.mkdir(parents=True, exist_ok=True)
    original.save(case_dir / "original.png")
    rebuilt.save(case_dir / "rebuilt.png")
    diff_image(original, rebuilt).save(case_dir / "difference.png")
    rebuilt.resize((240, 320), Image.Resampling.LANCZOS).save(case_dir / "preview_320.png")

    return {
        **case,
        "source_sha256": sha256(source_path),
        "rebuilt": str((case_dir / "rebuilt.png").relative_to(ROOT)).replace("\\", "/"),
        "difference": str((case_dir / "difference.png").relative_to(ROOT)).replace("\\", "/"),
        "changed_pixels_total": changed_total,
        "changed_pixels_outside_editable_regions": changed_outside,
        "changed_pixels_in_product_locked_region": product_changed,
        "text_metrics": metrics,
        "qa": "PASS" if changed_outside == 0 and product_changed == 0 and all(x["inside"] for x in metrics.values()) else "FAIL",
    }


def review_sheet(results: list[dict], out_dir: Path) -> None:
    tile_w, tile_h = 225, 300
    label_w, header_h = 290, 54
    columns = ["Original", "Rebuilt", "Difference", "320px preview"]
    sheet = Image.new("RGB", (label_w + tile_w * 4, header_h + tile_h * len(results)), "white")
    draw = ImageDraw.Draw(sheet)
    label_font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 18)
    small_font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 14)
    for i, name in enumerate(columns):
        draw.text((label_w + i * tile_w + 10, 17), name, font=label_font, fill="black")
    for row, item in enumerate(results):
        y = header_h + row * tile_h
        draw.rectangle((0, y, sheet.width - 1, y + tile_h - 1), outline=(210, 210, 210))
        draw.text((12, y + 20), item["vendor_code"], font=label_font, fill="black")
        draw.text((12, y + 52), item["title"], font=small_font, fill=(40, 40, 40))
        draw.text((12, y + 75), f"QA {item['qa']} | outside {item['changed_pixels_outside_editable_regions']} | product {item['changed_pixels_in_product_locked_region']}", font=small_font, fill=(0, 120, 40) if item["qa"] == "PASS" else (200, 0, 0))
        case_dir = ROOT / Path(item["rebuilt"]).parent
        paths = [case_dir / "original.png", case_dir / "rebuilt.png", case_dir / "difference.png", case_dir / "preview_320.png"]
        for col, path in enumerate(paths):
            im = Image.open(path).convert("RGB")
            if im.size != (tile_w, tile_h):
                im = im.resize((tile_w, tile_h), Image.Resampling.LANCZOS)
            sheet.paste(im, (label_w + col * tile_w, y))
    sheet.save(out_dir / "page01_five_case_review.png")


def master_comparison(results: list[dict], master_path: Path, out_dir: Path) -> None:
    item = next(x for x in results if (ROOT / x["source"]).resolve() == master_path.resolve())
    case_dir = ROOT / Path(item["rebuilt"]).parent
    columns = [
        ("V3 locked master", case_dir / "original.png"),
        ("Layered rebuild", case_dir / "rebuilt.png"),
        ("Difference (white = unchanged)", case_dir / "difference.png"),
    ]
    header_h = 64
    sheet = Image.new("RGB", (900 * len(columns), 1200 + header_h), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 28)
    for i, (label, path) in enumerate(columns):
        draw.text((i * 900 + 24, 17), label, font=font, fill="black")
        sheet.paste(Image.open(path).convert("RGB"), (i * 900, header_h))
    sheet.save(out_dir / "page01_master_original_rebuilt_difference.png")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--template-dir",
        default="data/template_registry/phone_v3/versions/v3_20260821_layered_page01_pilot",
    )
    parser.add_argument(
        "--output-dir",
        default="data/analysis/phone_v3_layered_page01_pilot_20260821",
    )
    args = parser.parse_args()
    template_dir = ROOT / args.template_dir
    out_dir = ROOT / args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    layout = load_json(template_dir / "page01/layout.json")
    fonts = load_json(template_dir / "page01/font_manifest.json")
    cases = load_json(template_dir / "page01/test_cases.json")
    master_path = ROOT / layout["locked_master"]
    master = Image.open(master_path).convert("RGB")
    results = [render_case(case, layout, fonts, master, out_dir) for case in cases]
    review_sheet(results, out_dir)
    master_comparison(results, master_path, out_dir)

    report = {
        "template_id": "phone_v3_page01_layered_pilot_20260821",
        "status": "REVIEW_REQUIRED" if all(x["qa"] == "PASS" for x in results) else "FAILED",
        "master": str(master_path.relative_to(ROOT)).replace("\\", "/"),
        "master_sha256": sha256(master_path),
        "font_sha256": {name: sha256(Path(spec["path"])) for name, spec in fonts.items() if isinstance(spec, dict)},
        "case_count": len(results),
        "pass_count": sum(x["qa"] == "PASS" for x in results),
        "results": results,
        "next_gate": "User visual approval is required before pages 02-08.",
    }
    (out_dir / "qa_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "cases": report["case_count"], "pass": report["pass_count"], "review": str(out_dir / "page01_five_case_review.png")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
