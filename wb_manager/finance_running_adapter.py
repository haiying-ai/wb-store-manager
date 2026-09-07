"""Bridge verified WB evidence to persistent owner accounts and a fixed template.

No marketplace calls or funding actions occur here. Native statistics and
unapproved management estimates remain separate from settlement cash facts.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .finance_running_account import (FinanceEvent, RunningAccount, METRICS,
    sale_event, refund_event, cash_event, materialize_events, money)
from .finance_running_sources import extract_running_account_sources
from .finance_costs import normalize_product_key
from .finance_owner_views import build_owner_view, period_windows

TEMPLATE_VERSION = "wb-owner-dashboard-v2"
MOSCOW = ZoneInfo("Europe/Moscow")
FIELD_MAP = {
    "salesAmountGCny": "gross_sales", "forPayCny": "seller_proceeds",
    "purchaseCostCny": "product_cost", "managedOperationsCny": "agency_accrued",
    "promotionReserveCny": "promotion_accrued", "returnRiskReserveCny": "return_reserve_accrued",
}

def _read(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else ({} if default is None else default)

def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".w-{uuid4().hex[:16]}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(7):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                # Windows can briefly hold the destination during another
                # atomic replacement or a scanner read. Keep the old file
                # intact, retry for <1 second, and surface persistent locks.
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 6:
                    raise
                time.sleep(0.01 * (2 ** attempt))
    finally:
        if temporary.exists():
            temporary.unlink()


def _merge_publication(database: Path, publication_key: str, record: dict[str, Any]) -> None:
    """Serialize read/merge/replace across independent weekly/monthly jobs.

    The ledger's SQLite write reservation is a bounded cross-process mutex;
    no ledger rows are changed here. Holding it through the atomic replacement
    prevents a stale manifest read from dropping another period's publication.
    """
    database.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = database.with_suffix(".publications.json")
    connection = sqlite3.connect(database, timeout=30)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            manifest = _read(manifest_path)
            manifest[publication_key] = record
            _write(manifest_path, manifest)
    finally:
        connection.close()

def _settings(service):
    cfg = service.periodic_standard.raw.get("runningAccounts", {})
    def path(key, fallback):
        p = Path(cfg.get(key) or fallback)
        return p if p.is_absolute() else service.repo_root / p
    paths = {
        "db": path("database", "data/finance/wb_running_accounts.sqlite3"),
        "decisions": path("decisions", "config/finance/owner_accounting_decisions.local.json"),
        "imports": path("sourceImports", "config/finance/running_source_imports.local.json"),
        "output": path("ownerReportRoot", "outputs/wb-owner-running-reports"),
        "builder": path("builder", "wb_manager/assets/build_running_finance_report.mjs"),
        "archive": path("archiveRoot", cfg["archiveRoot"]) if cfg.get("archiveRoot") else None,
    }
    ledger = Path(service.ledger.path).resolve()
    canonical = (service.repo_root / "data/finance/wb_finance.sqlite3").resolve()
    if ledger != canonical:
        # A custom --db (including a diagnostic/test database) must never write
        # the default owner's running ledger or consume unrelated owner facts.
        defaults = {
            "db": (service.repo_root / "data/finance/wb_running_accounts.sqlite3").resolve(),
            "decisions": (service.repo_root / "config/finance/owner_accounting_decisions.local.json").resolve(),
            "imports": (service.repo_root / "config/finance/running_source_imports.local.json").resolve(),
            "output": (service.repo_root / "outputs/wb-owner-running-reports").resolve(),
        }
        adjacent = {"db": ledger.with_name(ledger.stem + ".running.sqlite3"),
                    "decisions": ledger.with_name(ledger.stem + ".owner_decisions.json"),
                    "imports": ledger.with_name(ledger.stem + ".source_imports.json"),
                    "output": ledger.parent / (ledger.stem + ".running_reports")}
        for key, default in defaults.items():
            if paths[key].resolve() == default:
                paths[key] = adjacent[key]
        # Report roots inherited from the active owner's configuration can use
        # arbitrary thread/output names; never reuse them for a custom ledger.
        paths["output"] = adjacent["output"]
        if paths["archive"] is not None:
            paths["archive"] = ledger.parent / (ledger.stem + ".running_archive")
    return paths

def _state_path(service):
    return _settings(service)["db"].with_suffix(".state.json")

def _day(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=MOSCOW) if parsed.tzinfo is None else parsed.astimezone(MOSCOW)).date().isoformat()


def _instant(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=MOSCOW) if parsed.tzinfo is None else parsed.astimezone(MOSCOW)


def _raw(row):
    return row.get("payload", row.get("raw", row))


def _historical_cost(sources, sku, created):
    if not sku or not created:
        return None
    try:
        key = normalize_product_key(sku)
        moment = _instant(created)
    except ValueError:
        return None
    matches = []
    for row in sources.get("approvedCostVersions", []):
        if not row.get("approved_by") or not row.get("effective_from"):
            continue
        aliases = [row.get("sku"), *json.loads(row.get("sku_aliases_json") or "[]")]
        try:
            matched = any(normalize_product_key(alias) == key for alias in aliases if alias)
        except ValueError:
            matched = False
        if matched and _instant(row["effective_from"]) <= moment and (not row.get("effective_to") or moment < _instant(row["effective_to"])):
            matches.append(row)
    unique = {row["cost_version_uid"]: row for row in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _issue(alias, identity, occurred, reason):
    return {"storeAlias": alias, "eventId": identity, "eventDate": _day(occurred), "reason": reason}


def _coverage_days(row, cutoff):
    start = _instant(row["period_start"])
    end = _instant(row["period_end"])
    # Source coverage uses exclusive midnight for period-close boundaries;
    # ingestion ranges ending 23:59:59 remain inclusive.
    if end.time().replace(tzinfo=None) == datetime.min.time() and end > start:
        end -= timedelta(microseconds=1)
    return start.date().isoformat(), min(end.date().isoformat(), cutoff)


def _semantic_state(value):
    volatile = {"refreshedAt", "counts", "sourceStatuses", "contentHash", "sourceFetchedAt",
                "fetched_at", "captured_at", "elapsedMs"}
    if isinstance(value, dict):
        return {key: _semantic_state(item) for key, item in value.items() if key not in volatile}
    if isinstance(value, list):
        return [_semantic_state(item) for item in value]
    return value

def _upsert(account, event, counts):
    history = account.event_history(event.store_id, event.event_id)
    if history and history[-1]["payload"].get("metadata", {}).get("basis") == "platform_settlement" and event.metadata.get("basis") in {"awaiting_settlement", "original_accrual_reversal"}:
        counts["unchanged"] += 1
        return
    result = account.upsert_event(event, expected_revision=history[-1]["revision"] if history else None,
        correction_reason="Source evidence or explicitly confirmed owner facts changed" if history else None)
    counts["updated" if history and result["changed"] else "inserted" if result["changed"] else "unchanged"] += 1

def _imports(service):
    rows = []
    for entry in _read(_settings(service)["imports"]).get("imports", []):
        path = Path(entry["path"])
        path = path if path.is_absolute() else service.repo_root / path
        if path.is_file():
            rows.append((entry, _read(path)))
    return rows

def _supplement_marketplace_orders(service, sources):
    """Import explicitly configured, successful local order-history captures.

    These are identity/currency evidence, never new sale or cash events. Do
    not infer a store from its filename or accept a failed response as empty.
    """
    paths = _read(_settings(service)["imports"]).get("marketplaceOrderHistory", [])
    candidates = []
    for entry in paths:
        path = Path(entry)
        path = path if path.is_absolute() else service.repo_root / path
        if not path.is_file():
            sources.setdefault("issues", []).append({"reason": "marketplace_order_archive_missing", "sourceFile": str(path)})
            continue
        content = _read(path)
        response = content.get("response") or content
        status = content.get("status", content.get("statusCode", response.get("status", response.get("statusCode"))))
        alias = content.get("storeAlias", response.get("storeAlias"))
        data = response.get("data") or {}
        if str(status) != "200" or content.get("ok") is False or response.get("ok") is False:
            sources.setdefault("issues", []).append({"storeAlias": alias, "reason": "marketplace_order_archive_not_successful",
                "sourceFile": str(path), "statusCode": status})
            continue
        if alias not in service.store_aliases or not isinstance(data, dict) or not isinstance(data.get("orders"), list):
            sources.setdefault("issues", []).append({"storeAlias": alias, "reason": "marketplace_order_archive_invalid_scope_or_shape", "sourceFile": str(path)})
            continue
        for raw in data["orders"]:
            if not isinstance(raw, dict):
                sources.setdefault("issues", []).append({"storeAlias": alias, "reason": "marketplace_order_archive_invalid_order", "sourceFile": str(path)})
                continue
            if raw.get("id") in (None, "") and raw.get("rid") in (None, ""):
                sources.setdefault("issues", []).append({"storeAlias": alias, "reason": "marketplace_order_archive_missing_identity", "sourceFile": str(path)})
                continue
            candidates.append({"storeAlias": alias, "source": "marketplace_orders", "sourceType": "marketplace_orders",
                "occurredAt": raw.get("createdAt"), "sourceFile": str(path),
                "evidenceIds": ["marketplace-order-archive:" + str(path)], "raw": dict(raw)})
    if not candidates:
        return
    rows = []
    identities = {}
    for wrapped in [*sources.get("rawOrders", []), *candidates]:
        raw, alias = _raw(wrapped), wrapped.get("storeAlias")
        keys = [(alias, kind, str(value)) for kind, value in
            (("orderId", raw.get("id") or raw.get("orderId")), ("rid", raw.get("rid"))) if value not in (None, "")]
        indexes = {identities[key] for key in keys if key in identities}
        if len(indexes) > 1:
            sources.setdefault("issues", []).append({"storeAlias": alias, "reason": "marketplace_order_archive_conflicting_identity",
                "orderId": str(raw.get("id") or raw.get("orderId") or ""), "sourceFile": wrapped.get("sourceFile")})
            rows.append(wrapped)
            continue
        if indexes:
            index = next(iter(indexes))
            previous = rows[index]
            previous_raw = _raw(previous)
            if any(previous_raw.get(field) not in (None, "") and raw.get(field) not in (None, "")
                   and str(previous_raw[field]) != str(raw[field]) for field in ("id", "orderId", "rid")):
                sources.setdefault("issues", []).append({"storeAlias": alias, "reason": "marketplace_order_archive_conflicting_identity",
                    "orderId": str(raw.get("id") or raw.get("orderId") or ""), "sourceFile": wrapped.get("sourceFile")})
                rows.append(dict(wrapped))
                continue
            # A repeated archive may enrich a live order's missing identity,
            # but must not overwrite a field from the current local source.
            previous["raw"] = {**raw, **previous_raw}
            evidence = set(previous.get("evidenceIds") or []) | set(wrapped.get("evidenceIds") or [])
            if evidence:
                previous["evidenceIds"] = sorted(evidence)
            if wrapped.get("sourceFile"):
                previous.setdefault("sourceFile", wrapped["sourceFile"])
                previous.setdefault("sourceType", "marketplace_orders")
        else:
            index = len(rows)
            rows.append(dict(wrapped))
        for key in keys:
            identities[key] = index
    sources["rawOrders"] = rows


def refresh_running_accounts(service, *, through: date, source_statuses=None) -> dict[str, Any]:
    started = time.perf_counter()
    settings = _settings(service)
    previous_state = _read(_state_path(service))
    requested_cutoff = through.isoformat()
    cutoff = max(requested_cutoff, previous_state.get("through", requested_cutoff))
    sources = extract_running_account_sources(service.ledger.path, cutoff,
        registry_path=service.registry_path,
        historical_cash_path=service.periodic_standard.historical_cash_events_registry,
        policy_timeline=service.policy_timeline)
    _supplement_marketplace_orders(service, sources)
    # Source extraction returns latest balances; retain earlier observed facts
    # so a later refresh cannot erase the balance evidence of an older cutoff.
    balances = {}
    for row in [*previous_state.get("sources", {}).get("balanceSnapshots", []), *sources.get("balanceSnapshots", [])]:
        key = (row.get("storeAlias"), row.get("source"), row.get("observedAt"), row.get("eventId"))
        balances[key] = row
    sources["balanceSnapshots"] = sorted(balances.values(), key=lambda row: (row.get("observedAt", ""), row.get("storeAlias", ""), row.get("source") or ""))
    decisions = _read(settings["decisions"])
    issues = list(sources.get("issues", []))
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    native, provisional, desired = [], {}, {}
    supplemental = {}
    for entry, content in _imports(service):
        if entry.get("kind") == "recovered_statistics_orders_costs":
            for row in content.get("eventRows", []):
                supplemental[(row["storeAlias"], row["saleID"])] = row
    scope = set(service.store_aliases)
    orders, order_srid = {}, {}
    for wrapped in sources.get("rawOrders", []):
        raw, alias = _raw(wrapped), wrapped.get("storeAlias")
        for identity in (raw.get("id"), raw.get("orderId"), raw.get("srid")):
            if identity:
                orders[(alias, str(identity))] = raw
        if raw.get("srid"):
            for identity in (raw.get("id"), raw.get("orderId")):
                if identity:
                    order_srid[(alias, str(identity))] = str(raw["srid"])
    for (alias, _), extra in supplemental.items():
        raw, order = extra.get("statistics", {}), extra.get("order", {})
        for identity in (order.get("id"), order.get("orderId"), raw.get("srid")):
            if identity and order:
                orders[(alias, str(identity))] = order
        if raw.get("srid") and order.get("id"):
            order_srid[(alias, str(order["id"]))] = str(raw["srid"])

    def identity(alias, srid, order_id):
        srid = str(srid or order_srid.get((alias, str(order_id))) or "")
        if srid:
            return "srid:" + srid
        if order_id:
            return "order:" + str(order_id)
        return None

    def order_for(alias, raw, extra=None):
        return (extra or {}).get("order") or orders.get((alias, str(raw.get("orderId") or raw.get("order_id") or ""))) or orders.get((alias, str(raw.get("srid") or ""))) or {}

    statistics = {}
    for row in sources.get("rawStatistics", []):
        raw = _raw(row)
        alias = row.get("storeAlias", row.get("store_alias"))
        if raw.get("saleID"):
            statistics[(alias, str(raw["saleID"]))] = raw
    for key, row in supplemental.items():
        statistics.setdefault(key, row["statistics"])
    refunds = {}
    sale_by_identity = {}
    # Create the canonical sale once, regardless of the source that found it.
    # Statistics money always remains native RUB; only historical CNY cost is
    # permitted in the confirmed account while settlement is missing.
    for (alias, sale_id), raw in sorted(statistics.items()):
        day = _day(raw.get("date"))
        if not day or day > cutoff or alias not in scope:
            continue
        extra = supplemental.get((alias, sale_id), {})
        order = order_for(alias, raw, extra)
        order_id = str(order.get("id") or raw.get("orderId") or raw.get("srid") or "")
        key = identity(alias, raw.get("srid"), order_id)
        if not key:
            issues.append(_issue(alias, sale_id, day, "sale_identity_missing"))
            continue
        native.append({"store": alias, "saleId": sale_id, "date": day, "orderId": order_id,
                       "sku": order.get("article") or raw.get("supplierArticle"),
                       "grossRub": raw.get("priceWithDisc"), "proceedsRub": raw.get("forPay"),
                       "kind": "refund" if sale_id.startswith("R") else "sale"})
        if sale_id.startswith("R"):
            refunds.setdefault((alias, key), []).append({"kind": "statistics", "saleId": sale_id, "date": day,
                "orderId": order_id, "raw": raw, "order": order, "extra": extra})
            continue
        event_id = f"sale:{alias}:{key}"
        created = order.get("createdAt") or order.get("date")
        sku = order.get("article") or order.get("supplierArticle") or raw.get("supplierArticle")
        policy = service.policy_timeline.select(created) if created else None
        cost = _historical_cost(sources, sku, created)
        values = {metric: None for metric in FIELD_MAP.values()}
        values["shared_pool_accrued"] = None if not policy or policy.reserve_allocation_mode == "LEGACY_SHARED_POOL" else "0"
        values["product_cost"] = extra.get("purchaseCostCnyHistorical") or (cost.get("purchase_cost_cny") if cost else None)
        event = sale_event(alias, event_id, day, amounts=values, order_id=order_id,
            evidence=("WB statistics saleID:" + sale_id,), metadata={"basis": "awaiting_settlement",
                "srid": raw.get("srid"), "sku": sku, "quantity": 1, "createdAt": created,
                "costVersionId": cost.get("cost_version_uid") if cost else extra.get("costVersionId"),
                "policyId": policy.reporting_policy_id if policy else None,
                "reserveAllocationMode": policy.reserve_allocation_mode if policy else None})
        if event_id in desired:
            issues.append(_issue(alias, event_id, day, "multiple_statistics_sales_for_one_order_identity"))
            continue
        desired[event_id], sale_by_identity[(alias, key)] = event, event
        estimate = dict(extra.get("unapprovedEstimate", {}))
        if not estimate and order.get("convertedCurrencyCode") in (156, "156") and order.get("currencyCode") in (643, "643"):
            price, converted = Decimal(str(order.get("price") or 0)), Decimal(str(order.get("convertedPrice") or 0))
            if price > 0 and converted > 0:
                ratio = converted / price
                estimate = {"priceWithDiscCny": str(money(Decimal(str(raw["priceWithDisc"])) * ratio)) if raw.get("priceWithDisc") is not None else None,
                            "forPayCny": str(money(Decimal(str(raw["forPay"])) * ratio)) if raw.get("forPay") is not None else None}
        if policy and estimate.get("priceWithDiscCny") is not None:
            g = Decimal(str(estimate["priceWithDiscCny"]))
            amounts = {**values, "gross_sales": str(money(g)), "seller_proceeds": estimate.get("forPayCny"),
                "agency_accrued": str(money(g * policy.managed_operations_rate)),
                "promotion_accrued": str(money(g * policy.promotion_reserve_rate)),
                "return_reserve_accrued": str(money(g * policy.return_risk_reserve_rate)),
                "shared_pool_accrued": str(money(g * policy.promotion_reserve_rate)) if policy.reserve_allocation_mode == "LEGACY_SHARED_POOL" else "0"}
            provisional[event_id] = replace(event, amounts=amounts,
                metadata={**event.metadata, "basis": "unapproved_same_order_estimate"}).to_dict()
        issues.append(_issue(alias, event_id, day, "人民币结算待平台恢复；单独列示同单折算暂估" if estimate else "人民币结算及同单折算证据未齐"))
    official_refunds = {}
    official_seen = set()
    for row in sources.get("settlementSales", []):
        alias, day = row["storeAlias"], _day(row["occurredAt"])
        if alias not in scope or day > cutoff:
            continue
        order = order_for(alias, row)
        order_id = str(row.get("orderId") or order.get("id") or row.get("srid") or "")
        key = identity(alias, row.get("srid"), order_id)
        if not key:
            issues.append(_issue(alias, row["eventId"], day, "settlement_identity_missing"))
            continue
        if row.get("eventType") == "refund":
            official_refunds.setdefault((alias, key), []).append({"kind": "settlement", "date": day,
                "orderId": order_id, "raw": row, "order": order, "extra": {}})
            continue
        event_id = f"sale:{alias}:{key}"
        if event_id in official_seen:
            issues.append(_issue(alias, event_id, day, "multiple_financial_sales_for_one_order_identity_requires_reconciliation"))
            continue
        official_seen.add(event_id)
        created = row.get("orderCreatedAt") or order.get("createdAt") or order.get("date")
        sku = row.get("sku") or order.get("article") or order.get("supplierArticle")
        cost = _historical_cost(sources, sku, created)
        policy = service.policy_timeline.select(created) if created else None
        values = {metric: row.get(field) for field, metric in FIELD_MAP.items()}
        if values["product_cost"] is None and cost:
            values["product_cost"] = str(money(Decimal(cost["purchase_cost_cny"]) * abs(Decimal(str(row.get("quantity") or 1)))))
        if policy and values["gross_sales"] is not None:
            for metric, rate in (("agency_accrued", policy.managed_operations_rate), ("promotion_accrued", policy.promotion_reserve_rate), ("return_reserve_accrued", policy.return_risk_reserve_rate)):
                if values[metric] is None:
                    values[metric] = str(money(Decimal(values["gross_sales"]) * rate))
        mode = row.get("reserveAllocationMode") or (policy.reserve_allocation_mode if policy else None)
        values["shared_pool_accrued"] = values["promotion_accrued"] if mode == "LEGACY_SHARED_POOL" else ("0" if mode == "SEPARATE_RETURN_AND_PROMOTION" else None)
        event = sale_event(alias, event_id, day, amounts=values, order_id=order_id,
            evidence=tuple(row.get("evidenceIds", [])), metadata={"basis": "platform_settlement",
                "sourceEventId": row["eventId"], "sku": sku, "srid": row.get("srid"),
                "quantity": abs(row.get("quantity") or 1), "createdAt": created,
                "costVersionId": row.get("costVersionId") or (cost.get("cost_version_uid") if cost else None),
                "policyId": row.get("policyId") or (policy.reporting_policy_id if policy else None),
                "reserveAllocationMode": row.get("reserveAllocationMode") or (policy.reserve_allocation_mode if policy else None)})
        desired[event_id], sale_by_identity[(alias, key)] = event, event
        provisional.pop(event_id, None)
        issues = [item for item in issues if not (item.get("eventId") == event_id and str(item.get("reason", "")).startswith("人民币结算"))]
    # A single same-order official refund replaces its statistical precursor,
    # including a different posting date. Multiple unmatched refunds are held
    # open rather than guessing a correspondence between partial returns.
    refunds.update(official_refunds)
    exact_refunds = {}
    for (alias, key), rows in sorted(refunds.items()):
        original = sale_by_identity.get((alias, key))
        if len(rows) > 1:
            issues.append(_issue(alias, f"refund:{alias}:{key}", min(row["date"] for row in rows), "multiple_refunds_require_explicit_partial_linkage"))
        for index, row in enumerate(sorted(rows, key=lambda item: (item["date"], str(item["raw"].get("eventId") or item.get("saleId"))))):
            event_id = f"refund:{alias}:{key}:part:{index + 1}"
            original_quantity = Decimal(str(original.metadata.get("quantity", 1))) if original else Decimal("1")
            fraction = min(Decimal("1"), abs(Decimal(str(row["raw"].get("quantity") or 1))) / original_quantity)
            saleable = next((item["saleable"] for item in decisions.get("returnDecisions", [])
                if item["store"] == alias and str(item["orderId"]) == str(row["orderId"])), None)
            event = refund_event(alias, event_id, row["date"],
                original_sale_id=original.event_id if original else f"sale:{alias}:{key}", refund_fraction=str(fraction),
                saleable_return=saleable, order_id=row["orderId"],
                evidence=tuple(row["raw"].get("evidenceIds") or ["WB statistics refund:" + str(row.get("saleId"))]),
                metadata={"basis": "platform_settlement" if row["kind"] == "settlement" else "original_accrual_reversal",
                    "cashRefundSettled": row["kind"] == "settlement", "sku": row["order"].get("article") or (original.metadata.get("sku") if original else None)})
            desired[event_id] = event
            if row["kind"] == "settlement":
                exact_refunds[event_id] = row["raw"]
    for row in sources.get("settlementCharges", []):
        alias, day = row["storeAlias"], _day(row["occurredAt"])
        if alias not in scope or day > cutoff or row.get("amountCny") is None:
            continue
        kind = row.get("eventType")
        if kind == "return_logistics":
            original = sale_by_identity.get((alias, identity(alias, row.get("srid"), row.get("orderId"))))
            mode = original.metadata.get("reserveAllocationMode") if original else None
            order = order_for(alias, row)
            created = row.get("orderCreatedAt") or order.get("createdAt") or order.get("date")
            if not mode and created:
                # A rejected/cancelled order can incur actual return freight
                # without ever having a positive sale event. Its creation-time
                # evidence still selects the historical allocation policy.
                mode = service.policy_timeline.select(created).reserve_allocation_mode
            if mode == "LEGACY_SHARED_POOL":
                metric = "shared_pool_return_cost"
            elif mode == "SEPARATE_RETURN_AND_PROMOTION":
                metric = "separate_return_cost"
            else:
                issues.append(_issue(alias, row["eventId"], day, "return_logistics_policy_link_unknown"))
                metric = "logistics_cost"
        else:
            metric = {"forward_logistics": "logistics_cost", "other_platform_cost": "other_cost"}.get(kind)
        if metric is None:
            issues.append(_issue(alias, row["eventId"], day, "unclassified_settlement_charge"))
            continue
        amount = row["amountCny"]
        desired[row["eventId"]] = FinanceEvent(alias, row["eventId"], day,
            "adjustment" if Decimal(str(amount)) < 0 else "cash", amounts={metric: amount},
            order_id=row.get("orderId"), evidence=tuple(row.get("evidenceIds", [])),
            metadata={"basis": "platform_settlement", "description": row.get("operationName") or kind})
    ad = sources.get("advertisingReconciliation", {})
    for row in ad.get("events", []):
        alias, day = row.get("storeAlias"), _day(row.get("occurredAt"))
        if alias not in scope or not day or day > cutoff:
            continue
        values = {"cash_ad_spend": row.get("sellerCashCostCny"), "promo_credit_spend": row.get("platformPromoConsumptionCny")}
        event_id = "ad:" + row["eventId"]
        desired[event_id] = FinanceEvent(alias, event_id, day, "cash", amounts=values,
            evidence=(row["eventId"],), metadata={"basis": "official_ad_expense", "paymentSource": row.get("costFundingClass")})
        if row.get("costFundingClass") == "unknown":
            issues.append(_issue(alias, event_id, day, "advertising_funding_class_unknown"))
    with RunningAccount(settings["db"]) as account:
        for store in sources["stores"]:
            if store["storeAlias"] in scope:
                account.register_store(store["storeAlias"], _day(store.get("openedAt")), "CNY")
        for replacement in ad.get("duplicateReplacements", []):
            for superseded in replacement.get("supersededEventIds", []):
                history = account.event_history(replacement["storeAlias"], "ad:" + superseded)
                if history:
                    old = FinanceEvent.from_dict(history[-1]["payload"])
                    desired[old.event_id] = replace(old, amounts={"cash_ad_spend": "0", "promo_credit_spend": "0"},
                        metadata={**old.metadata, "supersededBy": replacement["retainedEventId"]})
        for event in sorted(desired.values(), key=lambda item: (item.store_id, item.event_date, item.event_id)):
            _upsert(account, event, counts)
        postings, _ = materialize_events(account.current_events())
        for posting in postings:
            if posting["event_id"] not in exact_refunds:
                continue
            raw = exact_refunds[posting["event_id"]]
            amounts = {}
            for source_field, metric in (("salesAmountGCny", "gross_sales"), ("forPayCny", "seller_proceeds")):
                actual, reversed_value = raw.get(source_field), posting["amounts"].get(metric)
                amounts[metric] = None if actual is None or reversed_value is None else str(money(Decimal(str(actual)) - Decimal(str(reversed_value))))
            _upsert(account, FinanceEvent(posting["store_id"], posting["event_id"] + ":settlement-delta", posting["event_date"],
                "adjustment", amounts=amounts, order_id=posting["order_id"], evidence=tuple(raw.get("evidenceIds", [])),
                metadata={"basis": "platform_settlement", "description": "Exact refund settlement minus original accrual reversal"}), counts)
        for row in sources.get("withdrawals", []):
            alias, identity, amount = row["storeAlias"], row["eventId"], row["amountCny"]
            if alias not in scope:
                continue
            for metric, suffix, event_date in (("withdrawal_requested", "requested", row.get("requestedAt")),
                    ("withdrawal_completed", "completed", row.get("settledAt") if row.get("settledByCutoff") else None)):
                if event_date and _day(event_date) <= cutoff:
                    _upsert(account, cash_event(alias, identity+":"+suffix, _day(event_date), metric=metric, amount=amount,
                        evidence=(str(row.get("externalReference") or identity),), metadata={"basis": "withdrawal_record", "recordId": row.get("recordId")}), counts)
        for row in sources.get("excludedWithdrawals", []):
            if row.get("storeAlias") not in scope or not row.get("cancelledAt"):
                continue
            for metric, suffix, occurred in (("withdrawal_requested", "requested", row.get("requestedAt")),
                                             ("withdrawal_cancelled", "cancelled", row["cancelledAt"])):
                if occurred and _day(occurred) <= cutoff:
                    _upsert(account, cash_event(row["storeAlias"], row["eventId"] + ":" + suffix, _day(occurred), metric=metric,
                        amount=row["amountCny"], evidence=(row["recordId"],), metadata={"basis": "withdrawal_record", "status": "voided"}), counts)
        for row in decisions.get("agencyPayments", []):
            if row["store"] in scope and row["date"] <= cutoff:
                _upsert(account, cash_event(row["store"], "agency-received:"+row["eventId"], row["date"],
                    metric="agency_paid", amount=row["amountCny"], evidence=(row["evidence"],)), counts)
        for row in decisions.get("cashReceipts", []):
            if row["store"] in scope and row["date"] <= cutoff:
                _upsert(account, cash_event(row["store"], "cash-received:" + row["eventId"], row["date"],
                    metric="receipts_received", amount=row["amountCny"], evidence=(row["evidence"],)), counts)
        finance_metrics = set(FIELD_MAP.values()) | {"logistics_cost", "other_cost", "shared_pool_return_cost", "separate_return_cost", "shared_pool_accrued"}
        for row in sorted(sources.get("sourceCoverage", []), key=lambda item: item.get("captured_at", "")):
            alias, source = row.get("store_alias"), row.get("source")
            if alias not in scope or source not in {"finance_detailed", "advertising_costs"}:
                continue
            start, end = _coverage_days(row, cutoff)
            if end < start:
                continue
            metrics = finance_metrics if source == "finance_detailed" else {"cash_ad_spend", "promo_credit_spend"}
            complete = row.get("coverage_status") == "complete"
            for metric in metrics & set(METRICS):
                account.set_coverage(alias, "CNY", metric, start, end, complete, (str(row.get("coverage_id") or source),))
                # An unresolved event invalidates its own business day, not
                # every earlier period covered by a complete source response.
                if source == "finance_detailed":
                    for issue in issues:
                        if issue.get("storeAlias") == alias and issue.get("reason") == "non_CNY_or_missing_currency_not_mapped_to_CNY":
                            failed_day = _day(issue.get("occurredAt") or issue.get("eventDate"))
                            if failed_day and start <= failed_day <= end:
                                account.set_coverage(alias, "CNY", metric, failed_day, failed_day, False, (str(issue.get("sourceId") or source),))
        for store in sources["stores"]:
            alias, opening = store["storeAlias"], _day(store["openedAt"])
            if alias not in scope or not opening:
                continue
            for group, metrics in (("agencyPaymentCoverage", ("agency_paid",)), ("cashReceiptCoverage", ("receipts_received",)),
                                   ("withdrawalCoverage", ("withdrawal_requested", "withdrawal_completed", "withdrawal_cancelled"))):
                for coverage in decisions.get(group, []):
                    if coverage["store"] == alias:
                        end = min(coverage["through"], cutoff)
                        if group == "agencyPaymentCoverage" and decisions.get("agencyReceiptMode") == "owner_notifies_each_receipt":
                            # This is the complete owner-reported receipt
                            # registry, not a claim to independently observe
                            # bank activity. The explicit standing instruction
                            # extends its confirmed opening baseline forward;
                            # each later owner-reported payment is a dated event.
                            if coverage["through"] <= cutoff:
                                end = cutoff
                        if end >= opening:
                            for metric in metrics:
                                account.set_coverage(alias, "CNY", metric, opening, end, True, (coverage["evidence"],))
        projection = account.refresh(service.store_aliases)
        _, account_issues = materialize_events(account.current_events())
    state = {"schemaVersion": 1, "through": cutoff, "templateVersion": TEMPLATE_VERSION,
        "refreshedAt": datetime.now(timezone.utc).isoformat(), "sources": sources,
        "nativeStatistics": native, "provisionalEvents": list(provisional.values()),
        "issues": issues+account_issues, "sourceStatuses": source_statuses or [],
        "ownerDecisions": decisions, "counts": counts}
    if decisions.get("agencyReceiptMode") == "owner_notifies_each_receipt":
        state["sources"]["agencyReceiptRegistryMode"] = "owner_notifies_each_receipt"
        state["sources"]["agencyReceiptRegistryNote"] = "代运营实收与待付金额按已确认期初及用户逐笔告知的实收登记计算；未声称独立读取银行流水。"
    state["contentHash"] = hashlib.sha256(json.dumps(_semantic_state(state), sort_keys=True, default=str).encode()).hexdigest()
    if state["contentHash"] == previous_state.get("contentHash"):
        state = {**previous_state, "counts": counts, "sourceStatuses": source_statuses or previous_state.get("sourceStatuses", [])}
    _write(_state_path(service), state)
    return {"status": "PENDING_DATA" if state["issues"] else "SUCCESS", **counts,
        "database": str(settings["db"]), "through": cutoff, "requestedThrough": requested_cutoff,
        "projection": projection, "elapsedMs": round((time.perf_counter()-started)*1000, 2)}

def _artifact_hashes(record):
    return {key: hashlib.sha256(Path(record[key]).read_bytes()).hexdigest()
            for key in ("workbook", "reportJson", "verification")}


def _publication_files_valid(record):
    expected = record.get("artifactHashes")
    if not expected or any(key not in expected for key in ("workbook", "reportJson", "verification")):
        return False
    try:
        if _artifact_hashes(record) != expected:
            return False
        verification = _read(Path(record["verification"]))
        return verification.get("formulaErrorCount") == 0 and bool(verification.get("totalsVerified"))
    except (OSError, KeyError, ValueError):
        return False


def _publication_is_current(service, row, *, period, period_start, through):
    if not row or not _publication_files_valid(row):
        return False
    if row.get("templateVersion") not in (None, TEMPLATE_VERSION):
        return False
    verification = _read(Path(row["verification"]))
    if verification.get("formulaErrorCount") != 0 or not verification.get("totalsVerified"):
        return False
    previous_fingerprint = row.get("businessFingerprint")
    if not previous_fingerprint:
        # An already published immutable artifact predating this field remains
        # reusable if its saved business evidence still matches the account.
        saved = _read(Path(row.get("reportJson") or Path(row["workbook"]).with_name("report.json")))
        if not saved or saved.get("templateVersion") != TEMPLATE_VERSION:
            return False
        previous_fingerprint = _business_fingerprint(saved)
    payload, _ = _running_account_payload(service, period=period, period_start=period_start,
        through=through, persist_snapshot=False)
    return previous_fingerprint == _business_fingerprint(payload)

def running_report_published(service, *, period: str, period_start: date, through: date) -> bool:
    manifest = _read(_settings(service)["db"].with_suffix(".publications.json"))
    key = f"{period}:{period_start.isoformat()}:{through.isoformat()}"
    if _publication_is_current(service, manifest.get(key), period=period, period_start=period_start, through=through):
        return True
    if period not in {"weekly", "monthly"}:
        return False
    # A Monday/month-start dashboard remains proof of that closing delivery
    # on the next run. Check one exact cumulative key, not all report history.
    state = _read(_state_path(service))
    openings = [date.fromisoformat(row["openedAt"][:10]) for row in state.get("sources", {}).get("stores", [])
                if row.get("storeAlias") in service.store_aliases and row.get("openedAt")]
    if not openings:
        return False
    opening = min(openings)
    dashboard = manifest.get(f"as-of:{opening.isoformat()}:{through.isoformat()}")
    if not _publication_is_current(service, dashboard, period="as-of", period_start=opening, through=through):
        return False
    saved = _read(Path(dashboard["reportJson"]))
    window_key = "last_week" if period == "weekly" else "last_month"
    return any(row.get("key") == window_key and row.get("start") == period_start.isoformat()
               and row.get("end") == through.isoformat() for row in saved.get("ownerView", {}).get("periods", []))


def latest_verified_owner_report(service, *, through: date):
    """Return dated historical evidence only, never claim fresh collection."""
    manifest = _read(_settings(service)["db"].with_suffix(".publications.json"))
    candidates = [row for row in manifest.values() if row.get("period") == "as-of"
                  and row.get("templateVersion") == TEMPLATE_VERSION and row.get("periodEnd", "9999") <= through.isoformat()]
    for row in sorted(candidates, key=lambda item: item["periodEnd"], reverse=True):
        if _publication_files_valid(row):
            return {**row, "status": "STALE_VERIFIED_FALLBACK", "isCurrent": False,
                    "requestedThrough": through.isoformat(), "dataThrough": row["periodEnd"],
                    "freshnessWarning": "本次未能完成新账本交付；这是上次已校验的历史报表，不代表已采集至本次截止日。"}
    return None


def _dated_before(row, cutoff):
    value = next((row.get(key) for key in ("event_date", "eventDate", "occurredAt", "observedAt", "date", "requestedAt") if row.get(key)), None)
    return not value or _day(value) <= cutoff


def _sources_through(sources, cutoff):
    result = dict(sources)
    for key in ("settlementSales", "settlementCharges", "rawStatistics", "rawOrders", "rawReturns", "balanceSnapshots", "withdrawals", "excludedWithdrawals", "issues"):
        result[key] = [row for row in sources.get(key, []) if _dated_before(row, cutoff)]
    result["withdrawals"] = [dict(row) for row in result["withdrawals"]]
    for row in result["withdrawals"]:
        if row.get("settledAt") and _day(row["settledAt"]) > cutoff:
            row["settledByCutoff"] = False
            row["latestStatus"] = "pending_at_report_cutoff"
    balances = {}
    for row in sorted(result["balanceSnapshots"], key=lambda item: item.get("observedAt", "")):
        balances[(row.get("storeAlias"), row.get("source"))] = row
    result["balanceSnapshots"] = list(balances.values())
    ad = dict(sources.get("advertisingReconciliation", {}))
    for key in ("events", "cashEvents", "platformPromoEvents", "unknownEvents", "duplicateReplacements", "ambiguousDuplicates"):
        ad[key] = [row for row in ad.get(key, []) if _dated_before(row, cutoff)]
    # Latest full-history source aggregates are not valid as-of aggregates.
    ad.pop("byStore", None)
    result["advertisingReconciliation"] = ad
    result["asOf"] = cutoff
    return result


def _business_fingerprint(payload):
    """Hash business evidence through this report cutoff, never run metadata.

    Monetary metric completeness is included: recovering a missing settlement
    republishes a pending week even when its known subtotal does not change.
    Newer-day transactions and confirmation coverage beyond this cutoff do not.
    """
    cutoff = payload["periodEnd"]
    scoped_postings = [row for row in payload.get("postings", []) if _dated_before(row, cutoff)]
    order_ids = {(row.get("store_id"), str(row.get("order_id"))) for row in scoped_postings if row.get("kind") == "refund"}

    def posting_value(row):
        fields = ("store_id", "event_id", "event_date", "kind", "currency", "amounts", "order_id",
                  "original_sale_id", "refund_fraction", "saleable_return")
        result = {key: row.get(key) for key in fields}
        metadata = row.get("metadata", {})
        result["policy"] = {key: metadata.get(key) for key in ("basis", "policyId", "costVersionId", "reserveAllocationMode", "cashRefundSettled", "supersededBy")}
        return result

    decisions = payload.get("ownerDecisions", {})
    relevant_decisions = {key: decisions.get(key) for key in ("refundRule", "actualAdvertisingRule", "estimateApproval", "agencyReceiptMode")}
    relevant_decisions["returns"] = sorted(({
        "store": row["store"], "orderId": str(row["orderId"]), "saleable": row["saleable"]}
        for row in decisions.get("returnDecisions", []) if (row.get("store"), str(row.get("orderId"))) in order_ids), key=lambda row: (row["store"], row["orderId"]))
    balances = []
    for row in payload.get("sources", {}).get("balanceSnapshots", []):
        if _dated_before(row, cutoff):
            balances.append({key: value for key, value in row.items() if key not in {"observedAt", "eventId", "sourceFetchedAt"}})
    business = {key: payload.get(key) for key in ("templateVersion", "templateBuilderHash", "period", "periodStart", "periodEnd", "actual", "provisional", "ownerPeriodAccounts", "ownerView")}
    business.update({"postings": [posting_value(row) for row in scoped_postings],
        "scenarioPostings": [posting_value(row) for row in payload.get("scenarioPostings", []) if _dated_before(row, cutoff)],
        "nativeStatistics": [row for row in payload.get("nativeStatistics", []) if _dated_before(row, cutoff)],
        "balanceSnapshots": balances, "ownerDecisions": relevant_decisions})
    # Stable order prevents a paginated source returning the same facts in a
    # different order from generating a spurious new workbook version.
    for key in ("postings", "scenarioPostings", "nativeStatistics", "balanceSnapshots"):
        business[key] = sorted(business[key], key=lambda row: json.dumps(row, sort_keys=True, default=str))
    return hashlib.sha256(json.dumps(business, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _running_account_payload(service, *, period: str, period_start: date | None, through: date,
                             persist_snapshot: bool = True) -> tuple[dict[str, Any], dict[str, str]]:
    """Build a dated model without invoking the workbook authoring process."""
    settings = _settings(service)
    configured_template = service.periodic_standard.raw.get("runningAccounts", {}).get("templateVersion")
    if configured_template and configured_template != TEMPLATE_VERSION:
        raise RuntimeError(f"Configured owner template {configured_template} does not match available stable template {TEMPLATE_VERSION}")
    state = _read(_state_path(service))
    if not state:
        refresh_running_accounts(service, through=through)
        state = _read(_state_path(service))
    if state["through"] < through.isoformat():
        raise ValueError("Requested cutoff is newer than the last materialized account; run account-refresh first")
    start = period_start or min(date.fromisoformat(x["openedAt"][:10]) for x in state["sources"]["stores"])
    with RunningAccount(settings["db"]) as account:
        actual = account.report(start.isoformat(), through.isoformat(), service.store_aliases)
        events = account.current_events()
        postings, _ = materialize_events(events)
        with RunningAccount(":memory:") as scenario:
            replacement = {(e["store_id"], e["event_id"]): e for e in state["provisionalEvents"]}
            for row in state["sources"]["stores"]:
                if row["storeAlias"] in service.store_aliases:
                    scenario.register_store(row["storeAlias"], _day(row.get("openedAt")))
            for event in events:
                key = (event.store_id, event.event_id)
                scenario.upsert_event(FinanceEvent.from_dict(replacement[key]) if key in replacement else event)
            for row in account.connection.execute("SELECT * FROM ra_coverage ORDER BY seq"):
                scenario.set_coverage(row["store_id"], row["currency"], row["metric"], row["start_date"], row["end_date"],
                    bool(row["complete"]), json.loads(row["evidence"]))
            proposed = scenario.report(start.isoformat(), through.isoformat(), service.store_aliases)
            scenario_postings, _ = materialize_events(scenario.current_events())
            # All four dashboard columns share the view module's calendar.
            # Each report carries period activity AND cumulative balances at
            # that column's own end, without creating another financial fact.
            windows = period_windows({"periodEnd": through.isoformat(), "sources": state["sources"]})
            owner_period_accounts = {
                key: {
                    "actual": account.report(window["start"], window["end"], service.store_aliases),
                    "provisional": scenario.report(window["start"], window["end"], service.store_aliases),
                }
                for key, window in windows.items()
            }
        cutoff = through.isoformat()
        issues = [row for row in state["issues"] if _dated_before(row, cutoff)]
        issues.extend(actual.get("issues", []))
        issues = list({json.dumps(row, sort_keys=True): row for row in issues}.values())
        payload = {"templateVersion": TEMPLATE_VERSION, "period": period,
            "templateBuilderHash": hashlib.sha256(settings["builder"].read_bytes()).hexdigest() if settings["builder"].is_file() else None,
            "periodStart": start.isoformat(), "periodEnd": through.isoformat(),
            "sourceCutoff": state["through"], "sourceRefreshedAt": state["refreshedAt"],
            "actual": actual, "provisional": proposed, "postings": [row for row in postings if _dated_before(row, cutoff)],
            "scenarioPostings": [row for row in scenario_postings if _dated_before(row, cutoff)],
            "nativeStatistics": [row for row in state["nativeStatistics"] if _dated_before(row, cutoff)],
            "sources": _sources_through(state["sources"], cutoff), "issues": issues, "ownerDecisions": state["ownerDecisions"],
            "ownerPeriodAccounts": owner_period_accounts}
        payload["ownerView"] = build_owner_view(payload)
        snapshot = account.save_report_snapshot(payload) if persist_snapshot else {}
    return payload, snapshot


def build_running_account_report(service, *, period: str, period_start: date | None, through: date) -> dict[str, Any]:
    started = time.perf_counter()
    settings = _settings(service)
    payload, snapshot = _running_account_payload(service, period=period, period_start=period_start, through=through, persist_snapshot=False)
    start = date.fromisoformat(payload["periodStart"])
    fingerprint = _business_fingerprint(payload)
    manifest_path = settings["db"].with_suffix(".publications.json")
    manifest = _read(manifest_path)
    publication_key = f"{period}:{start.isoformat()}:{through.isoformat()}"
    prior = manifest.get(publication_key)
    if prior and _publication_is_current(service, prior, period=period, period_start=start, through=through):
        return {**prior, "status": "NOOP", "businessFingerprint": fingerprint,
                "elapsedMs": round((time.perf_counter()-started)*1000, 2)}
    with RunningAccount(settings["db"]) as account:
        snapshot = account.save_report_snapshot(payload)
    digest = fingerprint[:12]
    label = {"weekly": "周报", "monthly": "月报", "as-of": "截至日报"}[period]
    folder = settings["output"] / "固定财务报表" / f"{start.isoformat()}_{through.isoformat()}" / digest
    workbook_name = (f"WB双店财务汇总_截至{through.isoformat()}.xlsx" if period == "as-of"
        else f"WB双店财务{label}_{start.isoformat()}至{through.isoformat()}_固定版.xlsx")
    workbook = folder / workbook_name
    model_path, verification = folder / "report.json", folder / "verification.json"
    if any(path.exists() for path in (workbook, model_path, verification)):
        # A matching business fingerprint is not proof that the saved Excel
        # is intact. Preserve an untrusted/incomplete prior artifact and
        # build a new repair revision instead of overwriting its history.
        folder = folder / ("repair-" + uuid4().hex[:16])
        workbook, model_path, verification = folder / workbook_name, folder / "report.json", folder / "verification.json"
    if not (workbook.is_file() and verification.is_file()):
        _write(model_path, payload)
        node = os.getenv("WB_FINANCE_NODE", "").strip()
        if not node:
            node = str(Path.home()/".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe")
        if not Path(node).is_file():
            raise RuntimeError("Bundled Node runtime is required for the fixed workbook")
        result = subprocess.run([node, str(settings["builder"]), "--input", str(model_path),
            "--output", str(workbook), "--verification", str(verification)], cwd=service.repo_root,
            capture_output=True, text=True, encoding="utf-8", timeout=240)
        if result.returncode:
            raise RuntimeError("Fixed owner workbook build failed: "+(result.stderr or result.stdout)[-1800:])
    verified = _read(verification)
    if verified.get("formulaErrorCount") != 0 or not verified.get("totalsVerified"):
        raise RuntimeError("Fixed workbook verification did not pass")
    record = {"workbook": str(workbook), "verification": str(verification), "reportJson": str(model_path),
        "period": period, "periodStart": start.isoformat(), "periodEnd": through.isoformat(),
        "templateVersion": TEMPLATE_VERSION, "reportStatus": payload["actual"]["status"],
        "businessFingerprint": fingerprint,
        "elapsedMs": round((time.perf_counter()-started)*1000, 2), "snapshot": snapshot}
    record["artifactHashes"] = _artifact_hashes(record)
    _merge_publication(settings["db"], publication_key, record)
    return record


def archive_running_account_report(service, report):
    """Archive verified bytes independently of calculation and delivery.

    Existing different bytes are never overwritten. A failed archive leaves
    the verified workspace report usable and is surfaced by the controller.
    """
    root = _settings(service)["archive"]
    if root is None:
        return {"status": "NOT_CONFIGURED"}
    if report.get("templateVersion") != TEMPLATE_VERSION or not _publication_files_valid(report):
        raise RuntimeError("Owner report archive requires the current template and verified artifact hashes")
    period = report["period"]
    period_folder = (report["periodEnd"] if period == "as-of" else report["periodStart"][:7]
                     if period == "monthly" else report["periodStart"] + "_" + report["periodEnd"])
    folder = root / period / period_folder / report["businessFingerprint"]
    folder.mkdir(parents=True, exist_ok=True)
    destinations, changed = {}, False
    for key in ("workbook", "reportJson", "verification"):
        source = Path(report[key])
        destination = folder / source.name
        expected = report["artifactHashes"][key]
        if destination.is_file():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"Existing archive differs; preserve and review {destination}")
        else:
            temporary = destination.with_name(f".a-{uuid4().hex[:16]}.tmp")
            try:
                shutil.copyfile(source, temporary)
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected:
                    raise RuntimeError("Archive copy does not match the verified source hash")
                try:
                    # Exclusive publication avoids overwriting a concurrent
                    # archive or leaving a partial destination after a crash.
                    os.link(temporary, destination)
                    changed = True
                except FileExistsError:
                    if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
                        raise RuntimeError("Concurrent archive contains different bytes")
            finally:
                temporary.unlink(missing_ok=True)
        destinations[key] = str(destination)
    result = {"status": "PASS" if changed else "NOOP", "period": period,
        "periodStart": report["periodStart"], "periodEnd": report["periodEnd"],
        "templateVersion": TEMPLATE_VERSION, "businessFingerprint": report["businessFingerprint"],
        "artifactHashes": report["artifactHashes"], **destinations}
    manifest = folder / "archive.json"
    if not manifest.is_file():
        _write(manifest, {**result, "archivedAt": datetime.now(timezone.utc).isoformat()})
    return result
