"""Warehouse sequencing and retained approval records; no marketplace calls."""
import argparse
import importlib.util
import json
from pathlib import Path

import pytest


STAGES = ["collect", "cancel_cleanup", "sequence_sync", "table_sync", "approve", "prepare", "wait_imei", "sync_imei", "labels", "archive"]


def module():
    path = Path(__file__).resolve().parents[1] / ".agents/skills/wb-product-workflow/scripts/workflow_state.py"
    spec = importlib.util.spec_from_file_location("wb_fulfillment_state", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def state_before(stage):
    stages = {name: {"status": "pending", "evidence": [], "blockers": []} for name in STAGES}
    for name in STAGES[:STAGES.index(stage)]:
        stages[name]["status"] = "passed"
        stages[name]["evidence"] = [f"{name}-readback.json"]
    if stages["approve"]["status"] == "passed":
        stages["approve"]["approved_by"] = "user: existing exact order-scope authorization"
    return {"flow": "fulfillment", "stages": stages}


@pytest.mark.parametrize("barrier,later", [
    ("cancel_cleanup", "sequence_sync"),
    ("sequence_sync", "table_sync"),
    ("cancel_cleanup", "table_sync"),
    ("table_sync", "prepare"),
    ("approve", "prepare"),
    ("wait_imei", "sync_imei"),
    ("wait_imei", "labels"),
    ("sync_imei", "labels"),
])
@pytest.mark.parametrize("barrier_status", ["pending", "blocked"])
@pytest.mark.parametrize("later_status", ["in_progress", "passed"])
def test_unfinished_warehouse_gate_prevents_start_or_completion(barrier, later, barrier_status, later_status):
    loaded = module()
    state = state_before(barrier)
    state["stages"][barrier]["status"] = barrier_status
    state["stages"][later]["status"] = later_status
    assert any(error.startswith(f"{later}:") for error in loaded.validate_state(state))


def test_approval_actor_is_required_before_preparation():
    loaded = module()
    state = state_before("prepare")
    del state["stages"]["approve"]["approved_by"]
    state["stages"]["prepare"]["status"] = "in_progress"
    assert "approve: approved_by is required" in loaded.validate_state(state)


def test_rejected_transition_does_not_change_saved_state(tmp_path):
    loaded = module()
    path = tmp_path / "fulfillment.json"
    path.write_text(json.dumps(state_before("table_sync")), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(SystemExit, match="prepare: cannot start"):
        loaded.command_set(argparse.Namespace(file=str(path), stage="prepare", status="in_progress",
                                             evidence=None, blocker=None, approved_by=None))
    assert path.read_bytes() == original


def test_existing_session_approval_is_retained_without_asking_again(tmp_path):
    loaded = module()
    path = tmp_path / "fulfillment.json"
    state = state_before("prepare")
    path.write_text(json.dumps(state), encoding="utf-8")
    assert loaded.command_set(argparse.Namespace(file=str(path), stage="prepare", status="in_progress",
                                                evidence=["prepare-plan.json"], blocker=None, approved_by=None)) == 0
    saved = loaded.read(path)
    assert saved["stages"]["approve"]["approved_by"] == state["stages"]["approve"]["approved_by"]
    assert loaded.validate_state(saved) == []


def test_full_fulfillment_state_is_valid():
    loaded = module()
    assert loaded.FLOWS["fulfillment"] == STAGES
    state = state_before("archive")
    state["stages"]["archive"]["status"] = "passed"
    state["stages"]["archive"]["evidence"] = ["warehouse-archive.json"]
    assert loaded.validate_state(state) == []


def test_pre_shipment_cancellation_cleanup_keeps_strict_evidence_and_archive_rules():
    text = (Path(__file__).resolve().parents[1]
            / ".agents/skills/wb-product-workflow/references/fulfillment-workflow.md").read_text(
                encoding="utf-8")
    for phrase in (
        "official same-order history proves cancellation before shipment",
        "full A:L snapshot",
        "verified tombstone",
        "post-shipment cancellation or refusal must not delete a row",
        "normally pending order deleted online still follows the restoration rule",
    ):
        assert phrase in text


def test_sequence_sync_keeps_bounded_a_column_contract():
    text = (Path(__file__).resolve().parents[1]
            / ".agents/skills/wb-product-workflow/references/fulfillment-workflow.md").read_text(
                encoding="utf-8")
    for phrase in (
        "renumber_shipping_sequences",
        "physical row 3",
        "sequence = row_number - 2",
        "only column A",
        "complete A:L state token",
        "exact store, order, physical row, old sequence and new sequence",
        "preflight the whole batch",
        "independently read back the full workbook",
        "issue no write when every sequence is already correct",
    ):
        assert phrase in text
