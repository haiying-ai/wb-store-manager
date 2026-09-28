import copy
import json

import pytest

from tools.reconcile_historical_mixed_deposit_20260909 import ACTION, validate_history


def evidence():
    plan = {"store": "new-store", "campaign": 39841968, "currency": "CNY", "source": "balance",
            "amountCny": "100.00", "cashAmountCny": "63.00", "cashbackAmountCny": "37.00",
            "sourceBalanceCny": "3327.00", "fingerprint": "exact", "cashbackPercent": 100,
            "cashbackExpiresAt": "2027-08-08T04:46:45.469037+00:00"}
    action = {"action_id": ACTION, "store_alias": "new-store", "lifecycle_status": "unknown",
              "evidence_json": json.dumps(plan)}
    before = {"currency": "CNY", "campaignStatus": 11, "sourceBalanceCny": "3327.00",
              "campaignBudgetBeforeCny": "0.00", "cashbacks": [{"sum": 37, "percent": 100,
              "expiration_date": plan["cashbackExpiresAt"]}]}
    events = [{"lifecycle_status": "executing", "occurred_at": "2026-09-08T02:39:32Z",
               "evidence_json": json.dumps({"planFingerprint": "exact", "livePreflight": before})},
              {"lifecycle_status": "unknown", "occurred_at": "2026-09-08T02:39:38Z",
               "evidence_json": json.dumps({"httpStatus": 200, "readback": {"status": 200,
                   "observedTotalCny": "100.00", "data": {"currency": "CNY"}}})}]
    archived = {"store": "new-store", "readOnly": True, "checkedAt": "2026-09-08T10:40:08+08:00", "calls": [
        {"label": "balance", "http": 200, "ok": True, "data": {"currency": "CNY", "balance": 0, "net": 3264}},
        {"label": "budget_39841968", "http": 200, "ok": True, "data": {"currency": "CNY", "total": 100}},
        {"label": "campaigns", "http": 200, "ok": True, "data": {"adverts": [
            {"id": 39841968, "status": 11, "nm_settings": [{"nm_id": 1452973747}]}]}}]}
    expenses = [{"advertId": 39841968, "currency": "CNY", "paymentType": "Кэшбэк",
                 "updSum": 37, "updTime": "2026-09-08T19:35:00+03:00"}]
    return [plan, action, events, archived, expenses, [action]]


def test_independent_cash_budget_and_official_cashback_prove_historical_depletion():
    result = validate_history(*evidence())
    assert result["matched"]
    assert result["fundingSourceReadback"]["cashbackObservedDirectly"] is False


@pytest.mark.parametrize("case", ["no_cashback_proof", "wrong_cash_debit", "wrong_budget",
                                  "repeated_post", "late_snapshot", "expired_bucket",
                                  "wrong_model", "concurrent_funding", "remaining_promo"])
def test_incomplete_or_ambiguous_evidence_never_confirms(case):
    args = copy.deepcopy(evidence())
    if case == "no_cashback_proof":
        args[4] = []
    elif case == "wrong_cash_debit":
        args[3]["calls"][0]["data"]["net"] = 3227
    elif case == "wrong_budget":
        args[3]["calls"][1]["data"]["total"] = 90
    elif case == "repeated_post":
        args[2].append(args[2][0])
    elif case == "late_snapshot":
        args[3]["checkedAt"] = "2026-09-08T11:40:08+08:00"
    elif case == "expired_bucket":
        args[0]["cashbackExpiresAt"] = "2026-09-07T00:00:00Z"
    elif case == "wrong_model":
        args[3]["calls"][2]["data"]["adverts"][0]["nm_settings"][0]["nm_id"] = 999
    elif case == "concurrent_funding":
        args[5].append({"action_id": "different", "lifecycle_status": "executing", "requested_at": "2026-09-08T02:39:40Z"})
    elif case == "remaining_promo":
        args[3]["calls"][0]["data"]["cashbacks"] = [{"sum": 37}]
    with pytest.raises(AssertionError):
        validate_history(*args)
