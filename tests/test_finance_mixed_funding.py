from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import pytest

from wb_manager.finance_funds import (
    FundingPlanError,
    build_advertising_deposit_plan,
    build_funding_plan,
    preflight_deposit_execution,
    select_cashback_split,
    validate_advertising_deposit_plan,
)
from wb_manager.finance_sources import FinanceSourceError, deposit_campaign_budget


def _basis(available="60.00", **changes):
    value = {
        "schemaVersion": 1,
        "basisType": "daily_confirmed_advertising",
        "basisId": "a" * 64,
        "fingerprint": "a" * 64,
        "store": "store-a",
        "dataThrough": "2026-09-06",
        "availableCashCny": available,
        "grossConfirmedAccrualCny": "150.00",
        "sharedReturnChargeCny": "10.00",
        "confirmedCashAllocatedCny": "75.00",
        "lockedCashCny": "5.00",
        "cashSpendCny": "70.00",
        "unspentBookBudgetCny": "70.00",
        "blockers": ["unrelated receipt reconciliation pending"],
        "evidence": [{"eventId": "synthetic-sale", "cashCny": "150.00"}],
    }
    value.update(changes)
    return value


def _values(**changes):
    value = {
        "report_id": None,
        "store": "store-a",
        "campaign": 42,
        "amount_cny": "100",
        "source": "balance",
        "currency": "CNY",
        "source_balance_cny": "60.00",
        "source_balance_evidence_at": "2026-09-07T09:05:00+03:00",
        "policy_cap_cny": "60.00",
        "available_advertising_reserve_cny": "60.00",
        "approved_by": "owner",
        "approved_at": "2026-09-07T09:10:00+03:00",
        "expires_at": "2026-09-07T09:25:00+03:00",
        "funding_basis": _basis(),
        "cashback_amount_cny": "40",
        "cashback_percent": 50,
        "cashback_available_cny": "45.00",
        "cashback_expires_at": "2026-10-01T00:00:00Z",
    }
    value.update(changes)
    return value


def _plan(**changes):
    return build_advertising_deposit_plan(**_values(**changes))


def _cash_only(**changes):
    return _plan(
        **{
            "amount_cny": 60,
            "cashback_amount_cny": 0,
            "cashback_percent": None,
            "cashback_available_cny": None,
            "cashback_expires_at": None,
            **changes,
        }
    )


def test_v3_total_does_not_consume_cash_reserve_or_live_net_twice():
    plan = _plan()
    payload = validate_advertising_deposit_plan(plan)
    assert payload["schemaVersion"] == 3
    assert payload["reportId"] is None
    assert payload["amountCny"] == "100.00"
    assert payload["cashAmountCny"] == "60.00"
    assert payload["cashbackAmountCny"] == "40.00"
    assert payload["fundingBasis"] == _basis()
    assert payload["policyCapCny"] == payload["sourceBalanceCny"] == "60.00"
    assert payload["cashbackPercent"] == 50
    assert payload["cashbackAvailableCny"] == "45.00"
    assert payload["cashbackExpiresAt"] == "2026-10-01T00:00:00+00:00"
    assert payload["withdrawalExecutionSupported"] is False
    assert _plan().fingerprint == plan.fingerprint


def test_v3_full_promo_allows_zero_cash_cap_without_granting_cash_authority():
    plan = _plan(
        source_balance_cny=0,
        policy_cap_cny=0,
        available_advertising_reserve_cny=0,
        funding_basis=_basis("0.00"),
        cashback_amount_cny=100,
        cashback_percent=100,
        cashback_available_cny=100,
    )
    assert validate_advertising_deposit_plan(plan)["cashAmountCny"] == "0.00"
    with pytest.raises(FundingPlanError):
        _cash_only(
            amount_cny=1, source_balance_cny=0, policy_cap_cny=0,
            available_advertising_reserve_cny=0, funding_basis=_basis("0.00"),
        )


def test_v3_cash_only_has_explicit_zero_promo_and_null_bucket_fields():
    payload = validate_advertising_deposit_plan(_cash_only())
    assert payload["cashAmountCny"] == payload["amountCny"] == "60.00"
    assert payload["cashbackAmountCny"] == "0.00"
    assert all(payload[key] is None for key in (
        "cashbackPercent", "cashbackAvailableCny", "cashbackExpiresAt",
    ))


def test_v2_pure_cash_payload_and_fingerprint_are_backward_compatible():
    payload = _cash_only(report_id="synthetic-weekly", funding_basis=None).as_dict()
    expected = {
        "schemaVersion": 2,
        "planType": "advertising_deposit",
        "actionType": "advertising_top_up",
        "reportId": "synthetic-weekly",
        "store": "store-a", "campaign": 42, "amountCny": "60.00",
        "source": "balance", "currency": "CNY", "sourceBalanceCny": "60.00",
        "sourceBalanceEvidenceAt": "2026-09-07T09:05:00+03:00",
        "policyCapCny": "60.00", "availableAdvertisingReserveCny": "60.00",
        "approvedBy": "owner", "approvedAt": "2026-09-07T09:10:00+03:00",
        "expiresAt": "2026-09-07T09:25:00+03:00",
        "withdrawalExecutionSupported": False,
    }
    expected["fingerprint"] = hashlib.sha256(json.dumps(
        expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert payload == expected
    assert validate_advertising_deposit_plan(payload) == expected
    for key, value in (("cashAmountCny", "60.00"), ("fundingBasis", _basis()),
                       ("cashbackAmountCny", "0.00")):
        with pytest.raises(FundingPlanError, match="fields differ"):
            validate_advertising_deposit_plan({**payload, key: value})
    with pytest.raises(FundingPlanError, match="schema v3"):
        _plan(report_id="synthetic-weekly", funding_basis=None)


@pytest.mark.parametrize("changes", [
    {"schemaVersion": True}, {"schemaVersion": 2}, {"basisType": "weekly"},
    {"store": "store-b"}, {"currency": "RUB"}, {"fingerprint": ""},
    {"fingerprint": "x" * 64}, {"dataThrough": "2026-9-6"},
    {"dataThrough": "2026-09-06T00:00:00Z"}, {"dataThrough": None},
    {"availableCashCny": None}, {"availableCashCny": "61.00"},
    {"availableCashCny": "59.999"}, {"availableCashCny": -1},
    {"evidence": float("nan")},
])
def test_v3_rejects_invalid_or_mismatched_daily_evidence(changes):
    with pytest.raises(FundingPlanError):
        _plan(funding_basis=_basis(**changes))


def test_v3_freezes_complete_nested_evidence_and_detects_any_tampering():
    basis = _basis()
    plan = _plan(funding_basis=basis)
    basis["evidence"][0]["cashCny"] = "999.00"
    exported = plan.as_dict()
    exported["fundingBasis"]["evidence"][0]["cashCny"] = "888.00"
    assert plan.as_dict()["fundingBasis"]["evidence"][0]["cashCny"] == "150.00"
    corrected = _basis(evidence=[{"eventId": "synthetic-correction"}])
    assert _plan(funding_basis=corrected).fingerprint != plan.fingerprint
    for key, value in (
        ("cashAmountCny", "59.00"), ("cashbackPercent", 60),
        ("cashbackAvailableCny", "46.00"),
        ("cashbackExpiresAt", "2026-10-02T00:00:00+00:00"),
        ("fundingBasis", corrected),
    ):
        with pytest.raises(FundingPlanError):
            validate_advertising_deposit_plan({**plan.as_dict(), key: value})


@pytest.mark.parametrize("changes", [
    {"report_id": "old-report"}, {"source": "account"}, {"source": "bonus"},
    {"amount_cny": "100.001"}, {"amount_cny": 0}, {"amount_cny": True},
    {"cashback_amount_cny": "40.001"}, {"cashback_amount_cny": -1},
    {"cashback_amount_cny": True}, {"cashback_amount_cny": 101},
    {"cashback_percent": None}, {"cashback_percent": True},
    {"cashback_percent": "50.1"}, {"cashback_percent": 0},
    {"cashback_percent": 101}, {"cashback_percent": 39},
    {"cashback_available_cny": "39.99"}, {"cashback_available_cny": "39.999"},
    {"cashback_available_cny": None},
    {"cashback_expires_at": "2026-09-07T09:10:00+03:00"},
    {"cashback_expires_at": "2026-09-07T09:24:59+03:00"},
    {"cashback_expires_at": "2026-10-01T00:00:00"},
    {"cashback_expires_at": None}, {"source_balance_cny": "59.99"},
    {"source_balance_cny": "59.999"}, {"policy_cap_cny": "59.00"},
    {"available_advertising_reserve_cny": "59.00"},
])
def test_v3_rejects_unsafe_mixed_funding(changes):
    with pytest.raises(FundingPlanError):
        _plan(**changes)


def test_v3_cash_only_cannot_keep_a_stale_or_unused_cashback_selection():
    with pytest.raises(FundingPlanError, match="unused cashback"):
        _cash_only(cashback_percent=50)


def test_v3_unknown_or_expired_outcome_never_authorizes_a_retry():
    plan = _plan()
    assert preflight_deposit_execution(
        plan, current_status="approved", now="2026-09-07T09:11:00+03:00",
    ).allowed is True
    for status, now in (("unknown", "2026-09-07T09:11:00+03:00"),
                        ("approved", "2026-09-07T09:26:00+03:00")):
        assert preflight_deposit_execution(plan, current_status=status, now=now).allowed is False


def test_readback_pending_keeps_the_unresolved_cash_commitment_locked():
    plan = build_funding_plan(
        report_id="synthetic-funding", store="store-a",
        platform_available_balance_cny="100.00",
        balance_evidence_at="2026-09-07T09:10:00+03:00",
        formal_activation_at="2026-09-01", advertising_accruals=[],
        deposit_commitments=[{
            "action_id": "synthetic-pending-deposit",
            "amount_cny": "60.00", "status": "readback_pending",
        }],
    )
    assert str(plan.unread_advertising_deposits_cny) == "60.00"
    assert str(plan.suggested_withdrawal_cny) == "40.00"
    assert plan.locked_deposit_action_ids == ("synthetic-pending-deposit",)


def test_readback_pending_is_readback_only_even_after_plan_expiry():
    check = preflight_deposit_execution(
        _plan(), current_status="readback_pending",
        now="2026-09-07T10:00:00+03:00",
    )
    assert check.allowed is False
    assert check.requires_readback is True
    assert check.automatic_retry_allowed is False
    assert "readback" in check.reason


def _bucket(sum=45, percent=50, expiration_date="2026-10-01T00:00:00Z"):
    return {"sum": sum, "percent": percent, "expiration_date": expiration_date}


def test_selector_uses_one_earliest_expiring_bucket_and_never_combines_them():
    buckets = [_bucket(80, 100, "2026-11-01T00:00:00Z"), _bucket(12, 50)]
    result = select_cashback_split(100, buckets, now="2026-09-07T00:00:00Z")
    assert result == select_cashback_split(100, reversed(buckets), now="2026-09-07T00:00:00Z")
    assert result["selectionStatus"] == "SELECTED"
    assert result["cashbackAmountCny"] == result["cashbackAvailableCny"] == "12.00"
    assert result["cashAmountCny"] == "88.00"
    assert result["cashbackPercent"] == 50


def test_selector_floors_to_whole_promo_and_skips_malformed_expired_buckets():
    buckets = [None, _bucket(100, 100, "2026-09-07T00:00:00Z"),
               _bucket(100, 101), _bucket(True), _bucket("99.999"),
               _bucket(100, 50, "2026-10-01T00:00:00"), _bucket(100, 50)]
    result = select_cashback_split(101, buckets, now="2026-09-07T00:00:00Z")
    assert result["cashbackAmountCny"] == "50.00"
    assert result["cashAmountCny"] == "51.00"
    assert result["cashbackAvailableCny"] == "100.00"
    result = select_cashback_split(100, [_bucket("0.99")], now="2026-09-07T00:00:00Z")
    assert result["selectionStatus"] == "NO_USABLE_CASHBACK"
    assert result["cashbackAmountCny"] == "0.00"
    assert result["cashAmountCny"] == "100.00"
    assert result["cashbackPercent"] is None
    assert result["cashbackAvailableCny"] is None
    assert result["cashbackExpiresAt"] is None


@pytest.mark.parametrize("buckets", [None, {}, "cashbacks", 42])
def test_selector_rejects_non_array_evidence(buckets):
    with pytest.raises(FundingPlanError):
        select_cashback_split(100, buckets)


@dataclass
class _Response:
    status: int
    ok: bool
    data: object = None


class _Client:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, *, body=None, retries=2):
        self.calls.append({"method": method, "url": url, "body": body, "retries": retries})
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _deposit(client, **changes):
    values = {
        "campaign_id": 42, "amount": 100, "source": "balance",
        "approved_by": "owner", "confirm_live": True,
        "cashback_sum": 40, "cashback_percent": 50,
    }
    values.update(changes)
    return deposit_campaign_budget(client, **values)


def test_mixed_deposit_request_uses_total_sum_and_cashback_body_without_retries():
    client = _Client(_Response(200, True, {"total": 120}))
    result = _deposit(client)
    assert result.state == "succeeded" and result.requires_readback
    assert result.amount == 100
    assert client.calls == [{
        "method": "POST", "url": "https://advert-api.wildberries.ru/adv/v1/budget/deposit?id=42",
        "body": {"sum": 100, "type": 1, "return": True, "cashback_sum": 40, "cashback_percent": 50},
        "retries": 0,
    }]


@pytest.mark.parametrize("changes", [
    {"cashback_sum": 40.1}, {"cashback_sum": True}, {"cashback_sum": -1},
    {"cashback_sum": 101}, {"cashback_sum": 51}, {"cashback_sum": None},
    {"cashback_percent": None}, {"cashback_percent": True},
    {"cashback_percent": 50.1}, {"cashback_percent": 0}, {"cashback_percent": 101},
    {"source": "bonus"},
])
def test_invalid_cashback_request_is_rejected_before_any_call(changes):
    client = _Client(_Response(200, True))
    with pytest.raises(FinanceSourceError):
        _deposit(client, **changes)
    assert client.calls == []


@pytest.mark.parametrize("response,state", [
    (_Response(400, False, {"error": "cashback unavailable"}), "failed"),
    (_Response(503, False), "unknown"), (TimeoutError("response lost"), "unknown"),
])
def test_promo_failure_never_falls_back_to_cash_or_retries(response, state):
    client = _Client(response)
    result = _deposit(client)
    assert result.state == state
    assert result.requires_readback is (state == "unknown")
    assert len(client.calls) == 1
    assert client.calls[0]["body"]["cashback_sum"] == 40
    assert client.calls[0]["retries"] == 0


def test_full_promo_request_is_supported_when_live_bucket_percent_is_one_hundred():
    client = _Client(_Response(200, True, {"total": 100}))
    _deposit(client, cashback_sum=100, cashback_percent=100)
    assert client.calls[0]["body"]["sum"] == client.calls[0]["body"]["cashback_sum"] == 100
