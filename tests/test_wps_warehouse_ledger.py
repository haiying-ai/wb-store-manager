"""Offline safety tests: no WB, browser, or online sheet access."""
import copy
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "tools/wps_warehouse/ledger.py"
SPEC = importlib.util.spec_from_file_location("warehouse_ledger", MODULE)
ledger_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger_module)
Ledger = ledger_module.Ledger
SHEET = "2026年9月发货"
ORDER_ID = "5589475494"


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root / "archive")
        self.ledger.lock("run-1")
        self.clock = datetime.now(timezone.utc) + timedelta(minutes=1)
        self.order = {"storeAlias": "new-store", "orderId": ORDER_ID,
                      "supplierStatus": "complete", "wbStatus": "waiting",
                      "warehouseActionRequired": True, "createdAt": "2026-08-26T11:14:03Z",
                      "productModel": "iPhone 14 Pro", "capacityGB": 128,
                      "colorChinese": "黑色", "productIdentityVerified": True}

    def tearDown(self):
        self.ledger.db.close()
        self.temp.cleanup()

    def stamp(self):
        self.clock += timedelta(seconds=1)
        return self.clock.isoformat()

    def collector(self, order=None, ok=True):
        return {"run_id": "run-1", "collected_at": self.stamp(), "all_sources_ok": ok,
                "orders": [copy.deepcopy(order or self.order)]}

    def snapshot(self, values=None, sheet=SHEET, complete=True):
        return {"captured_at": self.stamp(), "complete": complete,
                "sheets": [{"name": sheet, "rows": [] if values is None else [{"row_number": 3, "values": values}]}]}

    def plan(self, collector=None, snapshot=None):
        return self.ledger.plan(collector or self.collector(), snapshot or self.snapshot(), SHEET)

    def add_assets(self):
        for field, name in (("labelImagePath", "label.png"), ("supplyQrImagePath", "qr.png")):
            path = self.root / name
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
            self.order[field] = str(path)

    def install_baseline(self, assets=False):
        if assets:
            self.add_assets()
        operation = self.plan()["operations"][0]
        values = operation["values"][:]
        if assets:
            values[8:10] = ['=DISPIMG("ID_LABEL",1)', '=DISPIMG("ID_QR",1)']
        refs = {"order_sticker": values[8], "supply_qr": values[9]} if assets else None
        self.ledger.commit(operation["operation_id"], self.snapshot(values), refs)
        return values

    def test_restore_keeps_latest_manual_values_but_uses_platform_core(self):
        values = self.install_baseline(assets=True)
        edited = values[:]
        edited[4] = "incorrect manually edited model"
        edited[7] = "356000000000001"
        edited[8] = '=DISPIMG("UNVERIFIED_MANUAL_IMAGE",1)'
        edited[10] = "仓库核对：待配件"
        self.plan(snapshot=self.snapshot(edited))
        plan = self.plan()
        op = plan["operations"][0]
        self.assertEqual(op["action"], "restore")
        self.assertEqual(op["values"][4], "iPhone 14 Pro")
        self.assertEqual(op["values"][7], edited[7])
        self.assertEqual(op["values"][8], values[8])
        self.assertIn(edited[10], op["values"][10])
        self.assertIn("原表记录缺失，仍待仓库处理，自动补回，第1次", op["values"][10])
        self.assertIn("complete/waiting", op["values"][10])
        self.assertNotIn("被别人删除", op["values"][10])
        event = self.ledger.db.execute("SELECT body FROM events WHERE kind='sheet_row_observed' ORDER BY sequence DESC LIMIT 1").fetchone()
        self.assertEqual(json.loads(event[0])["before"]["values"][4], "iPhone 14 Pro")
        self.assertEqual(json.loads(event[0])["after"]["values"][7], edited[7])

    def test_observed_external_row_deleted_while_pending_is_restored(self):
        values = ["1", "2026/8/26", "Скиди Выбор（扬州沫德）", ORDER_ID,
                  "iPhone 14 Pro", "128GB", "黑色", "356000000000001", "", "", "仓库核对：待配件"]
        observed = self.plan(snapshot=self.snapshot(values))
        self.assertEqual(observed["operations"], [])
        self.assertFalse(self.ledger.state("new-store", ORDER_ID)["ever_synced"])
        self.assertEqual(self.ledger.summary()["restorations"], 0)
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM events WHERE kind='operation_committed'").fetchone()[0], 0)

        op = self.plan()["operations"][0]
        self.assertEqual(op["action"], "restore")
        self.assertEqual(op["restore_number"], 1)
        self.assertEqual(op["values"][7], values[7])
        self.assertIn(values[10], op["values"][10])
        self.assertIn("原表记录缺失，仍待仓库处理，自动补回，第1次", op["values"][10])
        self.assertEqual(self.ledger.summary()["restorations"], 0)
        self.assertEqual(self.ledger.commit(op["operation_id"], self.snapshot(op["values"]))["restore_count"], 1)

    def test_pending_field_notes_use_chinese_and_hide_unknown_technical_keys(self):
        self.order["pendingFields"] = ["wbLabelCode", "labelImagePath", "supplyQrImagePath", "supplyId", "unverifiedInternalKey"]
        note = self.plan()["operations"][0]["values"][10]
        self.assertIn("待补：WB标签编号、WB标签图片、供货二维码、供货单号、其他信息（尚未核实）", note)
        for key in self.order["pendingFields"]:
            self.assertNotIn(key, note)

    def test_retry_same_operation_and_only_verified_commit_increments_count(self):
        self.install_baseline(assets=True)
        op = self.plan()["operations"][0]
        retry = self.plan()["operations"][0]
        self.assertEqual(op["operation_id"], retry["operation_id"])
        self.assertEqual(self.ledger.summary()["restorations"], 0)
        bad = retry["values"][:]
        bad[8] = ""
        with self.assertRaisesRegex(ValueError, "image reference"):
            self.ledger.commit(op["operation_id"], self.snapshot(bad))
        self.assertEqual(self.ledger.summary()["restorations"], 0)
        committed = self.ledger.commit(op["operation_id"], self.snapshot(retry["values"]))
        self.assertEqual(committed["restore_count"], 1)
        self.assertEqual(self.ledger.commit(op["operation_id"], self.snapshot(retry["values"]))["status"], "already_committed")
        again = self.plan()["operations"][0]
        self.assertEqual(again["restore_number"], 2)
        self.assertNotEqual(again["operation_id"], op["operation_id"])

    def test_incomplete_or_ambiguous_evidence_never_generates_write(self):
        self.assertFalse(self.plan(collector=self.collector(ok=False))["write_allowed"])
        self.assertFalse(self.plan(snapshot=self.snapshot(complete=False))["write_allowed"])
        for identifier in ("5.589475494E9", "5589475494.0", 5589475494.0):
            values = ["1", "", "", identifier]
            result = self.plan(snapshot=self.snapshot(values))
            self.assertFalse(result["write_allowed"])
            self.assertIn("ambiguous", result["blockers"][0])

    def test_restore_commit_rejects_lost_or_changed_manual_imei_and_note(self):
        values = self.install_baseline()
        values[7], values[10] = "356000000000001", "仓库核对：待配件"
        self.plan(snapshot=self.snapshot(values))
        operation = self.plan()["operations"][0]
        for changed in ("", "356000000000002"):
            bad = operation["values"][:]
            bad[7] = changed
            with self.assertRaisesRegex(ValueError, "manual IMEI"):
                self.ledger.commit(operation["operation_id"], self.snapshot(bad))
        bad = operation["values"][:]
        bad[10] = "原表记录缺失，仍待仓库处理，自动补回，第1次"
        with self.assertRaisesRegex(ValueError, "manual note"):
            self.ledger.commit(operation["operation_id"], self.snapshot(bad))
        self.assertEqual(self.ledger.summary()["restorations"], 0)

    def test_collector_metadata_blockers_cannot_be_overridden_by_color_fallback(self):
        order = {**self.order, "colorChinese": "", "colorOriginal": "черный",
                 "metadataBlockers": ["product_identity_unverified"], "readyForWps": False}
        self.assertFalse(self.plan(collector=self.collector(order))["write_allowed"])

    def test_cross_month_dedup_and_store_conflict(self):
        values = self.install_baseline()
        snapshot = self.snapshot(values, sheet="2026年8月发货")
        snapshot["sheets"].append({"name": SHEET, "rows": []})
        self.assertEqual(self.plan(snapshot=snapshot)["operations"], [])
        values[2] = "Скиди（香港瑞盈）"
        result = self.plan(snapshot=self.snapshot(values))
        self.assertFalse(result["write_allowed"])
        self.assertEqual(result["blockers"][0]["reason"], "store_identity_conflict")

    def test_shipped_cancelled_and_unknown_do_not_restore(self):
        self.install_baseline()
        for action, status in ((False, "sorted"), (False, "canceled"), (None, "unrecognized")):
            order = {**self.order, "warehouseActionRequired": action, "wbStatus": status}
            result = self.plan(collector=self.collector(order))
            self.assertEqual(result["operations"], [])
        self.assertEqual(self.ledger.summary()["restorations"], 0)

    def test_verified_pre_shipment_retirement_preserves_archive_and_never_restores(self):
        values = self.install_baseline()
        archived = json.loads(self.ledger.state("new-store", ORDER_ID)["latest_row"])
        retired = self.ledger.retire_row(
            "new-store", ORDER_ID,
            reason_code="PRE_SHIPMENT_CANCELLATION_VERIFIED",
            run_id="run-1",
            row_snapshot=archived,
            evidence={"sha256": "a" * 64, "status": "confirm/canceled_by_client"},
        )
        self.assertEqual(retired["status"], "retired")
        self.assertEqual(self.ledger.summary()["retired_rows"], 1)
        self.assertEqual(
            json.loads(self.ledger.state("new-store", ORDER_ID)["latest_row"])["values"],
            values,
        )
        # Even an unexpected later actionable flag cannot silently resurrect a
        # row that was deliberately removed under verified cancellation proof.
        result = self.plan(collector=self.collector({**self.order, "warehouseActionRequired": True}))
        self.assertEqual(result["operations"], [])
        self.assertEqual(result["requires_commit"], [])
        self.assertEqual(
            result["blockers"][0]["reason"],
            "retired_cancellation_is_no_longer_terminal_review_required",
        )
        event = self.ledger.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='pre_shipment_cancellation_row_deleted'"
        ).fetchone()[0]
        self.assertEqual(event, 1)

    def test_uncommitted_existing_row_requires_readback_commit_without_second_append(self):
        op = self.plan()["operations"][0]
        next_plan = self.plan(snapshot=self.snapshot(op["values"]))
        self.assertEqual(next_plan["operations"], [])
        self.assertEqual(next_plan["requires_commit"][0]["operation_id"], op["operation_id"])
        self.assertFalse(self.ledger.state("new-store", ORDER_ID)["ever_synced"])

    def test_lock_covers_runs_and_never_steals_expired_lock(self):
        with self.assertRaisesRegex(ValueError, "another run"):
            self.ledger.lock("run-2")
        lock = self.ledger.summary()["run_lock"]
        lock["expires_at"] = "2000-01-01T00:00:00+00:00"
        with self.ledger.transaction():
            self.ledger.db.execute("UPDATE meta SET value=? WHERE key='run_lock'", (json.dumps(lock),))
        with self.assertRaisesRegex(ValueError, "never silently stolen"):
            self.ledger.lock("run-2")
        self.assertFalse(self.plan()["write_allowed"])
        self.ledger.lock("run-1")
        self.assertTrue(self.plan()["write_allowed"])
        self.ledger.unlock("run-1")
        self.assertEqual(self.ledger.lock("run-2")["run_id"], "run-2")

    def test_archived_images_survive_original_files_removed(self):
        self.add_assets()
        op = self.plan()["operations"][0]
        for field in ("labelImagePath", "supplyQrImagePath"):
            Path(self.order[field]).unlink()
        self.assertTrue(self.plan()["write_allowed"])
        for kind in ("order_sticker", "supply_qr"):
            asset = op["assets"][kind]
            self.assertTrue(Path(asset["absolute_path"]).is_file())
            self.assertTrue((self.ledger.root / asset["path"]).is_file())

    def test_L_feedback_history_is_archived_without_changing_order_fields(self):
        values = self.install_baseline()
        snapshot = self.snapshot(values)
        snapshot["warehouse_feedback_complete"] = True
        row = snapshot["sheets"][0]["rows"][0]
        row.update(warehouse_feedback="仓库：请核实平台是否已接收", warehouse_feedback_formula="")
        self.assertEqual(self.plan(snapshot=snapshot)["blockers"], [])
        saved = json.loads(self.ledger.state("new-store", ORDER_ID)["latest_row"])
        self.assertEqual(saved["warehouse_feedback"], row["warehouse_feedback"])
        self.assertEqual(saved["values"], values)
        snapshot["captured_at"] = self.stamp()
        row["warehouse_feedback"] = "仓库：补充了第二条说明"
        self.plan(snapshot=snapshot)
        event = self.ledger.db.execute("SELECT body FROM events WHERE kind='sheet_row_observed' ORDER BY sequence DESC LIMIT 1").fetchone()
        archived = json.loads(event[0])
        self.assertIn("请核实", archived["before"]["warehouse_feedback"])
        self.assertIn("第二条", archived["after"]["warehouse_feedback"])

    def test_complete_L_coverage_cannot_fabricate_missing_feedback(self):
        values = self.install_baseline()
        snapshot = self.snapshot(values)
        snapshot["warehouse_feedback_complete"] = True
        with self.assertRaisesRegex(ValueError, "feedback fields"):
            self.ledger.validate_snapshot(snapshot)


if __name__ == "__main__":
    unittest.main()
