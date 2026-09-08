"""Daily funding safety boundaries; synthetic ledger and fake GET/POST only.

No report builder, real credentials, source sync, or marketplace connection is
used. The basis provider is controlled evidence; the service/claim and live
preflight paths remain real.
"""
from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import importlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
from threading import Event

import pytest

from wb_manager.client import ApiResult
from wb_manager.finance_funds import build_advertising_deposit_plan
from wb_manager.finance_ledger import FinanceLedger
from wb_manager.finance_service import FinanceService
from wb_manager import finance_service as service_module
from wb_manager.finance_daily_funding import build_daily_funding_basis as guarded_daily_funding_basis
from wb_manager.finance_running_lock import RunningAccountLockError, running_account_snapshot_lock, serialized_running_account


NOW = datetime(2026, 9, 8, 3, 0, tzinfo=timezone.utc)
STORE = "synthetic-store"
CAMPAIGN = 1234567
EXPIRY = "2026-10-01T00:00:00+00:00"


def _basis(amount="120.00", *, blockers=None, revision=1):
    result = {
        "schemaVersion": 1, "basisType": "daily_confirmed_advertising", "currency": "CNY",
        "store": STORE, "dataThrough": "2026-09-07", "availableCashCny": amount,
        "grossConfirmedAccrualCny": amount, "sharedReturnChargeCny": "0.00",
        "confirmedCashAllocatedCny": "0.00", "lockedCashCny": "0.00", "cashSpendCny": "0.00",
        "unspentBookBudgetCny": amount, "blockers": blockers or [],
        "completeness": {"complete": not bool(blockers), "status": "CONFIRMED_SUBTOTAL"},
        "fundingAllowed": not bool(blockers), "evidence": {"syntheticRevision": revision},
    }
    fingerprint = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**result, "fingerprint": fingerprint, "basisId": f"daily-confirmed:{STORE}:2026-09-07:{fingerprint}"}


class FakeFundingClient:
    def __init__(self):
        self.calls = []
        self.cash = Decimal("500")
        self.cashback = Decimal("100")
        self.percent = 50
        self.expiry = EXPIRY
        self.budgets = {}
        self.apply_post = True
        self.raise_on_post = False
        self.fail_get = None
        self.on_post = None
        self.after_post = None

    @property
    def posts(self):
        return [row for row in self.calls if row["method"] == "POST"]

    def request(self, method, url, *, body=None, retries=2, **kwargs):
        self.calls.append({"method": method, "url": url, "body": deepcopy(body), "retries": retries})
        if self.fail_get and method == "GET" and self.fail_get in url:
            return ApiResult(503, False, {"error": "synthetic source unavailable"}, {})
        match = re.search(r"[?&](?:id|ids)=(\d+)", url)
        campaign = int(match.group(1)) if match else CAMPAIGN
        if method == "GET" and "/api/advert/v2/adverts" in url:
            return ApiResult(200, True, {"adverts": [{"id": campaign, "status": 9}]}, {})
        if method == "GET" and url.endswith("/api/v1/account/balance"):
            return ApiResult(200, True, {"currency": "CNY", "current": 10000, "for_withdraw": 0}, {})
        if method == "GET" and url.endswith("/adv/v1/balance"):
            return ApiResult(200, True, {"currency": "CNY", "net": str(self.cash), "balance": "0", "bonus": "0",
                "cashbacks": [{"sum": str(self.cashback), "percent": self.percent, "expiration_date": self.expiry}]}, {})
        if method == "GET" and "/adv/v1/budget?id=" in url:
            return ApiResult(200, True, {"total": str(self.budgets.get(campaign, Decimal("40"))), "cash": "0", "netting": "0"}, {})
        if method == "POST" and "/adv/v1/budget/deposit" in url:
            if self.on_post:
                self.on_post()
            if self.apply_post:
                total = Decimal(str(body["sum"]))
                promo = Decimal(str(body.get("cashback_sum", 0)))
                self.cash -= total - promo
                self.cashback -= promo
                self.budgets[campaign] = self.budgets.get(campaign, Decimal("40")) + total
            if self.after_post:
                self.after_post()
            if self.raise_on_post:
                raise TimeoutError("synthetic unknown POST result")
            return ApiResult(200, True, {"accepted": True}, {})
        raise AssertionError(f"Unexpected fake funding request: {method} {url}")


@pytest.fixture
def environment(tmp_path, monkeypatch):
    service = FinanceService.__new__(FinanceService)
    service.repo_root = tmp_path
    service.output_root = tmp_path / "reports"
    service.now = NOW
    service.store_aliases = [STORE]
    service.registry = {"reportingCurrency": "CNY", "stores": [{"alias": STORE, "enabled": True, "currency": "CNY"}]}
    service.ledger = FinanceLedger(tmp_path / "finance.sqlite3")
    service.ledger.register_store(STORE, scope_id="synthetic-account", at=NOW)
    service.policy = SimpleNamespace(reporting_policy_id="synthetic-policy",
        advertising_deposit_cap_rule=service_module.ADVERTISING_DEPOSIT_CAP_RULE)
    service.periodic_standard = SimpleNamespace(raw={"runningAccounts": {"funding": {"cashbackMode": "prefer_eligible"}}})
    service._promotion_credentials = lambda alias: SimpleNamespace(token="synthetic-token-never-sent")
    service._require_current_deposit_report = lambda *a, **kw: pytest.fail("Daily funding must never require a weekly workbook")
    client = FakeFundingClient()
    monkeypatch.setattr(service_module, "WbClient", lambda token: client)
    monkeypatch.setattr("wb_manager.client.WbClient", lambda token: client)
    holder = {"basis": _basis()}

    def current_basis(*args, **kwargs):
        assert kwargs["store_alias"] == STORE
        assert kwargs["through"] == date(2026, 9, 7)
        return deepcopy(holder["basis"])

    basis_module = importlib.import_module("wb_manager.finance_daily_funding")
    bridge_module = importlib.import_module("wb_manager.finance_funding_bridge")
    monkeypatch.setattr(basis_module, "build_daily_funding_basis", current_basis)
    for module in (bridge_module, service_module):
        if hasattr(module, "build_daily_funding_basis"):
            monkeypatch.setattr(module, "build_daily_funding_basis", current_basis)
    try:
        yield SimpleNamespace(service=service, client=client, holder=holder, root=tmp_path)
    finally:
        service.close()


def _plan(env, *, cash="80", promo="20", campaign=CAMPAIGN):
    service, client = env.service, env.client
    amount = Decimal(cash) + Decimal(promo)
    promotional = Decimal(promo) > 0
    plan = build_advertising_deposit_plan(
        report_id=None, store=STORE, campaign=campaign, amount_cny=amount,
        source="balance", currency="CNY", source_balance_cny=client.cash,
        source_balance_evidence_at=service._now(), policy_cap_cny=env.holder["basis"]["availableCashCny"],
        available_advertising_reserve_cny=env.holder["basis"]["availableCashCny"],
        approved_by="synthetic-owner", approved_at=service._now(),
        expires_at=service._now() + timedelta(minutes=15), funding_basis=deepcopy(env.holder["basis"]),
        cashback_amount_cny=promo,
        cashback_percent=client.percent if promotional else None,
        cashback_available_cny=client.cashback if promotional else None,
        cashback_expires_at=client.expiry if promotional else None,
    ).as_dict()
    path = env.root / f"plan-{campaign}-{plan['fingerprint'][:10]}.json"
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return path, plan


def _propose(env, path):
    return env.service.ads_deposit(plan_path=path, approved_by="synthetic-owner", confirm_live=False)


def _execute(env, path):
    return env.service.ads_deposit(plan_path=path, approved_by="synthetic-owner", confirm_live=True)


def test_daily_proposal_ignores_broken_weekly_report_and_never_posts(environment):
    env = environment
    path, payload = _plan(env)
    first, repeated = _propose(env, path), _propose(env, path)
    assert first["live"] is False and repeated["idempotent"] is True
    assert not env.client.posts
    rows = env.service.ledger.query_funding_actions(store_alias=STORE, action_type="advertising_top_up")
    assert len(rows) == 1 and rows[0]["report_id"] is None
    assert json.loads(rows[0]["evidence_json"])["fundingBasis"] == payload["fundingBasis"]


@pytest.mark.parametrize("use_cashback,expected_cash,expected_promo", [(True, "50.00", "50.00"), (False, "100.00", "0.00")])
def test_daily_funds_and_immutable_deposit_plan_are_get_only_and_weekly_independent(environment, use_cashback, expected_cash, expected_promo):
    env = environment
    funding = env.service.funds_plan(through="yesterday")
    assert funding["planType"] == "daily_funding"
    assert not env.client.calls
    result = env.service.ads_deposit_plan(funds_plan_path=Path(funding["path"]), store_alias=STORE,
        campaign=CAMPAIGN, amount_cny=100, approved_by="synthetic-owner", use_cashback=use_cashback)
    plan = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert plan["schemaVersion"] == 3 and plan["reportId"] is None
    assert plan["cashAmountCny"] == expected_cash and plan["cashbackAmountCny"] == expected_promo
    assert env.client.calls and all(row["method"] == "GET" for row in env.client.calls)
    assert env.service.ledger.query_funding_actions(store_alias=STORE) == []


def test_daily_live_execution_requires_previously_recorded_exact_proposal(environment):
    env = environment
    path, _ = _plan(env)
    with pytest.raises(ValueError, match="proposal"):
        _execute(env, path)
    assert not env.client.posts


@pytest.mark.parametrize("change", ["amount", "store", "cashback", "basis"])
def test_tampered_daily_plan_is_rejected_before_post(environment, change):
    env = environment
    path, payload = _plan(env)
    _propose(env, path)
    if change == "amount":
        payload["amountCny"] = "101.00"
    elif change == "store":
        payload["store"] = "another-store"
    elif change == "cashback":
        payload["cashbackAmountCny"] = "30.00"
    else:
        payload["fundingBasis"]["availableCashCny"] = "9999.00"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        _execute(env, path)
    assert not env.client.posts


def test_expired_daily_approval_is_cancelled_without_post(environment):
    env = environment
    path, _ = _plan(env)
    proposal = _propose(env, path)
    env.service.now += timedelta(minutes=16)
    with pytest.raises(ValueError, match="expired"):
        _execute(env, path)
    assert not env.client.posts
    row = env.service.ledger.query_funding_actions(store_alias=STORE)[0]
    assert row["action_id"] == proposal["actionId"] and row["lifecycle_status"] == "cancelled"


@pytest.mark.parametrize("blockers", [[], [{"code": "CASH_SOURCE_INCOMPLETE"}]])
def test_refund_or_coverage_change_invalidates_approved_daily_basis(environment, blockers):
    env = environment
    path, _ = _plan(env)
    _propose(env, path)
    env.holder["basis"] = _basis("10.00", blockers=blockers, revision=2)
    with pytest.raises(ValueError):
        _execute(env, path)
    assert not env.client.posts


def test_mixed_deposit_claim_precedes_one_post_and_never_double_debits_components(environment):
    env = environment
    path, _ = _plan(env)
    proposal = _propose(env, path)
    def assert_claimed():
        action = env.service.ledger.query_funding_actions(store_alias=STORE)[0]
        assert action["action_id"] == proposal["actionId"]
        assert action["lifecycle_status"] == "executing"
    env.client.on_post = assert_claimed
    result = _execute(env, path)
    assert result["status"] == "readback_confirmed"
    assert len(env.client.posts) == 1
    body = env.client.posts[0]["body"]
    assert body["sum"] == 100 and body["cashback_sum"] == 20 and body["type"] == 1
    assert env.client.cash == Decimal("420") and env.client.cashback == Decimal("80")
    assert env.client.posts[0]["retries"] == 0


def test_zero_confirmed_cash_reserve_can_use_only_explicit_promotional_component(environment):
    env = environment
    env.holder["basis"] = _basis("0.00")
    env.client.percent = 100
    path, _ = _plan(env, cash="0", promo="100")
    _propose(env, path)
    result = _execute(env, path)
    assert result["status"] == "readback_confirmed"
    assert env.client.cash == Decimal("500") and env.client.cashback == Decimal("0")


@pytest.mark.parametrize("pending_status", ["unknown", "readback_pending"])
def test_unknown_post_followup_is_read_only_even_after_approval_expires(environment, pending_status):
    env = environment
    path, _ = _plan(env)
    _propose(env, path)
    env.client.apply_post = False
    env.client.raise_on_post = True
    first = _execute(env, path)
    assert first["status"] == "unknown"
    if pending_status != "unknown":
        env.service.ledger.update_funding_action_status(first["actionId"], pending_status,
            occurred_at=NOW, evidence={"syntheticPendingReadback": True})
    env.service.now += timedelta(hours=2)
    second = _execute(env, path)
    assert second["status"] == "unknown"
    assert len(env.client.posts) == 1
    assert first["automaticRetryAllowed"] is False and second["automaticRetryAllowed"] is False


def test_existing_same_store_money_lock_rejects_another_plan_without_post(environment):
    env = environment
    path, _ = _plan(env)
    _propose(env, path)
    lock = f"wb-advertising-funds:{STORE}"
    assert env.service.ledger.acquire_lock(lock, "another-executor", ttl_seconds=900, now=NOW)
    with pytest.raises(ValueError, match="lock"):
        _execute(env, path)
    assert not env.client.posts
    assert env.service.ledger.release_lock(lock, "another-executor")


def test_unknown_cashback_claim_prevents_second_same_bucket_allocation(environment):
    env = environment
    env.holder["basis"] = _basis("0.00")
    env.client.percent = 100
    first_path, _ = _plan(env, cash="0", promo="70", campaign=CAMPAIGN)
    second_path, _ = _plan(env, cash="0", promo="70", campaign=CAMPAIGN + 1)
    _propose(env, first_path)
    _propose(env, second_path)
    env.client.apply_post = False
    env.client.raise_on_post = True
    assert _execute(env, first_path)["status"] == "unknown"
    with pytest.raises(ValueError):
        _execute(env, second_path)
    assert len(env.client.posts) == 1


@pytest.mark.parametrize("source", ["/api/advert/v2/adverts", "/api/v1/account/balance", "/adv/v1/balance", "/adv/v1/budget?id="])
def test_failed_required_live_get_never_posts_or_retains_approved_claim(environment, source):
    env = environment
    path, _ = _plan(env)
    _propose(env, path)
    env.client.fail_get = source
    with pytest.raises(ValueError):
        _execute(env, path)
    assert not env.client.posts
    action = env.service.ledger.query_funding_actions(store_alias=STORE)[0]
    assert action["lifecycle_status"] == "cancelled"


@pytest.mark.parametrize("component", ["cash", "cashback"])
def test_campaign_credit_alone_never_confirms_wrong_source_debit(environment, component):
    env = environment
    path, _ = _plan(env)
    _propose(env, path)
    def corrupt_source_readback():
        setattr(env.client, component, getattr(env.client, component) + Decimal("1"))
    env.client.after_post = corrupt_source_readback
    result = _execute(env, path)
    assert result["status"] == "unknown"
    assert result["readback"]["expectedTotalCny"] == result["readback"]["observedTotalCny"]
    assert result["readback"]["fundingSourceReadback"]["matched"] is False
    env.service.now += timedelta(hours=2)
    repeated = _execute(env, path)
    assert repeated["readbackOnly"] is True and repeated["status"] == "unknown"
    assert len(env.client.posts) == 1


def test_confirmed_cashback_debit_cannot_be_resurrected_by_stale_live_bucket(environment):
    env = environment
    env.holder["basis"] = _basis("0.00")
    env.client.percent = 100
    first_path, _ = _plan(env, cash="0", promo="70", campaign=CAMPAIGN)
    _propose(env, first_path)
    assert _execute(env, first_path)["status"] == "readback_confirmed"
    assert env.client.cashback == Decimal("30")
    env.client.cashback = Decimal("100")  # Simulate a stale source, not a new grant.
    second_path, _ = _plan(env, cash="0", promo="70", campaign=CAMPAIGN + 1)
    _propose(env, second_path)
    with pytest.raises(ValueError, match="confirmed post-deposit balance"):
        _execute(env, second_path)
    assert len(env.client.posts) == 1


def test_two_executors_cannot_post_while_same_store_claim_is_in_flight(environment):
    env = environment
    first_path, _ = _plan(env, campaign=CAMPAIGN)
    second_path, _ = _plan(env, campaign=CAMPAIGN + 1)
    _propose(env, first_path)
    _propose(env, second_path)
    entered_post, release_post = Event(), Event()
    def pause_first_fake_post():
        entered_post.set()
        assert release_post.wait(5), "Test must release the synthetic POST promptly"
    env.client.on_post = pause_first_fake_post
    def first_executor():
        # A separate connection models another scheduler/process, without
        # sharing a SQLite connection across threads.
        worker = FinanceService.__new__(FinanceService)
        worker.__dict__.update(env.service.__dict__)
        worker.ledger = FinanceLedger(env.root / "finance.sqlite3")
        try:
            return worker.ads_deposit(plan_path=first_path, approved_by="synthetic-owner", confirm_live=True)
        finally:
            worker.close()
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(first_executor)
        try:
            assert entered_post.wait(5), "First synthetic executor did not reach its POST boundary"
            with pytest.raises(ValueError, match="store lock"):
                _execute(env, second_path)
        finally:
            release_post.set()
        assert first.result(timeout=5)["status"] == "readback_confirmed"
    assert len(env.client.posts) == 1


@pytest.mark.parametrize("status", ["approved", "executing", "executed", "readback_pending"])
def test_unresolved_cashback_lifecycle_states_keep_bucket_claim(environment, status):
    env = environment
    env.holder["basis"] = _basis("0.00")
    env.client.percent = 100
    first_path, _ = _plan(env, cash="0", promo="70", campaign=CAMPAIGN)
    second_path, _ = _plan(env, cash="0", promo="70", campaign=CAMPAIGN + 1)
    first = _propose(env, first_path)
    _propose(env, second_path)
    env.service.ledger.update_funding_action_status(first["actionId"], status,
        occurred_at=NOW, evidence={"syntheticLifecycleSetup": True})
    with pytest.raises(ValueError, match="reserved"):
        _execute(env, second_path)
    assert not env.client.posts


def test_revoking_promotional_opt_in_invalidates_existing_proposal_without_cash_fallback(environment):
    env = environment
    path, _ = _plan(env)
    _propose(env, path)
    env.service.periodic_standard.raw["runningAccounts"]["funding"]["cashbackMode"] = "disabled"
    with pytest.raises(ValueError, match="not enabled"):
        _execute(env, path)
    assert not env.client.posts


def test_prior_day_funding_basis_is_not_new_spending_authority_after_midnight(environment):
    env = environment
    path, _ = _plan(env)
    _propose(env, path)
    env.service.now += timedelta(days=1)
    with pytest.raises(ValueError, match="stale"):
        _execute(env, path)
    assert not env.client.posts


@pytest.fixture
def refresh_chain(environment, monkeypatch):
    env = environment
    env.service.registry_path = env.root / "synthetic-registry.json"
    env.service.max_store_workers = 1
    env.service.source_factory = object()
    state = SimpleNamespace(calls=[], fail_stage=None, advance_clock=False,
        statuses=[{"storeAlias": STORE, "primaryComplete": True,
                   "sources": [{"source": "finance_detailed", "complete": True}]}])
    def sync(ledger, **kwargs):
        assert ledger is env.service.ledger
        state.calls.append(("sync", kwargs))
        if state.fail_stage == "sync":
            raise RuntimeError("synthetic source collection failed")
        if state.advance_clock:
            env.service.now += timedelta(days=1)
        return [SimpleNamespace(as_dict=lambda item=item: deepcopy(item)) for item in state.statuses]
    def refresh(**kwargs):
        state.calls.append(("account_refresh", kwargs))
        if state.fail_stage == "account_refresh":
            raise RuntimeError("synthetic account materialization failed")
        return {"status": "refreshed", "dataThrough": kwargs["through"].isoformat()}
    def plan(**kwargs):
        state.calls.append(("funds_plan", kwargs))
        complete = all(item["primaryComplete"] for item in state.statuses)
        return {"status": "READY_FOR_PREFLIGHT" if complete else "PENDING_DATA",
                "planType": "daily_funding", "dataThrough": kwargs["through"].isoformat()}
    def prohibited(*args, **kwargs):
        pytest.fail("Funding refresh must not build reports or enter a payment executor")
    monkeypatch.setattr(service_module, "sync_enabled_stores", sync)
    monkeypatch.setattr(env.service, "account_refresh", refresh)
    monkeypatch.setattr(env.service, "funds_plan", plan)
    for name in ("report", "account_report", "daily", "ads_deposit", "ads_deposit_plan"):
        monkeypatch.setattr(env.service, name, prohibited)
    return SimpleNamespace(env=env, state=state)


def test_funds_refresh_is_bounded_source_account_plan_only_with_one_moscow_cutoff(refresh_chain):
    env, state = refresh_chain.env, refresh_chain.state
    # Shanghai is already Sep 8; Moscow is still Sep 7, so cutoff is Sep 6.
    env.service.now = datetime(2026, 9, 7, 20, 30, tzinfo=timezone.utc)
    state.advance_clock = True
    result = env.service.funds_refresh(through="yesterday", correction_window_days=30)
    assert [name for name, _ in state.calls] == ["sync", "account_refresh", "funds_plan"]
    assert all(kwargs["through"] == date(2026, 9, 6) for _, kwargs in state.calls)
    sync_args = state.calls[0][1]
    assert sync_args["bootstrap"] is False and sync_args["correction_window_days"] == 30
    assert state.calls[1][1]["source_statuses"] == state.statuses
    assert result["runningAccounts"]["dataThrough"] == result["fundingPlan"]["dataThrough"] == "2026-09-06"
    assert result["fundingWriteAttempted"] is False
    assert not env.client.calls


@pytest.mark.parametrize("stage", ["sync", "account_refresh"])
def test_funds_refresh_failure_never_advances_downstream_or_hides_error(refresh_chain, stage):
    env, state = refresh_chain.env, refresh_chain.state
    state.fail_stage = stage
    with pytest.raises(RuntimeError, match="synthetic"):
        env.service.funds_refresh()
    expected = ["sync"] if stage == "sync" else ["sync", "account_refresh"]
    assert [name for name, _ in state.calls] == expected
    assert not env.client.calls


def test_funds_refresh_preserves_partial_source_evidence_for_basis_gating(refresh_chain):
    env, state = refresh_chain.env, refresh_chain.state
    state.statuses[0]["primaryComplete"] = False
    state.statuses[0]["sources"][0].update(complete=False, error="synthetic unavailable day")
    result = env.service.funds_refresh()
    assert state.calls[1][1]["source_statuses"] == state.statuses
    assert result["stores"] == state.statuses
    assert result["fundingPlan"]["status"] == "PENDING_DATA"
    assert result["fundingWriteAttempted"] is False and not env.client.calls


@pytest.mark.parametrize("window", [0, -1, 121])
def test_funds_refresh_rejects_unbounded_correction_window_before_collection(refresh_chain, window):
    env, state = refresh_chain.env, refresh_chain.state
    with pytest.raises(ValueError, match="between 1 and 120"):
        env.service.funds_refresh(correction_window_days=window)
    assert not state.calls and not env.client.calls


@pytest.mark.parametrize("evidence,expected_cash", [
    ({}, "100.00"),
    ({"schemaVersion": 2, "source": "balance", "amountCny": "100.00"}, "100.00"),
    ({"schemaVersion": 3, "source": "balance", "amountCny": "100.00", "cashAmountCny": "80.00", "cashbackAmountCny": "20.00"}, "80.00"),
    ({"schemaVersion": 3, "source": "balance", "amountCny": "100.00", "cashAmountCny": "0.00", "cashbackAmountCny": "100.00"}, "0.00"),
    ({"source": "bonus", "amountCny": "100.00"}, "0.00"),
])
def test_legacy_action_cash_compatibility_never_charges_promotion(evidence, expected_cash):
    action = {"store_alias": STORE, "requested_amount_cny": "100.00", "evidence_json": json.dumps(evidence)}
    assert FinanceService._funding_action_cash_amount(action) == Decimal(expected_cash)


@pytest.mark.parametrize("invalid_field,value", [("store", "another-store"), ("currency", "RUB"), ("cashbackAmountCny", "21.00")])
def test_mixed_legacy_reserve_rejects_conflicting_split_evidence(invalid_field, value):
    evidence = {"schemaVersion": 3, "source": "balance", "store": STORE, "currency": "CNY",
        "amountCny": "100.00", "cashAmountCny": "80.00", "cashbackAmountCny": "20.00"}
    evidence[invalid_field] = value
    action = {"store_alias": STORE, "requested_amount_cny": "100.00", "evidence_json": json.dumps(evidence)}
    with pytest.raises(ValueError, match="invalid cash/promotion"):
        FinanceService._funding_action_cash_amount(action)


def test_legacy_reserve_deducts_only_cash_for_confirmed_and_pending_mixed_actions(environment, monkeypatch):
    env = environment
    confirmed_path, _ = _plan(env, cash="80", promo="20", campaign=CAMPAIGN)
    pending_path, _ = _plan(env, cash="80", promo="20", campaign=CAMPAIGN + 1)
    confirmed = _propose(env, confirmed_path)
    pending = _propose(env, pending_path)
    env.service.ledger.update_funding_action_status(confirmed["actionId"], "readback_confirmed", occurred_at=NOW,
        evidence={"syntheticConfirmed": True})
    env.service.ledger.update_funding_action_status(pending["actionId"], "readback_pending", occurred_at=NOW,
        evidence={"syntheticPending": True})
    report = {"report_id": "synthetic-weekly", "report_key": "synthetic-week", "report_version": "v1",
        "period_end": "2026-09-06", "stores": [{"storeAlias": STORE, "advertisingProvisionCny": "300.00"}], "orders": []}
    activation = NOW - timedelta(days=30)
    monkeypatch.setattr(env.service, "_funding_activation_info", lambda *a, **kw: {"at": activation})
    monkeypatch.setattr(env.service, "_report_is_allocation_ready", lambda value: True)
    monkeypatch.setattr(env.service, "_advertising_report_accrued_at", lambda *a, **kw: NOW)
    monkeypatch.setattr(env.service, "_confirmed_deposit_effective_at", lambda action: NOW)
    monkeypatch.setattr(env.service.ledger, "find_reports", lambda **kw: [report] if kw["report_status"] == "FINAL" else [])
    monkeypatch.setattr(env.service.ledger, "get_report", lambda report_id: deepcopy(report))
    accruals = env.service._advertising_accruals(STORE, formal_activation_at=activation)
    assert len(accruals) == 1 and accruals[0].allocated_cny == Decimal("80.00")
    assert env.service._available_advertising_reserve_cny(STORE) == Decimal("140.00")
    assert env.service._available_advertising_reserve_cny(STORE, exclude_action_id=pending["actionId"]) == Decimal("220.00")
    assert not env.client.calls


def test_fractional_deposit_amount_is_rejected_before_any_live_collection(environment):
    env = environment
    funding = env.service.funds_plan(through="yesterday")
    with pytest.raises(ValueError, match="whole positive CNY"):
        env.service.ads_deposit_plan(funds_plan_path=Path(funding["path"]), store_alias=STORE,
            campaign=CAMPAIGN, amount_cny=Decimal("100.001"), approved_by="synthetic-owner")
    assert not env.client.calls
    assert not env.service.ledger.query_funding_actions(store_alias=STORE)


def test_real_refresh_and_funding_entrypoints_exclude_each_other_across_threads(environment, monkeypatch):
    from wb_manager import finance_running_adapter as adapter
    env = environment
    env.service.registry_path = env.root / "synthetic-registry.json"
    env.service.periodic_standard.historical_cash_events_registry = env.root / "synthetic-cash.json"
    env.service.policy_timeline = object()
    for active_kind in ("funding", "refresh"):
        entered, release = Event(), Event()
        def pause_then_fail(*args, **kwargs):
            entered.set()
            assert release.wait(5), "Release the synthetic snapshot operation promptly"
            raise RuntimeError("synthetic stop before data access")
        monkeypatch.setattr(adapter, "extract_running_account_sources", pause_then_fail)
        monkeypatch.setattr(env.service, "_funding_activation_info", pause_then_fail, raising=False)
        def refresh():
            return adapter.refresh_running_accounts(env.service, through=date(2026, 9, 7))
        def funding():
            return guarded_daily_funding_basis(env.service, store_alias=STORE, through=date(2026, 9, 7))
        active, competing = (refresh, funding) if active_kind == "refresh" else (funding, refresh)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(active)
            try:
                assert entered.wait(5), "Active snapshot operation never entered its guarded body"
                with pytest.raises(RunningAccountLockError, match="snapshot is busy"):
                    competing()
            finally:
                release.set()
            with pytest.raises(RuntimeError, match="synthetic stop before data access"):
                future.result(timeout=5)
    assert not env.client.calls


def test_snapshot_os_lock_releases_on_exception_without_removing_fixed_lock_file(tmp_path):
    database = tmp_path / "synthetic-running.sqlite3"
    with pytest.raises(RuntimeError, match="synthetic interrupted refresh"):
        with running_account_snapshot_lock(database):
            with pytest.raises(RunningAccountLockError):
                with running_account_snapshot_lock(database):
                    pytest.fail("Concurrent snapshot lock unexpectedly succeeded")
            raise RuntimeError("synthetic interrupted refresh")
    lock_path = database.with_name(database.name + ".snapshot.lock")
    assert lock_path.is_file() and lock_path.stat().st_size >= 1
    with running_account_snapshot_lock(database):
        assert lock_path.is_file()
    assert not database.exists()


def test_failed_snapshot_writer_blocks_funding_after_os_lock_is_released(environment):
    from wb_manager.finance_running_adapter import _settings
    env = environment
    @serialized_running_account(writer=True)
    def interrupted_refresh(service):
        raise RuntimeError("synthetic partially committed refresh")
    with pytest.raises(RuntimeError, match="partially committed"):
        interrupted_refresh(env.service)
    database = Path(_settings(env.service)["db"])
    assert database.with_name(database.name + ".refresh-in-progress").is_file()
    # This is the real funding entrypoint, not its mocked basis provider.
    with pytest.raises(RunningAccountLockError, match="incomplete refresh"):
        guarded_daily_funding_basis(env.service, store_alias=STORE, through=date(2026, 9, 7))
    assert not env.client.calls


def test_successful_snapshot_replay_clears_failed_marker_and_reenables_reader(environment):
    from wb_manager.finance_running_adapter import _settings
    env = environment
    database = Path(_settings(env.service)["db"])
    marker = database.with_name(database.name + ".refresh-in-progress")
    @serialized_running_account(writer=True)
    def interrupted_refresh(service):
        raise RuntimeError("synthetic interrupted refresh")
    @serialized_running_account(writer=True)
    def successful_replay(service):
        assert marker.is_file(), "Incomplete marker must cover the entire replay"
        return {"status": "synthetic complete"}
    @serialized_running_account
    def read_snapshot(service):
        assert not marker.exists()
        return "synthetic consistent snapshot"
    with pytest.raises(RuntimeError):
        interrupted_refresh(env.service)
    assert successful_replay(env.service)["status"] == "synthetic complete"
    assert read_snapshot(env.service) == "synthetic consistent snapshot"
    assert database.with_name(database.name + ".snapshot.lock").is_file()
    assert not env.client.calls
