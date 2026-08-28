from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from wb_manager.finance_funds import (
    AdvertisingAccrual,
    DepositCommitment,
    FundingPlanError,
    build_advertising_deposit_plan,
    build_funding_plan,
    preflight_deposit_execution,
    validate_advertising_deposit_plan,
    validate_funding_plan,
)


def _accrual(
    identifier: str,
    amount: str,
    timestamp: str,
    *,
    allocated: str = "0",
    historical: bool = False,
) -> AdvertisingAccrual:
    return AdvertisingAccrual(
        accrual_id=identifier,
        amount_cny=Decimal(amount),
        allocated_cny=Decimal(allocated),
        accrued_at=datetime.fromisoformat(timestamp),
        historical_bootstrap=historical,
    )


def _funding_plan(**overrides):
    values = {
        "report_id": "weekly-2026-08-17_2026-08-23-v1",
        "store": "owner-store",
        "platform_available_balance_cny": "100.00",
        "balance_evidence_at": "2026-08-24T09:00:00+03:00",
        "formal_activation_at": "2026-08-01",
        "advertising_accruals": [
            _accrual("at-boundary", "30", "2026-08-01T00:00:00+03:00"),
            _accrual("before-boundary", "9", "2026-07-31T23:59:59+03:00"),
        ],
        "deposit_commitments": [
            DepositCommitment(
                "deposit-approved",
                Decimal("10"),
                "approved",
            )
        ],
        "other_locked_funds_cny": "5",
    }
    values.update(overrides)
    return build_funding_plan(**values)


def _deposit_plan(**overrides):
    values = {
        "report_id": "weekly-2026-08-17_2026-08-23-v1",
        "store": "owner-store",
        "campaign": 1234567,
        "amount_cny": "100",
        "source": "balance",
        "currency": "CNY",
        "source_balance_cny": "500",
        "source_balance_evidence_at": "2026-08-24T09:05:00+03:00",
        "policy_cap_cny": "120",
        "available_advertising_reserve_cny": "120",
        "approved_by": "boss",
        "approved_at": "2026-08-24T09:10:00+03:00",
        "expires_at": "2026-08-24T09:25:00+03:00",
    }
    values.update(overrides)
    return build_advertising_deposit_plan(**values)


def test_withdrawal_formula_uses_only_post_activation_and_unread_locks() -> None:
    plan = _funding_plan()

    assert plan.unallocated_advertising_accrual_cny == Decimal("30.00")
    assert plan.excluded_historical_advertising_accrual_cny == Decimal("9.00")
    assert plan.unread_advertising_deposits_cny == Decimal("10.00")
    assert plan.other_locked_funds_cny == Decimal("5.00")
    assert plan.suggested_withdrawal_cny == Decimal("55.00")
    assert validate_funding_plan(plan)["suggestedWithdrawalCny"] == "55.00"

    floor = _funding_plan(platform_available_balance_cny="20")
    assert floor.suggested_withdrawal_cny == Decimal("0.00")


def test_readback_releases_a_deposit_lock_but_unknown_keeps_it() -> None:
    plan = _funding_plan(
        advertising_accruals=[],
        other_locked_funds_cny="0",
        deposit_commitments=[
            DepositCommitment("unknown", Decimal("12"), "unknown"),
            DepositCommitment(
                "read-back",
                Decimal("25"),
                "succeeded",
                datetime(2026, 8, 24, 9, 5, tzinfo=timezone.utc),
            ),
            DepositCommitment("failed", Decimal("50"), "failed"),
        ],
    )

    assert plan.unread_advertising_deposits_cny == Decimal("12.00")
    assert plan.locked_deposit_action_ids == ("unknown",)
    assert plan.suggested_withdrawal_cny == Decimal("88.00")


def test_first_historical_bootstrap_never_creates_retroactive_funding_duty() -> None:
    plan = _funding_plan(
        platform_available_balance_cny="500",
        advertising_accruals=[
            _accrual("historical-report-row", "120", "2026-08-20T10:00:00+03:00")
        ],
        deposit_commitments=[],
        other_locked_funds_cny="0",
        historical_bootstrap=True,
    )

    assert plan.unallocated_advertising_accrual_cny == Decimal("0.00")
    assert plan.excluded_historical_advertising_accrual_cny == Decimal("120.00")
    assert plan.suggested_withdrawal_cny == Decimal("500.00")
    assert plan.as_dict()["historicalBootstrap"] is True


def test_fingerprint_is_idempotent_and_order_independent() -> None:
    accruals = [
        _accrual("b", "5", "2026-08-03T00:00:00+03:00"),
        _accrual("a", "7", "2026-08-02T00:00:00+03:00"),
    ]
    deposits = [
        DepositCommitment("d2", Decimal("4"), "executing"),
        DepositCommitment("d1", Decimal("3"), "approved"),
    ]
    first = _funding_plan(
        advertising_accruals=accruals,
        deposit_commitments=deposits,
    )
    second = _funding_plan(
        advertising_accruals=reversed(accruals),
        deposit_commitments=reversed(deposits),
    )

    assert first.fingerprint == second.fingerprint
    assert first.as_dict() == second.as_dict()


@pytest.mark.parametrize(
    "overrides",
    [
        {"report_id": ""},
        {"store": ""},
        {"balance_evidence_at": ""},
        {"balance_evidence_at": "2026-08-24T09:00:00"},
        {"platform_available_balance_cny": "-0.01"},
        {"other_locked_funds_cny": "-1"},
    ],
)
def test_missing_evidence_and_negative_money_fail_closed(overrides: dict) -> None:
    with pytest.raises(FundingPlanError):
        _funding_plan(**overrides)


def test_invalid_accrual_and_tampered_plan_fail_closed() -> None:
    with pytest.raises(FundingPlanError, match="exceeds"):
        _funding_plan(
            advertising_accruals=[
                _accrual(
                    "overallocated",
                    "10",
                    "2026-08-02T00:00:00+03:00",
                    allocated="11",
                )
            ]
        )

    payload = _funding_plan().as_dict()
    payload["components"]["otherLockedFundsCny"] = "-1.00"
    with pytest.raises(FundingPlanError):
        validate_funding_plan(payload)

    missing_evidence = deepcopy(_funding_plan().as_dict())
    del missing_evidence["balanceEvidenceAt"]
    with pytest.raises(FundingPlanError):
        validate_funding_plan(missing_evidence)


@pytest.mark.parametrize(
    "overrides",
    [
        {"store": ""},
        {"campaign": 0},
        {"amount_cny": "-1"},
        {"amount_cny": "1.5"},
        {"source": "mystery"},
        {"currency": "RUB"},
        {"source_balance_cny": "99"},
        {"source_balance_evidence_at": "2026-08-24T09:10:01+03:00"},
        {"source_balance_evidence_at": "2026-08-24T08:54:59+03:00"},
        {"policy_cap_cny": "99"},
        {"policy_cap_cny": "121"},
        {"available_advertising_reserve_cny": "99"},
        {"approved_by": ""},
        {"approved_at": ""},
        {"approved_at": "2026-08-24T09:10:00"},
        {"expires_at": "2026-08-24T09:10:00+03:00"},
        {"expires_at": "2026-08-24T10:10:01+03:00"},
    ],
)
def test_advertising_deposit_requires_exact_approval(overrides: dict) -> None:
    with pytest.raises(FundingPlanError):
        _deposit_plan(**overrides)


def test_deposit_plan_has_stable_fingerprint_and_rejects_withdrawal_fields() -> None:
    first = _deposit_plan()
    second = _deposit_plan()
    assert first.fingerprint == second.fingerprint
    assert validate_advertising_deposit_plan(first)["actionType"] == "advertising_top_up"
    payload = first.as_dict()
    assert payload["currency"] == "CNY"
    assert payload["sourceBalanceCny"] == "500.00"
    assert payload["policyCapCny"] == "120.00"
    assert payload["availableAdvertisingReserveCny"] == "120.00"
    assert payload["expiresAt"] == "2026-08-24T09:25:00+03:00"

    payload["withdrawalAmountCny"] = "100.00"
    with pytest.raises(FundingPlanError):
        validate_advertising_deposit_plan(payload)


def test_deposit_preflight_allows_once_and_never_auto_retries_unknown() -> None:
    plan = _deposit_plan()
    approved = preflight_deposit_execution(
        plan,
        current_status="approved",
        now="2026-08-24T09:11:00+03:00",
    )
    assert approved.allowed is True
    assert approved.requires_readback is True
    assert approved.automatic_retry_allowed is False

    unknown = preflight_deposit_execution(
        plan,
        current_status="unknown",
        now="2026-08-24T09:11:00+03:00",
    )
    assert unknown.allowed is False
    assert unknown.requires_readback is True
    assert unknown.automatic_retry_allowed is False

    unrecognized = preflight_deposit_execution(
        plan,
        current_status="network_maybe_committed",
        now="2026-08-24T09:11:00+03:00",
    )
    assert unrecognized.allowed is False
    assert unrecognized.automatic_retry_allowed is False

    expired = preflight_deposit_execution(
        plan,
        current_status="approved",
        now="2026-08-24T09:25:00+03:00",
    )
    assert expired.allowed is False
    assert "expired" in expired.reason

    expired_unknown = preflight_deposit_execution(
        plan,
        current_status="unknown",
        now="2026-08-24T09:30:00+03:00",
    )
    assert expired_unknown.allowed is False
    assert expired_unknown.requires_readback is True
    assert expired_unknown.automatic_retry_allowed is False
