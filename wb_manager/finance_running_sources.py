"""Read-only, currency-safe evidence adapter for the running finance account.

This module never opens FinanceLedger (its constructor migrates the database),
never sums historical profit versions, and never reads raw_sales money columns.
Known facts are returned independently of the completeness of source coverage.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
CENT = Decimal("0.01")


def _money(value: Any) -> str | None:
    if value in (None, ""):
        return None
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Nonfinite monetary evidence")
    return format(number.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=MOSCOW) if parsed.tzinfo is None else parsed


def _business_date(raw: dict[str, Any], fallback: str | None) -> str:
    # A report's createDate is not the posting date of its underlying event.
    for key in ("rrDate", "rr_date", "saleDt", "sale_dt"):
        if raw.get(key):
            return _instant(raw[key]).astimezone(MOSCOW).isoformat()
    if fallback:
        return _instant(fallback).astimezone(MOSCOW).isoformat()
    raise ValueError("Missing financial event business date")


def _rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


def extract_running_account_sources(
    db_path: Path | str,
    cutoff: str,
    *,
    registry_path: Path | str | None = None,
    historical_cash_path: Path | str | None = None,
    policy_timeline: Any | None = None,
) -> dict[str, Any]:
    """Return original-currency facts through a complete Moscow business day.

    ``sourceCoverage`` and ``issues`` must accompany any cumulative display;
    missing later settlement records do not mean zero sales or zero expenses.
    Balances are snapshots, not cash receipts and not transaction flows.
    ``managedOperationsPaidCny=None`` means no payment evidence, NOT zero paid.
    """
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    cutoff_date = datetime.fromisoformat(cutoff).date()
    end = datetime.combine(cutoff_date + timedelta(days=1), time.min, MOSCOW)
    if policy_timeline is None:
        from .finance_policy import load_phone_financial_policy_timeline
        policy_timeline = load_phone_financial_policy_timeline()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        data = {name: _rows(connection, name) for name in (
            "store_registry", "raw_finance", "raw_sales", "raw_returns", "order_profit", "cost_versions", "raw_ad",
            "withdrawal_records", "withdrawal_record_events", "raw_balance", "source_coverage",
        )}
    if registry_path is not None:
        registry = json.loads(Path(registry_path).read_text(encoding="utf-8-sig"))
        stores = [{"storeAlias": x["alias"], "openedAt": x.get("openedAt"), "currency": x.get("currency")}
                  for x in registry["stores"] if x.get("enabled")]
    else:
        stores = [{"storeAlias": x["store_alias"], "openedAt": x.get("opened_at"), "currency": x.get("currency")}
                  for x in data["store_registry"] if x.get("enabled", True)]
    scope = {item["storeAlias"] for item in stores}
    issues: list[dict[str, Any]] = []
    # Lock a cost/rate snapshot by its original financial evidence ID. Never
    # aggregate any money across historical versions of an order snapshot.
    snapshots: dict[tuple[str, str], dict[str, Any]] = {}
    for version in sorted(data["order_profit"], key=lambda row: (row.get("calculated_at", ""), row.get("profit_version", "")), reverse=True):
        evidence = json.loads(version["evidence_json"])
        snapshot = evidence.get("fullOrderSnapshot", {})
        for evidence_id in evidence.get("evidenceIds", snapshot.get("evidenceIds", [])):
            snapshots.setdefault((version["store_alias"], str(evidence_id)), snapshot)
    costs = {row["cost_version_uid"]: row for row in data["cost_versions"]}
    sales: list[dict[str, Any]] = []
    charges: list[dict[str, Any]] = []
    dates: dict[str, list[str]] = {alias: [] for alias in scope}
    for row in data["raw_finance"]:
        alias = row["store_alias"]
        if alias not in scope:
            continue
        raw = json.loads(row["payload_json"])
        occurred = _business_date(raw, row.get("occurred_at"))
        if _instant(occurred) >= end:
            continue
        rid = str(row["rrd_id"])
        evidence_id = "finance:rrdId:" + rid
        if str(raw.get("currency", "")).upper() != "CNY":
            issues.append({"storeAlias": alias, "sourceId": evidence_id, "occurredAt": occurred,
                           "reason": "non_CNY_or_missing_currency_not_mapped_to_CNY"})
            continue
        dates[alias].append(occurred[:10])
        base = {"storeAlias": alias, "occurredAt": occurred, "currency": "CNY", "rrdId": rid,
                "orderId": str(row.get("order_id") or ""), "srid": row.get("srid"),
                "orderCreatedAt": raw.get("orderDt") or raw.get("order_dt"),
                "sku": raw.get("vendorCode") or raw.get("sa_name"),
                "source": "raw_finance.payload_json", "evidenceIds": [evidence_id],
                "sourceFetchedAt": row.get("fetched_at"), "basis": "platform_settlement"}
        g = _money(raw.get("retailPriceWithDisc", raw.get("retail_price_withdisc_rub")))
        pay = _money(raw.get("forPay", raw.get("ppvz_for_pay")))
        operation = str(raw.get("docTypeName") or raw.get("sellerOperName") or "").casefold()
        is_refund = any(term in operation for term in ("退", "возврат", "refund", "return"))
        is_sale = any(term in operation for term in ("销售", "продаж", "sale"))
        if (is_sale or is_refund) and (Decimal(g or "0") or Decimal(pay or "0")):
            sign = -1 if is_refund else 1
            snapshot = snapshots.get((alias, evidence_id), {})
            event = {**base, "eventId": f"finance:{alias}:rrd:{rid}:" + ("refund" if is_refund else "sale"),
                     "eventType": "refund" if is_refund else "sale", "quantity": sign * int(raw.get("quantity") or 1),
                     "salesAmountGCny": _money(sign * abs(Decimal(g))) if g is not None else None,
                     "forPayCny": _money(sign * abs(Decimal(pay))) if pay is not None else None,
                     "platformDeductionCny": _money(sign * (abs(Decimal(g)) - abs(Decimal(pay)))) if g is not None and pay is not None else None,
                     "acquiringFeeCny": _money(raw.get("acquiringFee")),
                     "purchaseCostCny": None, "costVersionId": None, "policyId": None}
            # Snapshot rates/cost may only augment the exact original settled
            # sale. Later refund-netted snapshots must not rewrite its basis.
            valid_snapshot = (not is_refund and snapshot
                              and _money(snapshot.get("salesAmountGCny")) == event["salesAmountGCny"]
                              and _money(snapshot.get("forPayCny")) == event["forPayCny"])
            cost = costs.get(str(snapshot.get("costVersionId"))) if valid_snapshot else None
            created = _instant(base["orderCreatedAt"]) if base["orderCreatedAt"] else None
            if cost and cost.get("approved_by") and created and _instant(cost["effective_from"]) <= created and (not cost.get("effective_to") or created < _instant(cost["effective_to"])):
                event.update({"purchaseCostCny": _money(cost["purchase_cost_cny"]),
                              "costVersionId": cost["cost_version_uid"],
                              "costEvidence": {key: cost.get(key) for key in ("quote_path", "quote_sha256", "quote_sheet", "quote_row", "approved_by", "approved_at", "effective_from", "effective_to")},
                              "policyId": snapshot.get("financialReportingPolicyVersionId"),
                              "reserveAllocationMode": snapshot.get("reserveAllocationMode")})
                for key in ("managedOperationsCny", "managedOperationsRate", "promotionReserveCny", "promotionReserveRate", "returnRiskReserveCny", "returnRiskReserveRate", "totalPolicyReserveCny", "totalReserveRate"):
                    event[key] = snapshot.get(key)
                # Some early snapshots predate explicit policy-rate columns.
                # Reconstruct allocations from the approved order-time policy,
                # never from the most recent report's current policy.
                policy = policy_timeline.select(base["orderCreatedAt"])
                event.update({"policyId": policy.reporting_policy_id, "reserveAllocationMode": policy.reserve_allocation_mode,
                              "managedOperationsRate": str(policy.managed_operations_rate),
                              "promotionReserveRate": str(policy.promotion_reserve_rate),
                              "returnRiskReserveRate": str(policy.return_risk_reserve_rate),
                              "totalReserveRate": str(policy.total_reserve_rate)})
                for key, rate in (("managedOperationsCny", policy.managed_operations_rate),
                                  ("promotionReserveCny", policy.promotion_reserve_rate),
                                  ("returnRiskReserveCny", policy.return_risk_reserve_rate),
                                  ("totalPolicyReserveCny", policy.total_reserve_rate)):
                    event[key] = _money(Decimal(event["salesAmountGCny"]) * rate)
            else:
                issues.append({"storeAlias": alias, "sourceId": evidence_id, "occurredAt": occurred,
                               "reason": "historical_cost_policy_snapshot_unavailable_or_conflicting"})
            sales.append(event)
        for column, category in (("logistics_cny", "forward_logistics"), ("return_logistics_cny", "return_logistics"), ("other_cost_cny", "other_platform_cost")):
            amount = _money(row.get(column))
            if amount is not None and Decimal(amount):
                charges.append({**base, "eventId": f"finance:{alias}:rrd:{rid}:{category}",
                                "eventType": category, "amountCny": amount,
                                "operationName": raw.get("sellerOperName"), "operationDetail": raw.get("bonusTypeName")})
    from .finance_sources import reconcile_advertising_cost_events
    ads = []
    for row in data["raw_ad"]:
        if row["store_alias"] not in scope or not row.get("occurred_at") or _instant(row["occurred_at"]) >= end:
            continue
        event = json.loads(row["payload_json"])
        if event.get("eventType") != "advertising_expense":
            # /payments records are funding, including failed RUB card
            # attempts. Their amount or currency says nothing about ad costs.
            continue
        raw = event.get("raw", event)
        if str(event.get("currency") or raw.get("currency") or "").upper() != "CNY":
            issues.append({"storeAlias": row["store_alias"], "reason": "advertising_missing_CNY_currency",
                           "sourceId": row["event_hash"], "occurredAt": row["occurred_at"]})
            continue
        event["currency"] = "CNY"
        event["storeAlias"] = row["store_alias"]
        ads.append(event)
    advertising = reconcile_advertising_cost_events(ads)
    status_events: dict[str, list[dict[str, Any]]] = {}
    withdrawal_by_id = {row["record_id"]: row for row in data["withdrawal_records"]}
    for event in data["withdrawal_record_events"]:
        evidence = json.loads(event.get("evidence_json") or "{}")
        withdrawal = withdrawal_by_id.get(event["record_id"], {})
        wrong_reference = evidence.get("incorrectExternalReference")
        correct_reference = evidence.get("correctExternalReference")
        # An evidenced OCR identity correction discovers that the extra
        # request never existed. It is not a new cancellation on discovery
        # day. Restate its original business date while preserving discovery.
        identity_correction = (event["status"] == "voided" and bool(wrong_reference)
                               and bool(correct_reference) and wrong_reference != correct_reference
                               and wrong_reference == withdrawal.get("external_reference"))
        effective_at = withdrawal.get("withdrawn_at") if identity_correction else event["occurred_at"]
        if effective_at and _instant(effective_at) < end:
            status_events.setdefault(event["record_id"], []).append({
                **event, "effective_at": effective_at,
                "historical_identity_correction": identity_correction,
                "correct_external_reference": correct_reference if identity_correction else None,
            })
    withdrawals = []
    excluded = []
    for row in data["withdrawal_records"]:
        if row["store_alias"] not in scope or _instant(row["withdrawn_at"]) >= end:
            continue
        status_rows = status_events.get(row["record_id"], [])
        latest = max(status_rows, key=lambda item: (_instant(item["effective_at"]), _instant(item["occurred_at"]), item["event_id"])) if status_rows else row
        if latest["status"] == "voided":
            excluded.append({"recordId": row["record_id"], "reason": "voided_by_status_event",
                             "storeAlias": row["store_alias"],
                             "eventId": "withdrawal:" + row["store_alias"] + ":" + str(row.get("external_reference") or row["record_id"]),
                             "externalReference": row.get("external_reference"),
                             "amountCny": _money(row["withdrawal_amount_cny"]),
                             "requestedAt": row["withdrawn_at"],
                             "cancelledAt": latest.get("effective_at", latest.get("occurred_at")),
                             "discoveredAt": latest.get("occurred_at"), "recordedAt": latest.get("occurred_at"),
                             "correctionType": "historical_identity_correction" if latest.get("historical_identity_correction") else "cancellation",
                             "correctedExternalReference": latest.get("correct_external_reference"),
                             "latestStatus": "voided"})
            continue
        settled = row.get("settled_at")
        success = latest["status"] == "readback_confirmed" and bool(settled) and _instant(settled) < end
        withdrawals.append({"eventId": "withdrawal:" + row["store_alias"] + ":" + str(row.get("external_reference") or row["record_id"]),
                            "recordId": row["record_id"], "storeAlias": row["store_alias"],
                            "amountCny": _money(row["withdrawal_amount_cny"]), "requestedAt": row["withdrawn_at"],
                            "settledAt": settled, "latestStatus": latest["status"], "settledByCutoff": success,
                            "externalReference": row.get("external_reference"), "evidenceSource": "withdrawal_records + latest withdrawal_record_events"})
    # Same withdrawal is often also in the historical configuration. Match
    # store/external identity across sources before appending an extra fact.
    if historical_cash_path is not None and Path(historical_cash_path).exists():
        historical = json.loads(Path(historical_cash_path).read_text(encoding="utf-8-sig"))
        identities = {(x["storeAlias"], x["externalReference"]) for x in withdrawals + excluded}
        for row in historical.get("events", []):
            if row.get("eventType") != "withdrawal" or row.get("store") not in scope or _instant(row["requestedAt"]) >= end:
                continue
            if row.get("status") == "voided":
                # A historical copy cannot resurrect a cancelled request.
                continue
            if (row["store"], row.get("externalReference")) in identities:
                continue
            settled = row.get("settledAt")
            withdrawals.append({"eventId": row["eventId"], "storeAlias": row["store"], "amountCny": _money(row["amountCny"]),
                                "requestedAt": row["requestedAt"], "settledAt": settled,
                                "latestStatus": row["status"], "externalReference": row.get("externalReference"),
                                "settledByCutoff": row["status"] == "readback_confirmed" and bool(settled) and _instant(settled) < end,
                                "evidenceSource": str(historical_cash_path)})
            identities.add((row["store"], row.get("externalReference")))
    balances: dict[tuple[str, str], dict[str, Any]] = {}
    for row in sorted(data["raw_balance"], key=lambda item: item["observed_at"]):
        if row["store_alias"] not in scope or _instant(row["observed_at"]) >= end:
            continue
        raw = json.loads(row["payload_json"])
        if str(raw.get("currency", "")).upper() != "CNY":
            continue
        balances[(row["store_alias"], raw.get("source", "unknown"))] = {
            "storeAlias": row["store_alias"], "observedAt": row["observed_at"], "source": raw.get("source"),
            "currency": "CNY", "sourceCurrencyField": "raw_balance.payload_json.currency",
            "accountBalanceCny": _money(row.get("account_balance_cny")),
            "platformWithdrawableCny": _money(row.get("withdrawable_balance_cny")),
            "advertisingAccountBalanceCny": _money(row.get("ad_balance_cny")),
            "mutualSettlementNetCny": _money(raw.get("net")),
            "eventId": row["event_hash"], "basis": "snapshot_not_cash_receipts"}
    statistics, orders, returns = [], [], []
    for row in data["raw_sales"]:
        if row["store_alias"] not in scope:
            continue
        raw = json.loads(row["payload_json"])
        occurred = raw.get("date") or raw.get("createdAt") or row.get("occurred_at")
        if not occurred or _instant(occurred) >= end:
            continue
        item = {"storeAlias": row["store_alias"], "eventId": row["event_hash"],
                "occurredAt": occurred, "sourceFetchedAt": row.get("fetched_at"), "raw": raw}
        if raw.get("saleID"):
            statistics.append({**item, "currency": "RUB", "source": "statistics-api/supplier/sales"})
        else:
            orders.append({**item, "source": "marketplace-api/orders" if "convertedPrice" in raw else "statistics-api/supplier/orders",
                           "currency": "RUB" if "convertedPrice" not in raw else None})
    for row in data["raw_returns"]:
        if row["store_alias"] in scope and row.get("occurred_at") and _instant(row["occurred_at"]) < end:
            returns.append({"storeAlias": row["store_alias"], "eventId": row["event_hash"],
                            "occurredAt": row["occurred_at"], "sourceFetchedAt": row.get("fetched_at"),
                            "raw": json.loads(row["payload_json"])})
    coverage: dict[tuple[str, str], dict[str, Any]] = {}
    for row in sorted(data["source_coverage"], key=lambda item: item["captured_at"]):
        if row["store_alias"] in scope:
            coverage[(row["store_alias"], row["source"])] = row
    return {"schemaVersion": 1, "asOf": cutoff, "financialTimezone": "Europe/Moscow", "sourceDatabase": str(path),
            "stores": stores, "settlementSales": sorted(sales, key=lambda x: (x["occurredAt"], x["eventId"])),
            "settlementCharges": sorted(charges, key=lambda x: (x["occurredAt"], x["eventId"])),
            "advertisingReconciliation": advertising, "withdrawals": withdrawals, "excludedWithdrawals": excluded,
            "balanceSnapshots": list(balances.values()),
            "rawStatistics": statistics, "rawOrders": orders, "rawReturns": returns,
            "approvedCostVersions": [row for row in data["cost_versions"] if row.get("approved_by")],
            "managedOperationsPayments": [], "managedOperationsPaidCny": None,
            "managedOperationsPaymentEvidenceStatus": "NOT_RECORDED_DO_NOT_ASSUME_ZERO_PAID",
            "knownSettlementBusinessDateRange": {alias: {"min": min(values) if values else None, "max": max(values) if values else None} for alias, values in dates.items()},
            "sourceCoverage": list(coverage.values()), "issues": issues,
            "limitations": ["Known ledger rows do not prove opening-to-cutoff completeness.",
                            "No confirmed agency-payment events were present in the inspected source schema.",
                            "Account balance is a point-in-time snapshot; never derive total cash receipts from it.",
                            "A reported withdrawal is requested, not settled; requests and settlements are separate dates."]}
