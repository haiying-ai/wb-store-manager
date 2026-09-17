"""Synthetic finance policy inputs; never read an installation's private policy."""
from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def synthetic_agency_fee_policy(tmp_path, monkeypatch):
    from wb_manager import finance_running_adapter as adapter

    policy_path = tmp_path / "synthetic_agency_withdrawal_fee_policy.json"
    policy_path.write_text(json.dumps({
        "schemaVersion": 1,
        "policyId": "synthetic-confirmed-withdrawal-fee-v1",
        "status": "active",
        "rate": "0.02",
        "basis": "confirmed_withdrawal_bank_credit_cny",
        "rounding": "ROUND_HALF_UP_PER_WITHDRAWAL_TO_CENT",
        "restatementScope": "all_available_historical_report_periods",
    }), encoding="utf-8")
    # An absolute path also isolates the package-relative fallback. Individual
    # policy-validation tests can override it with their own explicit input.
    monkeypatch.setattr(adapter, "AGENCY_FEE_POLICY_PATH", policy_path)
    return policy_path
