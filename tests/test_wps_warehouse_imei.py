"""Offline WB metadata side-effect and warehouse-reply ordering checks."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.wps_warehouse import imei_sync, ledger

IMEI = "123456789012345"
ORDER = {"storeAlias": "new-store", "orderId": "5664833385", "warehouseActionRequired": True,
         "supplierStatus": "confirm", "wbStatus": "waiting", "createdAt": "2026-09-04T07:55:37Z",
         "productModel": "iPhone 14 Pro", "capacityGB": 256, "colorChinese": "黑色", "productIdentityVerified": True}


class FakeWB:
    def __init__(self):
        self.value, self.supplier, self.mode, self.puts = "", "confirm", "success", []
        self.decision, self.after_put_decision, self.schema = "required", "filled", "metaDetails"
        self.read_decisions = []

    def status(self, oid):
        return {"id": int(oid), "supplierStatus": self.supplier, "wbStatus": "waiting"}

    def read(self, oid):
        if self.read_decisions:
            self.decision = self.read_decisions.pop(0)
        self.last_imei_read = {"order_id": oid, "value": self.value,
                               "decision": self.decision, "schema": self.schema}
        return self.value

    def put(self, oid, value):
        self.puts.append((oid, value))
        if self.mode != "timeout_before":
            self.value = value
            self.decision = self.after_put_decision
        if self.mode.startswith("timeout"):
            raise TimeoutError("simulated")
        return 204


class Runner:
    def __init__(self, root):
        self.run_id = "imei-test"
        self.ledger = ledger.Ledger(root)
        self.ledger.lock(self.run_id)
        self.evidence = []
        self.prechecks = 0
        self.fail_at = None

    def save(self, kind, value):
        self.evidence.append((kind, value))

    def precheck(self, current, collector):
        self.prechecks += 1
        if self.fail_at == self.prechecks:
            raise ValueError("回执已变更")
        return current


class MetadataContractTests(unittest.TestCase):
    """Official v3.OrderMetaAPI / MetaDetails response, without credentials.

    https://dev.wildberries.cn/docs/openapi/orders-fbs
    https://dev.wildberries.ru/en/news/302 (migration from meta on 2026-04-30)
    """
    def response(self, value=IMEI, decision="filled"):
        return {"orders": [{"id": int(ORDER["orderId"]), "metaDetails": [
            {"key": "uin", "value": None, "decision": "optional"},
            {"key": "imei", "value": value, "decision": decision},
        ]}]}

    def read(self, data, status=200):
        # __init__ loads credentials; the parser contract tests must never do so.
        client = object.__new__(imei_sync.Transport)
        client.request = Mock(return_value=(status, data))
        result = client.read(ORDER["orderId"])
        client.request.assert_called_once_with("POST", imei_sync.MARKET + imei_sync.META,
                                               {"orders": [int(ORDER["orderId"])]})
        return result

    def test_current_schema_reads_the_exact_imei_not_another_identifier(self):
        self.assertEqual(self.read(self.response()), IMEI)

    def test_current_schema_nullable_or_empty_value_means_unfilled(self):
        for value in [None, ""]:
            for decision in ["required", "optional"]:
                with self.subTest(value=value, decision=decision):
                    self.assertEqual(self.read(self.response(value, decision)), "")

    def test_current_schema_preserves_values_for_all_documented_decisions(self):
        # Stored-value equality is distinct from WB's validation/dispatch status.
        for decision in ["filled", "deadlineExceeded", "optional", "imeiMaySell",
                         "imeiSoldB2B", "pending", "required", "imeiInvalidFormat", "imeiAlreadySold"]:
            with self.subTest(decision=decision):
                self.assertEqual(self.read(self.response(IMEI, decision)), IMEI)

    def test_legacy_archived_schema_is_still_readable(self):
        for value, expected in [(IMEI, IMEI), (None, ""), ("", "")]:
            with self.subTest(value=value):
                data = {"orders": [{"id": int(ORDER["orderId"]), "meta": {"imei": {"value": value}}}]}
                self.assertEqual(self.read(data), expected)

    def test_dual_schema_must_agree(self):
        for old_value in [IMEI, "987654321098765", "", None]:
            with self.subTest(old_value=old_value):
                data = self.response()
                data["orders"][0]["meta"] = {"imei": {"value": old_value}}
                if old_value == IMEI:
                    self.assertEqual(self.read(data), IMEI)
                else:
                    with self.assertRaisesRegex(ValueError, "新旧IMEI元数据不一致"):
                        self.read(data)

    def test_missing_current_imei_never_falls_back_to_legacy_value(self):
        data = self.response()
        data["orders"][0]["metaDetails"] = []
        data["orders"][0]["meta"] = {"imei": {"value": IMEI}}
        with self.assertRaisesRegex(ValueError, "未开放IMEI"):
            self.read(data)

    def test_conflicting_or_duplicate_imei_entries_are_rejected(self):
        for value in [IMEI, "987654321098765", None]:
            with self.subTest(value=value):
                data = self.response()
                data["orders"][0]["metaDetails"].append({"key": "imei", "value": value, "decision": "filled"})
                with self.assertRaisesRegex(ValueError, "重复IMEI字段"):
                    self.read(data)

    def test_order_id_must_be_an_exact_unique_json_integer(self):
        for identity in [True, int(ORDER["orderId"]) + 1, ORDER["orderId"], float(ORDER["orderId"]), None]:
            with self.subTest(identity=identity):
                data = self.response()
                data["orders"][0]["id"] = identity
                with self.assertRaises(ValueError):
                    self.read(data)
        data = self.response()
        data["orders"].append(copy.deepcopy(data["orders"][0]))
        with self.assertRaisesRegex(ValueError, "重复订单ID"):
            self.read(data)

    def test_malformed_current_metadata_never_falls_back(self):
        for details in [None, {}, [None], [{"value": IMEI}],
                        [{"key": "imei", "decision": "filled"}],
                        [{"key": "imei", "value": IMEI}],
                        [{"key": "imei", "value": IMEI, "decision": False}]]:
            with self.subTest(details=details):
                data = self.response()
                data["orders"][0].update(metaDetails=details, meta={"imei": {"value": IMEI}})
                with self.assertRaises(ValueError):
                    self.read(data)

    def test_malformed_legacy_metadata_does_not_conceal_mixed_schema_conflict(self):
        for legacy in [None, {}, {"imei": None}, {"imei": {}}]:
            with self.subTest(legacy=legacy):
                data = self.response()
                data["orders"][0]["meta"] = legacy
                with self.assertRaises(ValueError):
                    self.read(data)

    def test_non_string_or_non_ascii_imei_values_are_rejected(self):
        for value in [123456789012345, 123456789012345.0, True, [], {},
                      "1.23456789012345E+14", "１２３４５６７８９０１２３４５", "12345678901234"]:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "IMEI格式异常"):
                    self.read(self.response(value))

    def test_http_errors_and_incomplete_envelopes_never_become_empty_imei(self):
        for data, status in [(self.response(), 403), (None, 200), ({}, 200),
                             ({"orders": {}}, 200), ({"orders": []}, 200),
                             ({"orders": [None]}, 200)]:
            with self.subTest(data=data, status=status):
                with self.assertRaises(ValueError):
                    self.read(data, status)

    def test_raw_duplicate_json_fields_are_rejected_before_they_can_hide_conflicts(self):
        client = object.__new__(imei_sync.Transport)
        client.token, client.alias, client.last_call = "offline-test-token", "new-store", 0.0
        client.runner = SimpleNamespace(save=Mock())
        duplicate = '{"orders":[{"id":5664833385,"metaDetails":[{"key":"imei","value":"987654321098765","value":"123456789012345","decision":"filled"}]}]}'
        response = SimpleNamespace(status_code=200, text=duplicate)
        with patch.object(imei_sync.requests, "request", return_value=response), patch.object(imei_sync.time, "sleep"):
            with self.assertRaises(ValueError):
                client.read(ORDER["orderId"])

    def test_transport_records_exact_current_decision_and_clears_it_on_failed_read(self):
        client = object.__new__(imei_sync.Transport)
        client.request = Mock(side_effect=[(200, self.response(IMEI, "imeiMaySell")), (403, None)])
        self.assertEqual(client.read(ORDER["orderId"]), IMEI)
        self.assertEqual(client.last_imei_read, {"order_id": ORDER["orderId"], "value": IMEI,
                                                "decision": "imeiMaySell", "schema": "metaDetails"})
        with self.assertRaises(ValueError):
            client.read(ORDER["orderId"])
        self.assertIsNone(client.last_imei_read)

    def test_transport_marks_legacy_value_as_lacking_validation_evidence(self):
        client = object.__new__(imei_sync.Transport)
        client.request = Mock(return_value=(200, {"orders": [{"id": int(ORDER["orderId"]), "meta": {"imei": {"value": IMEI}}}]}))
        self.assertEqual(client.read(ORDER["orderId"]), IMEI)
        self.assertIsNone(client.last_imei_read["decision"])
        self.assertEqual(client.last_imei_read["schema"], "legacy")


class ImeiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runner = Runner(Path(self.temp.name))
        self.wb = FakeWB()
        self.values = ["2", "2026/09/04", "Скиди Выбор（扬州沫德）", ORDER["orderId"], "iPhone 14 Pro", "256GB", "黑色", IMEI, "", "", "人工备注"]

    def tearDown(self):
        self.runner.ledger.db.close()
        self.temp.cleanup()

    def process(self, values=None, extra_rows=None):
        rows = [{"row_number": 4, "values": copy.deepcopy(values or self.values)}] + (extra_rows or [])
        current = {"snapshot": {"captured_at": ledger.now(), "complete": True, "sheets": [{"name": "2026年9月发货", "rows": rows}]}}
        collector = {"run_id": self.runner.run_id, "collected_at": ledger.now(), "orders": [copy.deepcopy(ORDER)]}
        summary = {}
        _, eligible = imei_sync.process(self.runner, current, collector, summary, lambda runner, alias: self.wb)
        return summary, eligible

    def test_empty_reply_waits_without_wb_mutation_or_label_eligibility(self):
        self.values[7] = ""
        summary, eligible = self.process()
        self.assertEqual(summary["shipment_progress"][0]["stage"], "WAIT_IMEI")
        self.assertFalse(eligible)
        self.assertFalse(self.wb.puts)

    def test_confirm_order_uploads_once_and_only_readback_releases_labels(self):
        summary, eligible = self.process()
        self.assertEqual(eligible, {("new-store", ORDER["orderId"])})
        self.assertEqual(self.wb.puts, [(ORDER["orderId"], IMEI)])
        self.assertNotIn(IMEI, json.dumps(summary))
        self.process()
        self.assertEqual(len(self.wb.puts), 1)

    def test_first_put_allows_empty_required_field_then_waits_for_validation(self):
        self.wb.after_put_decision = "pending"
        summary, eligible = self.process()
        self.assertEqual(len(self.wb.puts), 1)
        self.assertFalse(eligible)
        self.assertEqual(summary["shipment_progress"][0]["stage"], "WAIT_WB_VALIDATION")
        self.assertEqual(imei_sync.journal(self.runner, "new-store", ORDER["orderId"])["state"], "awaiting_validation")
        _, eligible = self.process()
        self.assertFalse(eligible)
        self.assertEqual(len(self.wb.puts), 1)
        self.wb.decision = "imeiMaySell"
        summary, eligible = self.process()
        self.assertTrue(eligible)
        self.assertEqual(summary["shipment_progress"][0]["stage"], "WB_IMEI_VERIFIED")
        self.assertEqual(len(self.wb.puts), 1)

    def test_only_finished_or_unneeded_validation_releases_existing_matching_value(self):
        self.wb.value = IMEI
        for decision in ["filled", "optional", "imeiMaySell", "imeiSoldB2B"]:
            with self.subTest(decision=decision):
                self.wb.decision = decision
                summary, eligible = self.process()
                self.assertTrue(eligible)
                self.assertEqual(summary["shipment_progress"][0]["wb_imei_decision"], decision)
        self.assertFalse(self.wb.puts)

    def test_pending_failed_and_unknown_decisions_block_labels_without_overwriting(self):
        self.wb.value = IMEI
        for decision, stage in [("pending", "WAIT_WB_VALIDATION"), ("deadlineExceeded", "WAIT_WB_VALIDATION"),
                                ("required", "NEEDS_REVIEW"), ("imeiInvalidFormat", "NEEDS_REVIEW"),
                                ("imeiAlreadySold", "NEEDS_REVIEW"), ("futureDecision", "NEEDS_REVIEW")]:
            with self.subTest(decision=decision):
                self.wb.decision = decision
                summary, eligible = self.process()
                self.assertFalse(eligible)
                self.assertEqual(summary["shipment_progress"][0]["stage"], stage)
                self.assertNotIn(IMEI, json.dumps(summary))
                self.assertFalse(self.wb.puts)

    def test_final_read_decision_blocks_even_if_post_put_read_was_filled(self):
        self.wb.read_decisions = ["required", "required", "filled", "pending"]
        summary, eligible = self.process()
        self.assertFalse(eligible)
        self.assertEqual(summary["shipment_progress"][0]["stage"], "WAIT_WB_VALIDATION")
        self.assertEqual(len(self.wb.puts), 1)

    def test_legacy_matching_value_never_claims_validation_passed(self):
        self.wb.value, self.wb.schema, self.wb.decision = IMEI, "legacy", None
        summary, eligible = self.process()
        self.assertFalse(eligible)
        self.assertEqual(summary["shipment_progress"][0]["stage"], "NEEDS_REVIEW")
        self.assertIn("旧版IMEI值", summary["shipment_progress"][0]["message"])
        self.assertFalse(self.wb.puts)

    def test_empty_pending_or_unknown_field_is_not_overwritten(self):
        for decision in ["pending", "deadlineExceeded", "imeiAlreadySold", "futureDecision"]:
            with self.subTest(decision=decision):
                self.wb.decision = decision
                _, eligible = self.process()
                self.assertFalse(eligible)
                self.assertFalse(self.wb.puts)

    def test_disappearing_value_after_pending_write_does_not_trigger_retry(self):
        self.wb.after_put_decision = "pending"
        self.process()
        self.wb.value, self.wb.decision = "", "required"
        _, eligible = self.process()
        self.assertFalse(eligible)
        self.assertEqual(len(self.wb.puts), 1)

    def test_new_and_complete_without_metadata_wait_without_status_mutations(self):
        for status in ["new", "complete"]:
            self.wb.supplier = status
            summary, eligible = self.process()
            self.assertEqual(summary["shipment_progress"][0]["stage"], "WAIT_WB_CONFIRM")
            self.assertFalse(eligible)
        self.assertFalse(self.wb.puts)

    def test_different_platform_imei_is_never_overwritten(self):
        self.wb.value = "987654321098765"
        summary, eligible = self.process()
        self.assertEqual(summary["shipment_progress"][0]["stage"], "NEEDS_REVIEW")
        self.assertFalse(self.wb.puts)
        self.assertFalse(eligible)

    def test_timeout_after_write_is_resolved_by_readback_without_second_put(self):
        self.wb.mode = "timeout_after"
        _, eligible = self.process()
        self.assertTrue(eligible)
        self.process()
        self.assertEqual(len(self.wb.puts), 1)

    def test_timeout_with_empty_readback_stops_future_retry(self):
        self.wb.mode = "timeout_before"
        summary, eligible = self.process()
        self.assertFalse(eligible)
        self.process()
        self.assertEqual(len(self.wb.puts), 1)

    def test_changed_reply_blocks_before_put(self):
        self.runner.fail_at = 1
        _, eligible = self.process()
        self.assertFalse(self.wb.puts)
        self.assertFalse(eligible)

    def test_invalid_and_duplicate_imei_never_write(self):
        self.values[7] = "1.23456789012345E+14"
        _, eligible = self.process()
        self.assertFalse(eligible)
        self.values[7] = IMEI
        other = self.values[:]
        other[3] = "1234567890"
        _, eligible = self.process(extra_rows=[{"row_number": 5, "values": other}])
        self.assertFalse(eligible)
        self.assertFalse(self.wb.puts)

    def test_new_entry_with_cached_labels_waits_for_imei_stage(self):
        order = {**ORDER, "imeiWorkflowEnabled": True}
        path = Path(self.temp.name) / "label.png"
        path.write_bytes(b"archived label fixture")
        order["labelImagePath"] = str(path)
        collector = {"run_id": self.runner.run_id, "collected_at": ledger.now(), "all_sources_ok": True, "orders": [order]}
        snapshot = {"captured_at": ledger.now(), "complete": True, "sheets": [{"name": "2026年9月发货", "rows": []}]}
        planned = self.runner.ledger.plan(collector, snapshot, "2026年9月发货")
        self.assertEqual(planned["operations"][0]["required_image_kinds"], [])
        self.assertEqual(planned["operations"][0]["values"][8:10], ["", ""])


if __name__ == "__main__":
    unittest.main()
