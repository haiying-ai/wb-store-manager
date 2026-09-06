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
RUN_BUDGET_SECONDS = 240
DEFAULT_REQUEST_TIMEOUT = 20
ANALYTICS_REQUEST_TIMEOUT = 60
RUN_DEADLINE_AT: float | None = None


def readonly_call(
    client: WbClient,
    method: str,
    url: str,
    body: dict | None = None,
    *,
    timeout: int | None = None,
) -> dict:
    """Return a status record instead of failing the entire morning report."""
    effective_timeout = timeout
    if RUN_DEADLINE_AT is not None:
        remaining = RUN_DEADLINE_AT - time.monotonic()
        if remaining <= 0:
            return {
                "status": None,
                "ok": False,
                "data": {"error": "采集总预算已耗尽；本数据源未执行，未取得 HTTP 状态。"},
            }
        effective_timeout = max(1, min(timeout or DEFAULT_REQUEST_TIMEOUT, int(remaining)))
    try:
        return call(client, method, url, body, timeout=effective_timeout)
    except WbApiError as exc:
        # A transport failure has no HTTP response.  Preserve that fact instead
        # of fabricating status 0 or substituting missing metrics with zeros.
        return {"status": exc.status or None, "ok": False, "data": {"error": str(exc)}}
    except Exception as exc:
        return {"status": None, "ok": False, "data": {"error": str(exc)}}


def call(
    client: WbClient,
    method: str,
    url: str,
    body: dict | None = None,
    *,
    timeout: int | None = None,
):
    # A fixed daily report must finish and retain failed-source status. Avoid
    # multiplying every transport timeout by the client's general retry loop;
    # HTTP 429 remains visible for a later read-only rerun.
    result = client.request(method, url, body=body, timeout=timeout, retries=0)
    return {"status": result.status, "ok": result.ok, "data": result.data}


def status_text(status: int | None) -> str:
    """Make the absence of an HTTP response explicit in human reports."""
    return f"HTTP {status}" if status is not None else "未取得 HTTP 状态（传输失败）"


def products_payload(start: date, end: date) -> dict:
    return {
        "selectedPeriod": {"start": start.isoformat(), "end": end.isoformat()},
        "pastPeriod": {"start": (start - timedelta(days=30)).isoformat(), "end": (start - timedelta(days=1)).isoformat()},
        # Current official v3 schema: lower-camel filter names and offset
        # pagination. `nmIDs`/`tagIDs` and cursor are not this endpoint's
        # request contract.
        "nmIds": [], "subjectIds": [], "tagIds": [], "skipDeletedNm": True,
        "orderBy": {"field": "openCard", "mode": "desc"},
        "limit": 1000, "offset": 0,
    }


def read_sales_funnel(client: WbClient, start: date, end: date) -> dict:
    """Read the store-wide v3 funnel using the documented request schema."""
    first = readonly_call(client, "POST", "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products", products_payload(start, end), timeout=ANALYTICS_REQUEST_TIMEOUT)
    if not first["ok"] or not isinstance(first.get("data"), dict):
        return first
    data = first["data"].get("data", {})
    rows = data.get("products", []) if isinstance(data, dict) else []
    if len(rows) < 1000:
        return first
    # Keep pagination correct for a larger future catalogue.
    time.sleep(20)
    second_payload = products_payload(start, end)
    second_payload["offset"] = 1000
    second = readonly_call(client, "POST", "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products", second_payload, timeout=ANALYTICS_REQUEST_TIMEOUT)
    if second["ok"] and isinstance(second.get("data"), dict):
        second_data = second["data"].get("data", {})
        second_rows = second_data.get("products", []) if isinstance(second_data, dict) else []
        data["products"] = rows + second_rows
    return first


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
    # Search is diagnostic evidence: preserve an HTTP failure in the daily
    # snapshot instead of aborting the entire read-only report.
    response = readonly_call(client, "POST", SEARCH_REPORT_URL, body, timeout=ANALYTICS_REQUEST_TIMEOUT)
    result = {"status": response["status"], "ok": response["ok"], "requestedSkuCount": len(nm_ids), "period": body["currentPeriod"]}
    if not response["ok"]:
        result.update({
            "returnedSkuCount": None,
            "missingSkuCount": None,
            "totals": None,
            "position": None,
            "topBySearchEntries": None,
        })
        return result
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
    # The v3 per-product history method accepts 1..20 `nmIds`; use a
    # representative first batch for the status surface instead of sending 145
    # IDs and receiving HTTP 400.
    history_body = {"selectedPeriod": {"start": (today - timedelta(days=6)).isoformat(), "end": today.isoformat()}, "nmIds": nm_ids[:20], "skipDeletedNm": True, "aggregationLevel": "day"}
    # WB publishes rate limits per API method. These are different methods, so
    # they do not require an unconditional cross-method 20-second delay. A 429
    # is retained with its real HTTP status instead of hidden by sleeping.
    product_history = readonly_call(client, "POST", "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products/history", history_body, timeout=ANALYTICS_REQUEST_TIMEOUT)
    grouped_history = readonly_call(client, "POST", "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/grouped/history", {**history_body, "brandNames": [], "subjectIds": [], "tagIds": []}, timeout=ANALYTICS_REQUEST_TIMEOUT)
    stock_report = readonly_call(client, "POST", "https://seller-analytics-api.wildberries.ru/api/v2/stocks-report/products/products", {"currentPeriod": {"start": (today - timedelta(days=6)).isoformat(), "end": today.isoformat()}, "stockType": "", "skipDeletedNm": True, "orderBy": {"field": "avgOrders", "mode": "desc"}, "availabilityFilters": ["deficient", "actual", "balanced", "nonActual", "nonLiquid", "invalidData"], "nmIDs": nm_ids, "limit": 1000, "offset": 0}, timeout=ANALYTICS_REQUEST_TIMEOUT)
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
    if not ad_count["ok"]:
        ads = {"status": None, "ok": False, "data": None}
        campaign_count = None
        stats_note = "未执行：广告活动计数不可用"
    elif ad_ids:
        ads = readonly_call(client, "GET", f"https://advert-api.wildberries.ru/adv/v3/fullstats?ids={','.join(ad_ids)}&beginDate={(today - timedelta(days=6)).isoformat()}&endDate={today.isoformat()}")
        campaign_count = len(ad_ids)
        stats_note = None
    else:
        ads = {"status": None, "ok": True, "data": []}
        campaign_count = 0
        stats_note = "未执行：没有可查询的广告活动"
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
        "orders": {
            "status": orders["status"],
            "ok": orders["ok"],
            "rows": len(orders["data"]) if isinstance(orders["data"], list) else None,
            "details": [
                {k: row.get(k) for k in ("date", "lastChangeDate", "orderId", "srid", "nmId", "barcode", "warehouseName", "warehouseType", "status", "supplierStatus", "isCancel", "cancelDate") if k in row}
                for row in (orders["data"] if isinstance(orders["data"], list) else [])
            ],
        },
        "sales": {"status": sales["status"], "ok": sales["ok"], "rows": len(sales["data"]) if isinstance(sales["data"], list) else None},
        "advertising": {"countStatus": ad_count["status"], "statsStatus": ads["status"], "campaigns": campaign_count, "statsNote": stats_note},
        "promotions": {"status": promotions["status"], "ok": promotions["ok"], "items": count_rows(promotions, "data", "promotions")},
    }


def previous_daily_snapshot(day: date) -> dict:
    """Return the most recent complete snapshot from an earlier calendar day."""
    candidates = []
    for path in OUT_DIR.glob("wb_daily_snapshot_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if data.get("dataComplete") is not True:
                continue
            captured = datetime.fromisoformat(data.get("generatedAt", "")).astimezone(TZ)
            if captured.date() < day:
                data["_snapshotPath"] = str(path)
                candidates.append((captured, data))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return max(candidates, default=(None, {}), key=lambda item: item[0])[1]


def compare_value(current: int, previous: dict, key: str) -> str:
    if key not in previous or not isinstance(previous.get(key), int):
        return f"{current}（—）"
    prior = previous[key]
    delta = current - prior
    return f"{current}（{delta:+d}）"


def main() -> None:
    global RUN_DEADLINE_AT
    RUN_DEADLINE_AT = time.monotonic() + RUN_BUDGET_SECONDS
    now = datetime.now(TZ)
    end = now.date()
    start = end - timedelta(days=29)
    client = WbClient(**load_settings().__dict__, timeout=DEFAULT_REQUEST_TIMEOUT)
    # Core calls must be degradable too.  Previously an exception here exited
    # before the snapshot/report paths were created.
    funnel = read_sales_funnel(client, start, end)
    warehouses = readonly_call(client, "GET", "https://marketplace-api.wildberries.ru/api/v3/warehouses")
    content_rows = []
    cursor = {"limit": 100}
    content_status = None
    for _ in range(10):
        content = readonly_call(client, "POST", "https://content-api.wildberries.ru/content/v2/get/cards/list", {
            "settings": {"cursor": cursor, "filter": {"withPhoto": -1, "textSearch": "", "tagIDs": [], "allowedCategoriesOnly": True, "objectIDs": [], "brands": [], "imtID": 0}}
        })
        content_status = content["status"]
        if not content["ok"]:
            break
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
    wb_stock_total = 0
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
        wb_stock_total += wb_stock
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
    core_available = funnel["ok"] and content_status == 200 and warehouses["ok"]
    snapshot = {
        "generatedAt": now.isoformat(timespec="seconds"), "period": {"start": start.isoformat(), "end": end.isoformat(), "timezone": "Asia/Shanghai"},
        "endpoints": {"salesFunnel": {"status": funnel["status"], "ok": funnel["ok"]}, "content": {"status": content_status, "ok": content_status == 200}, "warehouses": {"status": warehouses["status"], "ok": warehouses["ok"]}},
        "dataComplete": core_available,
        "cards": len(content_rows) if content_status == 200 else None,
        "analyticsCards": len(products) if funnel["ok"] else None,
        "totals": dict(totals) if funnel["ok"] else None,
        "categorySummary": {k: dict(v) for k, v in categories.items()} if funnel["ok"] and content_status == 200 else {},
        "searchPerformance": search,
        "extendedReadOnly": extended,
        "focus": focus[:10] if funnel["ok"] else [], "zeroStockSkus": zero_stock if funnel["ok"] else None,
        "activeWarehouses": active_warehouses if warehouses["ok"] else None,
        "sellerStock": stock_total if funnel["ok"] else None,
        "wbStock": wb_stock_total if funnel["ok"] else None,
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
    previous_search_totals = (previous_search.get("totals") or {}) if isinstance(previous_search, dict) else {}
    search_totals = search.get("totals") or {}
    search_position = search.get("position") or {}
    lines += ["", "## Search performance (daily snapshot comparison)", "", f"- Endpoint: POST /api/v2/search-report/report — HTTP {search['status']}", f"- Reporting period: {search['period']['start']} to {search['period']['end']} (30 days)", f"- Returned/requested SKUs: {search.get('returnedSkuCount', 0)}/{search.get('requestedSkuCount', 0)}; missing: {search.get('missingSkuCount', 0)}", "", "| Search metric | Current | Change vs previous daily snapshot |", "|---|---:|---:|"]
    for key, label in (("openCard", "Search card entries"), ("addToCart", "Search add-to-cart"), ("orders", "Search orders")):
        current = search_totals.get(key, 0)
        baseline = previous_search_totals.get(key)
        delta = "— (first comparable capture)" if baseline is None else f"{current - baseline:+d}"
        lines.append(f"| {label} | {current} | {delta} |")
    lines += [f"- Search average / median position: {search_position.get('average', 'unavailable')} / {search_position.get('median', 'unavailable')}.", "- Day-over-day values compare the same 30-day rolling search window and therefore indicate directional movement, not single-day demand."]
    lines += ["", "## 广告与促销状态", "", f"- 广告活动：{extended['advertising']['campaigns']} 个；活动查询 HTTP {extended['advertising']['countStatus']}，统计查询 HTTP {extended['advertising']['statsStatus']}。", f"- 未来 7 天促销：{extended['promotions']['items'] if extended['promotions']['items'] is not None else '不可用'} 个；HTTP {extended['promotions']['status']}。", "", "## 已验证事实、合理推断、待验证项", "", f"- 已验证事实：价格读回 HTTP {extended['prices']['status']}；错误卡 HTTP {extended['cardErrors']['status']}；未回复评价/问题分别为 {extended['feedbacks']['unanswered'] if extended['feedbacks']['unanswered'] is not None else '不可用'} / {extended['questions']['unanswered'] if extended['questions']['unanswered'] is not None else '不可用'}。", f"- 已验证事实：近 7 天订单/销售明细返回行数为 {extended['orders']['rows'] if extended['orders']['rows'] is not None else '不可用'} / {extended['sales']['rows'] if extended['sales']['rows'] is not None else '不可用'}。", "- 合理推断：滚动窗口变化仅表示方向，不等同于当日新增经营结果。", "- 待验证项：若价格、库存、错误卡或履约数据异常，只提出读回核验建议，不在日报中执行修复。", "", "## 数据状态", "", f"- 日趋势接口：商品历史 HTTP {extended['productHistory']['status']}；分组历史 HTTP {extended['groupedHistory']['status']}；库存报告 HTTP {extended['stockReport']['status']}。", "- 本报告只读：未创建或修改商品、价格、库存、广告、促销及买家消息。"]
    # 固定扩展日报合同：章节必须保持顺序，并且中文是交付硬性要求。
    prior_totals = previous.get("totals") if isinstance(previous, dict) else None
    if not isinstance(prior_totals, dict):
        prior_totals = {}
    prior_extended = previous.get("extendedReadOnly") if isinstance(previous, dict) else None
    if not isinstance(prior_extended, dict):
        prior_extended = {}

    def category_total(source: dict, key: str) -> int | None:
        direct = source.get(key)
        if isinstance(direct, int):
            return direct
        category_summary = source.get("categorySummary")
        if not isinstance(category_summary, dict) or not category_summary:
            return None
        values = [row.get(key) for row in category_summary.values() if isinstance(row, dict)]
        return sum(values) if values and all(isinstance(value, int) for value in values) else None

    def prior_extended_value(section: str, key: str, status_key: str = "status") -> int | None:
        source = prior_extended.get(section)
        if not isinstance(source, dict) or source.get(status_key) != 200:
            return None
        value = source.get(key)
        return value if isinstance(value, int) else None

    def metric_row(label: str, current: int | None, prior: int | None, status: int | None = 200) -> str:
        current_text = str(current) if isinstance(current, int) else f"不可用（{status_text(status)}）"
        if not isinstance(current, int) or not isinstance(prior, int):
            return f"| {label} | {current_text} | — | — |"
        return f"| {label} | {current} | {prior} | {current - prior:+d} |"

    current_extended = {
        "ads": extended["advertising"]["campaigns"] if extended["advertising"]["countStatus"] == 200 else None,
        "feedbacks": extended["feedbacks"]["unanswered"] if extended["feedbacks"]["status"] == 200 else None,
        "questions": extended["questions"]["unanswered"] if extended["questions"]["status"] == 200 else None,
        "orders": extended["orders"]["rows"] if extended["orders"]["status"] == 200 else None,
    }
    overview_rows = [
        metric_row("Content 商品卡", len(content_rows), previous.get("cards")),
        metric_row("Analytics 漏斗商品卡", len(products), previous.get("analyticsCards")),
        metric_row("浏览", totals["views"], prior_totals.get("views")),
        metric_row("加购", totals["carts"], prior_totals.get("carts")),
        metric_row("订单", totals["orders"], prior_totals.get("orders")),
        metric_row("买退", totals["buyouts"], prior_totals.get("buyouts")),
        metric_row("收藏", totals["wish"], prior_totals.get("wish")),
        metric_row("卖家库存", stock_total, previous.get("sellerStock")),
        metric_row("WB 库存", wb_stock_total, category_total(previous, "wbStock")),
        metric_row("可用 FBS 仓", active_warehouses, previous.get("activeWarehouses")),
        metric_row("广告活动", current_extended["ads"], prior_extended_value("advertising", "campaigns", "countStatus"), extended["advertising"]["countStatus"]),
        metric_row("未回复评价", current_extended["feedbacks"], prior_extended_value("feedbacks", "unanswered"), extended["feedbacks"]["status"]),
        metric_row("未回复问题", current_extended["questions"], prior_extended_value("questions", "unanswered"), extended["questions"]["status"]),
        metric_row("近 7 天 FBS 订单", current_extended["orders"], prior_extended_value("orders", "rows"), extended["orders"]["status"]),
    ]
    previous_focus = {
        str(item.get("nmID")): item
        for item in (previous.get("focus") or [])
        if isinstance(item, dict) and item.get("nmID") is not None
    }
    def focus_value(item: dict, key: str) -> str:
        current = item[key]
        before = previous_focus.get(str(item["nmID"]), {}).get(key)
        return f"{current}（{current - before:+d}）" if isinstance(before, int) else f"{current}（—）"
    focus_lines = [
        f"| {item['vendorCode']} | {item['nmID']} | {focus_value(item, 'views')} | {focus_value(item, 'carts')} | {focus_value(item, 'orders')} |"
        for item in focus[:10]
    ] or ["| 无 | — | — | — | — |"]
    opportunity_lines = [
        f"- 只读核验优先项：{item['vendorCode']} 有 {item['carts']} 次加购、{item['orders']} 笔订单；提出任何变更前，先核验到手价、配送承诺和前台购买控件。"
        for item in focus[:3] if item["carts"] and not item["orders"]
    ] or ["- 今日没有需要单独执行只读核验的“有加购、无订单”SKU。"]
    search_detail_lines: list[str] = []
    if search["ok"]:
        previous_position = previous_search.get("position") if isinstance(previous_search, dict) else None
        if not isinstance(previous_position, dict):
            previous_position = {}
        search_detail_lines = [
            "| 搜索指标 | 本次 | 上次 | 变化 |", "|---|---:|---:|---:|",
            metric_row("搜索进卡", search_totals.get("openCard"), previous_search_totals.get("openCard")),
            metric_row("搜索加购", search_totals.get("addToCart"), previous_search_totals.get("addToCart")),
            metric_row("搜索订单", search_totals.get("orders"), previous_search_totals.get("orders")),
            metric_row("平均位置", search_position.get("average"), previous_position.get("average")),
            metric_row("中位位置", search_position.get("median"), previous_position.get("median")),
            "", "| 搜索 Top SKU | nmID | 进卡 | 加购 | 订单 | 平均位置 |", "|---|---:|---:|---:|---:|---:|",
        ]
        search_detail_lines += [
            f"| {item.get('vendorCode') or '—'} | {item.get('nmID') or '—'} | {item.get('openCard')} | {item.get('addToCart')} | {item.get('orders')} | {item.get('avgPosition')} |"
            for item in (search.get("topBySearchEntries") or [])
        ] or ["| 无 | — | — | — | — | — |"]
    report_lines = [
        f"# WB 固定早间日报（扩展只读版）— {end.isoformat()}", "",
        "## 1. 快照与口径", "",
        f"- 采集时间：{now.strftime('%Y-%m-%d %H:%M %Z')}；滚动周期：{start} 至 {end}（30 天，Asia/Shanghai）。",
        f"- 上一有效早间快照：{previous.get('generatedAt', '无')}；路径：{previous.get('_snapshotPath', '无')}。",
        "- 比较规则：仅与上一日以前最近的 dataComplete=true 快照比较；同为 30 天滚动窗口，变化只表示方向。",
        "- 商品卡为 Content 商品卡；漏斗指标为 Analytics v3 所选周期汇总；卖家库存为 Marketplace 卖家库存。",
        f"- 核心状态：Sales Funnel {status_text(funnel['status'])}；Content {status_text(content_status)}；搜索 {status_text(search['status'])}；FBS 仓 {status_text(warehouses['status'])}。",
        f"- 扩展状态：商品历史 {status_text(extended['productHistory']['status'])}；分组历史 {status_text(extended['groupedHistory']['status'])}；库存报告 {status_text(extended['stockReport']['status'])}；价格 {status_text(extended['prices']['status'])}；错误卡 {status_text(extended['cardErrors']['status'])}；评价 {status_text(extended['feedbacks']['status'])}；问题 {status_text(extended['questions']['status'])}；订单 {status_text(extended['orders']['status'])}；销售 {status_text(extended['sales']['status'])}；广告计数 {status_text(extended['advertising']['countStatus'])}；促销 {status_text(extended['promotions']['status'])}。",
        "- 本日报仅只读：未修改价格、库存、广告、商品卡、促销或买家消息。", "",
        "## 2. 店铺总览（本次 / 上次 / 变化）", "",
        "| 指标 | 本次 | 上次 | 变化 |", "|---|---:|---:|---:|", *overview_rows, "",
        "## 3. 类目", "",
        "| 类目 | Content 商品卡 | 漏斗商品卡 | 浏览 | 加购 | 订单 | 收藏 | 卖家库存 | WB 库存 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for category, values in sorted(snapshot["categorySummary"].items()):
        prior_category = previous_categories.get(category, {}) if isinstance(previous_categories, dict) else {}
        # Category payloads can be partial when an upstream source is unavailable.
        # Preserve missing values as unavailable instead of aborting report generation.
        report_lines.append("| " + category + " | " + " | ".join(compare_value(values.get(key), prior_category, key) for key in ("contentCards", "analyticsCards", "views", "carts", "orders", "wish", "sellerStock", "wbStock")) + " |")
    report_lines += ["", "## 4. 重点 SKU", "", "| SKU | nmID | 浏览（变化） | 加购（变化） | 订单（变化） |", "|---|---:|---:|---:|---:|", *focus_lines,
        "", "## 5. 搜索表现", "", *(([
            f"- {status_text(search['status'])}；已返回 {search['returnedSkuCount']}/{search['requestedSkuCount']} 个请求 SKU，缺失 {search['missingSkuCount']} 个。",
        ] if search["ok"] else [
            f"- 搜索报告：{status_text(search['status'])}；业务指标不可用，未以零值替代。",
        ])),
        *search_detail_lines,
        "- 搜索变化比较相同的 30 天滚动窗口，仅反映方向，不能视为单日需求。", "",
        "## 6. 异常", "",
        f"- 零库存 SKU：{len(zero_stock)} 个；示例：{', '.join(str(item['vendorCode']) for item in zero_stock[:5]) or '无'}。",
        f"- 有加购无订单的重点 SKU：{sum(1 for item in focus if item['carts'] and not item['orders'])} 个；仅列为只读核验对象。",
        f"- 商品历史 {status_text(extended['productHistory']['status'])}；库存报告 {status_text(extended['stockReport']['status'])}（失败不以零值替代）。",
        f"- 错误卡 {status_text(extended['cardErrors']['status'])}；未回复评价 / 问题：{extended['feedbacks']['unanswered'] if extended['feedbacks']['unanswered'] is not None else '不可用'} / {extended['questions']['unanswered'] if extended['questions']['unanswered'] is not None else '不可用'}（{status_text(extended['feedbacks']['status'])} / {status_text(extended['questions']['status'])}）。",
        f"- 价格读回 {status_text(extended['prices']['status'])}，覆盖 {extended['prices']['goods'] if extended['prices']['goods'] is not None else '不可用'}/{len(content_rows)} 个 Content 商品卡。", "",
        "## 7. 只读行动机会", "", *opportunity_lines, "",
        "## 8. 广告与促销", "",
        f"- 广告活动：{extended['advertising']['campaigns'] if extended['advertising']['campaigns'] is not None else '不可用'} 个；活动计数 {status_text(extended['advertising']['countStatus'])}；广告统计 {status_text(extended['advertising']['statsStatus']) if extended['advertising']['statsStatus'] is not None else extended['advertising']['statsNote']}。",
        f"- 未来 7 天促销：{extended['promotions']['items'] if extended['promotions']['items'] is not None else '不可用'} 个；{status_text(extended['promotions']['status'])}；未自动报名或创建活动。", "",
        "## 9. 事实 / 推断 / 待验证", "",
        f"- 事实：价格 {status_text(extended['prices']['status'])}（{extended['prices']['goods'] if extended['prices']['goods'] is not None else '不可用'} 个商品）；近 7 天订单 / 销售行数 {extended['orders']['rows'] if extended['orders']['rows'] is not None else '不可用'} / {extended['sales']['rows'] if extended['sales']['rows'] is not None else '不可用'}（分别为 {status_text(extended['orders']['status'])} / {status_text(extended['sales']['status'])}）。",
        "- 推断：有加购、无订单仅说明应优先核验，不能据此证明价格、内容或物流存在缺陷。",
        "- 待验证：对重点 SKU 读取前台到手价、预计送达日期和购买控件；核验期间不修改店铺数据。", "",
        "## 10. 数据状态与产物路径", "",
        f"- 核心接口：Sales Funnel {status_text(funnel['status'])}；Content {status_text(content_status)}；搜索 {status_text(search['status'])}；FBS 仓 {status_text(warehouses['status'])}。",
        f"- 扩展读取：商品历史 {status_text(extended['productHistory']['status'])}；分组历史 {status_text(extended['groupedHistory']['status'])}；库存报告 {status_text(extended['stockReport']['status'])}；价格 {status_text(extended['prices']['status'])}；错误卡 {status_text(extended['cardErrors']['status'])}；评价 {status_text(extended['feedbacks']['status'])}；问题 {status_text(extended['questions']['status'])}；订单 {status_text(extended['orders']['status'])}；销售 {status_text(extended['sales']['status'])}；广告计数 {status_text(extended['advertising']['countStatus'])}；促销 {status_text(extended['promotions']['status'])}。",
        f"- 广告统计：{status_text(extended['advertising']['statsStatus']) if extended['advertising']['statsStatus'] is not None else extended['advertising']['statsNote']}。",
        "- 未执行任何写操作：未修改价格、库存、广告、商品资料、促销或买家消息，未创建广告/促销活动。",
        f"- 快照：{raw}", f"- 日报：{report}",
    ]
    if not core_available:
        # Never render missing funnel/search data as zero.  This compact
        # degraded report still fulfils the fixed section contract and records
        # every available source while making the failed sources explicit.
        report_lines = [
            f"# WB \u56fa\u5b9a\u65e9\u95f4\u65e5\u62a5\uff08\u6269\u5c55\u53ea\u8bfb\u7248\uff09\u2014 {end.isoformat()}", "",
            "## 1. \u5feb\u7167\u4e0e\u53e3\u5f84", f"- \u91c7\u96c6\u65f6\u95f4\uff1a{now.strftime('%Y-%m-%d %H:%M %Z')}\uff1b30 \u5929\u6eda\u52a8\u7a97\u53e3\uff1a{start} \u81f3 {end}\u3002", f"- \u6f0f\u6597\uff1a{status_text(funnel['status'])}\uff1bContent\uff1a{status_text(content_status)}\uff1bFBS \u4ed3\uff1a{status_text(warehouses['status'])}\u3002", "",
            "## 2. \u5e97\u94fa\u603b\u89c8\uff08\u672c\u6b21 / \u4e0a\u6b21 / \u53d8\u5316\uff09", f"- Content \u5546\u54c1\u5361\uff1a\u4e0d\u53ef\u7528\uff08{status_text(content_status)}\uff09 / {previous.get('cards') if isinstance(previous.get('cards'), int) else '\u4e0d\u53ef\u7528'} / \u4e0d\u53ef\u6bd4\uff0c\u672a\u4ee5\u96f6\u503c\u4ee3\u66ff\u3002", f"- Analytics \u6f0f\u6597\u3001\u6d4f\u89c8\u3001\u52a0\u8d2d\u3001\u8ba2\u5355\u3001\u6536\u85cf\u3001\u5356\u5bb6\u5e93\u5b58\uff1a\u4e0d\u53ef\u7528\uff08{status_text(funnel['status'])}\uff09\uff0c\u672a\u4ee5\u96f6\u503c\u4ee3\u66ff\u3002", "",
            "## 3. \u7c7b\u76ee", f"- Content \u4e0e Analytics \u6f0f\u6597\u5747\u4e0d\u53ef\u7528\uff08Content {status_text(content_status)}\uff1b\u6f0f\u6597 {status_text(funnel['status'])}\uff09\uff1b\u672a\u4ee5\u65e7\u5feb\u7167\u6216\u96f6\u503c\u66ff\u4ee3\u3002", "",
            "## 4. \u91cd\u70b9 SKU", "- \u6f0f\u6597\u5931\u8d25\uff0c\u65e0\u6cd5\u751f\u6210\u6709\u52a0\u8d2d\u65e0\u8ba2\u5355\u7684 SKU \u6392\u5e8f\u3002", "",
            "## 5. \u641c\u7d22\u8868\u73b0", f"- \u641c\u7d22\u62a5\u544a\uff1a{status_text(search['status'])}\uff1b{'\u8bf7\u6c42 ' + str(search['requestedSkuCount']) + ' \u4e2a SKU' if content_status == 200 else '\u8bf7\u6c42 SKU \u6e05\u5355\u4e0d\u53ef\u7528\uff08Content \u672a\u8fd4\u56de\uff09'}\uff0c\u641c\u7d22\u6307\u6807\u4e0d\u53ef\u7528\uff0c\u672a\u4ee5\u96f6\u503c\u4ee3\u66ff\u3002", "",
            "## 6. \u5f02\u5e38", f"- \u6f0f\u6597 {status_text(funnel['status'])}\uff1b\u5546\u54c1\u5386\u53f2 {status_text(extended['productHistory']['status'])}\uff1b\u5206\u7ec4\u5386\u53f2 {status_text(extended['groupedHistory']['status'])}\uff1b\u5e93\u5b58\u62a5\u544a {status_text(extended['stockReport']['status'])}\u3002", f"- \u9519\u8bef\u5361 {status_text(extended['cardErrors']['status'])}\uff1b\u672a\u56de\u590d\u8bc4\u4ef7/\u95ee\u9898\uff1a{extended['feedbacks']['unanswered'] if extended['feedbacks']['unanswered'] is not None else '\u4e0d\u53ef\u7528'} / {extended['questions']['unanswered'] if extended['questions']['unanswered'] is not None else '\u4e0d\u53ef\u7528'}\uff08\u5206\u522b\u4e3a {status_text(extended['feedbacks']['status'])} / {status_text(extended['questions']['status'])}\uff09\u3002", "",
            "## 7. \u53ea\u8bfb\u884c\u52a8\u673a\u4f1a", "- \u5f85\u6f0f\u6597\u4e0e\u641c\u7d22\u6062\u590d\u540e\uff0c\u4ec5\u8bfb\u56de\u9ad8\u52a0\u8d2d\u96f6\u8ba2\u5355 SKU \u7684\u4ef7\u683c\u3001\u5c65\u7ea6\u4e0e\u8d2d\u4e70\u63a7\u4ef6\u3002", "",
            "## 8. \u5e7f\u544a\u4e0e\u4fc3\u9500", f"- \u5e7f\u544a\u6d3b\u52a8\uff1a{extended['advertising']['campaigns'] if extended['advertising']['campaigns'] is not None else '\u4e0d\u53ef\u7528'}\uff1b\u6d3b\u52a8\u8ba1\u6570 {status_text(extended['advertising']['countStatus'])}\uff0c\u5e7f\u544a\u7edf\u8ba1 {status_text(extended['advertising']['statsStatus']) if extended['advertising']['statsStatus'] is not None else extended['advertising']['statsNote']}\u3002", f"- \u672a\u6765 7 \u5929\u4fc3\u9500\uff1a{extended['promotions']['items'] if extended['promotions']['items'] is not None else '\u4e0d\u53ef\u7528'}\uff1b{status_text(extended['promotions']['status'])}\u3002", "",
            "## 9. \u4e8b\u5b9e / \u63a8\u65ad / \u5f85\u9a8c\u8bc1", f"- \u4e8b\u5b9e\uff1a\u4ef7\u683c {status_text(extended['prices']['status'])}\uff08{'\u5546\u54c1\u6570 ' + str(extended['prices']['goods']) if extended['prices']['goods'] is not None else '\u5546\u54c1\u6570\u4e0d\u53ef\u7528'}\uff09\uff1b\u8fd1 7 \u5929\u8ba2\u5355/\u9500\u552e\u884c\u6570 {extended['orders']['rows'] if extended['orders']['rows'] is not None else '\u4e0d\u53ef\u7528'} / {extended['sales']['rows'] if extended['sales']['rows'] is not None else '\u4e0d\u53ef\u7528'}\u3002", "- \u63a8\u65ad\uff1a\u672c\u6b21\u4e0d\u8db3\u4ee5\u5f52\u56e0\u4e3a\u4ef7\u683c\u3001\u5e93\u5b58\u6216\u5185\u5bb9\u95ee\u9898\u3002", "- \u5f85\u9a8c\u8bc1\uff1a\u91cd\u8bd5 Analytics \u540e\u518d\u8fdb\u884c SKU \u6f0f\u6597\u4e0e\u524d\u53f0\u53ea\u8bfb\u6838\u9a8c\u3002", "",
            "## 10. \u6570\u636e\u72b6\u6001\u4e0e\u4ea7\u7269\u8def\u5f84", f"- \u6838\u5fc3\uff1a\u6f0f\u6597 {status_text(funnel['status'])}\uff1bContent {status_text(content_status)}\uff1bFBS \u4ed3 {status_text(warehouses['status'])}\uff1b\u641c\u7d22 {status_text(search['status'])}\u3002", f"- \u6269\u5c55\uff1a\u5546\u54c1\u5386\u53f2 {status_text(extended['productHistory']['status'])}\uff1b\u5206\u7ec4\u5386\u53f2 {status_text(extended['groupedHistory']['status'])}\uff1b\u5e93\u5b58\u62a5\u544a {status_text(extended['stockReport']['status'])}\uff1b\u4ef7\u683c {status_text(extended['prices']['status'])}\uff1b\u9519\u8bef\u5361 {status_text(extended['cardErrors']['status'])}\uff1b\u8bc4\u4ef7 {status_text(extended['feedbacks']['status'])}\uff1b\u95ee\u9898 {status_text(extended['questions']['status'])}\uff1b\u8ba2\u5355 {status_text(extended['orders']['status'])}\uff1b\u9500\u552e {status_text(extended['sales']['status'])}\uff1b\u5e7f\u544a\u8ba1\u6570 {status_text(extended['advertising']['countStatus'])}\uff1b\u5e7f\u544a\u7edf\u8ba1 {status_text(extended['advertising']['statsStatus']) if extended['advertising']['statsStatus'] is not None else extended['advertising']['statsNote']}\uff1b\u4fc3\u9500 {status_text(extended['promotions']['status'])}\u3002", "- \u5168\u7a0b\u53ea\u8bfb\uff1b\u672a\u4fee\u6539\u4ef7\u683c\u3001\u5e93\u5b58\u3001\u5e7f\u544a\u3001\u5546\u54c1\u8d44\u6599\u3001\u4fc3\u9500\u6216\u4e70\u5bb6\u6d88\u606f\u3002", f"- \u5feb\u7167\uff1a{raw}", f"- \u65e5\u62a5\uff1a{report}",
        ]
    report.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report), "snapshot": str(raw), **snapshot}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except WbApiError as exc:
        raise SystemExit(str(exc))
