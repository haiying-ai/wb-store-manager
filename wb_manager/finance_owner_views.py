"""Pure owner-facing event views with store-isolated, evidence-based joins.

The flow view is the one financial population. The order view is a labelled
subset, not another set of transactions to add to it. Scenarios replace the
same event's values; ``estimateAmounts`` contains only the incremental change.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Any
from zoneinfo import ZoneInfo

from .finance_running_account import METRICS

MOSCOW = ZoneInfo("Europe/Moscow")
CENT = Decimal("0.01")
_INVALID = {"", "0", "none", "null", "undefined"}


def _day(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        moment = moment.replace(tzinfo=MOSCOW) if moment.tzinfo is None else moment.astimezone(MOSCOW)
        return moment.date().isoformat()
    except (ValueError, TypeError):
        return None


def _identifier(value: Any, *, numeric: bool = False) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    result = str(value).strip()
    if result.casefold() in _INVALID or (numeric and not result.isdigit()):
        return None
    return result


def _money(value: Any) -> str | None:
    if value is None:
        return None
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Nonfinite monetary value in owner view")
    return format(number.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def _periods(cutoff: str, opened_at: str) -> list[dict[str, str]]:
    through = date.fromisoformat(cutoff)
    run_day = through + timedelta(days=1)
    month_end = run_day.replace(day=1) - timedelta(days=1)
    week_end = run_day - timedelta(days=run_day.weekday() + 1)
    return [
        {"key": "cumulative", "label": "累计（开店至截止）", "start": opened_at, "end": cutoff},
        {"key": "last_month", "label": "上月", "start": month_end.replace(day=1).isoformat(), "end": month_end.isoformat()},
        {"key": "last_week", "label": "上周", "start": (week_end - timedelta(days=6)).isoformat(), "end": week_end.isoformat()},
        {"key": "yesterday", "label": "昨日", "start": cutoff, "end": cutoff},
    ]


def period_windows(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Return the four canonical calendar windows for account/report callers."""
    cutoff = _day(payload.get("periodEnd"))
    if not cutoff:
        raise ValueError("Owner view requires periodEnd")
    stores = (payload.get("sources") or {}).get("stores") or []
    opened_at = min((_day(row.get("openedAt")) or cutoff for row in stores),
                    default=_day(payload.get("periodStart")) or cutoff)
    return {row["key"]: row for row in _periods(cutoff, opened_at)}


def _raw(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("raw") or row.get("payload") or row


def _evidence_ids(row: dict[str, Any]) -> list[str]:
    values = row.get("evidence") or row.get("evidenceIds") or []
    return [str(value) for value in values]


def _typed_ids(row: dict[str, Any], *, posting: bool = False) -> set[tuple[str, str]]:
    raw = _raw(row)
    metadata = row.get("metadata") or {}
    identifiers: set[tuple[str, str]] = set()
    for source in (raw, row, metadata):
        for kind, keys in (("orderId", ("orderId", "order_id", "odid")),
                           ("srid", ("srid",)), ("rid", ("rid",)),
                           ("shkId", ("shkId", "shk_id", "stickerId", "sticker")),
                           ("rrdId", ("rrdId", "rrd_id")),
                           ("saleId", ("saleID", "saleId", "sale_id"))):
            for key in keys:
                value = _identifier(source.get(key), numeric=kind in {"orderId", "shkId", "rrdId"})
                if value:
                    identifiers.add((kind, value))
        # Native statistics historically labelled an srid as orderId. Keep
        # that identity usable, but never show it as a numeric WB order number.
        possible_srid = _identifier(source.get("order_id") or source.get("orderId"))
        if possible_srid and not possible_srid.isdigit() and "." in possible_srid:
            identifiers.add(("srid", possible_srid.removeprefix("srid:")))
    for evidence in _evidence_ids(row):
        for value in re.findall(r"(?:rrdId:|:rrd:)(\d+)", evidence):
            identifiers.add(("rrdId", value))
        for value in re.findall(r"\b([SR]\d{5,})\b", evidence):
            identifiers.add(("saleId", value))
    marketplace_order = (str(row.get("source") or "").startswith("marketplace") or
                         {"id", "rid", "createdAt", "price"}.issubset(raw))
    if marketplace_order:
        # WB marketplace orders use id for the numeric assembly-task order
        # number and rid for the exact order line identity seen as srid by
        # finance/statistics. Never strip item suffixes or use orderUid alone.
        order_id = _identifier(raw.get("id"), numeric=True)
        rid = _identifier(raw.get("rid"))
        if order_id:
            identifiers.add(("orderId", order_id))
        if rid:
            identifiers.add(("srid", rid))
    if posting:
        event_id = str(row["event_id"])
        identifiers.add(("eventId", event_id))
        if row.get("original_sale_id"):
            identifiers.add(("eventId", str(row["original_sale_id"])))
        for value in re.findall(r":rrd:(\d+)(?::|$)", event_id):
            identifiers.add(("rrdId", value))
        if ":srid:" in event_id:
            identifiers.add(("srid", event_id.split(":srid:", 1)[1].split(":part:", 1)[0]))
    return identifiers


class _IdentityIndex:
    def __init__(self):
        self.parent: list[int] = []
        self.nodes: list[dict[str, Any]] = []
        self.lookup: dict[tuple[str, str, str], int] = {}

    def find(self, node: int) -> int:
        if self.parent[node] != node:
            self.parent[node] = self.find(self.parent[node])
        return self.parent[node]

    def add(self, store: str, row: dict[str, Any], label: str, *, posting: bool = False) -> int:
        node = len(self.nodes)
        identifiers = _typed_ids(row, posting=posting)
        self.parent.append(node)
        self.nodes.append({"store": store, "row": row, "label": label, "ids": identifiers})
        for kind, value in identifiers:
            key = (store, kind, value)
            if key in self.lookup:
                self.parent[self.find(node)] = self.find(self.lookup[key])
            else:
                self.lookup[key] = node
        return node

    def groups(self) -> dict[int, dict[str, Any]]:
        groups: dict[int, dict[str, Any]] = {}
        for index, node in enumerate(self.nodes):
            group = groups.setdefault(self.find(index), {"ids": defaultdict(set), "nodes": []})
            for kind, value in node["ids"]:
                group["ids"][kind].add(value)
            group["nodes"].append(node)
        return groups


def _kind_label(row: dict[str, Any]) -> str:
    if row.get("kind") == "sale":
        return "销售"
    if row.get("kind") == "refund":
        return "退款冲回"
    amounts = row.get("amounts") or {}
    event_id = str(row.get("event_id") or "")
    for metric, label in (("withdrawal_cancelled", "提现记录更正／取消"), ("withdrawal_completed", "提现到账"),
                          ("withdrawal_requested", "申请提现"), ("agency_paid", "代运营费实收"),
                          ("receipts_received", "店铺回款到账"), ("promo_credit_spend", "平台促销金消耗"),
                          ("cash_ad_spend", "现金广告消耗")):
        if metric in amounts and amounts[metric] not in (None, "0", "0.00", 0):
            return label
    if "return_logistics" in event_id or any(key in amounts for key in ("shared_pool_return_cost", "separate_return_cost")):
        return "退货／取消运费"
    if "forward_logistics" in event_id or "logistics_cost" in amounts:
        return "去程运费"
    if "other_cost" in amounts:
        return "其他平台费用"
    return "其他流水"


def _store_level(row: dict[str, Any]) -> bool:
    metadata = row.get("metadata") or {}
    scope = str(metadata.get("scope") or "").strip().casefold()
    # Only an explicit source/accounting scope establishes that an account
    # charge belongs to the store. Missing order identifiers alone do not.
    if scope in {"store", "store_level"} and row.get("kind") not in {"sale", "refund"}:
        return True
    metrics = set((row.get("amounts") or {}).keys())
    store_metrics = {"cash_ad_spend", "promo_credit_spend", "withdrawal_requested", "withdrawal_completed",
                     "withdrawal_cancelled", "agency_paid", "receipts_received"}
    # Campaign spend and store cash movements have no reliable order
    # attribution in these sources. A campaign's SKU is not an order linkage.
    return bool(metrics & store_metrics) and row.get("kind") not in {"sale", "refund"}


def _postings(rows: list[dict[str, Any]], cutoff: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    result = {}
    for row in rows:
        event_day = _day(row.get("event_date"))
        if not event_day:
            raise ValueError(f"Undated financial event {row.get('event_id')}")
        if event_day > cutoff:
            continue
        key = (str(row["store_id"]), str(row["event_id"]), str(row.get("currency") or "CNY"))
        if key in result and result[key] != row:
            raise ValueError(f"Conflicting repeated financial event {key}")
        result[key] = row
    return result


def build_owner_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Build stable order-event and all-flow views without mutating input.

    Values are decimal strings or ``None``. ``amounts`` selects the approved
    reference scenario once; it must not be added to ``actualAmounts`` again.
    Every financial flow is either an order-event row or a store-level flow.
    Pending order associations remain in ``orderRows`` with clear status.
    """
    cutoff = _day(payload.get("periodEnd"))
    if not cutoff:
        raise ValueError("Owner view requires periodEnd")
    sources = payload.get("sources") or {}
    stores = sources.get("stores") or [{"storeAlias": alias, "openedAt": payload.get("periodStart")} for alias in payload.get("actual", {}).get("stores", {}) if alias != "ALL"]
    aliases = {str(row["storeAlias"]) for row in stores}
    periods = list(period_windows(payload).values())
    actual = _postings(payload.get("postings") or [], cutoff)
    scenario = _postings(payload.get("scenarioPostings") or [], cutoff)
    approved = (payload.get("ownerDecisions") or {}).get("estimateApproval") == "approved_reference_only"
    index = _IdentityIndex()
    source_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for category in ("settlementSales", "settlementCharges", "rawOrders", "rawStatistics", "rawReturns"):
        for ordinal, row in enumerate(sources.get(category) or []):
            store = str(row.get("storeAlias") or "")
            if store not in aliases:
                continue
            raw = _raw(row)
            event_day = _day(row.get("occurredAt") or raw.get("date") or raw.get("createdAt"))
            if event_day and event_day > cutoff:
                continue
            source_counts[store][category] += 1
            index.add(store, row, f"{category}:{row.get('eventId') or row.get('rrdId') or ordinal}")
    for ordinal, row in enumerate(payload.get("nativeStatistics") or []):
        store = str(row.get("store") or row.get("storeAlias") or "")
        if store in aliases and (_day(row.get("date")) or cutoff) <= cutoff:
            index.add(store, row, f"nativeStatistics:{row.get('saleId') or ordinal}")
    posting_nodes = {}
    for key in sorted(actual.keys() | scenario.keys()):
        if key[0] not in aliases:
            continue
        row = actual.get(key) or scenario[key]
        if not _store_level(row):
            posting_nodes[key] = index.add(key[0], row, "posting:" + key[1], posting=True)
    groups = index.groups()
    output = {"schemaVersion": 1, "cutoff": cutoff, "financialTimezone": "Europe/Moscow", "periods": periods,
              "referenceEstimateApproved": approved, "stores": {}, "issues": [],
              "reconciliationRule": "orderRows plus flowRows where isOrderEvent=false equals all flowRows; actual/scenario are alternative bases, never additive"}
    for store in stores:
        alias = str(store["storeAlias"])
        output["stores"][alias] = {"displayName": store.get("displayName") or store.get("name") or alias,
            "openedAt": _day(store.get("openedAt")), "orderRows": [], "flowRows": [], "mappingSummary": {}}
    for key in sorted(actual.keys() | scenario.keys()):
        alias, event_id, currency = key
        if alias not in output["stores"]:
            continue
        a = actual.get(key)
        s = scenario.get(key)
        base = a or s
        if a and s and (_day(a["event_date"]) != _day(s["event_date"]) or a.get("kind") != s.get("kind")):
            raise ValueError(f"Scenario changes financial event identity/date {key}")
        selected = s if approved and s is not None else a
        if selected is None:
            continue
        metric_names = sorted(set(METRICS) | set((a or {}).get("amounts") or {}) | set((s or {}).get("amounts") or {}))
        actual_amounts = {metric: _money((a or {}).get("amounts", {}).get(metric, 0)) for metric in metric_names}
        selected_amounts = {metric: _money(selected.get("amounts", {}).get(metric, 0)) for metric in metric_names}
        estimates = {metric: (None if selected_amounts[metric] is None else _money(Decimal(selected_amounts[metric]) - Decimal(actual_amounts[metric] or "0"))) for metric in metric_names}
        estimated = [metric for metric in metric_names if selected_amounts[metric] is not None and
                     (actual_amounts[metric] is None or estimates[metric] != "0.00")]
        group = groups.get(index.find(posting_nodes[key])) if key in posting_nodes else None
        ids = group["ids"] if group else defaultdict(set)
        order_ids = sorted(ids.get("orderId", set()))
        srids = sorted(ids.get("srid", set()))
        store_level = _store_level(base)
        is_order = not store_level
        mapping_status = "店铺级" if store_level else ("已关联" if len(order_ids) == 1 else "待匹配")
        order_id = order_ids[0] if len(order_ids) == 1 else None
        metadata = base.get("metadata") or {}
        sku = metadata.get("sku")
        created = metadata.get("createdAt") or metadata.get("orderCreatedAt")
        labels = []
        skus = set()
        created_candidates = []
        if group:
            for node in group["nodes"]:
                raw = _raw(node["row"])
                item_sku = node["row"].get("sku") or raw.get("vendorCode") or raw.get("supplierArticle") or raw.get("article")
                if item_sku:
                    skus.add(str(item_sku))
                item_created = node["row"].get("orderCreatedAt") or raw.get("createdAt") or raw.get("orderDt")
                if not item_created and node["label"].startswith("rawOrders:"):
                    item_created = raw.get("date")
                if item_created:
                    created_candidates.append(str(item_created))
                labels.append(node["label"])
                labels.extend(_evidence_ids(node["row"]))
                if node["row"].get("sourceFile"):
                    labels.append("sourceFile:" + str(node["row"]["sourceFile"]))
        if not sku and len(skus) == 1:
            sku = next(iter(skus))
        if not created and created_candidates:
            created = min(created_candidates, key=lambda value: _day(value) or "9999-12-31")
        own_ids = _typed_ids(base, posting=True)
        own_srids = sorted(value for kind, value in own_ids if kind == "srid")
        own_rrds = sorted(value for kind, value in own_ids if kind == "rrdId")
        own_sales = sorted(value for kind, value in own_ids if kind == "saleId")
        if not own_sales and group:
            prefix = "R" if base.get("kind") == "refund" else "S"
            own_sales = sorted(value for value in ids.get("saleId", []) if value.startswith(prefix))
        event_day = _day(base["event_date"])
        reason = ("广告消耗按活动／店铺记账，无可靠订单归属" if store_level and event_id.startswith("ad:") else
                  "店铺资金流水，不强挂订单" if store_level else
                  "多个订单号冲突，未自动合并" if len(order_ids) > 1 else
                  "已凭平台稳定标识关联" if order_id else "保留原始标识，尚缺可靠WB订单号")
        row = {"eventId": event_id, "eventDate": event_day, "kind": base.get("kind"), "kindLabel": _kind_label(base),
               "storeAlias": alias, "currency": currency, "orderId": order_id,
               "srid": own_srids[0] if len(own_srids) == 1 else (srids[0] if len(srids) == 1 else None),
               "srids": srids, "shkIds": sorted(ids.get("shkId", set())), "rid": sorted(ids.get("rid", set())),
               "rrdId": own_rrds[0] if len(own_rrds) == 1 else None, "rrdIds": own_rrds,
               "saleId": own_sales[0] if len(own_sales) == 1 else None, "saleIds": own_sales,
               "sku": sku, "orderCreatedAt": created, "originalSaleId": base.get("original_sale_id"),
               "mappingStatus": mapping_status, "mappingReason": reason, "linkEvidence": sorted(set(labels + _evidence_ids(base))),
               "isOrderEvent": is_order, "actualAmounts": actual_amounts, "estimateAmounts": estimates, "amounts": selected_amounts,
               "estimatedMetrics": estimated, "unknownActualMetrics": [metric for metric, value in actual_amounts.items() if value is None],
               "amountBasis": "含暂估" if estimated else ("待结算" if any(value is None for value in selected_amounts.values()) else "已核实／已确认规则"),
               "costVersionId": metadata.get("costVersionId"), "policyId": metadata.get("policyId"),
               "sourceBasis": metadata.get("basis"), "saleableReturn": base.get("saleable_return"),
               "periodMembership": [period["key"] for period in periods if period["start"] <= event_day <= period["end"]]}
        if len(order_ids) > 1:
            output["issues"].append({"storeAlias": alias, "eventId": event_id, "reason": "conflicting_order_identities", "orderIds": order_ids})
        output["stores"][alias]["flowRows"].append(row)
        if is_order:
            output["stores"][alias]["orderRows"].append(deepcopy(row))
    for alias, store in output["stores"].items():
        for field in ("orderRows", "flowRows"):
            store[field].sort(key=lambda row: (row["eventDate"], row["eventId"], row["currency"]))
        flows, orders = store["flowRows"], store["orderRows"]
        store["mappingSummary"] = {"financialEvents": len(flows), "orderFinancialEvents": len(orders),
            "linkedOrderEvents": sum(row["mappingStatus"] == "已关联" for row in orders),
            "pendingOrderEvents": sum(row["mappingStatus"] == "待匹配" for row in orders),
            "storeLevelEvents": sum(not row["isOrderEvent"] for row in flows),
            "distinctMappedOrderIds": len({row["orderId"] for row in orders if row["orderId"]}),
            "eventsWithEstimate": sum(bool(row["estimatedMetrics"]) for row in flows),
            "sourceRecords": dict(source_counts[alias]),
            "partitionComplete": len(orders) + sum(not row["isOrderEvent"] for row in flows) == len(flows)}
    return output
