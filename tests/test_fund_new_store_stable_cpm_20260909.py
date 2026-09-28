import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from tools import fund_new_store_stable_cpm_20260909 as funding


def execution():
    return {"planId": funding.PLAN_ID, "store": "new-store", "currency": "CNY", "totalAdditionalCny": 900,
            "approvedBy": funding.APPROVER, "fundingSource": "balance", "cashOnly": True,
            "targets": [{"advertId": 39841968, "nmID": 1452973747, "additionalBudgetCny": 300},
                        {"advertId": 42, "nmID": 1452974279, "additionalBudgetCny": 600}]}


def created():
    return {"store": "new-store", "advertId": 42, "nmID": 1452974279}


class FakeService:
    def __init__(self, out):
        self.out = out
        self.ledger = self
        self.actions = []
        self.calls = []
        self.funds_calls = 0
        self.outcome = "readback_confirmed"

    def query_funding_actions(self, **kwargs):
        return copy.deepcopy(self.actions)

    def funds_plan(self, **kwargs):
        self.funds_calls += 1
        committed = sum(float(json.loads(row["evidence_json"])["amountCny"]) for row in self.actions)
        return {"path": "synthetic-funds.json", "stores": [{"store": "new-store", "fundingAllowed": True,
                    "blockers": [], "availableCashCny": str(907.46 - committed)}]}

    def ads_deposit_plan(self, **kwargs):
        assert kwargs["use_cashback"] is False
        plan = {"store": "new-store", "currency": "CNY", "campaign": kwargs["campaign"],
                "amountCny": str(kwargs["amount_cny"]), "cashAmountCny": str(kwargs["amount_cny"]),
                "cashbackAmountCny": "0", "approvedBy": funding.APPROVER,
                "fingerprint": str(kwargs["campaign"]),
                "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()}
        path = self.out / f"synthetic-{kwargs['campaign']}.json"
        funding.save(path, plan)
        return {"status": "immutable_plan_created", "path": str(path), "plan": plan}

    def ads_deposit(self, *, plan_path, approved_by, confirm_live):
        plan = funding.load(plan_path)
        self.calls.append(confirm_live)
        rows = [r for r in self.actions if r["action_id"] == plan["fingerprint"]]
        if not confirm_live:
            row = {"action_id": plan["fingerprint"], "lifecycle_status": "proposed", "evidence_json": json.dumps(plan)}
            if not rows:
                self.actions.append(row)
            return {"status": "proposal_recorded", "actionId": row["action_id"]}
        row = rows[0]
        readback = row["lifecycle_status"] == "unknown"
        row["lifecycle_status"] = self.outcome
        return {"status": self.outcome, "actionId": row["action_id"], "readbackOnly": readback}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    funding.save(tmp_path / "execution_plan.json", execution())
    funding.save(tmp_path / "created_campaign.json", created())
    monkeypatch.setattr(funding, "validate_advertising_deposit_plan", lambda data: data)
    service = FakeService(tmp_path)
    def run(action, campaign=39841968):
        return funding.run(service, action=action, campaign=campaign, out=tmp_path,
                           identity_reader=lambda service, target: {"http": 200})
    return service, run


def test_two_targets_use_fresh_sequential_basis_and_exact_900_cash(setup):
    service, run = setup
    for target in (39841968, 42):
        assert run("prepare", target)["status"] == "prepared"
        assert run("dry-run", target)["status"] == "proposal_recorded"
        assert run("execute", target)["status"] == "readback_confirmed"
    assert service.funds_calls == 2
    assert service.calls == [False, True, False, True]
    assert sum(int(json.loads(x["evidence_json"])["amountCny"]) for x in service.actions) == 900
    for target, amount in ((39841968, 300), (42, 600)):
        journal = funding.load(service.out / f"deposit_{target}.json")
        assert journal["status"] == "readback_confirmed"
        assert int(journal["amountCny"]) == amount


def test_confirmed_execution_cannot_be_repeated_or_reprepared(setup):
    service, run = setup
    for action in ("prepare", "dry-run", "execute"):
        run(action)
    for action in ("execute", "prepare", "dry-run"):
        with pytest.raises(RuntimeError):
            run(action)
    assert run("readback")["status"] == "readback_confirmed"
    assert service.calls == [False, True]


def test_unknown_only_routes_to_original_get_reconciliation(setup):
    service, run = setup
    service.outcome = "unknown"
    for action in ("prepare", "dry-run", "execute"):
        run(action)
    with pytest.raises(RuntimeError):
        run("execute")
    assert run("readback")["readbackOnly"] is True


def test_readback_cannot_execute_proposal(setup):
    service, run = setup
    run("prepare")
    run("dry-run")
    with pytest.raises(RuntimeError):
        run("readback")
    assert service.calls == [False]


@pytest.mark.parametrize("change", ["wrong_total", "promo", "missing_id", "wrong_sku", "old_campaign", "fractional"])
def test_scope_mutations_fail_before_service(change):
    plan = execution()
    new = created()
    if change == "wrong_total":
        plan["targets"][0]["additionalBudgetCny"] = 301
    elif change == "promo":
        plan["cashOnly"] = False
    elif change == "missing_id":
        plan["targets"][1]["advertId"] = None
    elif change == "wrong_sku":
        plan["targets"][1]["nmID"] = 1452973747
    elif change == "old_campaign":
        new["advertId"] = plan["targets"][1]["advertId"] = 39841968
    else:
        plan["targets"][0]["additionalBudgetCny"] = 300.0
    with pytest.raises(RuntimeError):
        funding.validate_scope(plan, new, 39841968)


def test_old_63_cash_37_promo_never_becomes_part_of_new_batch():
    old = {"lifecycle_status": "readback_confirmed", "evidence_json": json.dumps({"approvedBy": "user-confirmed-20260908",
           "amountCny": "100.00", "cashAmountCny": "63.00", "cashbackAmountCny": "37.00"})}
    assert funding.scoped_actions([old], execution()) == []


def test_duplicate_new_batch_actions_are_rejected(setup):
    service, run = setup
    run("prepare")
    run("dry-run")
    service.actions.append(copy.deepcopy(service.actions[0]))
    with pytest.raises(RuntimeError, match="Duplicate"):
        run("execute")
    assert service.calls == [False]
