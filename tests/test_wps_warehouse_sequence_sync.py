"""Offline safety tests for WPS shipping-sequence renumbering."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.wps_warehouse import ledger, sequence_sync


SHEET = "2026年9月发货"
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


def table_row(number, store, oid, sequence=None, *, note="", feedback=""):
    return {
        "row_number": number,
        "values": [str(number - 2) if sequence is None else str(sequence),
                   "2026/09/07", store, oid, "iPhone 16", "256GB", "黑色",
                   "", "", "", note],
        "warehouse_feedback": feedback,
        "warehouse_feedback_formula": "",
    }


def workbook(rows, *, historical=None):
    sheets = [{"name": SHEET, "title": ["发货"], "headers": ["h"] * 11,
               "warehouse_feedback_title": "", "warehouse_feedback_header": "状态",
               "used_row_end": max([row["row_number"] for row in rows] or [2]),
               "rows": copy.deepcopy(rows)}]
    if historical is not None:
        sheets.append({"name": "2026年8月发货", "title": ["历史"],
                       "headers": ["h"] * 11, "warehouse_feedback_title": "",
                       "warehouse_feedback_header": "状态",
                       "used_row_end": max([row["row_number"] for row in historical] or [2]),
                       "rows": copy.deepcopy(historical)})
    return {"captured_at": NOW, "complete": True,
            "warehouse_feedback_complete": True, "read_columns": "A:L",
            "workbook_url": "https://www.kdocs.cn/l/cof93WEpp0yG", "sheets": sheets}


class FakeSync:
    def __init__(self, book, run_id, rows, *, historical=None, mode="normal",
                 build=sequence_sync.BUILD):
        self.ledger = book
        self.run_id = run_id
        self.target_sheet = SHEET
        self.mode = mode
        self.build = build
        self.snapshot = workbook(rows, historical=historical)
        self.write_calls = []
        self.saved = []
        self.prechecks = 0

    def state_token(self):
        stable = copy.deepcopy(self.snapshot)
        stable.pop("captured_at", None)
        return json.dumps(stable, ensure_ascii=False, sort_keys=True)

    def current(self):
        return {"version": "warehouse-airscript-v1", "build": self.build,
                "action": "read", "status": "ok",
                "snapshot": copy.deepcopy(self.snapshot),
                "state_token": self.state_token()}

    def save(self, kind, value):
        self.saved.append((kind, copy.deepcopy(value)))
        return self.ledger.archive(value)

    def precheck(self, current, collector):
        del collector
        self.prechecks += 1
        if self.mode == "concurrent_before":
            self.snapshot["sheets"][0]["rows"][0]["values"][10] = "写前他人修改"
        observed = self.current()
        if observed["state_token"] != current["state_token"]:
            raise ValueError("stale precheck")
        return observed

    def _apply(self, operations):
        target = self.snapshot["sheets"][0]
        for operation in operations:
            matches = [row for row in target["rows"]
                       if row["row_number"] == operation["row_number"]
                       and row["values"][3] == operation["order_id"]]
            if len(matches) != 1:
                raise ValueError("wrong row identity")
            matches[0]["values"][0] = operation["new_sequence"]
        self.snapshot["captured_at"] = datetime.now(timezone.utc).isoformat()

    def invoke(self, request):
        self.write_calls.append(copy.deepcopy(request))
        if request["expected_state_token"] != self.state_token():
            raise ValueError("stale state")
        if self.mode == "timeout_before":
            raise TimeoutError("write outcome unknown before mutation")
        self._apply(request["operations"])
        if self.mode == "timeout_after":
            raise TimeoutError("write outcome unknown after mutation")
        if self.mode == "concurrent_after":
            # A change anywhere else in the workbook invalidates A-only proof.
            self.snapshot["sheets"][1]["rows"][0]["values"][10] = "他人并发修改"
        result = {"version": "warehouse-airscript-v1", "build": sequence_sync.BUILD,
                  "action": sequence_sync.ACTION, "request_id": request["request_id"],
                  "status": "applied_pending_readback", "receipts": []}
        return {"http_status": 200,
                "response": {"status": "finished", "error": ""},
                "result": result}

    def read(self):
        return self.current()


class SequenceSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.book = ledger.Ledger(Path(self.temp.name))
        self.addCleanup(self.book.db.close)
        self.run_id = "sequence-sync-test"
        self.book.lock(self.run_id)

    @staticmethod
    def collector():
        return {"run_id": "sequence-sync-test", "all_sources_ok": True,
                "collected_at": NOW, "orders": []}

    def test_row_10_sequence_10_is_changed_to_8_and_fully_bound(self):
        rows = [table_row(3, "瑞盈8597", "70001"),
                table_row(10, "沫德DL05", "5686033990", sequence="10",
                          note="待仓库处理", feedback="【自动状态】待发货")]
        sync = FakeSync(self.book, self.run_id, rows)
        summary = {}
        observed = sequence_sync.process(sync, sync.current(), self.collector(), summary)

        result = summary["sequence_sync"]
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["required_build"], "20260908-r11")
        self.assertEqual(result["updated"][0]["old_sequence"], "10")
        self.assertEqual(result["updated"][0]["new_sequence"], "8")
        self.assertEqual(observed["snapshot"]["sheets"][0]["rows"][1]["values"][0], "8")
        self.assertEqual(len(sync.write_calls), 1)
        request = sync.write_calls[0]
        self.assertEqual(request["action"], "renumber_shipping_sequences")
        self.assertEqual(request["expected_state_token"], sync.saved[0][1].get(
            "expected_state_token", request["expected_state_token"]))
        self.assertEqual(len(request["operations"]), 1)
        operation = request["operations"][0]
        self.assertEqual(operation["store_alias"], "new-store")
        self.assertEqual(operation["order_id"], "5686033990")
        self.assertEqual(operation["row_number"], 10)
        self.assertEqual(len(operation["expected_values"]), 11)
        self.assertEqual(operation["expected_values"][0], "10")
        self.assertEqual(operation["expected_warehouse_feedback"], "【自动状态】待发货")
        self.assertEqual(operation["expected_warehouse_feedback_formula"], "")
        kinds = [kind for kind, _ in sync.saved]
        self.assertLess(kinds.index("shipping-sequence-journal-started"),
                        kinds.index("request-" + sequence_sync.ACTION))

    def test_no_inconsistent_sequence_is_unchanged_without_write(self):
        sync = FakeSync(self.book, self.run_id, [
            table_row(3, "瑞盈8597", "70001"),
            table_row(4, "沫德DL05", "70002")])
        summary = {}
        current = sync.current()
        returned = sequence_sync.process(sync, current, self.collector(), summary)
        self.assertIs(returned, current)
        self.assertEqual(summary["sequence_sync"], {
            "status": "unchanged", "updated": [], "blockers": [],
            "required_build": sequence_sync.BUILD})
        self.assertEqual(sync.write_calls, [])
        self.assertEqual(sync.prechecks, 0)

    def test_unknown_store_and_cross_sheet_duplicate_identity_block_writes(self):
        cases = [
            ([table_row(10, "未知店铺", "5686033990", sequence="10")], None),
            ([table_row(10, "沫德DL05", "5686033990", sequence="10")],
             [table_row(3, "沫德DL05", "5686033990")]),
        ]
        for index, (rows, historical) in enumerate(cases):
            with self.subTest(index=index):
                # Each subcase needs its own persistent journal database.
                root = Path(self.temp.name) / str(index)
                book = ledger.Ledger(root)
                self.addCleanup(book.db.close)
                run_id = "identity-" + str(index)
                book.lock(run_id)
                sync = FakeSync(book, run_id, rows, historical=historical)
                summary = {}
                sequence_sync.process(sync, sync.current(), {}, summary)
                self.assertEqual(summary["sequence_sync"]["status"], "blocked")
                self.assertTrue(summary["sequence_sync"]["blockers"])
                self.assertEqual(sync.write_calls, [])

    def test_unrelated_concurrent_change_after_write_blocks_full_workbook_proof(self):
        sync = FakeSync(
            self.book, self.run_id,
            [table_row(10, "沫德DL05", "5686033990", sequence="10")],
            historical=[table_row(3, "瑞盈8597", "60001", note="历史")],
            mode="concurrent_after")
        summary = {}
        with self.assertRaisesRegex(sequence_sync.SequenceSyncBlocked, "目标A列序号外"):
            sequence_sync.process(sync, sync.current(), self.collector(), summary)
        self.assertEqual(summary["sequence_sync"]["status"], "blocked")
        saved = self.book.db.execute(
            "SELECT value FROM meta WHERE key=?", (sequence_sync.JOURNAL_KEY,)).fetchone()
        self.assertEqual(json.loads(saved[0])["state"], "uncertain")

    def test_timeout_after_mutation_is_accepted_by_independent_exact_readback(self):
        sync = FakeSync(self.book, self.run_id,
                        [table_row(10, "沫德DL05", "5686033990", sequence="10")],
                        mode="timeout_after")
        summary = {}
        sequence_sync.process(sync, sync.current(), self.collector(), summary)
        self.assertEqual(summary["sequence_sync"]["status"], "verified")
        self.assertTrue(summary["sequence_sync"]["updated"][0]["reconciled_from_uncertain"])
        self.assertEqual(sync.snapshot["sheets"][0]["rows"][0]["values"][0], "8")

    def test_timeout_before_mutation_is_persisted_and_never_blindly_retried(self):
        rows = [table_row(10, "沫德DL05", "5686033990", sequence="10")]
        first = FakeSync(self.book, self.run_id, rows, mode="timeout_before")
        with self.assertRaises(sequence_sync.SequenceSyncBlocked):
            sequence_sync.process(first, first.current(), self.collector(), {})
        self.assertEqual(len(first.write_calls), 1)

        second = FakeSync(self.book, self.run_id, rows)
        summary = {}
        with self.assertRaisesRegex(sequence_sync.SequenceSyncBlocked, "停止自动重试"):
            sequence_sync.process(second, second.current(), self.collector(), summary)
        self.assertEqual(second.write_calls, [])
        self.assertEqual(summary["sequence_sync"]["status"], "blocked")
        self.assertEqual(summary["sequence_sync"]["blockers"][0]["reason"],
                         "previous_sequence_write_uncertain_and_rows_unchanged")

    def test_build_mismatch_requests_r10_without_any_write(self):
        sync = FakeSync(self.book, self.run_id,
                        [table_row(10, "沫德DL05", "5686033990", sequence="10")],
                        build="20260907-r9")
        summary = {}
        current = sync.current()
        self.assertIs(sequence_sync.process(sync, current, self.collector(), summary), current)
        self.assertEqual(summary["sequence_sync"]["status"], "needs_script_update")
        self.assertEqual(summary["sequence_sync"]["required_build"], "20260908-r11")
        self.assertEqual(sync.write_calls, [])


if __name__ == "__main__":
    unittest.main()
