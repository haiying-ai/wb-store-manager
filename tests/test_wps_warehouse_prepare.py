"""Offline preparation idempotency and readback gates; synthetic orders only."""
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from tools.wps_warehouse import ledger, prepare

OID = "7123456789"
SID = "WB-GI-test-one"
ORDER = {"storeAlias": "new-store", "orderId": OID, "warehouseActionRequired": True,
         "supplierStatus": "new", "wbStatus": "waiting"}


class Runner:
    def __init__(self, path):
        self.run_id = "prepare-test"
        self.ledger = ledger.Ledger(path)
        self.ledger.lock(self.run_id)
        self.events, self.saved, self.prechecks = [], [], 0
        self.fail_at = None

    def save(self, kind, value):
        self.saved.append((kind, copy.deepcopy(value)))

    def precheck(self, current, collector):
        self.events.append("table_readback")
        self.prechecks += 1
        if self.fail_at == self.prechecks:
            raise ValueError("table changed")
        return current


class WB:
    def __init__(self, runner):
        self.runner = runner
        self.calls, self.supplies, self.members = [], {}, []
        self.supplier, self.wb_status, self.mode = "new", "waiting", "success"
        self.source = {"id": int(OID), "deliveryType": "fbs", "warehouseId": 903111, "supplyId": None}
        self.closed, self.other_member, self.extra_page = False, False, False
        self.fail_detail_once = False
        self.status_sequence = []

    def status(self, oid):
        self.runner.events.append("status")
        if self.status_sequence:
            self.supplier = self.status_sequence.pop(0)
        return {"id": int(oid), "supplierStatus": self.supplier, "wbStatus": self.wb_status}

    def request(self, method, endpoint, body=None):
        self.calls.append((method, endpoint, copy.deepcopy(body)))
        self.runner.events.append(method + " " + endpoint)
        if method == "GET" and endpoint == prepare.NEW_ORDERS:
            return 200, {"orders": [copy.deepcopy(self.source)]}
        if method == "POST":
            # A pre-write journal must already exist even when the response is lost.
            record = prepare.journal(self.runner, "new-store", OID)
            assert record["state"] == "create_started"
            assert record["name"] == body["name"]
            if self.mode == "create_rejected":
                return 403, {"code": "denied"}
            if self.mode != "create_timeout_before":
                self.supplies[SID] = {"id": SID, "name": body["name"], "createdAt": ledger.now(),
                    "done": self.closed, "closedAt": ledger.now() if self.closed else None,
                    "scanDt": None, "shippingDt": "2099-01-01T00:00:00Z"}
                self.members = [int(OID) + 1] if self.other_member else []
                if self.mode == "cancel_after_create":
                    self.supplier = "cancel"
            if self.mode.startswith("create_timeout"):
                raise TimeoutError("synthetic response loss")
            return 201, {"id": SID}
        if method == "PATCH":
            record = prepare.journal(self.runner, "new-store", OID)
            assert record["state"] == "add_started" and record["supply_id"] == SID
            assert body == {"orders": [int(OID)]}
            if self.mode == "add_rejected":
                return 409, {"code": "rejected"}
            if self.mode != "add_timeout_before":
                self.members, self.supplier = [int(OID)], "confirm"
            if self.mode.startswith("add_timeout"):
                raise TimeoutError("synthetic response loss")
            return 204, None
        if endpoint.startswith(prepare.SUPPLIES + "?"):
            if endpoint.endswith("next=0"):
                return 200, {"next": 11 if self.extra_page else 0, "supplies": list(self.supplies.values())}
            return 200, {"next": 0, "supplies": []}
        if endpoint.endswith("/order-ids"):
            return 200, {"orderIds": self.members[:]}
        if endpoint == prepare.SUPPLIES + "/" + SID:
            if self.fail_detail_once:
                self.fail_detail_once = False
                raise TimeoutError("detail unavailable")
            return 200, copy.deepcopy(self.supplies[SID])
        raise AssertionError("unexpected route " + endpoint)

    def count(self, method):
        return sum(call[0] == method for call in self.calls)


@pytest.fixture
def context(tmp_path):
    runner = Runner(tmp_path / "ledger")
    wb = WB(runner)
    current = {"snapshot": {"captured_at": ledger.now(), "complete": True, "sheets": [{"name": "2026年9月发货", "rows": [
        {"row_number": 3, "values": ["1", "2026/9/4", "Скиди Выбор（扬州沫德）", OID, "Phone", "128GB", "黑色", "", "", "", "待仓库处理"]}
    ]}]}, "state_token": "synthetic-stable-table"}
    collector = {"run_id": runner.run_id, "collected_at": ledger.now(), "all_sources_ok": True, "orders": [copy.deepcopy(ORDER)]}
    yield runner, wb, current, collector
    runner.ledger.db.close()


def execute(context):
    runner, wb, current, collector = context
    summary = {}
    returned = prepare.process(runner, current, collector, summary, transport_factory=lambda r, a: wb)
    assert returned is current
    return summary["preparation"][0]


def test_missing_table_order_never_even_initializes_writable_client(context):
    runner, wb, current, collector = context
    current["snapshot"]["sheets"][0]["rows"] = []
    factory = Mock(side_effect=AssertionError("must not connect"))
    summary = {}
    prepare.process(runner, current, collector, summary, transport_factory=factory)
    assert summary["preparation"][0]["stage"] == "NEEDS_REVIEW"
    factory.assert_not_called()
    assert wb.calls == []


def test_new_order_reads_table_before_creating_then_reads_membership_and_confirm(context):
    runner, wb, _, collector = context
    result = execute(context)
    assert result["stage"] == "PREPARED"
    assert wb.count("POST") == wb.count("PATCH") == 1
    assert runner.events.index("table_readback") < runner.events.index("POST " + prepare.SUPPLIES)
    patch_at = next(i for i, value in enumerate(runner.events) if value.startswith("PATCH "))
    assert "table_readback" in runner.events[:patch_at]
    assert any(value.endswith("/order-ids") for value in runner.events[patch_at + 1:])
    assert "status" in runner.events[patch_at + 1:]
    assert collector["orders"][0]["supplierStatus"] == "confirm"
    assert collector["orders"][0]["supplyId"] == SID
    stored = json.loads(runner.ledger.state("new-store", OID)["platform"])
    assert stored["supplierStatus"] == "confirm" and stored["supplyId"] == SID


def test_repeated_run_does_not_create_or_add_again(context):
    assert execute(context)["stage"] == "PREPARED"
    assert execute(context)["stage"] == "ALREADY_PREPARED"
    assert context[1].count("POST") == context[1].count("PATCH") == 1


def test_unknown_create_recovers_only_after_exhausting_pages(context):
    runner, wb, _, _ = context
    wb.mode, wb.extra_page = "create_timeout_after", True
    assert execute(context)["stage"] == "PREPARED"
    assert wb.count("POST") == 1
    end_page = runner.events.index("GET " + prepare.SUPPLIES + "?limit=1000&next=11")
    patch_at = next(i for i, value in enumerate(runner.events) if value.startswith("PATCH "))
    assert end_page < patch_at


@pytest.mark.parametrize("mode,stage", [("create_timeout_before", "create_uncertain"), ("add_timeout_before", "add_uncertain")])
def test_unresolved_write_is_not_blindly_resent_next_run(context, mode, stage):
    runner, wb, _, _ = context
    wb.mode = mode
    assert execute(context)["stage"] == "NEEDS_REVIEW"
    writes = wb.count("POST"), wb.count("PATCH")
    assert prepare.journal(runner, "new-store", OID)["state"] == stage
    assert execute(context)["stage"] == "NEEDS_REVIEW"
    assert (wb.count("POST"), wb.count("PATCH")) == writes


def test_lost_add_response_is_verified_by_reads_without_another_patch(context):
    context[1].mode = "add_timeout_after"
    assert execute(context)["stage"] == "PREPARED"
    assert execute(context)["stage"] == "ALREADY_PREPARED"
    assert context[1].count("PATCH") == 1


def test_known_supply_id_survives_detail_read_failure(context):
    runner, wb, _, _ = context
    wb.fail_detail_once = True
    assert execute(context)["stage"] == "NEEDS_REVIEW"
    assert prepare.journal(runner, "new-store", OID)["supply_id"] == SID
    assert execute(context)["stage"] == "PREPARED"
    assert wb.count("POST") == 1


@pytest.mark.parametrize("change", ["closed", "other_member"])
def test_closed_or_mixed_supply_never_gets_an_order(context, change):
    setattr(context[1], change, True)
    assert execute(context)["stage"] == "NEEDS_REVIEW"
    assert context[1].count("PATCH") == 0


def test_cancelled_after_create_does_not_receive_patch(context):
    context[1].mode = "cancel_after_create"
    assert execute(context)["stage"] == "NEEDS_REVIEW"
    assert context[1].count("POST") == 1 and context[1].count("PATCH") == 0


@pytest.mark.parametrize("fail_at,creates", [(1, 0), (2, 1)])
def test_changed_table_precheck_blocks_the_next_write(context, fail_at, creates):
    context[0].fail_at = fail_at
    assert execute(context)["stage"] == "NEEDS_REVIEW"
    assert context[1].count("POST") == creates and context[1].count("PATCH") == 0


@pytest.mark.parametrize("field,value", [("deliveryType", "dbs"), ("warehouseId", None), ("supplyId", "WB-GI-existing")])
def test_no_creation_for_non_fbs_unknown_warehouse_or_already_assigned_order(context, field, value):
    context[1].source[field] = value
    assert execute(context)["stage"] == "NEEDS_REVIEW"
    assert context[1].count("POST") == context[1].count("PATCH") == 0


@pytest.mark.parametrize("status,expected", [("confirm", "ALREADY_PREPARED"), ("complete", "NOT_NEW"), ("cancel", "NOT_NEW")])
def test_existing_orders_are_not_reset_or_prepared_again(context, status, expected):
    context[1].supplier = status
    assert execute(context)["stage"] == expected
    assert context[1].count("POST") == context[1].count("PATCH") == 0
    assert context[3]["orders"][0]["warehouseActionRequired"] is (status != "cancel")


def test_later_confirmation_recovers_unknown_add_without_resubmitting(context):
    _, wb, _, _ = context
    wb.mode = "add_timeout_before"
    assert execute(context)["stage"] == "NEEDS_REVIEW"
    wb.members, wb.supplier = [int(OID)], "confirm"
    assert execute(context)["stage"] == "ALREADY_PREPARED"
    assert wb.count("POST") == wb.count("PATCH") == 1


@pytest.mark.parametrize("mode", ["create_rejected", "add_rejected"])
def test_explicit_rejection_is_journaled_without_infinite_retry(context, mode):
    context[1].mode = mode
    assert execute(context)["stage"] == "NEEDS_REVIEW"
    writes = context[1].count("POST"), context[1].count("PATCH")
    assert execute(context)["stage"] == "NEEDS_REVIEW"
    assert writes == (context[1].count("POST"), context[1].count("PATCH"))


def test_unknown_create_with_duplicate_names_never_adopts_one_arbitrarily(context):
    runner, wb, _, _ = context
    wb.mode = "create_timeout_before"
    assert execute(context)["stage"] == "NEEDS_REVIEW"
    record = prepare.journal(runner, "new-store", OID)
    for sid in [SID, "WB-GI-duplicate"]:
        wb.supplies[sid] = {"id": sid, "name": record["name"], "createdAt": ledger.now(), "done": False}
    assert execute(context)["stage"] == "NEEDS_REVIEW"
    assert wb.count("POST") == 1 and wb.count("PATCH") == 0


@pytest.mark.parametrize("method,path,body", [
    ("PATCH", "/api/v3/supplies/WB-GI-x/deliver", None),
    ("DELETE", "/api/v3/supplies/WB-GI-x", None),
    ("PATCH", "/api/v3/orders/123/cancel", None),
    ("PATCH", "/api/marketplace/v3/supplies/WB-GI-x/orders", {"orders": [1, 2]}),
    ("PATCH", "/api/marketplace/v3/supplies/WB-GI-x/orders", {"orders": [True]}),
    ("GET", "/api/v3/supplies/WB-GI-x/order-ids", None),
])
def test_exact_route_allowlist_denies_dispatch_delete_cancel_and_batch_add(method, path, body):
    client = object.__new__(prepare.Transport)
    with patch.object(prepare.requests, "request") as network:
        with pytest.raises(ValueError, match="范围外"):
            client.request(method, prepare.MARKET + path, body)
        network.assert_not_called()


def test_transport_uses_tls_defaults_no_redirects_no_retries_and_redacts_secret(context):
    runner = context[0]
    client = object.__new__(prepare.Transport)
    client.runner, client.alias = runner, "new-store"
    client.identity = SimpleNamespace(token="synthetic-secret", last_call=0)
    response = SimpleNamespace(status_code=200, text='{"echo":"synthetic-secret"}')
    with patch.object(prepare.requests, "request", return_value=response) as network:
        code, data = client.request("GET", prepare.NEW_ORDERS)
    assert network.call_count == 1 and code == 200
    assert network.call_args.kwargs["allow_redirects"] is False
    assert network.call_args.kwargs.get("verify", True) is True
    assert data == {"echo": "[REDACTED]"}
    assert "synthetic-secret" not in json.dumps(runner.saved)


def mark_current_collection(context, supplier="confirm"):
    """Synthetic evidence fields actually emitted by collect.collect_store."""
    runner, wb, current, collector = context
    stamp = ledger.now()
    collector.update(collected_at=stamp, store_results=[
        {"store_alias": alias, "ok": True, "blockers": []} for alias in prepare.imei_sync.SELLERS])
    current["snapshot"]["captured_at"] = stamp
    collector["orders"][0].update(
        supplierStatus=supplier, wbStatus="waiting", collectedAt=stamp, statusCheckedAt=stamp,
        sellerId=prepare.imei_sync.SELLERS["new-store"], statusBlockers=[],
        supplyId=SID, supplyCheckedAt=stamp, supplyDone=False, supplyClosedAt=None, supplyScanDt=None)
    wb.supplier = supplier
    return collector["orders"][0]


def test_current_confirmed_order_without_imei_needs_no_prepare_transport(context):
    runner, _, current, collector = context
    mark_current_collection(context)
    assert current["snapshot"]["sheets"][0]["rows"][0]["values"][7] == ""
    factory, summary = Mock(side_effect=AssertionError("no prepare request expected")), {}
    with patch.object(prepare, "record_prepared", side_effect=AssertionError("no synthetic receipt")):
        prepare.process(runner, current, collector, summary, transport_factory=factory)
    factory.assert_not_called()
    result = summary["preparation"][0]
    assert result["stage"] == "ALREADY_PREPARED" and result["reused_collection"] is True
    assert result["preparation_write_performed"] is False
    assert prepare.journal(runner, "new-store", OID) is None
    assert runner.events == []


def test_current_confirmed_prepared_order_preserves_journal_and_archived_order(context):
    runner, wb, current, collector = context
    assert execute(context)["stage"] == "PREPARED"
    mark_current_collection(context)
    record = prepare.journal(runner, "new-store", OID)
    stored = runner.ledger.state("new-store", OID)["platform"]
    factory, summary = Mock(side_effect=AssertionError("no prepare request expected")), {}
    with patch.object(prepare, "record_prepared", side_effect=AssertionError("must not re-ingest")):
        prepare.process(runner, current, collector, summary, transport_factory=factory)
    factory.assert_not_called()
    assert summary["preparation"][0]["stage"] == "ALREADY_PREPARED"
    assert prepare.journal(runner, "new-store", OID) == record
    assert runner.ledger.state("new-store", OID)["platform"] == stored
    assert wb.count("POST") == wb.count("PATCH") == 1


def test_current_complete_order_does_not_repeat_supply_reads(context):
    runner, _, current, collector = context
    order = mark_current_collection(context, supplier="complete")
    order.update(supplyDone=True, supplyClosedAt=ledger.now())
    factory, summary = Mock(side_effect=AssertionError("no prepare request expected")), {}
    prepare.process(runner, current, collector, summary, transport_factory=factory)
    factory.assert_not_called()
    assert summary["preparation"][0]["stage"] == "NOT_NEW"
    assert summary["preparation"][0]["reused_collection"] is True


@pytest.mark.parametrize("invalid", [
    "wrong_run", "stale_run", "stale_order", "different_round_status", "missing_status_time",
    "missing_order_time", "wrong_seller", "missing_store", "failed_store", "duplicate_store",
    "not_complete", "missing_supply_time", "old_supply_time", "closed_supply", "scanned_supply",
    "missing_scan_field", "cached_terminal", "status_blocker", "short_runner_window",
])
def test_incomplete_or_old_collection_falls_back_to_original_transport(context, invalid):
    runner, wb, current, collector = context
    order = mark_current_collection(context)
    old = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    if invalid == "wrong_run": collector["run_id"] = "another-run"
    elif invalid == "stale_run":
        collector["collected_at"] = order["collectedAt"] = order["statusCheckedAt"] = old
    elif invalid == "stale_order": order["statusCheckedAt"] = old
    elif invalid == "different_round_status":
        order["statusCheckedAt"] = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    elif invalid == "missing_status_time": del order["statusCheckedAt"]
    elif invalid == "missing_order_time": del order["collectedAt"]
    elif invalid == "wrong_seller": order["sellerId"] = prepare.imei_sync.SELLERS["owner-store"]
    elif invalid == "missing_store": collector["store_results"].pop()
    elif invalid == "failed_store": collector["store_results"][0]["ok"] = False
    elif invalid == "duplicate_store": collector["store_results"][0] = collector["store_results"][1]
    elif invalid == "not_complete": collector["all_sources_ok"] = False
    elif invalid == "missing_supply_time": del order["supplyCheckedAt"]
    elif invalid == "old_supply_time": order["supplyCheckedAt"] = old
    elif invalid == "closed_supply": order["supplyDone"] = True
    elif invalid == "scanned_supply": order["supplyScanDt"] = ledger.now()
    elif invalid == "missing_scan_field": del order["supplyScanDt"]
    elif invalid == "cached_terminal": order["statusIsCachedTerminal"] = True
    elif invalid == "status_blocker": order["statusBlockers"] = ["supply_receipt_unverified"]
    elif invalid == "short_runner_window":
        runner.max_age_seconds = 1
        recent = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        collector["collected_at"] = order["collectedAt"] = order["statusCheckedAt"] = order["supplyCheckedAt"] = recent
    factory, summary = Mock(return_value=wb), {}
    prepare.process(runner, current, collector, summary, transport_factory=factory)
    factory.assert_called_once_with(runner, "new-store")
    assert "status" in runner.events
    assert summary["preparation"][0].get("reused_collection") is not True


def test_different_supply_from_prepared_receipt_falls_back_to_member_verification(context):
    runner, wb, current, collector = context
    assert execute(context)["stage"] == "PREPARED"
    mark_current_collection(context)["supplyId"] = "WB-GI-different"
    before = len(wb.calls)
    factory, summary = Mock(return_value=wb), {}
    prepare.process(runner, current, collector, summary, transport_factory=factory)
    factory.assert_called_once_with(runner, "new-store")
    assert any(call[1].endswith("/order-ids") for call in wb.calls[before:])
    assert summary["preparation"][0].get("reused_collection") is not True


@pytest.mark.parametrize("state", ["planned", "create_started", "create_uncertain", "created", "add_started", "add_uncertain", "create_rejected", "add_rejected"])
def test_unresolved_or_rejected_preparation_is_never_skipped_using_collector(context, state):
    runner, wb, current, collector = context
    assert execute(context)["stage"] == "PREPARED"
    record = prepare.journal(runner, "new-store", OID)
    record["state"] = state
    prepare.journal(runner, "new-store", OID, record)
    mark_current_collection(context)
    before = len(wb.calls)
    factory, summary = Mock(return_value=wb), {}
    prepare.process(runner, current, collector, summary, transport_factory=factory)
    factory.assert_called_once_with(runner, "new-store")
    assert any(call[1].endswith("/order-ids") for call in wb.calls[before:])
    assert summary["preparation"][0].get("reused_collection") is not True


def test_fast_noop_still_requires_unique_current_table_row(context):
    runner, wb, current, collector = context
    mark_current_collection(context)
    rows = current["snapshot"]["sheets"][0]["rows"]
    duplicate = copy.deepcopy(rows[0])
    duplicate["row_number"] += 1
    rows.append(duplicate)
    factory, summary = Mock(return_value=wb), {}
    prepare.process(runner, current, collector, summary, transport_factory=factory)
    factory.assert_not_called()
    assert summary["preparation"][0]["stage"] == "NEEDS_REVIEW"
