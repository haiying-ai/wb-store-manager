"""Daily confirmed funding, distinct from reports and money execution.

Plan generation does not approve an action. The existing same-store executor
owns proposal records, serialization, the single POST and independent readback.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Mapping

from .finance_daily_funding import build_daily_funding_basis

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def _fail(message):
    from .finance_service import FinanceServiceError
    raise FinanceServiceError(message)


def _money(value):
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        _fail("Non-finite funding amount")
    return parsed.quantize(CENT)


def _instant(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        _fail("Funding evidence timestamp must include its timezone")
    return parsed.astimezone(timezone.utc)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def is_daily(payload):
    return payload.get("schemaVersion") == 3


def cash_amount(payload):
    return _money(payload["cashAmountCny"] if is_daily(payload) else payload["amountCny"])


def action_cash_amount(action):
    """Keep legacy reserve views compatible without charging promotional money."""
    from .finance_daily_funding import _action_split
    evidence = _action_plan(action)
    if is_daily(evidence) or evidence.get("source") in {"bonus", "cashback"}:
        try:
            cash, _ = _action_split(dict(action), evidence)
            return cash
        except (TypeError, ValueError, ArithmeticError):
            _fail("Funding allocation has invalid cash/promotion evidence")
    return _money(action["requested_amount_cny"])


def _promo_policy(service):
    return service.periodic_standard.raw.get("runningAccounts", {}).get("funding", {}).get("cashbackMode") == "prefer_eligible"


def daily_funds_plan(service, *, through):
    cutoff = service._complete_financial_cutoff(through)
    stores = []
    for alias in service.store_aliases:
        stores.append(build_daily_funding_basis(service, store_alias=alias, through=cutoff))
    payload = {"schemaVersion": 1, "planType": "daily_funding", "dataThrough": cutoff.isoformat(),
        "withdrawalExecutionSupported": False, "stores": stores}
    payload["fingerprint"] = _hash(payload)
    path = service.output_root / "funds" / "daily" / f"{cutoff}-{payload['fingerprint'][:16]}.json"
    service._write_immutable_json(path, payload)
    return {**payload, "path": str(path), "live": False,
        "status": "PENDING_DATA" if any(row.get("blockers") for row in stores) else "READY_FOR_PREFLIGHT"}


def require_daily_basis(service, payload, *, exclude_action_id=None):
    basis = payload.get("fundingBasis")
    if not isinstance(basis, Mapping) or basis.get("store") != payload.get("store"):
        _fail("Daily funding basis is missing or belongs to another store")
    cutoff = service._complete_financial_cutoff(basis["dataThrough"])
    # A prior complete day can be examined, but never reused as today's new
    # spending authority. Resume by refreshing sources, not by changing dates.
    if cutoff != service._complete_financial_cutoff("yesterday"):
        _fail("Daily funding basis is stale; refresh the last complete financial day")
    current = build_daily_funding_basis(service, store_alias=payload["store"],
        through=cutoff, exclude_action_id=exclude_action_id)
    if current.get("blockers"):
        _fail("Daily funding basis has unresolved funding blockers: " + json.dumps(current["blockers"], ensure_ascii=False))
    if dict(basis) != current:
        _fail("Daily funding basis changed; generate and approve a new exact plan")
    reserve = _money(current["availableCashCny"])
    if reserve < ZERO:
        _fail("Confirmed advertising funds have a deficit")
    if _money(payload["policyCapCny"]) != reserve or _money(payload["availableAdvertisingReserveCny"]) != reserve:
        _fail("Daily funding cap differs from the confirmed cash reserve")
    if cash_amount(payload) > reserve:
        _fail("Cash portion exceeds the confirmed daily advertising reserve")
    return reserve


def _bucket_key(percent, expires_at):
    return int(percent), _instant(expires_at).isoformat()


def _action_plan(action):
    try:
        value = json.loads(action.get("evidence_json") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _bucket_sum(cashbacks, key):
    if not isinstance(cashbacks, list):
        _fail("Current promotional bucket evidence is unavailable")
    matching = []
    for bucket in cashbacks:
        try:
            if _bucket_key(bucket["percent"], bucket["expiration_date"]) == key:
                matching.append(_money(bucket["sum"]))
        except (TypeError, KeyError, ValueError):
            _fail("Malformed promotional bucket evidence")
    if len(matching) > 1:
        _fail("Ambiguous promotional bucket identity; do not merge balances")
    return matching[0] if matching else ZERO


def require_cashback_live(service, payload, live, *, exclude_action_id=None):
    promo = _money(payload.get("cashbackAmountCny", "0"))
    if promo == ZERO:
        return
    if not _promo_policy(service):
        _fail("Promotional funding is not enabled by the active private funding rule")
    if _instant(payload["cashbackExpiresAt"]) <= service._now().astimezone(timezone.utc):
        _fail("The approved promotional bucket has expired")
    key = _bucket_key(payload["cashbackPercent"], payload["cashbackExpiresAt"])
    live_sum = _bucket_sum(live.get("cashbacks"), key)
    if live_sum != _money(payload["cashbackAvailableCny"]):
        _fail("Promotional balance changed after plan creation; rebuild the exact plan")
    locked = ZERO
    for action in service.ledger.query_funding_actions(store_alias=payload["store"], action_type="advertising_top_up"):
        if str(action["action_id"]) == str(exclude_action_id or ""):
            continue
        plan = _action_plan(action)
        if not is_daily(plan) or _money(plan.get("cashbackAmountCny", "0")) == ZERO:
            continue
        if _bucket_key(plan["cashbackPercent"], plan["cashbackExpiresAt"]) != key:
            continue
        if service._funding_action_locks_reserve(action):
            locked += _money(plan["cashbackAmountCny"])
        if action.get("lifecycle_status") == "readback_confirmed":
            # A stale balance endpoint must not resurrect a spent bonus after
            # the store lock is released. New grants with an identical bucket
            # identity require distinct evidence before this ceiling can rise.
            for event in reversed(service.ledger.get_funding_action_events(str(action["action_id"]))):
                evidence = _action_plan(event)
                funding = (evidence.get("readback") or {}).get("fundingSourceReadback")
                if funding and funding.get("matched"):
                    if live_sum > _money(funding["observedCashbackCny"]):
                        _fail("Promotional balance exceeds its confirmed post-deposit balance; reconcile stale data or a new grant")
                    break
    if promo > live_sum - locked:
        _fail("Promotional funds are already reserved by another unresolved action")


def daily_ads_deposit_plan(service, funding, *, source_path, store_alias, campaign,
                           amount_cny, approved_by, use_cashback=True):
    from .client import WbClient
    from .finance_funds import build_advertising_deposit_plan, select_cashback_split
    if funding.get("schemaVersion") != 1 or funding.get("withdrawalExecutionSupported") is not False:
        _fail("Invalid daily funding plan")
    unsigned = {key: value for key, value in funding.items() if key != "fingerprint"}
    if funding.get("fingerprint") != _hash(unsigned):
        _fail("Daily funding file fingerprint does not match its contents")
    if store_alias not in service.store_aliases:
        _fail("The requested funding store is not enabled")
    stores = [row for row in funding.get("stores", []) if row.get("store") == store_alias]
    if len(stores) != 1:
        _fail("Daily funding file must contain exactly one matching store")
    basis = stores[0]
    cap = _money(basis["availableCashCny"])
    # Check all local authority before touching the platform.
    provisional = {"schemaVersion": 3, "store": store_alias, "fundingBasis": basis,
        "cashAmountCny": "0.00", "policyCapCny": str(cap), "availableAdvertisingReserveCny": str(cap)}
    require_daily_basis(service, provisional)
    exact_amount = Decimal(str(amount_cny)) if not isinstance(amount_cny, bool) else ZERO
    if not exact_amount.is_finite() or exact_amount <= ZERO or exact_amount != exact_amount.to_integral_value():
        _fail("Advertising deposit amount must be whole positive CNY")
    if isinstance(campaign, bool) or int(campaign) <= 0 or str(int(campaign)) != str(campaign):
        _fail("Campaign must be a positive integer")
    if not str(approved_by or "").strip():
        _fail("The proposed exact plan requires its intended approver")
    client = WbClient(service._promotion_credentials(store_alias).token)
    live = service._live_advertising_funding_snapshot(client, store_alias=store_alias,
        campaign_id=int(campaign), currency="CNY", source="balance")
    now = service._now()
    buckets = live.get("cashbacks", []) if use_cashback and _promo_policy(service) else []
    split = select_cashback_split(amount_cny, buckets, now=now)
    cash = _money(split["cashAmountCny"])
    if cash > cap or cash > _money(live["sourceBalanceCny"]):
        return {"status": "INSUFFICIENT_CONFIRMED_CASH", "live": False, "networkWriteAttempted": False,
            "store": store_alias, "campaign": campaign, "requestedAmountCny": str(amount_cny),
            "availableCashCny": str(cap), "cashShortfallCny": str(max(ZERO, cash-cap)),
            "settlementBalanceShortfallCny": str(max(ZERO, cash-_money(live["sourceBalanceCny"]))),
            "split": split, "fundingBasis": basis, "livePreflight": live,
            "nextStep": "Choose a smaller platform-supported amount or wait for confirmed cash evidence; no authority was bypassed"}
    expires = now + timedelta(minutes=30)
    if split.get("cashbackExpiresAt"):
        expires = min(expires, _instant(split["cashbackExpiresAt"]))
    plan = build_advertising_deposit_plan(report_id=None, store=store_alias,
        campaign=campaign, amount_cny=amount_cny, source="balance", currency="CNY",
        source_balance_cny=live["sourceBalanceCny"], source_balance_evidence_at=live["sourceBalanceEvidenceAt"],
        policy_cap_cny=cap, available_advertising_reserve_cny=cap,
        approved_by=approved_by, approved_at=now, expires_at=expires, funding_basis=basis,
        cashback_amount_cny=split["cashbackAmountCny"], cashback_percent=split["cashbackPercent"],
        cashback_available_cny=split["cashbackAvailableCny"], cashback_expires_at=split["cashbackExpiresAt"])
    payload = plan.as_dict()
    require_cashback_live(service, payload, live)
    path = service.output_root / "funds" / "advertising-deposit" / f"daily-{store_alias}-{campaign}-{plan.fingerprint[:16]}.json"
    service._write_immutable_json(path, payload)
    evidence_path = path.with_suffix(".preflight.json")
    service._write_immutable_json(evidence_path, {"networkWriteAttempted": False,
        "planFingerprint": plan.fingerprint, "fundingPlanPath": str(source_path), "livePreflight": live})
    return {"status": "immutable_plan_created", "live": False, "networkWriteAttempted": False,
        "path": str(path), "planPath": str(path), "evidencePath": str(evidence_path),
        "plan": payload, "livePreflight": live, "automaticRetryAllowed": False}


def daily_live_preflight(service, client, payload, *, exclude_action_id):
    live = service._live_advertising_funding_snapshot(client, store_alias=payload["store"],
        campaign_id=int(payload["campaign"]), currency=payload["currency"], source=payload["source"])
    if _money(live["sourceBalanceCny"]) != _money(payload["sourceBalanceCny"]):
        _fail("Advertising source balance changed after approval; create a new plan")
    cap = require_daily_basis(service, payload, exclude_action_id=exclude_action_id)
    if cash_amount(payload) > _money(live["sourceBalanceCny"]):
        _fail("Live settlement balance cannot cover the approved cash portion")
    require_cashback_live(service, payload, live, exclude_action_id=exclude_action_id)
    return {**live, "policyCapCny": str(cap), "availableAdvertisingReserveCny": str(cap)}


def funding_source_readback(client, payload):
    """Independent source reconciliation; failure retains the action lock."""
    from .finance_sources import ADVERTISING_BASE_URL
    expected_cash = _money(payload["sourceBalanceCny"]) - cash_amount(payload)
    promo = _money(payload.get("cashbackAmountCny", "0"))
    expected_promo = _money(payload["cashbackAvailableCny"]) - promo if promo else ZERO
    output = {"matched": False, "expectedCashBalanceCny": str(expected_cash),
        "expectedCashbackCny": str(expected_promo)}
    try:
        response = client.request("GET", f"{ADVERTISING_BASE_URL}/adv/v1/balance", retries=0)
        if not response.ok or not isinstance(response.data, Mapping):
            return {**output, "httpStatus": response.status}
        currency = response.data.get("currency")
        if currency and currency != payload["currency"]:
            return {**output, "error": "Source readback currency differs"}
        observed_cash = _money(response.data["net"])
        observed_promo = _bucket_sum(response.data.get("cashbacks"),
            _bucket_key(payload["cashbackPercent"], payload["cashbackExpiresAt"])) if promo else ZERO
        return {**output, "observedCashBalanceCny": str(observed_cash),
            "observedCashbackCny": str(observed_promo),
            "matched": observed_cash == expected_cash and observed_promo == expected_promo}
    except Exception as exc:
        return {**output, "error": str(exc)}
