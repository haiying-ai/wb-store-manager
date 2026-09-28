"""Offline tests for the reviewed warehouse status-note runner."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.wps_warehouse.update_status_note import Runner


SHEET = "2026年9月发货"
WORKBOOK = "https://www.kdocs.cn/l/cof93WEpp0yG"
ORDER_ID = "910009001"


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class FakeStatusNote:
    def __init__(self, plan, *, timeout_after_write=False, concurrent_change=False,
                 blocked_receipt=False):
        self.plan = plan
        self.timeout_after_write = timeout_after_write
        self.concurrent_change = concurrent_change
        self.blocked_receipt = blocked_receipt
        self.timeout_used = False
        self.concurrent_used = False
        self.update_count = 0
        self.calls = []
        self.sheets = [
            {"name": SHEET, "title": ["仓库发货表"] + [""] * 10,
             "headers": ["序号", "平台下单 日期", "店铺名尾号", "订单编号", "型号", "内存", "颜色", "IMEI",
                         "WB 标签码", "供货订单二维码", "备注"],
             "warehouse_feedback_title": "仓库反馈", "warehouse_feedback_header": "反馈内容",
             "rows": [{
                "row_number": plan["row_number"],
                "values": ["1", "2026/9/5", "沫德DL05", ORDER_ID, "Synthetic Phone",
                           "128GB", "黑色", "synthetic-imei", '=DISPIMG("I",1)',
                           '=DISPIMG("J",1)', plan["expected_old_note"]],
                "warehouse_feedback": "仓库原反馈保持不变",
                "warehouse_feedback_formula": "",
            }], "used_row_end": plan["row_number"]},
            {"name": "2026年8月发货", "title": ["历史仓库发货表"] + [""] * 10,
             "headers": ["序号", "平台下单 日期", "店铺名尾号", "订单编号", "型号", "内存", "颜色", "IMEI",
                         "WB 标签码", "供货订单二维码", "备注"],
             "warehouse_feedback_title": "仓库反馈", "warehouse_feedback_header": "反馈内容",
             "rows": [{
                "row_number": 3,
                "values": ["1", "2026/8/1", "瑞盈8597", "810008001", "Other Phone",
                           "256GB", "白色", "other-imei", "", "", "历史状态"],
                "warehouse_feedback": "历史反馈",
                "warehouse_feedback_formula": "",
            }], "used_row_end": 3},
        ]
        self.original = copy.deepcopy(self.sheets)

    def snapshot(self):
        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "complete": True,
            "warehouse_feedback_complete": True,
            "read_columns": "A:L",
            "workbook_url": WORKBOOK,
            "target_sheet": SHEET,
            "sheets": copy.deepcopy(self.sheets),
        }

    def state_token(self):
        return compact({"workbook_url": WORKBOOK,
                        "sheet_names": [sheet["name"] for sheet in self.sheets],
                        "sheets": self.sheets})

    def result(self):
        snapshot = self.snapshot()
        return {
            "version": "warehouse-airscript-v1", "build": "20260908-r11",
            "action": "read", "status": "ok", "snapshot": snapshot,
            "state_token": self.state_token(),
        }

    def envelope(self, result):
        return {"captured_at": datetime.now(timezone.utc).isoformat(), "http_status": 200,
                "response": {"status": "finished", "error": ""}, "result": result}

    def target(self):
        return self.sheets[0]["rows"][0]

    def __call__(self, argv):
        self.calls.append(copy.deepcopy(argv))
        if argv["action"] == "read":
            return self.envelope(self.result())
        if argv["action"] != "update_status_notes":
            raise AssertionError("Unexpected action: " + argv["action"])
        if self.blocked_receipt:
            return self.envelope({
                "version": "warehouse-airscript-v1", "build": "20260908-r11",
                "action": "update_status_notes", "status": "blocked",
                "snapshot": None, "state_token": None,
                "request_id": argv["request_id"],
                "independent_http_readback_required": False,
            })
        if self.concurrent_change and not self.concurrent_used:
            self.concurrent_used = True
            self.target()["values"][4] = "Collaborator changed model"
        if argv["expected_state_token"] != self.state_token():
            raise ValueError("Synthetic concurrent workbook change")
        operation = argv["operation"]
        self.assert_operation(argv, operation)
        if self.target()["values"][10] != operation["expected_old_note"]:
            raise ValueError("Synthetic stale status note")
        self.target()["values"][10] = operation["new_note"]
        self.update_count += 1
        if self.timeout_after_write and not self.timeout_used:
            self.timeout_used = True
            raise TimeoutError("Synthetic timeout after K was updated")
        return self.envelope({
            "version": "warehouse-airscript-v1", "build": "20260908-r11",
            "action": "update_status_notes", "status": "applied_pending_readback",
            "snapshot": None, "state_token": None,
            "request_id": argv["request_id"], "order_id": operation["order_id"],
            "row_number": operation["row_number"],
            "expected_old_note": operation["expected_old_note"],
            "new_note": operation["new_note"], "independent_http_readback_required": True,
        })

    def assert_operation(self, argv, operation):
        expected = {key: self.plan[key] for key in
                    ("store_alias", "order_id", "row_number", "expected_old_note", "new_note")}
        if operation != expected:
            raise AssertionError("Update operation differs from the reviewed plan")
        if argv.get("sheet") != SHEET or argv.get("request_id") != self.plan["request_id"]:
            raise AssertionError("Update request identity differs from the reviewed plan")


class UpdateStatusNoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def case(self, name, *, evidence_age=timedelta(0), **fake_options):
        directory = self.base / name
        directory.mkdir()
        captured_at = (datetime.now(timezone.utc) - evidence_age).isoformat()
        evidence = directory / "wb-evidence.json"
        evidence.write_text(compact({
            "captured_at": captured_at, "all_verified": True,
            "order_ids": [ORDER_ID], "orders": [{
                "store_alias": "new-store", "order_id": ORDER_ID,
                "supplier_status": "complete", "wb_status": "sorted",
                "warehouse_action_required": False,
                "exact_order_identity_verified": True, "seller_identity_verified": True,
            }],
        }), encoding="utf-8")
        plan = {
            "schema_version": 1, "plan_id": "offline-status-" + name,
            "authorization": {"approved": True, "statement": "Approve this synthetic K-only note update."},
            "fresh_wb_evidence": {"path": str(evidence), "captured_at": captured_at},
            "sheet": SHEET, "store_alias": "new-store", "order_id": ORDER_ID,
            "row_number": 3, "expected_old_note": "待处理",
            "new_note": "已核实状态，等待后续处理", "request_id": "offline-status-request-1",
            "expected_supplier_status": "complete", "expected_wb_status": "sorted",
            "expected_warehouse_action_required": False,
        }
        plan_path = directory / "plan.json"
        plan_path.write_text(compact(plan), encoding="utf-8")
        return plan_path, directory / "runs", FakeStatusNote(plan, **fake_options), plan

    def test_dryrun_writes_nothing_and_live_changes_only_k_with_readbacks(self):
        plan_path, root, fake, plan = self.case("normal")
        summary = Runner(plan_path, root, invoke=fake).run(live=False)
        self.assertEqual(summary["status"], "dryrun")
        self.assertEqual(fake.update_count, 0)
        self.assertEqual(fake.sheets, fake.original)
        live_start = len(fake.calls)

        summary = Runner(plan_path, root, invoke=fake).run(live=True)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual([call["action"] for call in fake.calls[live_start:]],
                         ["read", "update_status_notes", "read"])
        self.assertEqual(fake.update_count, 1)
        expected = copy.deepcopy(fake.original)
        expected[0]["rows"][0]["values"][10] = plan["new_note"]
        self.assertEqual(fake.sheets, expected, "Only the exact target K value may change")

    def test_timeout_after_write_is_verified_then_resume_does_not_resend(self):
        plan_path, root, fake, plan = self.case("timeout", timeout_after_write=True)
        summary = Runner(plan_path, root, invoke=fake).run(live=True)
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(fake.update_count, 1)
        self.assertEqual([call["action"] for call in fake.calls],
                         ["read", "update_status_notes", "read"])
        self.assertEqual(fake.target()["values"][10], plan["new_note"])
        resume_start = len(fake.calls)

        summary = Runner(plan_path, root, invoke=fake).run(live=True)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual([call["action"] for call in fake.calls[resume_start:]], ["read"])
        self.assertEqual(fake.update_count, 1)

    def test_server_pre_mutation_block_is_closed_after_unchanged_readback(self):
        plan_path, root, fake, _ = self.case("server-blocked", blocked_receipt=True)
        summary = Runner(plan_path, root, invoke=fake).run(live=True)
        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(summary["operation_status"], "not_applied")
        self.assertEqual(fake.update_count, 0)
        self.assertEqual(fake.sheets, fake.original)
        self.assertEqual([call["action"] for call in fake.calls],
                         ["read", "update_status_notes", "read"])
        import sqlite3
        database = sqlite3.connect(root / "ledger.sqlite3")
        try:
            self.assertIsNone(database.execute(
                "SELECT value FROM meta WHERE key='status_note_active_plan'").fetchone())
        finally:
            database.close()

    def test_stale_evidence_or_concurrent_other_field_change_blocks_k_write(self):
        with self.subTest("stale evidence"):
            plan_path, root, fake, _ = self.case("stale", evidence_age=timedelta(days=2))
            summary = Runner(plan_path, root, invoke=fake).run(live=True)
            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(fake.update_count, 0)
            self.assertEqual(fake.sheets, fake.original)

        with self.subTest("concurrent non-K edit"):
            plan_path, root, fake, plan = self.case("concurrent", concurrent_change=True)
            summary = Runner(plan_path, root, invoke=fake).run(live=True)
            self.assertIn(summary["status"], ("stopped", "blocked"))
            self.assertEqual(fake.update_count, 0)
            self.assertEqual(fake.target()["values"][10], plan["expected_old_note"])
            self.assertEqual([call["action"] for call in fake.calls],
                             ["read", "update_status_notes", "read"])

    def test_evidence_fact_mismatches_and_oversized_notes_block_before_primary_read(self):
        mutations = [
            ("supplier status", {"supplier_status": "pending"}),
            ("WB status", {"wb_status": "waiting"}),
            ("warehouse action", {"warehouse_action_required": True}),
            ("store alias", {"store_alias": "owner-store"}),
            ("exact identity", {"exact_order_identity_verified": False}),
            ("seller identity", {"seller_identity_verified": False}),
        ]
        for index, (name, changes) in enumerate(mutations):
            with self.subTest(name):
                plan_path, root, fake, plan = self.case("evidence-mismatch-" + str(index))
                evidence_path = Path(plan["fresh_wb_evidence"]["path"])
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                evidence["orders"][0].update(changes)
                evidence_path.write_text(compact(evidence), encoding="utf-8")
                summary = Runner(plan_path, root, invoke=fake).run(live=True)
                self.assertEqual(summary["status"], "blocked")
                self.assertEqual(fake.calls, [])
                self.assertEqual(fake.update_count, 0)

        for field in ("expected_old_note", "new_note"):
            with self.subTest(field + " exceeds 500 characters"):
                plan_path, root, fake, plan = self.case("oversized-" + field)
                plan[field] = "x" * 501
                plan_path.write_text(compact(plan), encoding="utf-8")
                summary = Runner(plan_path, root, invoke=fake).run(live=True)
                self.assertEqual(summary["status"], "blocked")
                self.assertEqual(fake.calls, [])
                self.assertEqual(fake.update_count, 0)


if __name__ == "__main__":
    unittest.main()
