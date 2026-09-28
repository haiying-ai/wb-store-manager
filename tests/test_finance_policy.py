from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from wb_manager.finance_policy import (
    ADVERTISING_BALANCE_FIELD,
    ADVERTISING_DEDUCTION_MODE,
    ADVERTISING_DEPOSIT_CAP_RULE,
    ADVERTISING_FUNDING_SOURCE,
    ADVERTISING_REQUEST_TYPE,
    FinancialPolicyError,
    SEPARATE_RETURN_AND_PROMOTION,
    load_active_phone_financial_policy,
    load_phone_financial_policy_timeline,
)


def valid_reporting_policy() -> dict:
    return {
        "schemaVersion": 2,
        "policyId": "reporting-test-v1",
        "status": "active",
        "effectiveFrom": "2026-01-01",
        "scope": {"reportingCurrency": "CNY"},
        "pricingReferences": {
            "activePricingPolicyId": "pricing-test-v1",
            "reportingPolicyId": "reporting-test-v1",
        },
        "costs": {
            "managedOperations": {"rate": 0.02},
            "advertisingAndReturns": {
                "combinedPoolRate": 0.03,
                "actualReturnLogisticsSource": "platform finance transaction rows",
            },
        },
        "formulas": {
            "ownerProfitCny": "revenueCny - combinedAdvertisingAndReturnPoolCny"
        },
        "funding": {
            "advertisingDeposit": {
                "fundingSource": ADVERTISING_FUNDING_SOURCE,
                "platformBalanceField": ADVERTISING_BALANCE_FIELD,
                "platformRequestType": ADVERTISING_REQUEST_TYPE,
                "separateWithdrawalRequired": False,
                "manualCashTopUpRequired": False,
                "deductionMode": ADVERTISING_DEDUCTION_MODE,
                "actualDeductionEvidenceRequired": True,
                "perActionCapRule": ADVERTISING_DEPOSIT_CAP_RULE,
                "requireExactReserveEquality": True,
                "requirePriorDryRunProposal": True,
                "requireLiveSellerBalanceCurrencyReadback": True,
            },
            "withdrawalAdvertisingDebt": {
                "cutoverEffectiveFrom": "2026-01-01",
                "modeFromCutover": "not_applicable_to_withdrawal",
                "preserveHistoricalRecords": True,
                "noRetroactiveClearing": True,
            },
        },
        "governance": {
            "returnLogisticsMustComeFromPlatformFinance": True,
            "noReturnLogisticsFallback": True,
            "actualAdvertisingSpendReconciliationOnly": True,
            "noDoubleCountActualAdvertisingOutsideCombinedPool": True,
            "platformAdvertisingSettlementDeductionReconciliationOnly": True,
            "noNewWithdrawalLinkedAdvertisingDebtAfterCutover": True,
        },
    }


def write_policies(
    tmp_path: Path, reporting: dict, pricing: dict | None = None
) -> tuple[Path, Path]:
    reporting_path = tmp_path / "reporting.json"
    pricing_path = tmp_path / "pricing.json"
    reporting_path.write_text(json.dumps(reporting), encoding="utf-8")
    pricing_path.write_text(
        json.dumps(
            pricing
            if pricing is not None
            else {"status": "active", "policyId": "pricing-test-v1"}
        ),
        encoding="utf-8",
    )
    return reporting_path, pricing_path


def load(tmp_path: Path, reporting: dict, pricing: dict | None = None):
    reporting_path, pricing_path = write_policies(tmp_path, reporting, pricing)
    return load_active_phone_financial_policy(
        reporting_path, pricing_policy_path=pricing_path
    )


def test_valid_policy_exposes_decimal_contract(tmp_path):
    policy = load(tmp_path, valid_reporting_policy())

    assert policy.reporting_policy_id == "reporting-test-v1"
    assert policy.pricing_policy_id == "pricing-test-v1"
    assert policy.reporting_currency == "CNY"
    assert policy.managed_operations_rate == Decimal("0.02")
    assert policy.combined_advertising_return_pool_rate == Decimal("0.03")
    assert policy.advertising_deposit_cap_rule == ADVERTISING_DEPOSIT_CAP_RULE
    assert policy.advertising_funding_source == "balance"
    assert policy.advertising_balance_field == "net"
    assert policy.advertising_request_type == 1
    assert policy.advertising_deduction_mode == "future_sales_settlement_netting"
    assert policy.managed_operations_amount(Decimal("100")) == Decimal("2.00")
    assert policy.combined_pool_amount(Decimal("100")) == Decimal("3.00")


def test_repository_active_policy_satisfies_the_finance_contract():
    policy = load_active_phone_financial_policy()

    assert policy.reporting_policy_id == policy.raw["policyId"]
    assert policy.pricing_policy_id == policy.raw["pricingReferences"][
        "activePricingPolicyId"
    ]
    assert policy.reporting_currency == "CNY"


def test_repository_policy_timeline_uses_exact_readback_boundary():
    timeline = load_phone_financial_policy_timeline()

    # Preserve these historical boundaries even after a new policy is activated.
    historical_separate = timeline.select("2026-09-06T13:08:38+03:00")
    assert historical_separate.return_risk_reserve_rate == Decimal("0.02")
    assert historical_separate.promotion_reserve_rate == Decimal("0.03")
    assert historical_separate.total_reserve_rate == Decimal("0.05")
    assert timeline.select(
        "2026-09-06T13:08:37+03:00"
    ).reporting_policy_id.endswith("20260905-v6")
    assert timeline.select(
        "2026-09-06T13:08:38+03:00"
    ).reporting_policy_id.endswith("20260906-v7")
    assert (
        timeline.active_policy.raw["funding"]["withdrawalAdvertisingDebt"][
            "cutoverEffectiveFrom"
        ]
        == "2026-09-05"
    )


def separate_policy_pair(promotion: str = "0.03") -> tuple[dict, dict]:
    reporting = valid_reporting_policy()
    reserve = reporting["costs"]["advertisingAndReturns"]
    reserve.pop("combinedPoolRate")
    reserve.update(
        reserveAllocationMode=SEPARATE_RETURN_AND_PROMOTION,
        returnRiskReserveRate="0.02",
        promotionReserveRate=promotion,
        totalReserveRate=str(Decimal("0.02") + Decimal(promotion)),
    )
    pricing = {
        "status": "active",
        "policyId": "pricing-test-v1",
        "variableRates": {
            "returnRiskReserve": "0.02",
            "advertisingReserve": promotion,
        },
    }
    return reporting, pricing


@pytest.mark.parametrize("promotion", ["0.03", "0.02", "0"])
def test_separate_reserves_follow_referenced_pricing_policy(tmp_path, promotion):
    reporting, pricing = separate_policy_pair(promotion)
    policy = load(tmp_path, reporting, pricing)

    assert policy.return_risk_reserve_rate == Decimal("0.02")
    assert policy.promotion_reserve_rate == Decimal(promotion)
    assert policy.total_reserve_rate == Decimal("0.02") + Decimal(promotion)
    assert policy.promotion_reserve_amount(Decimal("100")) == Decimal(promotion) * 100


@pytest.mark.parametrize(
    "return_rate,promotion_rate", [("0.01", "0.03"), ("0.02", "0.03")]
)
def test_separate_components_must_match_not_just_total(
    tmp_path, return_rate, promotion_rate
):
    reporting, pricing = separate_policy_pair("0.02")
    pricing["variableRates"].update(
        returnRiskReserve=return_rate, advertisingReserve=promotion_rate
    )
    with pytest.raises(FinancialPolicyError, match="referenced pricing policy"):
        load(tmp_path, reporting, pricing)


def test_separate_reserve_total_must_equal_components(tmp_path):
    reporting, pricing = separate_policy_pair("0.02")
    reporting["costs"]["advertisingAndReturns"]["totalReserveRate"] = "0.05"
    with pytest.raises(FinancialPolicyError, match="add to totalReserveRate"):
        load(tmp_path, reporting, pricing)


@pytest.mark.parametrize(
    "source,key",
    [
        ("reporting", "returnRiskReserveRate"),
        ("reporting", "promotionReserveRate"),
        ("reporting", "totalReserveRate"),
        ("pricing", "returnRiskReserve"),
        ("pricing", "advertisingReserve"),
    ],
)
@pytest.mark.parametrize("value", [None, "NaN", "Infinity", "-0.01", "1", True])
def test_separate_rates_are_valid_finite_nonnegative_rates(
    tmp_path, source, key, value
):
    reporting, pricing = separate_policy_pair("0.02")
    target = (
        reporting["costs"]["advertisingAndReturns"]
        if source == "reporting"
        else pricing["variableRates"]
    )
    target[key] = value
    with pytest.raises(FinancialPolicyError):
        load(tmp_path, reporting, pricing)


def test_separate_rates_require_referenced_pricing_components(tmp_path):
    reporting, pricing = separate_policy_pair("0.02")
    pricing.pop("variableRates")
    with pytest.raises(FinancialPolicyError, match="pricing.variableRates"):
        load(tmp_path, reporting, pricing)


def test_new_promotion_rate_does_not_rewrite_historical_orders(tmp_path):
    old_reporting, _ = separate_policy_pair("0.03")
    new_reporting, new_pricing = separate_policy_pair("0.02")
    boundary = "2026-09-15T14:00:00+03:00"
    old_reporting.update(status="historical", effectiveTo=boundary)
    new_reporting.update(
        policyId="reporting-test-v2",
        effectiveFrom=boundary,
        supersedes=old_reporting["policyId"],
    )
    new_reporting["pricingReferences"]["reportingPolicyId"] = "reporting-test-v2"
    new_reporting["pricingReferences"]["activePricingPolicyId"] = "pricing-test-v2"
    new_pricing["policyId"] = "pricing-test-v2"
    reporting_path, pricing_path = write_policies(tmp_path, new_reporting, new_pricing)
    archive = tmp_path / "policies"
    archive.mkdir()
    (archive / "reporting-test-v1.json").write_text(
        json.dumps(old_reporting), encoding="utf-8"
    )

    timeline = load_phone_financial_policy_timeline(
        reporting_path, pricing_policy_path=pricing_path
    )
    old_order = timeline.select("2026-09-15T13:59:59+03:00")
    new_order = timeline.select(boundary)
    assert old_order.reporting_policy_id == "reporting-test-v1"
    assert old_order.promotion_reserve_rate == Decimal("0.03")
    assert old_order.total_reserve_rate == Decimal("0.05")
    assert new_order.reporting_policy_id == "reporting-test-v2"
    assert new_order.promotion_reserve_rate == Decimal("0.02")
    assert new_order.total_reserve_rate == Decimal("0.04")
    assert old_order.promotion_reserve_amount(Decimal("100")) == Decimal("3")
    assert new_order.promotion_reserve_amount(Decimal("100")) == Decimal("2")
    assert json.loads((archive / "reporting-test-v1.json").read_text("utf-8")) == old_reporting


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("status",), "paused"),
        (("scope", "reportingCurrency"), "RUB"),
        (("costs", "managedOperations", "rate"), 0.021),
        (("costs", "advertisingAndReturns", "combinedPoolRate"), 0.04),
        (
            ("governance", "returnLogisticsMustComeFromPlatformFinance"),
            False,
        ),
        (("governance", "noReturnLogisticsFallback"), False),
        (("governance", "actualAdvertisingSpendReconciliationOnly"), False),
        (
            ("governance", "noDoubleCountActualAdvertisingOutsideCombinedPool"),
            False,
        ),
        (
            (
                "governance",
                "platformAdvertisingSettlementDeductionReconciliationOnly",
            ),
            False,
        ),
        (
            ("funding", "advertisingDeposit", "perActionCapRule"),
            "self_declared_numeric_cap",
        ),
        (
            ("funding", "advertisingDeposit", "requirePriorDryRunProposal"),
            False,
        ),
        (("funding", "advertisingDeposit", "fundingSource"), "account"),
        (("funding", "advertisingDeposit", "platformRequestType"), 0),
        (("funding", "advertisingDeposit", "separateWithdrawalRequired"), True),
        (
            ("funding", "withdrawalAdvertisingDebt", "noRetroactiveClearing"),
            False,
        ),
    ],
)
def test_unsafe_policy_values_fail_closed(tmp_path, path, value):
    policy = deepcopy(valid_reporting_policy())
    target = policy
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(FinancialPolicyError):
        load(tmp_path, policy)


def test_actual_return_logistics_requires_platform_finance_rows(tmp_path):
    policy = valid_reporting_policy()
    policy["costs"]["advertisingAndReturns"]["actualReturnLogisticsSource"] = (
        "manual estimate"
    )

    with pytest.raises(FinancialPolicyError, match="platform finance"):
        load(tmp_path, policy)


@pytest.mark.parametrize(
    ("container", "key", "value"),
    [
        (
            ("costs", "advertisingAndReturns"),
            "actualReturnLogisticsPerUnitCny",
            8,
        ),
        (
            ("governance",),
            "returnLogisticsFallbackCnyPerReturnedUnit",
            16,
        ),
        (
            ("formulas",),
            "returnLogisticsCny",
            "8 * returnedUnitCount",
        ),
    ],
)
def test_fixed_eight_or_sixteen_return_fallback_is_forbidden(
    tmp_path, container, key, value
):
    policy = deepcopy(valid_reporting_policy())
    target = policy
    for part in container:
        target = target[part]
    target[key] = value

    with pytest.raises(FinancialPolicyError, match="[Ff]ixed|forbidden"):
        load(tmp_path, policy)


def test_actual_advertising_cannot_be_deducted_again(tmp_path):
    policy = valid_reporting_policy()
    policy["formulas"]["ownerProfitCny"] += " - actualAdvertisingSpendCny"

    with pytest.raises(FinancialPolicyError, match="reconciliation-only"):
        load(tmp_path, policy)


def test_pricing_and_reporting_policy_ids_must_match(tmp_path):
    policy = valid_reporting_policy()
    policy["pricingReferences"]["activePricingPolicyId"] = "wrong-pricing-id"

    with pytest.raises(FinancialPolicyError, match="active pricing policy"):
        load(tmp_path, policy)

    policy = valid_reporting_policy()
    policy["pricingReferences"]["reportingPolicyId"] = "wrong-reporting-id"
    with pytest.raises(FinancialPolicyError, match="reportingPolicyId"):
        load(tmp_path, policy)
