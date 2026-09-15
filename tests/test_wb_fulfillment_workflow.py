"""Passive warehouse tracking state and documentation contracts."""
import argparse
import importlib.util
import json
from pathlib import Path

import pytest


STAGES = ["collect", "cancel_cleanup", "sequence_sync", "table_sync", "return_sync", "status_sync", "archive"]


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
    return {"flow": "fulfillment", "stages": stages}


@pytest.mark.parametrize("barrier,later", [
    ("cancel_cleanup", "sequence_sync"),
    ("sequence_sync", "table_sync"),
    ("cancel_cleanup", "table_sync"),
    ("table_sync", "return_sync"),
    ("return_sync", "status_sync"),
    ("status_sync", "archive"),
])
@pytest.mark.parametrize("barrier_status", ["pending", "blocked"])
@pytest.mark.parametrize("later_status", ["in_progress", "passed"])
def test_unfinished_warehouse_gate_prevents_start_or_completion(barrier, later, barrier_status, later_status):
    loaded = module()
    state = state_before(barrier)
    state["stages"][barrier]["status"] = barrier_status
    state["stages"][later]["status"] = later_status
    assert any(error.startswith(f"{later}:") for error in loaded.validate_state(state))


def test_rejected_transition_does_not_change_saved_state(tmp_path):
    loaded = module()
    path = tmp_path / "fulfillment.json"
    path.write_text(json.dumps(state_before("return_sync")), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(SystemExit, match="status_sync: cannot start"):
        loaded.command_set(argparse.Namespace(file=str(path), stage="status_sync", status="in_progress",
                                             evidence=None, blocker=None, approved_by=None))
    assert path.read_bytes() == original


def test_status_sync_can_start_after_prior_tracking_stages_pass(tmp_path):
    loaded = module()
    path = tmp_path / "fulfillment.json"
    state = state_before("status_sync")
    path.write_text(json.dumps(state), encoding="utf-8")
    assert loaded.command_set(argparse.Namespace(file=str(path), stage="status_sync", status="in_progress",
                                                evidence=["wb-readonly-status.json"], blocker=None,
                                                approved_by=None)) == 0
    saved = loaded.read(path)
    assert saved["stages"]["status_sync"]["evidence"] == ["wb-readonly-status.json"]
    assert loaded.validate_state(saved) == []


def test_full_fulfillment_state_is_valid():
    loaded = module()
    assert loaded.FLOWS["fulfillment"] == STAGES
    for legacy_stage in ("approve", "prepare", "wait_imei", "sync_imei", "labels"):
        assert legacy_stage not in loaded.FLOWS["fulfillment"]
    state = state_before("archive")
    state["stages"]["archive"]["status"] = "passed"
    state["stages"]["archive"]["evidence"] = ["warehouse-archive.json"]
    assert loaded.validate_state(state) == []


def test_passive_tracking_contract_forbids_wb_fulfillment_writes_and_mirrors_status_to_wps():
    text = (Path(__file__).resolve().parents[1]
            / ".agents/skills/wb-product-workflow/references/fulfillment-workflow.md").read_text(
                encoding="utf-8")
    for phrase in (
        "do not create or select a supply for it",
        "add it to a supply",
        "change its WB status or metadata",
        "submit IMEI",
        "request and fill an order label or supply QR code",
        "retrieve fresh same-store/same-order WB evidence in read-only mode",
        "update only the configured WPS status field",
        "independent readback",
    ):
        assert phrase in text


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


def test_return_lifecycle_is_separate_from_seller_pickup_eligibility():
    text = (Path(__file__).resolve().parents[1]
            / ".agents/skills/wb-product-workflow/references/fulfillment-workflow.md").read_text(
                encoding="utf-8")
    for phrase in (
        "one unique definitive record with complete same-store and same-order identity",
        "set column L to 退货 even while the parcel is still travelling back",
        "Do not add that record to the return worksheet until every seller-pickup gate",
        "must not be treated as proof that no return exists",
    ):
        assert phrase in text
