"""Read-only WB daily snapshot from Analytics v3 and seller stock APIs."""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wb_manager.client import WbApiError, WbClient
from wb_manager.config import load_settings


OUT_DIR = ROOT / "data" / "operations"
TZ = ZoneInfo("Asia/Shanghai")


def call(client: WbClient, method: str, url: str, body: dict | None = None):
    result = client.request(method, url, body=body)
    return {"status": result.status, "ok": result.ok, "data": result.data}


def products_payload(start: date, end: date) -> dict:
    return {
        "selectedPeriod": {"start": start.isoformat(), "end": end.isoformat()},
        "pastPeriod": {"start": (start - timedelta(days=30)).isoformat(), "end": (start - timedelta(days=1)).isoformat()},
        "nmIDs": [], "tagIDs": [], "timezone": "Asia/Shanghai", "limit": 1000, "offset": 0,
    }


def pick(obj: dict, *keys: str) -> int:
    for key in keys:
        if key in obj and obj[key] is not None:
            return obj[key]
    return 0


def previous_daily_snapshot(day: date) -> dict:
    """Return the most recent snapshot from an earlier calendar day."""
    candidates = []
    for path in OUT_DIR.glob("wb_daily_snapshot_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            captured = datetime.fromisoformat(data.get("generatedAt", "")).astimezone(TZ)
            if captured.date() < day:
                candidates.append((captured, data))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return max(candidates, default=(None, {}), key=lambda item: item[0])[1]


def compare_value(current: int, previous: dict, key: str) -> str:
    if key not in previous:
        return f"{current}（—）"
    prior = previous[key]
    delta = current - prior
    return f"{current}（{delta:+d}）"


def main() -> None:
    now = datetime.now(TZ)
    end = now.date()
    start = end - timedelta(days=29)
    client = WbClient(**load_settings().__dict__)
    funnel = call(client, "POST", "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products", products_payload(start, end))
    warehouses = call(client, "GET", "https://marketplace-api.wildberries.ru/api/v3/warehouses")
    content_rows = []
    cursor = {"limit": 100}
    content_status = 0
    for _ in range(10):
        content = call(client, "POST", "https://content-api.wildberries.ru/content/v2/get/cards/list", {
            "settings": {"cursor": cursor, "filter": {"withPhoto": -1, "textSearch": "", "tagIDs": [], "allowedCategoriesOnly": True, "objectIDs": [], "brands": [], "imtID": 0}}
        })
        content_status = content["status"]
        page = content["data"] if isinstance(content["data"], dict) else {}
        rows = page.get("cards", []) or []
        content_rows.extend(row for row in rows if isinstance(row, dict))
        next_cursor = page.get("cursor", {}) or {}
        if len(rows) < 100 or not next_cursor.get("updatedAt") or not next_cursor.get("nmID"):
            break
        next_value = {"limit": 100, "updatedAt": next_cursor["updatedAt"], "nmID": next_cursor["nmID"]}
        if next_value == cursor:
            break
        cursor = next_value

    products = []
    if funnel["ok"] and isinstance(funnel["data"], dict):
        products = funnel["data"].get("data", {}).get("products", []) or []
    totals = Counter()
    categories: dict[str, Counter] = defaultdict(Counter)
    focus = []
    zero_stock = []
    stock_total = 0
    for row in products:
        product = row.get("product", {}) if isinstance(row.get("product"), dict) else {}
        statistic = row.get("statistic", {}) if isinstance(row.get("statistic"), dict) else {}
        metrics = statistic.get("selected", row.get("statistics", {}).get("selectedPeriod", row.get("statistics", {})))
        if not isinstance(metrics, dict):
            metrics = row
        views = pick(metrics, "openCount", "views")
        carts = pick(metrics, "cartCount", "addToCart")
        orders = pick(metrics, "orderCount", "ordersCount")
        buyouts = pick(metrics, "buyoutCount")
        wish = pick(metrics, "addToWishlist", "wishlistCount")
        subject_id = product.get("subjectId") or row.get("subjectId") or 0
        cat = {515: "智能手机", 1967: "光子脱毛仪"}.get(subject_id, f"类目 {subject_id}")
        for name, value in (("views", views), ("carts", carts), ("orders", orders), ("buyouts", buyouts), ("wish", wish)):
            totals[name] += value
            categories[cat][name] += value
        categories[cat]["analyticsCards"] += 1
        stocks = product.get("stocks", {}) if isinstance(product.get("stocks"), dict) else {}
        mp_stock = pick(stocks, "mp")
        wb_stock = pick(stocks, "wb")
        stock_total += mp_stock
        categories[cat]["sellerStock"] += mp_stock
        categories[cat]["wbStock"] += wb_stock
        if mp_stock + wb_stock == 0:
            zero_stock.append({"vendorCode": product.get("vendorCode") or "—", "nmID": product.get("nmId") or product.get("nmID") or "—"})
        if carts:
            focus.append({
                "vendorCode": product.get("vendorCode") or row.get("vendorCode") or "—",
                "nmID": product.get("nmId") or product.get("nmID") or row.get("nmId") or row.get("nmID") or "—",
                "views": views, "carts": carts, "orders": orders,
            })
    focus.sort(key=lambda x: (x["carts"], x["views"]), reverse=True)
    for row in content_rows:
        subject_id = row.get("subjectID") or row.get("object") or 0
        cat = {515: "智能手机", 1967: "光子脱毛仪"}.get(subject_id, f"类目 {subject_id}")
        categories[cat]["contentCards"] += 1
    active_warehouses = 0
    if warehouses["ok"] and isinstance(warehouses["data"], list):
        active_warehouses = len(warehouses["data"])
    snapshot = {
        "generatedAt": now.isoformat(timespec="seconds"), "period": {"start": start.isoformat(), "end": end.isoformat(), "timezone": "Asia/Shanghai"},
        "endpoints": {"salesFunnel": {"status": funnel["status"], "ok": funnel["ok"]}, "content": {"status": content_status, "ok": content_status == 200}, "warehouses": {"status": warehouses["status"], "ok": warehouses["ok"]}},
        "cards": len(content_rows), "analyticsCards": len(products), "totals": dict(totals), "categorySummary": {k: dict(v) for k, v in categories.items()},
        "focus": focus[:10], "zeroStockSkus": zero_stock, "activeWarehouses": active_warehouses, "sellerStock": stock_total,
    }
    stamp = now.strftime("%Y%m%d_%H%M%S")
    raw = OUT_DIR / f"wb_daily_snapshot_{stamp}.json"
    report = OUT_DIR / f"wb_daily_report_{stamp}.md"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    previous = previous_daily_snapshot(end)
    previous_categories = previous.get("categorySummary", {}) if isinstance(previous, dict) else {}
    lines = [f"# WB 店铺日报 — {end.isoformat()}", "", f"生成时间：{now.strftime('%Y-%m-%d %H:%M %Z')}；数据周期：{start} 至 {end}（30天滚动）。", "", "## 核心指标", "", "| 指标 | 数值 |", "|---|---:|", f"| 商品卡总数（Content 内容卡） | {len(content_rows)} |", f"| 纳入 Analytics 漏斗的商品卡 | {len(products)} |", f"| 浏览 | {totals['views']} |", f"| 加购 | {totals['carts']} |", f"| 订单 / 买退 | {totals['orders']} / {totals['buyouts']} |", f"| 收藏 | {totals['wish']} |", f"| 可用卖家仓 | {active_warehouses} |", "", "## 有加购商品（前10）", "", "| SKU | nmID | 浏览 | 加购 | 订单 |", "|---|---:|---:|---:|---:|"]
    lines += [f"| {x['vendorCode']} | {x['nmID']} | {x['views']} | {x['carts']} | {x['orders']} |" for x in focus[:10]] or ["| 无 | — | 0 | 0 | 0 |"]
    lines += ["", "## 数据状态", "", f"- Content 内容卡读取：HTTP {content_status}。", f"- Analytics v3 Sales Funnel：HTTP {funnel['status']}。", f"- FBS 仓库读取：HTTP {warehouses['status']}。", "- 本报告为只读快照；未改动价格、库存、广告或商品资料。"]
    lines += ["", "## 类目指标对比", "", "| 类目 | 内容卡 | 漏斗卡 | 浏览 | 加购 | 订单 | 收藏 | 卖家仓库存 | WB 仓库存 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for category, values in sorted(snapshot["categorySummary"].items()):
        prior = previous_categories.get(category, {}) if isinstance(previous_categories, dict) else {}
        measures = ("contentCards", "analyticsCards", "views", "carts", "orders", "wish", "sellerStock", "wbStock")
        lines.append("| " + category + " | " + " | ".join(compare_value(values.get(key, 0), prior, key) for key in measures) + " |")
    lines += ["", "## 异常提醒", ""]
    if zero_stock:
        samples = "、".join(str(item["vendorCode"]) for item in zero_stock[:5])
        lines.append(f"- 零库存 SKU：{len(zero_stock)} 个；示例：{samples}。")
    if totals["carts"] and not totals["orders"]:
        lines.append(f"- 漏斗断点：近 30 天已有 {totals['carts']} 次加购，但订单为 0；优先核验到手价、配送时效与前台购买按钮。")
    if not zero_stock and not totals["carts"]:
        lines.append("- 未发现需要日报升级处理的库存或漏斗异常。")
    lines += ["", "## 可行动机会", ""]
    for item in focus[:3]:
        if item["carts"] and not item["orders"]:
            lines.append(f"- {item['vendorCode']}：{item['carts']} 次加购、0 订单；作为优先核验对象。")
    lines += ["", "## 广告与促销状态", "", "- 广告 fullstats：条件模块；仅在存在在投广告时读取并展示曝光、点击、花费、加购、订单与 ROI。", "- 促销日历：条件模块；后续接入后仅展示未来 7 天可报名或临近截止的活动。"]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report), "snapshot": str(raw), **snapshot}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except WbApiError as exc:
        raise SystemExit(str(exc))
