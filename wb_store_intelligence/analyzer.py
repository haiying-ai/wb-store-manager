from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


STORE_LABELS = {
    "owner-store": "自有店",
    "benchmark-a": "对标店A",
    "benchmark-b": "对标店B",
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))["data"]


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 2) if values else 0


def _pct(part: float, total: float) -> float:
    return round(part / total * 100, 2) if total else 0


def analyze_store(store_dir: Path, *, subject_id: int = 515) -> dict[str, Any]:
    cards_all = _load(store_dir / "cards-list.json")
    cards = [card for card in cards_all if card.get("subjectID") == subject_id]
    card_ids = {card.get("nmID") for card in cards}

    price_payload = _load(store_dir / "prices.json")
    goods_all = price_payload.get("data", {}).get("listGoods", [])
    goods = [good for good in goods_all if good.get("nmID") in card_ids]
    discounted_prices = [
        min(
            (
                size.get("discountedPrice", 0)
                for size in good.get("sizes", [])
                if size.get("discountedPrice", 0) > 0
            ),
            default=0,
        )
        for good in goods
    ]
    discounted_prices = [price for price in discounted_prices if price > 0]

    funnel_payload = _load(store_dir / "sales-funnel-products.json")
    products_all = funnel_payload.get("data", {}).get("products", [])
    products = [
        row for row in products_all if row.get("product", {}).get("subjectId") == subject_id
    ]
    selected = [row.get("statistic", {}).get("selected", {}) for row in products]
    totals = {
        key: sum(float(stat.get(key, 0) or 0) for stat in selected)
        for key in (
            "openCount",
            "cartCount",
            "orderCount",
            "orderSum",
            "buyoutCount",
            "buyoutSum",
            "cancelCount",
            "addToWishlist",
        )
    }

    fbs = _load(store_dir / "fbs-stocks.json")
    positive_stock = [row for row in fbs if row.get("amount", 0) > 0]
    wb_payload = _load(store_dir / "wb-warehouse-stocks.json")
    wb_rows = wb_payload.get("data", {}).get("items", [])
    wb_rows = [row for row in wb_rows if row.get("nmId") in card_ids]

    photo_counts = [len(card.get("photos") or []) for card in cards]
    title_lengths = [len(card.get("title") or "") for card in cards]
    description_lengths = [len(card.get("description") or "") for card in cards]
    characteristic_counts = [len(card.get("characteristics") or []) for card in cards]
    size_counts = [len(card.get("sizes") or []) for card in cards]

    top_rows = sorted(
        products,
        key=lambda row: row.get("statistic", {})
        .get("selected", {})
        .get("openCount", 0),
        reverse=True,
    )[:10]
    top_products = []
    for row in top_rows:
        product = row.get("product", {})
        stat = row.get("statistic", {}).get("selected", {})
        top_products.append(
            {
                "vendorCode": product.get("vendorCode"),
                "nmId": product.get("nmId"),
                "views": stat.get("openCount", 0),
                "carts": stat.get("cartCount", 0),
                "orders": stat.get("orderCount", 0),
                "cartRate": _pct(stat.get("cartCount", 0), stat.get("openCount", 0)),
                "orderRate": _pct(stat.get("orderCount", 0), stat.get("cartCount", 0)),
                "avgPrice": stat.get("avgPrice", 0),
            }
        )

    return {
        "cards": len(cards),
        "analyticsCards": len(products),
        "analyticsCoverage": _pct(len(products), len(cards)),
        "photosMedian": _median(photo_counts),
        "photosAtLeast5": _pct(sum(v >= 5 for v in photo_counts), len(photo_counts)),
        "photosAtLeast7": _pct(sum(v >= 7 for v in photo_counts), len(photo_counts)),
        "videoCoverage": _pct(
            sum(bool(card.get("video")) for card in cards), len(cards)
        ),
        "titleLengthMedian": _median(title_lengths),
        "descriptionLengthMedian": _median(description_lengths),
        "characteristicsMedian": _median(characteristic_counts),
        "sizesMedian": _median(size_counts),
        "priceMedian": _median(discounted_prices),
        "priceMin": min(discounted_prices, default=0),
        "priceMax": max(discounted_prices, default=0),
        "discountMedian": _median(
            [float(good.get("discount", 0) or 0) for good in goods]
        ),
        "views": int(totals["openCount"]),
        "carts": int(totals["cartCount"]),
        "orders": int(totals["orderCount"]),
        "buyouts": int(totals["buyoutCount"]),
        "cancels": int(totals["cancelCount"]),
        "wish": int(totals["addToWishlist"]),
        "cartRate": _pct(totals["cartCount"], totals["openCount"]),
        "orderRate": _pct(totals["orderCount"], totals["cartCount"]),
        "buyoutRate": _pct(totals["buyoutCount"], totals["orderCount"]),
        "viewsPerCard": round(totals["openCount"] / len(products), 2)
        if products
        else 0,
        "fbsPositiveRows": len(positive_stock),
        "fbsUnits": sum(int(row.get("amount", 0) or 0) for row in positive_stock),
        "wbUnits": sum(int(row.get("quantity", 0) or 0) for row in wb_rows),
        "topProducts": top_products,
        "subjects": Counter(card.get("subjectID") for card in cards_all).most_common(),
    }


def build_report(snapshot_root: Path, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics = {
        store: analyze_store(snapshot_root / store)
        for store in ("owner-store", "benchmark-a", "benchmark-b")
    }
    json_path = output_dir / "three_store_metrics.json"
    json_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# WB 三店手机类目对标分析",
        "",
        f"- 数据快照：`{snapshot_root.name}`",
        "- 对比类目：`subjectID=515`",
        "- 漏斗周期：最近完整 7 天，对比前 7 天",
        "- 结论口径：事实、推断和建议分开；本报告未执行任何店铺修改。",
        "",
        "## 核心指标",
        "",
        "| 指标 | 自有店 | 对标店A | 对标店B |",
        "|---|---:|---:|---:|",
    ]
    keys = [
        ("商品卡", "cards"),
        ("漏斗覆盖率", "analyticsCoverage"),
        ("浏览量", "views"),
        ("单卡浏览", "viewsPerCard"),
        ("加购量", "carts"),
        ("浏览→加购", "cartRate"),
        ("订单量", "orders"),
        ("加购→订单", "orderRate"),
        ("成交量", "buyouts"),
        ("中位折后价", "priceMedian"),
        ("中位图片数", "photosMedian"),
        ("至少5图占比", "photosAtLeast5"),
        ("至少7图占比", "photosAtLeast7"),
        ("视频覆盖率", "videoCoverage"),
        ("中位标题长度", "titleLengthMedian"),
        ("中位描述长度", "descriptionLengthMedian"),
        ("中位属性数", "characteristicsMedian"),
        ("FBS正库存行", "fbsPositiveRows"),
        ("FBS库存件数", "fbsUnits"),
    ]
    pct_keys = {
        "analyticsCoverage",
        "cartRate",
        "orderRate",
        "photosAtLeast5",
        "photosAtLeast7",
        "videoCoverage",
    }
    for label, key in keys:
        values = []
        for store in ("owner-store", "benchmark-a", "benchmark-b"):
            value = metrics[store][key]
            values.append(f"{value}%" if key in pct_keys else str(value))
        lines.append(f"| {label} | {' | '.join(values)} |")

    lines += ["", "## 各店流量最高 SKU", ""]
    for store in ("owner-store", "benchmark-a", "benchmark-b"):
        lines += [
            f"### {STORE_LABELS[store]}",
            "",
            "| SKU | nmID | 浏览 | 加购 | 订单 | 浏览→加购 | 加购→订单 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for row in metrics[store]["topProducts"][:5]:
            lines.append(
                f"| {row['vendorCode']} | {row['nmId']} | {row['views']} | "
                f"{row['carts']} | {row['orders']} | {row['cartRate']}% | "
                f"{row['orderRate']}% |"
            )
        lines.append("")

    owner = metrics["owner-store"]
    best_views = max(metrics, key=lambda store: metrics[store]["viewsPerCard"])
    best_cart = max(metrics, key=lambda store: metrics[store]["cartRate"])
    best_order = max(metrics, key=lambda store: metrics[store]["orderRate"])
    best_media = max(metrics, key=lambda store: metrics[store]["photosMedian"])
    lines += [
        "## 已验证事实",
        "",
        f"- 单卡浏览最高：{STORE_LABELS[best_views]}，"
        f"{metrics[best_views]['viewsPerCard']} 次/卡；自有店为 {owner['viewsPerCard']}。",
        f"- 浏览到加购最高：{STORE_LABELS[best_cart]}，"
        f"{metrics[best_cart]['cartRate']}%；自有店为 {owner['cartRate']}%。",
        f"- 加购到订单最高：{STORE_LABELS[best_order]}，"
        f"{metrics[best_order]['orderRate']}%；自有店为 {owner['orderRate']}%。",
        f"- 图片中位数最高：{STORE_LABELS[best_media]}，"
        f"{metrics[best_media]['photosMedian']} 张；自有店为 {owner['photosMedian']} 张。",
        "",
        "## 初步判断",
        "",
        "- 单卡浏览差距主要属于流量获取与商品组合问题，不能仅凭本报告归因于主图。",
        "- 浏览到加购反映价格、首屏表达、商品匹配度和信任感的综合结果。",
        "- 加购到订单还会受配送时效、区域到手价、支付和库存稳定性影响。",
        "- 图片数量只能说明素材完整度，不能替代主图视觉质量审核。",
        "",
        "## 下一步行动",
        "",
        "1. 对三店各自流量前 10 的 SKU 建立同型号匹配表，优先比较共同型号和容量。",
        "2. 对共同型号比较折后价、折扣、图片数、标题、属性数、库存和漏斗差异。",
        "3. 建立 WB 图片 CDN 精确白名单后，下载每店前 5 SKU 的主图并做缩略图视觉评分。",
        "4. 将对标店高加购 SKU 的首屏要素转化为自有店单 SKU 小流量实验，不直接批量修改。",
        "5. 对自有店有加购无订单 SKU 优先核查地区到手价、配送日期和库存连续性。",
    ]
    report_path = output_dir / "three_store_benchmark_report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path, json_path
