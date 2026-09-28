"""Read-only WB daily snapshot from Analytics v3 and seller stock APIs."""
from __future__ import annotations

import json
import os
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
EXCLUDED_DAILY_SKUS = {
    "AI09B", "AI22-W", "AI22-S",
    "wb2puneize", "wb4rq3qu0a", "wb5s8veazu", "wb1pbrecq",
    "wb3mxii822", "wb77qu0scq", "wbeubgnl6", "wb4hny8p2i",
}
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
    promotions = readonly_call(client, "GET", f"https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions?startDateTime={today.isoformat()}T00:00:00Z&endDateTime={(today + timedelta(days=6)).isoformat()}T23:59:59Z&allPromo=true")
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
        "promotions": {"status": promotions["status"], "ok": promotions["ok"], "items": count_rows(promotions, "data", "promotions"), "period": {"start": today.isoformat(), "end": (today + timedelta(days=6)).isoformat(), "timezone": "UTC"}},
    }


def previous_daily_snapshot(day: date) -> dict:
    """Return the most recent complete snapshot from an earlier calendar day."""
    candidates = []
    alias = os.environ.get("WB_DAILY_STORE_ALIAS")
    baseline_dir = OUT_DIR / "multi_store" / alias if alias else OUT_DIR
    for path in baseline_dir.glob("wb_daily_snapshot_*.json"):
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
    low_stock = []
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
        vendor_code = product.get("vendorCode") or "—"
        if mp_stock + wb_stock == 0 and vendor_code not in EXCLUDED_DAILY_SKUS:
            zero_stock.append({"vendorCode": product.get("vendorCode") or "—", "nmID": product.get("nmId") or product.get("nmID") or "—"})
        if mp_stock + wb_stock < 15 and vendor_code not in EXCLUDED_DAILY_SKUS:
            low_stock.append({"vendorCode": vendor_code, "nmID": product.get("nmId") or product.get("nmID") or "—", "sellerStock": mp_stock, "wbStock": wb_stock, "totalStock": mp_stock + wb_stock})
        if carts and vendor_code not in EXCLUDED_DAILY_SKUS:
            focus.append({
                "vendorCode": vendor_code or row.get("vendorCode") or "—",
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
        "storeAlias": os.environ.get("WB_DAILY_STORE_ALIAS"),
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
        "lowStockSkus": sorted(low_stock, key=lambda x: x["totalStock"]) if funnel["ok"] else None,
        "activeWarehouses": active_warehouses if warehouses["ok"] else None,
        "sellerStock": stock_total if funnel["ok"] else None,
        "wbStock": wb_stock_total if funnel["ok"] else None,
    }
    stamp = now.strftime("%Y%m%d_%H%M%S")
    raw = OUT_DIR / f"wb_daily_snapshot_{stamp}.json"
    report = OUT_DIR / f"wb_daily_report_{stamp}.md"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    from tools.wb_daily_report_render import render_store

    previous = previous_daily_snapshot(end)
    report.write_text(
        render_store(snapshot, previous, os.environ.get("WB_DAILY_STORE_ALIAS", "owner-store"), str(raw), str(report)),
        encoding="utf-8",
    )
    print(json.dumps({"report": str(report), "snapshot": str(raw), **snapshot}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except WbApiError as exc:
        raise SystemExit(str(exc))
