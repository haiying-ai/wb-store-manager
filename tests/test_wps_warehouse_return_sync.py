"""Offline safety tests for WB seller-return WPS synchronization."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.wps_warehouse import ledger, return_sync


NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


def return_row(number, values=None):
    return {"row_number": number,
            "values": copy.deepcopy(values or [str(number - 2)] + [""] * 10)}


def workbook(rows, *, shipping_note="", headers=None):
    return {
        "captured_at": NOW,
        "complete": True,
        "warehouse_feedback_complete": True,
        "read_columns": "A:L",
        "return_read_columns": "A:K",
        "workbook_url": "https://www.kdocs.cn/l/cof93WEpp0yG",
        "sheets": [
            {
                "name": "2026年9月发货", "kind": "shipping",
                "read_columns": "A:L", "title": ["发货"] + [""] * 10,
                "headers": ["h"] * 11, "warehouse_feedback_title": "",
                "warehouse_feedback_header": "状态", "used_row_end": 3,
                "rows": [{"row_number": 3,
                          "values": ["1", "2026/09/08", "瑞盈8597", "90001",
                                     "iPhone", "128GB", "黑色", "", "", "",
                                     shipping_note],
                          "warehouse_feedback": "",
                          "warehouse_feedback_formula": ""}],
            },
            {
                "name": return_sync.TARGET_SHEET, "kind": "returns",
                "read_columns": "A:K", "title": ["退货"] + [""] * 10,
                "headers": copy.deepcopy(headers or return_sync.HEADERS),
                "used_row_end": max([row["row_number"] for row in rows] or [2]),
                "rows": copy.deepcopy(rows),
            },
        ],
    }


def record(**changes):
    item = {
        "storeAlias": "owner-store",
        "returnKey": "owner-store:56287024150",
        "shkId": "56287024150",
        "stickerId": "56287024150",
        "orderId": "5369901737",
        "srid": "mp.exact.r",
        "identityComplete": True,
        "storeIdentityMatches": True,
        "statusOriginal": "Готов к выдаче",
        "statusCode": "WAITING_PICKUP",
        "statusChinese": "待提货",
        "statusDefinitive": True,
        "pickupActionRequired": True,
        "isStatusActive": True,
        "readyToReturnDt": "2026-09-08T09:55:37",
        "completedDt": None,
        "expiredDt": None,
        "dstOfficeId": "206860",
        "dstOfficeAddress": "Москва, Новороссийская Улица 25к1",
        "checkedAt": NOW,
        "productIdentityVerified": True,
        "productModel": "iPhone 17 Air",
        "capacityGB": "256",
        "colorChinese": "黑色",
        "quantity": 1,
    }
    item.update(changes)
    return item


def collector(item):
    return {"run_id": "return-collector-test", "collected_at": NOW,
            "all_sources_ok": True, "records": [item], "blockers": []}


class FakeSync:
    def __init__(self, book, run_id, rows, *, mode="normal", headers=None):
        self.ledger = book
        self.run_id = run_id
        self.max_age_seconds = 600
        self.mode = mode
        self.snapshot = workbook(rows, headers=headers)
        self.write_calls = []
        self.saved = []
        self.prechecks = 0

    def state_token(self):
        stable = copy.deepcopy(self.snapshot)
        stable.pop("captured_at", None)
        return json.dumps(stable, ensure_ascii=False, sort_keys=True)

    def current(self):
        return {"version": "warehouse-airscript-v1",
                "build": return_sync.BUILD, "action": "read", "status": "ok",
                "snapshot": copy.deepcopy(self.snapshot),
                "state_token": self.state_token()}

    def save(self, kind, value):
        self.saved.append((kind, copy.deepcopy(value)))
        return self.ledger.archive(value)

    def precheck(self, current, observed_collector):
        del observed_collector
        self.prechecks += 1
        # A real independent read always has a newer capture timestamp; this
        # must not make a deterministic return operation drift.
        self.snapshot["captured_at"] = datetime.now(timezone.utc).isoformat()
        observed = self.current()
        if observed["state_token"] != current["state_token"]:
            raise ValueError("stale precheck")
        return observed

    def _apply(self, operations):
        target = self.snapshot["sheets"][1]
        for operation in operations:
            matches = [row for row in target["rows"]
                       if row["row_number"] == operation["row_number"]]
            if len(matches) != 1 or matches[0]["values"] != operation["expected_values"]:
                raise ValueError("wrong return target")
            matches[0]["values"] = copy.deepcopy(operation["new_values"])
        self.snapshot["captured_at"] = datetime.now(timezone.utc).isoformat()

    def invoke(self, request):
        self.write_calls.append(copy.deepcopy(request))
        if request["expected_state_token"] != self.state_token():
            raise ValueError("stale state")
        if self.mode == "timeout_before":
            raise TimeoutError("unknown before mutation")
        self._apply(request["operations"])
        if self.mode == "timeout_after":
            raise TimeoutError("unknown after mutation")
        if self.mode == "concurrent_after":
            self.snapshot["sheets"][0]["rows"][0]["values"][10] = "他人并发修改"
        ids = [item["operation_id"] for item in request["operations"]]
        payload = {"version": "warehouse-airscript-v1",
                   "build": return_sync.BUILD, "action": return_sync.ACTION,
                   "status": "applied_pending_readback", "operation_ids": ids,
                   "operation_count": len(ids), "receipts": []}
        return {"http_status": 200,
                "response": {"status": "finished", "error": ""},
                "result": payload}

    def read(self):
        return self.current()


class ReturnSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.book = ledger.Ledger(Path(self.temp.name))
        self.addCleanup(self.book.db.close)
        self.run_id = "return-sync-test"
        self.book.lock(self.run_id)

    def test_append_uses_fixed_mapping_and_isolated_return_operation_table(self):
        sync = FakeSync(self.book, self.run_id, [return_row(3)])
        summary = {}
        observed = return_sync.process(
            sync, sync.current(), collector(record(exactImei="123456789012345")),
            summary)

        self.assertEqual(summary["return_sync"]["status"], "verified")
        self.assertEqual(observed["snapshot"]["sheets"][1]["rows"][0]["values"][1:9],
                         ["WB", "瑞盈8597", "5369901737", "iPhone 17 Air",
                          "256GB", "黑色", "123456789012345", "1"])
        self.assertIn("【自动退货】提货地址：", sync.write_calls[0]["operations"][0]["new_values"][9])
        self.assertEqual(sync.write_calls[0]["action"], "sync_return_rows")
        self.assertEqual(set(sync.write_calls[0]["operations"][0]), {
            "operation_id", "mode", "return_key", "store_alias", "order_id",
            "row_number", "expected_values", "new_values"})
        operation_id = sync.write_calls[0]["operations"][0]["operation_id"]
        self.assertEqual(self.book.return_operation(operation_id)["status"], "committed")
        self.assertEqual(self.book.db.execute(
            "SELECT COUNT(*) FROM operations").fetchone()[0], 0)
        # Identical IDs live safely in independent tables.
        self.book.db.execute(
            """INSERT INTO operations(id,store_alias,order_id,action,status,payload,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (operation_id, "owner-store", "5369901737", "append", "committed",
             "{}", NOW))
        self.book.db.commit()
        self.assertEqual(self.book.db.execute(
            "SELECT COUNT(*) FROM return_operations").fetchone()[0], 1)
        self.assertEqual(self.book.db.execute(
            "SELECT COUNT(*) FROM operations").fetchone()[0], 1)

    def test_append_accepts_template_row_with_sequence_and_WB_only(self):
        template = ["1", "WB"] + [""] * 9
        sync = FakeSync(self.book, self.run_id, [return_row(3, template)])

        return_sync.process(sync, sync.current(), collector(record()), {})

        operation = sync.write_calls[0]["operations"][0]
        self.assertEqual(operation["mode"], "append")
        self.assertEqual(operation["expected_values"], template)
        self.assertEqual(operation["new_values"][1], "WB")

    def test_empty_order_row_rejects_non_WB_platform_or_any_C_and_E_to_K_data(self):
        variants = [(1, "其他平台"), (2, "瑞盈8597")]
        variants.extend((index, "残留业务数据") for index in range(4, 11))
        for case, (index, value) in enumerate(variants):
            with self.subTest(column="ABCDEFGHIJK"[index]):
                root = Path(self.temp.name) / f"partial-template-{case}"
                book = ledger.Ledger(root)
                self.addCleanup(book.db.close)
                run_id = f"partial-template-{case}"
                book.lock(run_id)
                partial = ["1", "WB"] + [""] * 9
                partial[index] = value
                sync = FakeSync(book, run_id, [return_row(3, partial)])
                summary = {}

                return_sync.process(sync, sync.current(), collector(record()), summary)

                self.assertEqual(summary["return_sync"]["status"], "blocked")
                self.assertEqual(sync.write_calls, [])
                self.assertTrue(any(
                    "return_row_has_business_data_without_order_id" in str(blocker)
                    for blocker in summary["return_sync"]["blockers"]
                ))

    def test_update_preserves_manual_imei_and_replaces_only_managed_note_line(self):
        values = ["1", "WB", "瑞盈8597", "5369901737", "iPhone 17 Air",
                  "256GB", "黑色", "999999999999999", "1",
                  "仓库人工回复\n【自动退货】旧地址", "运输中"]
        sync = FakeSync(self.book, self.run_id, [return_row(3, values)])
        return_sync.process(sync, sync.current(), collector(record()), {})
        changed = sync.write_calls[0]["operations"][0]["new_values"]
        self.assertEqual(changed[7], "999999999999999")
        self.assertTrue(changed[9].startswith("仓库人工回复\n【自动退货】"))
        self.assertNotIn("旧地址", changed[9])
        self.assertEqual(changed[10], "待提货")

    def test_update_preserves_manual_H_and_J_whitespace_exactly(self):
        manual_imei = " 999999999999999 "
        manual_note = "  仓库人工回复  \n\n【自动退货】旧地址\n"
        values = ["1", "WB", "瑞盈8597", "5369901737", "iPhone 17 Air",
                  "256GB", "黑色", manual_imei, "1", manual_note, "运输中"]
        sync = FakeSync(self.book, self.run_id, [return_row(3, values)])

        return_sync.process(sync, sync.current(), collector(record()), {})

        operation = sync.write_calls[0]["operations"][0]
        self.assertEqual(operation["expected_values"][7], manual_imei)
        self.assertEqual(operation["expected_values"][9], manual_note)
        self.assertEqual(operation["new_values"][7], manual_imei)
        self.assertEqual(
            return_sync._manual_note_lines(operation["new_values"][9]),
            return_sync._manual_note_lines(manual_note),
        )

    def test_deleted_actionable_row_restores_archive_but_terminal_does_not(self):
        archived_values = ["1", "WB", "瑞盈8597", "5369901737", "iPhone 17 Air",
                           "256GB", "黑色", "999999999999999", "1",
                           "仓库人工回复\n【自动退货】旧事实", "待提货"]
        item = record()
        archived_row = {"sheet": return_sync.TARGET_SHEET, "row_number": 3,
                        "values": archived_values, "order_id": item["orderId"],
                        "store_alias": item["storeAlias"], "occupied": True}
        with self.book.transaction():
            self.book.ingest_return(item, NOW)
            self.book.observe_return_row(item["storeAlias"], item["returnKey"],
                                         item["orderId"], archived_row, NOW,
                                         {"sha256": "fixture"})

        sync = FakeSync(self.book, self.run_id, [return_row(3)])
        summary = {}
        return_sync.process(sync, sync.current(), collector(item), summary)
        operation = sync.write_calls[0]["operations"][0]
        self.assertEqual(operation["mode"], "restore")
        self.assertEqual(operation["new_values"][7], "999999999999999")
        self.assertIn("仓库人工回复", operation["new_values"][9])
        self.assertIn("【自动补回】", operation["new_values"][9])
        self.assertEqual(self.book.return_state(
            item["storeAlias"], item["returnKey"])["restore_count"], 1)

        terminal = record(statusOriginal="Выдано", statusCode="PICKED_UP",
                          statusChinese="已提货", pickupActionRequired=False,
                          isStatusActive=False,
                          completedDt="2026-09-08T12:00:00")
        # Use a separate physical item which has never been placed in the table.
        terminal.update(returnKey="owner-store:777", shkId="777",
                        orderId="5369901777", srid="mp.terminal.r")
        empty = FakeSync(self.book, self.run_id, [return_row(3)])
        result = {}
        returned = return_sync.process(empty, empty.current(), collector(terminal), result)
        self.assertIs(returned["snapshot"] is empty.snapshot, False)
        self.assertEqual(result["return_sync"]["status"], "unchanged")
        self.assertEqual(empty.write_calls, [])
        self.assertEqual(result["return_sync"]["unchanged"][0]["reason"],
                         "terminal_missing_not_restored")

    def test_timeout_after_mutation_is_accepted_by_independent_readback(self):
        sync = FakeSync(self.book, self.run_id, [return_row(3)], mode="timeout_after")
        summary = {}
        return_sync.process(sync, sync.current(), collector(record()), summary)
        self.assertEqual(summary["return_sync"]["status"], "verified")
        self.assertTrue(summary["return_sync"]["updated"][0][
            "reconciled_from_uncertain"])

    def test_timeout_before_mutation_is_never_blindly_retried(self):
        rows = [return_row(3)]
        first = FakeSync(self.book, self.run_id, rows, mode="timeout_before")
        with self.assertRaises(return_sync.ReturnSyncBlocked):
            return_sync.process(first, first.current(), collector(record()), {})
        self.assertEqual(len(first.write_calls), 1)

        second = FakeSync(self.book, self.run_id, rows)
        summary = {}
        with self.assertRaisesRegex(return_sync.ReturnSyncBlocked, "停止自动重试"):
            return_sync.process(second, second.current(), collector(record()), summary)
        self.assertEqual(second.write_calls, [])
        self.assertEqual(summary["return_sync"]["blockers"][0]["reason"],
                         "previous_return_write_uncertain_and_rows_unchanged")

    def test_header_or_unverified_new_product_blocks_before_any_write(self):
        cases = (
            (record(), ["错误"] * 11),
            (record(productIdentityVerified=False), return_sync.HEADERS),
        )
        for index, (item, headers) in enumerate(cases):
            with self.subTest(index=index):
                root = Path(self.temp.name) / f"blocked-{index}"
                book = ledger.Ledger(root)
                self.addCleanup(book.db.close)
                run_id = f"blocked-{index}"
                book.lock(run_id)
                sync = FakeSync(book, run_id, [return_row(3)], headers=headers)
                summary = {}
                try:
                    return_sync.process(sync, sync.current(), collector(item), summary)
                except return_sync.ReturnSyncBlocked:
                    pass
                self.assertEqual(sync.write_calls, [])
                self.assertEqual(summary["return_sync"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
