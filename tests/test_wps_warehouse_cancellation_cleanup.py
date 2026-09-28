"""Offline proof, journaling and full-readback tests for cancelled-row deletion."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.wps_warehouse import cancellation_cleanup as cleanup
from tools.wps_warehouse import ledger


SHEET = "2026年9月发货"
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


def table_row(number, store, oid, *, imei="", label="", qr="", note="待仓库处理", status=""):
    return {"row_number": number,
            "values": [str(number - 2), "2026/09/07", store, oid, "iPhone 16", "256GB", "黑色",
                       imei, label, qr, note],
            "warehouse_feedback": status, "warehouse_feedback_formula": ""}


def sheet_snapshot(rows, *, extra_sheet=False):
    sheets = [{"name": SHEET, "title": ["发货"], "headers": ["h"] * 11,
               "warehouse_feedback_title": "", "warehouse_feedback_header": "状态",
               "used_row_end": max([row["row_number"] for row in rows] or [2]),
               "rows": copy.deepcopy(rows)}]
    if extra_sheet:
        sheets.append({"name": "2026年8月发货", "title": ["旧"], "headers": ["h"] * 11,
                       "warehouse_feedback_title": "", "warehouse_feedback_header": "状态",
                       "used_row_end": 3,
                       "rows": [table_row(3, "瑞盈8597", "80001", note="历史记录")]})
    return {"captured_at": NOW, "complete": True, "warehouse_feedback_complete": True,
            "read_columns": "A:L", "workbook_url": "https://www.kdocs.cn/l/cof93WEpp0yG",
            "sheets": sheets}


def cancellation_order(oid, **changes):
    value = {"storeAlias": "new-store", "orderId": oid, "supplierStatus": "confirm",
             "wbStatus": "canceled_by_client", "statusCheckedAt": NOW, "statusBlockers": [],
             "statusIsCachedTerminal": False, "warehouseActionRequired": False,
             "supplyId": "WB-GI-1", "supplyScanDt": None, "supplyDone": False,
             "supplyClosedAt": None, "imei": None, "wbLabelCode": None,
             "labelImagePath": None, "supplyQrImagePath": None, "metaDetails": None}
    value.update(changes)
    return value


class FakeSync:
    def __init__(self, book, run_id, rows, *, mode="normal"):
        self.ledger = book
        self.run_id = run_id
        self.target_sheet = SHEET
        self.max_age_seconds = 600
        self.mode = mode
        self.calls = []
        self.saved = []
        self.snapshot = sheet_snapshot(rows, extra_sheet=True)
        self.read_fails = mode == "timeout_after_delete_and_read_failure"

    @property
    def retired(self):
        rows = self.ledger.db.execute(
            "SELECT store_alias,order_id,reason_code,row_snapshot,evidence FROM row_retirements "
            "ORDER BY order_id").fetchall()
        return [{"store_alias": row["store_alias"], "order_id": row["order_id"],
                 "reason_code": row["reason_code"],
                 "row_snapshot": json.loads(row["row_snapshot"]),
                 "evidence": json.loads(row["evidence"])} for row in rows]

    def state_token(self):
        stable = copy.deepcopy(self.snapshot)
        stable.pop("captured_at", None)
        return json.dumps(stable, ensure_ascii=False, sort_keys=True)

    def current(self):
        return {"version": "warehouse-airscript-v1", "build": cleanup.BUILD,
                "action": "read", "status": "ok", "snapshot": copy.deepcopy(self.snapshot),
                "state_token": self.state_token()}

    def save(self, kind, value):
        self.saved.append((kind, copy.deepcopy(value)))
        return self.ledger.archive(value)

    def _delete(self, operations):
        target = self.snapshot["sheets"][0]
        for operation in sorted(operations, key=lambda item: item["row_number"], reverse=True):
            target["rows"] = [row for row in target["rows"]
                              if not (row["row_number"] == operation["row_number"]
                                      and row["values"][3] == operation["order_id"])]
            for row in target["rows"]:
                if row["row_number"] > operation["row_number"]:
                    row["row_number"] -= 1
            target["used_row_end"] -= 1
        self.snapshot["captured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def invoke(self, request):
        self.calls.append(copy.deepcopy(request))
        if request["expected_state_token"] != self.state_token():
            raise ValueError("stale state")
        if self.mode == "timeout_before_delete":
            raise TimeoutError("unknown before mutation")
        self._delete(request["operations"])
        if self.mode in {"timeout_after_delete", "timeout_after_delete_and_read_failure"}:
            raise TimeoutError("unknown after mutation")
        if self.mode == "collaborator_change":
            self.snapshot["sheets"][0]["rows"][-1]["values"][10] = "他人修改"
        result = {"version": "warehouse-airscript-v1", "build": cleanup.BUILD,
                  "action": cleanup.ACTION, "request_id": request["request_id"],
                  "status": "applied_pending_readback", "receipts": []}
        return {"http_status": 200, "response": {"status": "finished", "error": ""},
                "result": result}

    def read(self):
        if self.read_fails:
            self.read_fails = False
            raise TimeoutError("read unavailable")
        return self.current()


class CancellationCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.book = ledger.Ledger(Path(self.temp.name))
        self.addCleanup(self.book.db.close)
        self.run_id = "cancel-cleanup-test"
        self.book.lock(self.run_id)

    def history(self, oid, *observations):
        with self.book.transaction():
            first = copy.deepcopy(observations[0])
            self.book.db.execute(
                "INSERT OR IGNORE INTO orders(store_alias,order_id,platform,platform_at,assets) "
                "VALUES (?,?,?,?,?)", ("new-store", oid, ledger.dumps(first), NOW, "{}"))
            for observation in observations:
                self.book.event("platform_observed",
                                {"before": None, "after": copy.deepcopy(observation),
                                 "collected_at": NOW}, "new-store", oid)

    def waiting(self, oid, **changes):
        value = {"storeAlias": "new-store", "orderId": oid, "supplierStatus": "new",
                 "wbStatus": "waiting", "statusBlockers": [], "warehouseActionRequired": True,
                 "supplyScanDt": None, "supplyDone": False, "supplyClosedAt": None}
        value.update(changes)
        return value

    def collector(self, *orders, run_id=None):
        return {"run_id": run_id or self.run_id, "all_sources_ok": True,
                "collected_at": NOW, "orders": list(orders)}

    def test_two_exact_rows_delete_descending_and_verify_all_survivors(self):
        first, second = "5685827269", "5685829340"
        self.history(first, self.waiting(first))
        self.history(second, self.waiting(second, supplierStatus="confirm"))
        rows = [table_row(3, "瑞盈8597", "70001", note="保留前行"),
                table_row(10, "沫德DL05", first, status="【自动状态】发货前取消"),
                table_row(11, "沫德DL05", second, status="【自动状态】发货前取消"),
                table_row(12, "瑞盈8597", "70002", note="保留后行")]
        sync = FakeSync(self.book, self.run_id, rows)
        summary = {}
        after = cleanup.process(sync, sync.current(),
                                (collector := self.collector(
                                    cancellation_order(first), cancellation_order(second))), summary)
        self.assertEqual(summary["cancellation_cleanup"]["status"], "verified")
        self.assertEqual([item["order_id"] for item in summary["cancellation_cleanup"]["deleted"]],
                         [second, first])
        request = sync.calls[0]
        self.assertEqual([op["row_number"] for op in request["operations"]], [11, 10])
        for operation in request["operations"]:
            self.assertEqual(len(operation["expected_values"]), 11)
            self.assertRegex(operation["evidence_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(operation["expected_a_l_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(operation["evidence_code"], cleanup.REASON_CODE)
        remaining = after["snapshot"]["sheets"][0]["rows"]
        self.assertEqual([(row["row_number"], row["values"][3], row["values"][10]) for row in remaining],
                         [(3, "70001", "保留前行"), (10, "70002", "保留后行")])
        self.assertEqual({item["order_id"] for item in sync.retired}, {first, second})
        self.assertTrue(all(item["row_snapshot"]["warehouse_feedback"] for item in sync.retired))
        self.assertTrue(all(order[cleanup.VERIFIED_PHASE_FIELD] == "before_handover"
                            for order in collector["orders"]))

    def test_missing_waiting_history_never_calls_wps(self):
        oid = "5685827269"
        self.history(oid, cancellation_order(oid))
        sync = FakeSync(self.book, self.run_id, [table_row(3, "沫德DL05", oid)])
        summary = {}
        cleanup.process(sync, sync.current(), self.collector(cancellation_order(oid)), summary)
        self.assertEqual(summary["cancellation_cleanup"]["status"], "blocked")
        self.assertIn("new_or_confirm_waiting_history_missing",
                      summary["cancellation_cleanup"]["blockers"][0]["details"])
        self.assertEqual(sync.calls, [])

    def test_any_handover_imei_label_or_table_evidence_blocks(self):
        cases = {
            "sorted": ({"wbStatus": "sorted", "supplierStatus": "complete"}, {}, {}),
            "scan": ({"supplyScanDt": NOW}, {}, {}),
            "done": ({"supplyDone": True}, {}, {}),
            "closed": ({"supplyClosedAt": NOW}, {}, {}),
            "imei": ({"imei": "356000000000001"}, {}, {}),
            "meta-imei": ({"metaDetails": [{"key": "imei", "value": "356000000000001"}]}, {}, {}),
            "label": ({"wbLabelCode": "123 456"}, {}, {}),
            "current-label": ({}, {"labelImagePath": "archived-label.png"}, {}),
            "table-imei": ({}, {}, {"imei": "356000000000001"}),
            "table-label": ({}, {}, {"label": '=DISPIMG("X",1)'}),
        }
        for index, (name, (historic_change, current_change, row_change)) in enumerate(cases.items(), 1):
            with self.subTest(name=name):
                oid = str(60000 + index)
                self.history(oid, self.waiting(oid), self.waiting(oid, **historic_change))
                sync = FakeSync(self.book, self.run_id,
                                [table_row(3, "沫德DL05", oid, **row_change)])
                summary = {}
                cleanup.process(sync, sync.current(),
                                self.collector(cancellation_order(oid, **current_change)), summary)
                self.assertEqual(summary["cancellation_cleanup"]["status"], "blocked")
                self.assertEqual(sync.calls, [])

    def test_timeout_after_mutation_is_accepted_only_by_independent_readback(self):
        oid = "5685827269"
        self.history(oid, self.waiting(oid))
        sync = FakeSync(self.book, self.run_id,
                        [table_row(3, "沫德DL05", oid), table_row(4, "瑞盈8597", "70001")],
                        mode="timeout_after_delete")
        summary = {}
        cleanup.process(sync, sync.current(), self.collector(cancellation_order(oid)), summary)
        self.assertEqual(summary["cancellation_cleanup"]["status"], "verified")
        self.assertTrue(summary["cancellation_cleanup"]["deleted"][0]["reconciled_from_uncertain"])
        self.assertEqual(len(sync.retired), 1)

    def test_timeout_before_mutation_is_not_retried_while_row_remains(self):
        oid = "5685827269"
        self.history(oid, self.waiting(oid))
        rows = [table_row(3, "沫德DL05", oid)]
        first = FakeSync(self.book, self.run_id, rows, mode="timeout_before_delete")
        with self.assertRaisesRegex(cleanup.CancellationCleanupBlocked, "独立回读"):
            cleanup.process(first, first.current(), self.collector(cancellation_order(oid)), {})
        self.assertEqual(len(first.calls), 1)
        second = FakeSync(self.book, self.run_id, rows)
        summary = {}
        cleanup.process(second, second.current(), self.collector(cancellation_order(oid)), summary)
        self.assertEqual(second.calls, [])
        self.assertEqual(summary["cancellation_cleanup"]["status"], "blocked")
        self.assertEqual(summary["cancellation_cleanup"]["blockers"][0]["reason"],
                         "previous_delete_result_uncertain_row_still_present")

    def test_next_run_reconciles_absent_row_after_readback_failure_without_retry(self):
        oid = "5685827269"
        self.history(oid, self.waiting(oid))
        first = FakeSync(self.book, self.run_id,
                         [table_row(3, "沫德DL05", oid), table_row(4, "瑞盈8597", "70001")],
                         mode="timeout_after_delete_and_read_failure")
        with self.assertRaisesRegex(cleanup.CancellationCleanupBlocked, "独立回读失败"):
            cleanup.process(first, first.current(), self.collector(cancellation_order(oid)), {})
        absent_rows = copy.deepcopy(first.snapshot["sheets"][0]["rows"])
        second = FakeSync(self.book, self.run_id, absent_rows)
        summary = {}
        cleanup.process(second, second.current(), self.collector(cancellation_order(oid)), summary)
        self.assertEqual(second.calls, [])
        self.assertEqual(summary["cancellation_cleanup"]["status"], "verified")
        self.assertTrue(summary["cancellation_cleanup"]["deleted"][0]["reconciled_from_uncertain"])
        self.assertEqual(len(second.retired), 1)

    def test_unrelated_change_after_delete_fails_whole_snapshot_verification(self):
        oid = "5685827269"
        self.history(oid, self.waiting(oid))
        sync = FakeSync(self.book, self.run_id,
                        [table_row(3, "沫德DL05", oid), table_row(4, "瑞盈8597", "70001")],
                        mode="collaborator_change")
        with self.assertRaisesRegex(cleanup.CancellationCleanupBlocked, "除目标整行删除外"):
            cleanup.process(sync, sync.current(), self.collector(cancellation_order(oid)), {})
        self.assertEqual(sync.retired, [])

    def test_wrong_store_identity_and_non_target_sheet_block(self):
        oid = "5685827269"
        self.history(oid, self.waiting(oid))
        wrong_store = FakeSync(self.book, self.run_id, [table_row(3, "瑞盈8597", oid)])
        summary = {}
        cleanup.process(wrong_store, wrong_store.current(), self.collector(cancellation_order(oid)), summary)
        self.assertEqual(summary["cancellation_cleanup"]["status"], "blocked")
        self.assertEqual(wrong_store.calls, [])

    def test_r10_is_required_before_any_delete(self):
        oid = "5685827269"
        self.history(oid, self.waiting(oid))
        sync = FakeSync(self.book, self.run_id, [table_row(3, "沫德DL05", oid)])
        current = sync.current()
        current["build"] = "20260906-r8"
        summary = {}
        cleanup.process(sync, current, self.collector(cancellation_order(oid)), summary)
        self.assertEqual(summary["cancellation_cleanup"]["status"], "needs_script_update")
        self.assertEqual(sync.calls, [])


if __name__ == "__main__":
    unittest.main()
