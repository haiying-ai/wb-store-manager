from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from wb_manager.finance_policy import (
    ADVERTISING_BALANCE_FIELD,
    ADVERTISING_DEPOSIT_CAP_RULE,
    ADVERTISING_FUNDING_SOURCE,
    ADVERTISING_REQUEST_TYPE,
    FinancialPolicyError,
    load_active_phone_financial_policy,
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
                "perActionCapRule": ADVERTISING_DEPOSIT_CAP_RULE,
                "requireExactReserveEquality": True,
                "requirePriorDryRunProposal": True,
                "requireLiveSellerBalanceCurrencyReadback": True,
            }
        },
        "governance": {
            "returnLogisticsMustComeFromPlatformFinance": True,
            "noReturnLogisticsFallback": True,
            "actualAdvertisingSpendReconciliationOnly": True,
            "noDoubleCountActualAdvertisingOutsideCombinedPool": True,
        },
    }


def write_policies(tmp_path: Path, reporting: dict) -> tuple[Path, Path]:
    reporting_path = tmp_path / "reporting.json"
    pricing_path = tmp_path / "pricing.json"
    reporting_path.write_text(json.dumps(reporting), encoding="utf-8")
    pricing_path.write_text(
        json.dumps({"status": "active", "policyId": "pricing-test-v1"}),
        encoding="utf-8",
    )
    return reporting_path, pricing_path


def load(tmp_path: Path, reporting: dict):
    reporting_path, pricing_path = write_policies(tmp_path, reporting)
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
    assert policy.managed_operations_amount(Decimal("100")) == Decimal("2.00")
    assert policy.combined_pool_amount(Decimal("100")) == Decimal("3.00")
    assert policy.settlement_balance_contract_enabled is False
    assert policy.settlement_balance_enabled is False
    assert policy.advertising_funding_source is None
    assert policy.advertising_balance_field is None
    assert policy.advertising_request_type is None
    assert policy.advertising_deduction_mode is None
    assert policy.withdrawal_advertising_debt_cutover is None


def test_repository_active_policy_satisfies_the_finance_contract():
    policy = load_active_phone_financial_policy()

    assert policy.reporting_policy_id == policy.raw["policyId"]
    assert policy.pricing_policy_id == policy.raw["pricingReferences"][
        "activePricingPolicyId"
    ]
    assert policy.reporting_currency == "CNY"
    assert policy.settlement_balance_contract_enabled is False


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
            ("funding", "advertisingDeposit", "perActionCapRule"),
            "self_declared_numeric_cap",
        ),
        (
            ("funding", "advertisingDeposit", "requirePriorDryRunProposal"),
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


def settlement_balance_policy() -> dict:
    policy = valid_reporting_policy()
    policy["effectiveFrom"] = "2026-09-05"
    policy["funding"]["advertisingDeposit"].update(
        {
            "fundingSource": "balance",
            "platformBalanceField": "net",
            "platformRequestType": 1,
            "deductionMode": "future_sales_settlement_netting",
            "separateWithdrawalRequired": False,
            "manualCashTopUpRequired": False,
            "actualDeductionEvidenceRequired": True,
        }
    )
    policy["funding"]["withdrawalAdvertisingDebt"] = {
        "cutoverEffectiveFrom": "2026-09-05",
        "modeFromCutover": "not_applicable_to_withdrawal",
        "preserveHistoricalRecords": True,
        "noRetroactiveClearing": True,
    }
    return policy


def test_settlement_balance_policy_is_explicit_and_complete(tmp_path):
    policy = load(tmp_path, settlement_balance_policy())

    assert policy.settlement_balance_contract_enabled is True
    assert policy.settlement_balance_enabled is True
    assert policy.advertising_funding_source == ADVERTISING_FUNDING_SOURCE
    assert policy.advertising_balance_field == ADVERTISING_BALANCE_FIELD
    assert policy.advertising_request_type == ADVERTISING_REQUEST_TYPE
    assert policy.withdrawal_advertising_debt_cutover == date(2026, 9, 5)


def test_settlement_balance_cutover_must_equal_policy_effective_from(tmp_path):
    policy = settlement_balance_policy()
    policy["effectiveFrom"] = "2026-09-04"

    with pytest.raises(FinancialPolicyError, match="must equal.*effectiveFrom"):
        load(tmp_path, policy)


def test_future_settlement_balance_policy_uses_its_own_matching_cutover(tmp_path):
    policy = settlement_balance_policy()
    policy["effectiveFrom"] = "2027-01-15"
    policy["funding"]["withdrawalAdvertisingDebt"]["cutoverEffectiveFrom"] = (
        "2027-01-15"
    )

    loaded = load(tmp_path, policy)

    assert loaded.settlement_balance_contract_enabled is True
    assert loaded.withdrawal_advertising_debt_cutover == date(2027, 1, 15)


@pytest.mark.parametrize(
    ("container", "key", "value"),
    [
        (("funding", "advertisingDeposit"), "fundingSource", "account"),
        (("funding", "advertisingDeposit"), "platformBalanceField", "balance"),
        (("funding", "advertisingDeposit"), "platformRequestType", 0),
        (("funding", "advertisingDeposit"), "deductionMode", "cash_recharge"),
        (("funding", "advertisingDeposit"), "separateWithdrawalRequired", True),
        (("funding", "advertisingDeposit"), "manualCashTopUpRequired", True),
        (
            ("funding", "advertisingDeposit"),
            "actualDeductionEvidenceRequired",
            False,
        ),
        (
            ("funding", "withdrawalAdvertisingDebt"),
            "modeFromCutover",
            "withdrawal_debt",
        ),
        (
            ("funding", "withdrawalAdvertisingDebt"),
            "preserveHistoricalRecords",
            False,
        ),
        (
            ("funding", "withdrawalAdvertisingDebt"),
            "noRetroactiveClearing",
            False,
        ),
    ],
)
def test_incomplete_settlement_balance_contract_fails_closed(
    tmp_path, container, key, value
):
    policy = settlement_balance_policy()
    target = policy
    for part in container:
        target = target[part]
    target[key] = value

    with pytest.raises(FinancialPolicyError):
        load(tmp_path, policy)


@pytest.mark.parametrize(
    ("container", "key"),
    [
        (("funding", "advertisingDeposit"), "fundingSource"),
        (("funding", "advertisingDeposit"), "deductionMode"),
        (("funding", "withdrawalAdvertisingDebt"), "cutoverEffectiveFrom"),
        (("funding",), "withdrawalAdvertisingDebt"),
    ],
)
def test_missing_modern_settlement_balance_fields_fail_closed(
    tmp_path,
    container,
    key,
):
    policy = settlement_balance_policy()
    target = policy
    for part in container:
        target = target[part]
    del target[key]

    with pytest.raises(FinancialPolicyError):
        load(tmp_path, policy)


def test_settlement_balance_request_type_rejects_boolean_alias(tmp_path):
    policy = settlement_balance_policy()
    policy["funding"]["advertisingDeposit"]["platformRequestType"] = True

    with pytest.raises(FinancialPolicyError, match="platformRequestType"):
        load(tmp_path, policy)
