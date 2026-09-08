"""Read-only confirmed daily funding evidence, independent of weekly reports.

This is a cash *allocation* allowance, not a live balance or an authorization.
Confirmed allocations (not allocations plus their later consumption) consume it.
Incomplete positive settlements are excluded; unidentified negative liabilities
fail closed. Every amount remains a qualified subtotal until coverage is complete.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .finance_running_account import FinanceEvent, materialize_events, money
from .finance_running_lock import serialized_running_account


_ZERO = Decimal("0")
_METRICS = ("promotion_accrued", "shared_pool_return_cost", "cash_ad_spend")
_ATTEMPTED = {"executing", "executed", "succeeded", "unknown", "readback_pending"}
_INACTIVE = {"proposed", "failed", "cancelled"}
_REQUIRED_COLLECTIONS = ("finance_detailed", "statistics_sales", "statistics_orders",
                         "goods_returns", "advertising_costs", "advertising_payments")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), default=str).encode()).hexdigest()


def _instant(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def _mapping(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    result = json.loads(value or "{}")
    if not isinstance(result, dict):
        raise ValueError("Expected an evidence object")
    return result


def _nonnegative(value: Any) -> Decimal:
    result = money(value)
    if result < 0:
        raise ValueError("Negative allocation")
    return result


def _action_split(action: dict, evidence: dict) -> tuple[Decimal, Decimal]:
    total = _nonnegative(action["requested_amount_cny"])
    if evidence.get("currency", "CNY") != "CNY":
        raise ValueError("Non-CNY allocation")
    if evidence.get("store") not in (None, action["store_alias"]):
        raise ValueError("Allocation evidence belongs to a different store")
    explicit = any(key in evidence for key in ("cashAmountCny", "cashbackAmountCny"))
    if int(evidence.get("schemaVersion") or 0) >= 3 or explicit:
        cash = _nonnegative(evidence.get("cashAmountCny"))
        promo = _nonnegative(evidence.get("cashbackAmountCny"))
        if cash + promo != total:
            raise ValueError("Cash and cashback do not reconcile to allocation")
    elif evidence.get("source") in {"balance", "account"}:
        cash, promo = total, _ZERO
    elif evidence.get("source") in {"bonus", "cashback"}:
        cash, promo = _ZERO, total
    else:
        raise ValueError("Allocation funding source is unknown")
    if evidence.get("amountCny") is not None and _nonnegative(evidence["amountCny"]) != total:
        raise ValueError("Allocation amount conflicts with immutable evidence")
    return cash, promo


def _coverage(rows: list[dict], start: str, end: str) -> dict:
    """Latest overlapping coverage wins; capture timestamps do not change semantics."""
    result = {}
    left, right = date.fromisoformat(start), date.fromisoformat(end) + timedelta(days=1)
    for metric in _METRICS:
        selected = [row for row in rows if row["metric"] == metric]
        points = {left, right}
        for row in selected:
            points.update((max(left, date.fromisoformat(row["start_date"])),
                           min(right, date.fromisoformat(row["end_date"]) + timedelta(days=1))))
        segments = []
        ordered = sorted(points)
        for a, b in zip(ordered, ordered[1:]):
            if a >= b:
                continue
            candidates = [r for r in selected if r["start_date"] <= a.isoformat() <= r["end_date"]]
            complete = bool(candidates and candidates[-1]["complete"])
            if segments and segments[-1]["complete"] == complete:
                segments[-1]["end"] = (b - timedelta(days=1)).isoformat()
            else:
                segments.append({"start": a.isoformat(), "end": (b-timedelta(days=1)).isoformat(),
                                 "complete": complete})
        result[metric] = {"complete": bool(segments) and all(s["complete"] for s in segments),
                          "segments": segments}
    return result


def _collection_evidence(state: dict, alias: str, cutoff: str) -> list[dict]:
    """A replay watermark is not a successful collection watermark.

    Successful empty responses are legitimate evidence. Transport errors,
    pagination failures and missing source intervals cannot establish that no
    later refund, charge or external advertising payment occurred.
    """
    def business_day(value, *, exclusive_end=False):
        if not value:
            return None
        parsed = _instant(value)
        if parsed:
            local = parsed.astimezone(ZoneInfo("Europe/Moscow"))
            if exclusive_end and local.hour == local.minute == local.second == 0:
                local -= timedelta(microseconds=1)
            return local.date().isoformat()
        return str(value)[:10]

    statuses = [row for row in state.get("sourceStatuses", []) if row.get("storeAlias") == alias]
    source_rows = state.get("sources", {}).get("sourceCoverage", [])
    result = []
    for source in _REQUIRED_COLLECTIONS:
        evidence = {"source": source, "dataThrough": cutoff, "complete": False,
                    "reason": "REQUIRED_SOURCE_NOT_COVERED"}
        candidates = [row for row in source_rows if row.get("store_alias") == alias
                      and row.get("source") == source
                      and (business_day(row.get("period_start")) or "9999") <= cutoff
                      and (business_day(row.get("period_end"), exclusive_end=True) or "") >= cutoff]
        if candidates:
            row = max(candidates, key=lambda item: str(item.get("captured_at") or ""))
            metadata = _mapping(row.get("metadata_json"))
            http = metadata.get("httpStatus")
            ok = row.get("coverage_status") == "complete" and (http is None or str(http).startswith("2"))
            evidence.update(complete=ok, reason=None if ok else "REQUIRED_SOURCE_COLLECTION_FAILED",
                            httpStatus=http, sourceSnapshotHash=row.get("snapshot_hash"))
        for store in statuses:
            for row in store.get("sources", []):
                if row.get("source") != source:
                    continue
                start = str(row.get("requestedFrom") or store.get("requestedFrom") or "9999")[:10]
                end = str(row.get("requestedThrough") or store.get("requestedThrough") or "")[:10]
                if start <= cutoff <= end and (not row.get("complete") or row.get("blocker")
                                               or not str(row.get("status") or "").startswith("2")):
                    # A new failed attempt cannot reuse an older success as a
                    # claim that this refresh observed current negative events.
                    evidence.update(complete=False, reason="REQUIRED_SOURCE_COLLECTION_FAILED",
                                    httpStatus=row.get("status"))
        result.append(evidence)
    return result


@serialized_running_account
def build_daily_funding_basis(service, *, store_alias: str, through: date,
                              exclude_action_id: str | None = None) -> dict[str, Any]:
    """Read current confirmed events/reservations without financial writes.

    Business events are frozen at ``through``. Funding actions are deliberately
    current (even when made after that cutoff), otherwise two same-day deposits
    could spend yesterday's allowance twice. An excluded action is ignored only
    before an irreversible attempt; confirmed/unknown attempts always consume it.
    ``fundingAllowed`` is an evidence gate, never permission to call the platform.
    """
    from .finance_running_adapter import _settings

    if isinstance(through, datetime) or not isinstance(through, date):
        raise ValueError("through must be a business date")
    alias, cutoff = str(store_alias).strip(), through.isoformat()
    if alias not in service.store_aliases:
        raise ValueError("Funding store must be explicitly enabled")
    now = service._now()
    if now.tzinfo is None:
        raise ValueError("Funding clock must have a timezone")
    now = now.astimezone(timezone.utc)
    settings = _settings(service)
    database = Path(settings["db"])
    blockers, warnings, excluded, event_evidence, action_evidence = [], [], [], [], []

    def block(code, **details):
        blockers.append({"code": code, **details})

    activation_info = service._funding_activation_info(alias, required=False)
    activation = _instant(activation_info.get("at") or activation_info.get("atIso"))
    policy = service.policy
    policy_fields = ("reporting_policy_id", "pricing_policy_id", "effective_from", "reporting_currency",
                     "advertising_deposit_cap_rule", "advertising_funding_source", "advertising_balance_field",
                     "advertising_request_type", "reserve_allocation_mode", "promotion_reserve_rate",
                     "return_risk_reserve_rate", "combined_advertising_return_pool_rate")
    policy_identity = {key: str(getattr(policy, key)) for key in policy_fields if hasattr(policy, key)}
    if not policy_identity.get("reporting_policy_id"):
        block("CURRENT_POLICY_UNRESOLVED")
    # Financial event dates in the canonical adapter are WB/Moscow dates.
    zone = ZoneInfo("Europe/Moscow")
    activation_day = activation.astimezone(zone).date().isoformat() if activation else None
    if not activation:
        block("FUNDING_ACTIVATION_UNRESOLVED")
    if cutoff >= now.astimezone(zone).date().isoformat():
        block("CUTOFF_IS_NOT_A_COMPLETE_FINANCIAL_DAY")
    events, coverage_rows, opening = [], [], None
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            store = connection.execute("SELECT * FROM ra_stores WHERE store_id=?", (alias,)).fetchone()
            if store is None or store["currency"] != "CNY":
                block("RUNNING_STORE_OR_CURRENCY_UNRESOLVED")
            else:
                opening = store["opened_on"]
            rows = connection.execute("""SELECT payload FROM (
                SELECT payload, ROW_NUMBER() OVER(PARTITION BY event_id ORDER BY revision DESC) AS current
                FROM ra_events WHERE store_id=?) WHERE current=1""", (alias,))
            for row in rows:
                event = FinanceEvent.from_dict(json.loads(row["payload"]))
                if event.store_id != alias:
                    block("EVENT_STORE_CONFLICT", eventId=event.event_id)
                elif event.event_date <= cutoff:
                    events.append(event)
            start = max(filter(None, (activation_day, opening)), default=cutoff)
            coverage_rows = [dict(row) for row in connection.execute("""SELECT * FROM ra_coverage
                WHERE store_id=? AND currency='CNY' AND end_date>=? AND start_date<=? ORDER BY seq""",
                (alias, start, cutoff))]
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        block("RUNNING_ACCOUNT_UNAVAILABLE", errorType=type(exc).__name__)
    start = max(filter(None, (activation_day, opening)), default=cutoff)
    if start > cutoff:
        block("CUTOFF_BEFORE_FUNDING_ACTIVATION")
        coverage = {metric: {"complete": False, "segments": []} for metric in _METRICS}
    else:
        coverage = _coverage(coverage_rows, start, cutoff)
    postings, issues = materialize_events(events)
    sales = {row["event_id"]: row for row in postings if row["kind"] == "sale"}
    eligible = set()
    for row in sales.values():
        meta, values = row["metadata"], row["amounts"]
        if row["event_date"] < start:
            continue
        required = ("gross_sales", "seller_proceeds", "product_cost", "promotion_accrued")
        if (row["currency"] == "CNY" and meta.get("basis") == "platform_settlement"
                and meta.get("policyId") and meta.get("costVersionId") and row["evidence"]
                and all(values.get(key) is not None for key in required)):
            eligible.add(row["event_id"])
        else:
            excluded.append({"eventId": row["event_id"], "date": row["event_date"],
                             "reason": "UNSETTLED_OR_UNVERIFIED_POSITIVE_ACCRUAL"})
    for issue in issues:
        if issue["code"] != "RETURN_INVENTORY_STATUS_UNKNOWN":
            block(issue["code"], eventId=issue["event_id"])
    totals = {metric: _ZERO for metric in _METRICS}
    for row in postings:
        if row["event_date"] < start:
            continue
        values, meta, included = row["amounts"], row["metadata"], {}
        if row["kind"] == "sale":
            # A statistics sale deliberately persists null settlement metrics
            # beside its known purchase cost. It is a positive excluded sale,
            # never an unexplained signed adjustment (None is not "0.00").
            if row["event_id"] in eligible:
                included["promotion_accrued"] = money(values["promotion_accrued"])
        elif row["kind"] == "refund":
            if row["original_sale_id"] in eligible:
                if row["currency"] != "CNY" or values.get("promotion_accrued") is None:
                    block("REFUND_ACCRUAL_UNRESOLVED", eventId=row["event_id"])
                else:
                    included["promotion_accrued"] = money(values["promotion_accrued"])
            elif row["original_sale_id"] in sales:
                excluded.append({"eventId": row["event_id"], "date": row["event_date"],
                                 "reason": "REFUND_OF_EXCLUDED_ACCRUAL"})
        elif "promotion_accrued" in values and values["promotion_accrued"] != "0.00":
            block("UNCLASSIFIED_ACCRUAL_ADJUSTMENT", eventId=row["event_id"])
        for metric in ("shared_pool_return_cost", "cash_ad_spend"):
            if metric not in values:
                continue
            if values[metric] == "0.00":
                continue
            trusted = meta.get("basis") == ("platform_settlement" if metric == "shared_pool_return_cost" else "official_ad_expense")
            if row["currency"] != "CNY" or values[metric] is None or not trusted or not row["evidence"]:
                block("UNRESOLVED_" + metric.upper(), eventId=row["event_id"])
            else:
                included[metric] = money(values[metric])
        if meta.get("description") == "return_logistics" and "logistics_cost" in values:
            block("RETURN_POOL_POLICY_UNRESOLVED", eventId=row["event_id"])
        if included:
            for metric, value in included.items():
                totals[metric] += value
            semantic = {key: row[key] for key in ("event_id", "event_date", "kind", "currency", "order_id", "original_sale_id", "refund_fraction", "evidence")}
            semantic["amounts"] = {key: str(value) for key, value in included.items()}
            semantic["eventAmounts"] = values
            semantic["historicalFacts"] = {key: meta.get(key) for key in ("basis", "policyId", "costVersionId", "reserveAllocationMode", "createdAt", "sourceEventId")}
            if row["kind"] == "refund" and row["original_sale_id"] in sales:
                semantic["originalPolicyId"] = sales[row["original_sale_id"]]["metadata"].get("policyId")
            event_evidence.append({**semantic, "contentHash": _hash(semantic)})
    state_path = database.with_suffix(".state.json")
    state_through = None
    collection_evidence = []
    if state_path.is_file():
        try:
            state = _mapping(state_path.read_text(encoding="utf-8-sig"))
            collection_evidence = _collection_evidence(state, alias, cutoff)
            for collection in collection_evidence:
                if not collection["complete"]:
                    block(collection["reason"], source=collection["source"], dataThrough=cutoff,
                          httpStatus=collection.get("httpStatus"))
            state_through = state.get("through")
            if state_through and state_through < cutoff:
                block("RUNNING_ACCOUNT_CUTOFF_STALE", stateThrough=state_through)
            for issue in state.get("issues", []):
                if issue.get("storeAlias", issue.get("store_id")) != alias:
                    continue
                day = str(issue.get("eventDate") or issue.get("occurredAt") or issue.get("event_date") or "")[:10]
                if day and not start <= day <= cutoff:
                    continue
                reason = str(issue.get("reason") or issue.get("code") or "")
                if any(text in reason for text in ("multiple_refunds", "return_logistics_policy", "unclassified_settlement_charge", "advertising_funding_class_unknown")):
                    block("SOURCE_NEGATIVE_LIABILITY_UNRESOLVED", reason=reason, eventId=issue.get("eventId"))
        except (OSError, ValueError, TypeError):
            block("RUNNING_STATE_INVALID")
    else:
        block("RUNNING_COLLECTION_EVIDENCE_MISSING")
    allocated, locked, allocated_promo, locked_promo = _ZERO, _ZERO, _ZERO, _ZERO
    actions = service.ledger.query_funding_actions(store_alias=alias, action_type="advertising_top_up")
    seen = {}
    for action in actions:
        if action.get("store_alias") != alias or action.get("action_type") != "advertising_top_up":
            block("FUNDING_ACTION_SCOPE_CONFLICT")
            continue
        identity = str(action["action_id"])
        if identity in seen:
            if seen[identity] != action:
                block("FUNDING_ACTION_IDENTITY_CONFLICT", actionId=identity)
            continue
        seen[identity] = action
        status = str(action.get("lifecycle_status") or action.get("status") or "").lower()
        if status in _INACTIVE:
            continue
        if identity == exclude_action_id and status == "approved":
            continue
        try:
            evidence = _mapping(action.get("evidence_json"))
            expiry = _instant(evidence.get("expiresAt"))
            if status == "approved" and expiry and now >= expiry:
                continue
            cash, promo = _action_split(action, evidence)
        except (ValueError, TypeError, KeyError):
            block("FUNDING_CASH_SPLIT_UNRESOLVED", actionId=identity)
            continue
        effective = (service._confirmed_deposit_effective_at(action)
                     if status == "readback_confirmed" or status in _ATTEMPTED
                     else _instant(action.get("requested_at")))
        if effective is None or effective > now:
            block("FUNDING_ACTION_TIME_UNRESOLVED", actionId=identity)
        if activation and effective and effective < activation:
            continue
        if status == "readback_confirmed":
            allocated += cash
            allocated_promo += promo
        elif status in _ATTEMPTED or status == "approved":
            locked += cash
            locked_promo += promo
            if status != "approved":
                block("FUNDING_OUTCOME_UNRESOLVED", actionId=identity, status=status)
        else:
            block("FUNDING_ACTION_STATUS_UNRESOLVED", actionId=identity, status=status)
        action_evidence.append({"actionId": identity, "status": status, "cashAmountCny": str(money(cash)),
            "cashbackAmountCny": str(money(promo)), "effectiveAt": effective.isoformat() if effective else None,
            "expiresAt": expiry.isoformat() if status == "approved" and expiry else None,
            "planFingerprint": evidence.get("fingerprint") or evidence.get("planFingerprint"),
            "campaign": evidence.get("campaign"), "source": evidence.get("source")})
    available = totals["promotion_accrued"] - totals["shared_pool_return_cost"] - allocated - locked
    book = totals["promotion_accrued"] - totals["shared_pool_return_cost"] - totals["cash_ad_spend"]
    if available < 0:
        block("NEGATIVE_CONFIRMED_CASH_ALLOWANCE")
    if totals["cash_ad_spend"] > allocated:
        block("CASH_SPEND_EXCEEDS_RECONCILED_ALLOCATIONS")
    source_complete = (all(row["complete"] for row in coverage.values())
                       and bool(collection_evidence)
                       and all(row["complete"] for row in collection_evidence))
    if not source_complete:
        warnings.append({"code": "PARTIAL_SOURCE_COVERAGE", "meaning": "Known confirmed subtotal only; missing evidence is not zero"})
    if excluded:
        warnings.append({"code": "UNSETTLED_POSITIVE_ACCRUALS_EXCLUDED", "count": len(excluded)})
    warnings.append({"code": "NEW_BASIS_REQUIRES_NEW_EXACT_APPROVAL",
                     "meaning": "Legacy weekly allowances and approvals are not transferable; reconcile any difference before using a higher daily allowance"})
    complete = source_complete and not excluded and not blockers
    result = {"schemaVersion": 1, "basisType": "daily_confirmed_advertising", "store": alias,
        "currency": "CNY", "dataThrough": cutoff, "activationDate": activation_day,
        "stateThrough": state_through, "cutoffView": ("historical" if state_through and cutoff < state_through else
            "stale" if state_through and cutoff > state_through else "latest_materialized"),
        "availableCashCny": str(money(available)), "grossConfirmedAccrualCny": str(money(totals["promotion_accrued"])),
        "sharedReturnChargeCny": str(money(totals["shared_pool_return_cost"])),
        "confirmedCashAllocatedCny": str(money(allocated)), "lockedCashCny": str(money(locked)),
        "confirmedCashbackAllocatedCny": str(money(allocated_promo)), "lockedCashbackCny": str(money(locked_promo)),
        "cashSpendCny": str(money(totals["cash_ad_spend"])), "unspentBookBudgetCny": str(money(book)),
        "fundingAllowed": not blockers, "blockers": blockers, "warnings": warnings,
        "completeness": {"complete": complete, "status": "BLOCKED" if blockers else ("CONFIRMED_SUBTOTAL" if complete else "PARTIAL_CONFIRMED"),
                         "excludedEventCount": len(excluded), "sourceCoverageComplete": source_complete},
        "evidence": {"events": sorted(event_evidence, key=lambda x: x["event_id"]),
                     "fundingActions": sorted(action_evidence, key=lambda x: x["actionId"]),
                     "sourceCoverage": coverage, "excludedEvents": sorted(excluded, key=lambda x: x["eventId"]),
                     "requiredCollections": collection_evidence,
                     "policy": policy_identity, "activationSource": activation_info.get("source")},
        "limitations": ["Not a live platform balance or payment authorization", "Funding actions are current, while business events stop at dataThrough",
                        "Campaign transfers and their consumption are not both subtracted", "Unsettled management estimates cannot increase confirmed cash allowance"]}
    # State refresh timestamps, newer unrelated events, and same-fact coverage
    # recaptures are not changes to a frozen period's funding business facts.
    semantic = {key: value for key, value in result.items() if key not in {"stateThrough", "cutoffView", "limitations"}}
    fingerprint = _hash(semantic)
    return {**result, "fingerprint": fingerprint, "basisId": f"daily-confirmed:{alias}:{cutoff}:{fingerprint}"}
