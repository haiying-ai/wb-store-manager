"""Read-only WB daily snapshot from Analytics v3 and seller stock APIs."""
from __future__ import annotations

import json
import sys
import time
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
SEARCH_REPORT_URL = "https://seller-analytics-api.wildberries.ru/api/v2/search-report/report"


def readonly_call(client: WbClient, method: str, url: str, body: dict | None = None) -> dict:
    """Return a status record instead of failing the entire morning report."""
    try:
        return call(client, method, url, body)
    except Exception as exc:
        return {"status": 0, "ok": False, "data": {"error": str(exc)}}


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


def search_performance(client: WbClient, nm_ids: list[int], now: datetime) -> dict:
    """Return a compact, JSON-safe store search summary for the daily snapshot."""
    end = now.date()
    start = end - timedelta(days=29)
    past_end = start - timedelta(days=1)
    body = {
        "currentPeriod": {"start": start.isoformat(), "end": end.isoformat()},
        "pastPeriod": {"start": (past_end - timedelta(days=29)).isoformat(), "end": past_end.isoformat()},
        "nmIds": nm_ids, "subjectIds": [], "brandNames": [], "tagIds": [],
        "positionCluster": "all", "orderBy": {"field": "avgPosition", "mode": "asc"},
        "includeSubstitutedSKUs": True, "includeSearchTexts": True, "limit": 1000, "offset": 0,
    }
    response = call(client, "POST", SEARCH_REPORT_URL, body)
    result = {"status": response["status"], "ok": response["ok"], "requestedSkuCount": len(nm_ids), "period": body["currentPeriod"]}
    data = response["data"].get("data", {}) if response["ok"] and isinstance(response["data"], dict) else {}
    groups = data.get("groups", []) if isinstance(data, dict) else []
    items = [item for group in groups if isinstance(group, dict) for item in (group.get("items") or []) if isinstance(item, dict)]
    def value(item: dict, key: str) -> int:
        field = item.get(key, {})
        return pick(field, "current") if isinstance(field, dict) else 0
    rows = [{"nmID": item.get("nmId"), "vendorCode": item.get("vendorCode"), "openCard": value(item, "openCard"), "addToCart": value(item, "addToCart"), "orders": value(item, "orders"), "avgPosition": value(item, "avgPosition"), "visibility": value(item, "visibility")} for item in items]
    rows.sort(key=lambda row: (row["avgPosition"] or 10**9, -row["openCard"]))
    result.update({
        "returnedSkuCount": len(rows), "missingSkuCount": max(0, len(nm_ids) - len(rows)),
        "totals": {key: sum(row[key] for row in rows) for key in ("openCard", "addToCart", "orders")},
        "position": {"average": pick(data.get("positionInfo", {}).get("average", {}), "current"), "median": pick(data.get("positionInfo", {}).get("median", {}), "current")},
        "topBySearchEntries": sorted(rows, key=lambda row: row["openCard"], reverse=True)[:5],
    })
    return result


def extended_readonly_metrics(client: WbClient, now: datetime, nm_ids: list[int]) -> dict:
    """Collect every recommended daily read-only source with compact evidence."""
    today = now.date()
    history_body = {"selectedPeriod": {"start": (today - timedelta(days=6)).isoformat(), "end": today.isoformat()}, "nmIds": nm_ids, "skipDeletedNm": True, "aggregationLevel": "day"}
    # Analytics is limited to three calls/minute; the base report already makes two analytics calls.
    time.sleep(20)
    product_history = readonly_call(client, "POST", "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products/history", history_body)
    time.sleep(20)
    grouped_history = readonly_call(client, "POST", "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/grouped/history", {**history_body, "brandNames": [], "subjectIds": [], "tagIds": []})
    time.sleep(20)
    stock_report = readonly_call(client, "POST", "https://seller-analytics-api.wildberries.ru/api/v2/stocks-report/products/products", {"currentPeriod": {"start": (today - timedelta(days=6)).isoformat(), "end": today.isoformat()}, "stockType": "", "skipDeletedNm": True, "orderBy": {"field": "avgOrders", "mode": "desc"}, "availabilityFilters": ["deficient", "actual", "balanced", "nonActual", "nonLiquid", "invalidData"], "nmIDs": nm_ids, "limit": 1000, "offset": 0})
    prices = readonly_call(client, "GET", "https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=1000&offset=0")
    errors = readonly_call(client, "POST", "https://content-api.wildberries.ru/content/v2/cards/error/list?locale=ru", {"cursor": {"limit": 100}, "order": {"ascending": False}})
    feedbacks = readonly_call(client, "GET", "https://feedbacks-api.wildberries.ru/api/v1/feedbacks?isAnswered=false&take=100&skip=0")
    questions = readonly_call(client, "GET", "https://feedbacks-api.wildberries.ru/api/v1/questions?isAnswered=false&take=100&skip=0&order=dateDesc")
    date_from = (today - timedelta(days=7)).isoformat()
    orders = readonly_call(client, "GET", f"https://statistics-api.wildberries.ru/api/v1/supplier/orders?dateFrom={date_from}&flag=0")
    sales = readonly_call(client, "GET", f"https://statistics-api.wildberries.ru/api/v1/supplier/sales?dateFrom={date_from}&flag=0")
    ad_count = readonly_call(client, "GET", "https://advert-api.wildberries.ru/adv/v1/promotion/count")
    ad_ids = []
    for group in ((ad_count.get("data") or {}).get("adverts") or []) if isinstance(ad_count.get("data"), dict) else []:
        for item in group.get("advert_list") or []:
            if item.get("advert_id"):
                ad_ids.append(str(item["advert_id"]))
    ads = readonly_call(client, "GET", f"https://advert-api.wildberries.ru/adv/v3/fullstats?ids={','.join(ad_ids)}&beginDate={(today - timedelta(days=6)).isoformat()}&endDate={today.isoformat()}") if ad_ids else {"status": 200, "ok": True, "data": [], "note": "no active campaigns"}
    promotions = readonly_call(client, "GET", f"https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions?startDateTime={today.isoformat()}T00:00:00Z&endDateTime={(today + timedelta(days=7)).isoformat()}T23:59:59Z&allPromo=true")
    def count_rows(response: dict, *paths: str) -> int | None:
        value = response.get("data")
        for key in paths:
            value = value.get(key) if isinstance(value, dict) else None
        return len(value) if isinstance(value, list) else None
    return {
        "productHistory": {"status": product_history["status"], "ok": product_history["ok"]},
        "groupedHistory": {"status": grouped_history["status"], "ok": grouped_history["ok"]},
        "stockReport": {"status": stock_report["status"], "ok": stock_report["ok"]},
        "prices": {"status": prices["status"], "ok": prices["ok"], "goods": count_rows(prices, "data", "listGoods")},
        "cardErrors": {"status": errors["status"], "ok": errors["ok"]},
        "feedbacks": {"status": feedbacks["status"], "ok": feedbacks["ok"], "unanswered": count_rows(feedbacks, "data", "feedbacks")},
        "questions": {"status": questions["status"], "ok": questions["ok"], "unanswered": count_rows(questions, "data", "questions")},
        "orders": {"status": orders["status"], "ok": orders["ok"], "rows": len(orders["data"]) if isinstance(orders["data"], list) else None},
        "sales": {"status": sales["status"], "ok": sales["ok"], "rows": len(sales["data"]) if isinstance(sales["data"], list) else None},
        "advertising": {"countStatus": ad_count["status"], "statsStatus": ads["status"], "campaigns": len(ad_ids)},
        "promotions": {"status": promotions["status"], "ok": promotions["ok"], "items": count_rows(promotions, "data", "promotions")},
    }


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

    nm_ids = sorted({int(row["nmID"]) for row in content_rows if row.get("nmID")})
    search = search_performance(client, nm_ids, now)
    extended = extended_readonly_metrics(client, now, nm_ids)
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
        "searchPerformance": search,
        "extendedReadOnly": extended,
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
    previous_search = previous.get("searchPerformance", {}) if isinstance(previous, dict) else {}
    previous_search_totals = previous_search.get("totals", {}) if isinstance(previous_search, dict) else {}
    search_totals = search.get("totals", {})
    lines += ["", "## Search performance (daily snapshot comparison)", "", f"- Endpoint: POST /api/v2/search-report/report — HTTP {search['status']}", f"- Reporting period: {search['period']['start']} to {search['period']['end']} (30 days)", f"- Returned/requested SKUs: {search.get('returnedSkuCount', 0)}/{search.get('requestedSkuCount', 0)}; missing: {search.get('missingSkuCount', 0)}", "", "| Search metric | Current | Change vs previous daily snapshot |", "|---|---:|---:|"]
    for key, label in (("openCard", "Search card entries"), ("addToCart", "Search add-to-cart"), ("orders", "Search orders")):
        current = search_totals.get(key, 0)
        baseline = previous_search_totals.get(key)
        delta = "— (first comparable capture)" if baseline is None else f"{current - baseline:+d}"
        lines.append(f"| {label} | {current} | {delta} |")
    lines += [f"- Search average / median position: {search.get('position', {}).get('average', 0)} / {search.get('position', {}).get('median', 0)}.", "- Day-over-day values compare the same 30-day rolling search window and therefore indicate directional movement, not single-day demand."]
    lines += ["", "## 广告与促销状态", "", f"- 广告活动：{extended['advertising']['campaigns']} 个；活动查询 HTTP {extended['advertising']['countStatus']}，统计查询 HTTP {extended['advertising']['statsStatus']}。", f"- 未来 7 天促销：{extended['promotions']['items'] if extended['promotions']['items'] is not None else '不可用'} 个；HTTP {extended['promotions']['status']}。", "", "## 已验证事实、合理推断、待验证项", "", f"- 已验证事实：价格读回 HTTP {extended['prices']['status']}；错误卡 HTTP {extended['cardErrors']['status']}；未回复评价/问题分别为 {extended['feedbacks']['unanswered'] if extended['feedbacks']['unanswered'] is not None else '不可用'} / {extended['questions']['unanswered'] if extended['questions']['unanswered'] is not None else '不可用'}。", f"- 已验证事实：近 7 天订单/销售明细返回行数为 {extended['orders']['rows'] if extended['orders']['rows'] is not None else '不可用'} / {extended['sales']['rows'] if extended['sales']['rows'] is not None else '不可用'}。", "- 合理推断：滚动窗口变化仅表示方向，不等同于当日新增经营结果。", "- 待验证项：若价格、库存、错误卡或履约数据异常，只提出读回核验建议，不在日报中执行修复。", "", "## 数据状态", "", f"- 日趋势接口：商品历史 HTTP {extended['productHistory']['status']}；分组历史 HTTP {extended['groupedHistory']['status']}；库存报告 HTTP {extended['stockReport']['status']}。", "- 本报告只读：未创建或修改商品、价格、库存、广告、促销及买家消息。"]
    # 固定扩展日报合同：章节必须保持顺序，并且中文是交付硬性要求。
    prior_totals = previous.get("totals", {}) if isinstance(previous, dict) else {}
    def change(key: str) -> str:
        before = prior_totals.get(key)
        return "—（无上次快照）" if before is None else f"{before} / {totals[key] - before:+d}"
    prior_cards = previous.get("cards") if isinstance(previous, dict) else None
    cards_change = "—（无上次快照）" if prior_cards is None else f"{prior_cards} / {len(content_rows) - prior_cards:+d}"
    focus_lines = [
        f"| {item['vendorCode']} | {item['nmID']} | {item['views']} | {item['carts']} | {item['orders']} |"
        for item in focus[:10]
    ] or ["| 无 | — | — | — | — |"]
    opportunity_lines = [
        f"- 只读核验优先项：{item['vendorCode']} 有 {item['carts']} 次加购、{item['orders']} 笔订单；提出任何变更前，先核验到手价、配送承诺和前台购买控件。"
        for item in focus[:3] if item["carts"] and not item["orders"]
    ] or ["- 今日没有需要单独执行只读核验的“有加购、无订单”SKU。"]
    report_lines = [
        f"# WB 固定早间日报（扩展只读版）— {end.isoformat()}", "",
        "## 1. 快照与口径", "",
        f"- 采集时间：{now.strftime('%Y-%m-%d %H:%M %Z')}；滚动周期：{start} 至 {end}（30 天，Asia/Shanghai）。",
        "- 商品卡为 Content 商品卡；漏斗指标为 Analytics v3 所选周期汇总；卖家库存为 Marketplace 卖家库存。",
        "- 本日报仅只读：未修改价格、库存、广告、商品卡、促销或买家消息。", "",
        "## 2. 店铺总览（本次 / 上次 / 变化）", "",
        "| 指标 | 本次 | 上次 / 变化 |", "|---|---:|---:|",
        f"| Content 商品卡 | {len(content_rows)} | {cards_change} |",
        f"| Analytics 漏斗商品卡 | {len(products)} | {previous.get('analyticsCards', '—') if isinstance(previous, dict) else '—'} / — |",
        f"| 浏览 | {totals['views']} | {change('views')} |",
        f"| 加购 | {totals['carts']} | {change('carts')} |",
        f"| 订单 | {totals['orders']} | {change('orders')} |",
        f"| 收藏 | {totals['wish']} | {change('wish')} |",
        f"| 卖家库存 | {stock_total} | {previous.get('sellerStock', '—') if isinstance(previous, dict) else '—'} / {'—' if not isinstance(previous, dict) or 'sellerStock' not in previous else f'{stock_total - previous['sellerStock']:+d}'} |", "",
        "## 3. 类目", "",
        "| 类目 | Content 商品卡 | 漏斗商品卡 | 浏览 | 加购 | 订单 | 收藏 | 卖家库存 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for category, values in sorted(snapshot["categorySummary"].items()):
        report_lines.append("| " + category + " | " + " | ".join(str(values.get(key, 0)) for key in ("contentCards", "analyticsCards", "views", "carts", "orders", "wish", "sellerStock")) + " |")
    report_lines += ["", "## 4. 重点 SKU", "", "| SKU | nmID | 浏览 | 加购 | 订单 |", "|---|---:|---:|---:|---:|", *focus_lines,
        "", "## 5. 搜索表现", "", f"- HTTP {search['status']}；已返回 {search.get('returnedSkuCount', 0)}/{search.get('requestedSkuCount', 0)} 个请求 SKU，缺失 {search.get('missingSkuCount', 0)} 个。",
        f"- 搜索进卡 / 加购 / 订单：{search_totals.get('openCard', 0)} / {search_totals.get('addToCart', 0)} / {search_totals.get('orders', 0)}。平均 / 中位位置：{search.get('position', {}).get('average', 0)} / {search.get('position', {}).get('median', 0)}。",
        "- 搜索变化比较相同的 30 天滚动窗口，仅反映方向，不能视为单日需求。", "",
        "## 6. 异常", "",
        f"- 零库存 SKU：{len(zero_stock)} 个；示例：{', '.join(str(item['vendorCode']) for item in zero_stock[:5]) or '无'}。",
        f"- 商品历史接口：HTTP {extended['productHistory']['status']}（保留失败状态，不以零值替代）。",
        f"- 错误卡 HTTP {extended['cardErrors']['status']}；未回复评价 / 问题：{extended['feedbacks']['unanswered']} / {extended['questions']['unanswered']}。", "",
        "## 7. 只读行动机会", "", *opportunity_lines, "",
        "## 8. 广告与促销", "",
        f"- 广告活动：{extended['advertising']['campaigns']} 个；活动计数 HTTP {extended['advertising']['countStatus']}，统计 HTTP {extended['advertising']['statsStatus']}。",
        f"- 未来 7 天促销：{extended['promotions']['items'] if extended['promotions']['items'] is not None else '不可用'} 个；HTTP {extended['promotions']['status']}。", "",
        "## 9. 事实 / 推断 / 待验证", "",
        f"- 事实：价格 HTTP {extended['prices']['status']}（{extended['prices']['goods']} 个商品）；近 7 天订单 / 销售行数 {extended['orders']['rows']} / {extended['sales']['rows']}（均为 HTTP 200）。",
        "- 推断：有加购、无订单仅说明应优先核验，不能据此证明价格、内容或物流存在缺陷。",
        "- 待验证：对重点 SKU 读取前台到手价、预计送达日期和购买控件；核验期间不修改店铺数据。", "",
        "## 10. 数据状态与产物路径", "",
        f"- 核心接口：Sales Funnel HTTP {funnel['status']}；Content HTTP {content_status}；FBS 仓 HTTP {warehouses['status']}。",
        f"- 扩展读取：分组历史 HTTP {extended['groupedHistory']['status']}；库存报告 HTTP {extended['stockReport']['status']}；价格 HTTP {extended['prices']['status']}；订单 HTTP {extended['orders']['status']}；销售 HTTP {extended['sales']['status']}。",
        f"- 快照：{raw}", f"- 日报：{report}",
    ]
    report.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report), "snapshot": str(raw), **snapshot}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except WbApiError as exc:
        raise SystemExit(str(exc))
