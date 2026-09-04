"""Offline behavior checks; fictional identities, no credentials or WB writes."""
import copy
import importlib.util
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / ".agents/skills/wb-product-workflow/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample():
    target = {"store": "fixture", "advert_id": 1, "nm_id": 2}
    return {
        "target": target,
        "snapshot": {"target": dict(target), "status": "ok", "complete": True,
                     "retrieved_at": "2026-01-01T00:00:00Z", "evidence": "fixture-read",
                     "norm_queries": ["existing term"]},
        "decisions": [{"target": dict(target), "query": "iphone 12",
                       "classification": "wrong_model", "action": "exclude",
                       "reason": "fixture target is iPhone 15", "evidence": "fixture-stats",
                       "approved": True, "approval_ref": "fixture-approval"}],
    }


def test_union_preserves_existing_exact_strings_and_is_replay_noop():
    doc = sample()
    plan = load("ad_exclusion_plan").build_plan(doc)
    assert plan["payload"]["norm_queries"] == ["existing term", "iphone 12"]
    assert plan["removed"] == []
    doc["snapshot"]["norm_queries"] = plan["payload"]["norm_queries"]
    repeat = load("ad_exclusion_plan").build_plan(doc)
    assert repeat["write_required"] is False
    assert repeat["payload"] is None


@pytest.mark.parametrize("field,value", [("status", "failed"), ("complete", False),
                                        ("norm_queries", None), ("retrieved_at", "")])
def test_missing_or_failed_source_blocks(field, value):
    doc = sample()
    doc["snapshot"][field] = value
    with pytest.raises(ValueError):
        load("ad_exclusion_plan").build_plan(doc)


@pytest.mark.parametrize("location", ["snapshot", "decision"])
@pytest.mark.parametrize("key,value", [("store", "other"), ("advert_id", 8), ("nm_id", 9)])
def test_cross_identity_blocks(location, key, value):
    doc = sample()
    row = doc["snapshot"] if location == "snapshot" else doc["decisions"][0]
    row["target"][key] = value
    with pytest.raises(ValueError):
        load("ad_exclusion_plan").build_plan(doc)


@pytest.mark.parametrize("classification", ["relevant", "broad_or_uncertain"])
def test_relevant_or_uncertain_cannot_be_excluded(classification):
    doc = sample()
    doc["decisions"][0]["classification"] = classification
    with pytest.raises(ValueError):
        load("ad_exclusion_plan").build_plan(doc)


def test_no_global_model_blacklist():
    doc = sample()
    doc["decisions"][0].update(classification="relevant", action="keep",
                                reason="fixture target is now iPhone 12")
    assert load("ad_exclusion_plan").build_plan(doc)["payload"] is None


def test_no_candidates_never_clears_existing_exclusions():
    doc = sample()
    doc["decisions"] = []
    assert load("ad_exclusion_plan").build_plan(doc)["payload"] is None


def test_empty_verified_list_can_receive_first_exclusion():
    doc = sample()
    doc["snapshot"]["norm_queries"] = []
    assert load("ad_exclusion_plan").build_plan(doc)["added"] == ["iphone 12"]


@pytest.mark.parametrize("field,value", [("approved", False), ("approval_ref", ""),
                                        ("reason", ""), ("evidence", "")])
def test_missing_approval_or_evidence_blocks(field, value):
    doc = sample()
    doc["decisions"][0][field] = value
    with pytest.raises(ValueError):
        load("ad_exclusion_plan").build_plan(doc)


def test_conflicting_decisions_block():
    doc = sample()
    doc["decisions"].append(copy.deepcopy(doc["decisions"][0]))
    doc["decisions"][1]["action"] = "keep"
    with pytest.raises(ValueError):
        load("ad_exclusion_plan").build_plan(doc)


def test_advertising_approval_and_order_gates():
    module = load("workflow_state")
    stages = {name: {"status": "pending"} for name in module.FLOWS["advertising"]}
    state = {"flow": "advertising", "stages": stages}
    stages["execute"]["status"] = "passed"
    assert module.validate_state(state)
    for name in ("preflight", "plan", "approve"):
        stages[name]["status"] = "passed"
    assert module.validate_state(state)
    stages["approve"]["approved_by"] = "fixture-user"
    assert not module.validate_state(state)
