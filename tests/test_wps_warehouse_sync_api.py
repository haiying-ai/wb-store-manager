"""Offline adapter tests. Fake API only; no credentials, network, or desktop."""
import base64
import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from tools.wps_warehouse import sync_api as api
from tools.wps_warehouse import return_sync

SHEET = "2026年9月发货"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class FakeCloud:
    def __init__(self):
        self.rows = []
        self.calls = []
        self.reads = 0
        self.on_read = None
        self.write_mode = "normal"
        self.fail_read_after_write = False
        self.applied = False
        self.build = "20260908-r11"

    def result(self, action="read", status="ok", **extra):
        sheets = [{"name": SHEET, "headers": api.HEADERS, "rows": copy.deepcopy(self.rows)}]
        return {"version": api.VERSION, "build": self.build,
                "action": action, "status": status,
                "snapshot": {"captured_at": api.ledger.now(), "complete": True, "workbook_url": api.WORKBOOK_URL, "sheets": sheets},
                "state_token": json.dumps(sheets, ensure_ascii=False), **extra}

    def __call__(self, argv):
        self.calls.append(copy.deepcopy(argv))
        action = argv["action"]
        if action == "read":
            self.reads += 1
            if self.on_read:
                self.on_read(self)
            if self.applied and self.fail_read_after_write:
                self.fail_read_after_write = False
                raise TimeoutError("fake read timeout")
            result = self.result()
        else:
            if argv["expected_state_token"] != self.result()["state_token"]:
                raise ValueError("fake concurrency check")
            if action == "delete_pre_shipment_cancellations":
                for operation in sorted(argv["operations"], key=lambda item: item["row_number"], reverse=True):
                    self.rows = [row for row in self.rows if not (
                        row["row_number"] == operation["row_number"]
                        and row["values"][3] == operation["order_id"])]
                    for row in self.rows:
                        if row["row_number"] > operation["row_number"]:
                            row["row_number"] -= 1
                self.applied = True
                result = self.result(
                    action, "applied_pending_readback",
                    request_id=argv["request_id"], receipts=[],
                )
                return {"http_status": 200,
                        "response": {"status": "finished", "error": ""},
                        "result": result}
            op = argv["operation"]
            if self.write_mode == "timeout_without_row":
                raise TimeoutError("fake ambiguous write timeout")
            if action == "apply":
                values = op["values"][:]
                values[8:10] = ["", ""]
                self.rows.append({"row_number": op["row_number"], "values": values})
                self.applied = True
            row = next(r for r in self.rows if r["values"][3] == op["order_id"])
            inserted = []
            for kind, index in api.IMAGE_COLUMNS.items():
                if kind not in argv["images"] or row["values"][index]:
                    continue
                if self.write_mode == "partial_image" and action == "apply" and kind == "supply_qr":
                    continue
                if self.write_mode == "partial_fill" and action == "repair_images" and kind == "supply_qr":
                    continue
                row["values"][index] = '=DISPIMG("ID_' + kind.upper() + '",1)'
                inserted.append(kind)
            if self.write_mode == "timeout_with_row":
                raise TimeoutError("fake write timeout after mutation")
            refs = {kind: row["values"][index] for kind, index in api.IMAGE_COLUMNS.items() if row["values"][index]}
            partial = (self.write_mode == "partial_image" and action == "apply") or (self.write_mode == "partial_fill" and action == "repair_images")
            result = self.result(action, "partial" if partial else "verified",
                                 operation_id=op["operation_id"], image_refs=refs, inserted_image_kinds=inserted)
        return {"http_status": 200, "response": {"status": "finished", "error": ""}, "result": result}


class ApiAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cloud = FakeCloud()
        self.runners = []
        self.collect_calls = 0
        self.order = {"storeAlias": "new-store", "orderId": "5589475494", "supplierStatus": "complete", "wbStatus": "waiting",
                      "warehouseActionRequired": True, "createdAt": "2026-08-26T11:14:03Z", "productModel": "iPhone 14 Pro",
                      "capacityGB": 128, "colorChinese": "黑色", "productIdentityVerified": True}

    def tearDown(self):
        for runner in self.runners:
            runner.ledger.db.close()
        self.temp.cleanup()

    def runner(self, run_id, invoke=None, *, active_shipment=True):
        def collect_orders():
            self.collect_calls += 1
            return {"run_id": run_id, "all_sources_ok": True, "collected_at": api.ledger.now(), "orders": [copy.deepcopy(self.order)],
                    "store_results": [{"store_alias": "new-store", "ok": True}, {"store_alias": "owner-store", "ok": True}]}
        runner = api.ApiSync(
            self.root,
            invoke=invoke or self.cloud,
            collect_orders=collect_orders,
            run_id=run_id,
            target_sheet=SHEET,
            allow_active_fulfillment_for_tests=active_shipment,
        )
        # Most legacy adapter tests exercise the explicitly enabled fulfillment
        # implementation. Production/default behavior is covered separately.
        if active_shipment:
            runner.shipment_mode = api.ACTIVE_FULFILLMENT_MODE
            runner.label_sync_enabled = True
            runner.image_fill_enabled = True
        self.runners.append(runner)
        return runner

    def enable_returns(self):
        archive_root = self.root / "return-archive"
        (self.root / "monitor.json").write_text(json.dumps({
            "return_workflow": {
                "enabled": True,
                "archive_root": str(archive_root),
            }
        }), encoding="utf-8")
        return archive_root.resolve()

    def return_result(self, *, all_sources_ok=True):
        record = {
            "storeAlias": "owner-store",
            "returnKey": "owner-store:1234567890",
            "shkId": "1234567890",
            "orderId": self.order["orderId"],
            "identityComplete": True,
            "storeIdentityMatches": True,
            "pickupActionRequired": True,
            "statusDefinitive": True,
            "productModel": "iPhone 16 Pro",
            "capacityGB": 256,
            "colorChinese": "白色",
            "productIdentityVerified": True,
            "metadataEvidencePath": "content-card-evidence.json",
            "metadataBlockers": [],
            "tableReady": True,
        }
        return {
            "schema_version": 1,
            "run_id": "return-fixture",
            "collected_at": api.ledger.now(),
            "all_sources_ok": all_sources_ok,
            "stores": [
                {"store_alias": "owner-store", "complete": all_sources_ok,
                 "windows": [{"date_from": "2026-08-09", "date_to": "2026-09-08"}],
                 "record_count": 1},
                {"store_alias": "new-store", "complete": all_sources_ok,
                 "windows": [{"date_from": "2026-08-09", "date_to": "2026-09-08"}],
                 "record_count": 0},
            ],
            "records": [record],
            "active_records": [record],
            "pickup_records": [record],
            "blockers": [] if all_sources_ok else [{"reason": "fixture_incomplete"}],
            "collector_path": str(self.root / "return-archive" / "collector.json"),
            "collector_sha256": "a" * 64,
        }

    def assets(self):
        for kind, field in (("label", "labelImagePath"), ("qr", "supplyQrImagePath")):
            path = self.root / (kind + ".png")
            path.write_bytes(PNG)
            self.order[field] = str(path)

    def writes(self):
        return [c for c in self.cloud.calls if c["action"] != "read"]

    def test_passive_mode_overrides_legacy_flags_and_only_tracks_table_status(self):
        (self.root / "monitor.json").write_text(json.dumps({
            "shipment_workflow": {"enabled": True, "prepare_enabled": True},
            "order_status_sync": {"enabled": True},
        }), encoding="utf-8")
        self.order.update(orderId="900000089", supplierStatus="new")
        self.assets()  # Old local assets must not be inserted on a passive append.
        runner = self.runner("passive-tracking", active_shipment=False)
        self.assertFalse(runner.prepare_enabled)
        self.assertFalse(runner.imei_workflow)
        self.assertFalse(runner.label_sync_enabled)
        self.assertFalse(runner.image_fill_enabled)
        # Mode is the final safety gate even if stale code flips old booleans.
        runner.prepare_enabled = True
        runner.imei_workflow = True
        runner.label_sync_enabled = True
        runner.image_fill_enabled = True

        def sync_status(actual, current, collector, summary, return_collector):
            summary["order_status_sync"] = {"updated": [], "unchanged": ["900000089"]}
            return current

        with patch.object(api.prepare, "process") as prepare_call, \
                patch.object(api.imei_sync, "process") as imei_call, \
                patch.object(api.imei_sync, "fetch_verified_labels") as label_call, \
                patch.object(api.image_fill, "fill_existing") as image_call, \
                patch.object(api.status_sync, "process", side_effect=sync_status) as status_call:
            result = runner.run(live=True)

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["shipment_mode"], api.PASSIVE_TRACKING_MODE)
        self.assertEqual(self.cloud.rows[0]["values"][3], "900000089")
        self.assertEqual(self.cloud.rows[0]["values"][8:10], ["", ""])
        self.assertEqual(self.cloud.rows[0]["values"][10],
                         "待仓库自行处理；系统仅同步WB状态")
        prepare_call.assert_not_called()
        imei_call.assert_not_called()
        label_call.assert_not_called()
        image_call.assert_not_called()
        status_call.assert_called_once()

    def test_passive_collector_defers_all_label_downloads(self):
        runner = self.runner("passive-collector", active_shipment=False)
        runner.current_table_orders = [("owner-store", "5770227108")]
        with patch.object(api.collect, "run", return_value={}) as collect_call:
            runner._collect()
        args = collect_call.call_args.args[0]
        self.assertIs(args.defer_label_downloads, True)
        self.assertEqual(args.force_order, ["owner-store:5770227108"])
        self.assertEqual(args.store, ["owner-store", "new-store"])

    def test_configured_owner_store_filters_collection_and_table_forcing(self):
        (self.root / "monitor.json").write_text(json.dumps({
            "stores": ["owner-store"],
            "shipment_workflow": {"mode": api.PASSIVE_TRACKING_MODE},
        }), encoding="utf-8")
        runner = api.ApiSync(
            self.root, invoke=self.cloud, run_id="owner-only-collection",
            target_sheet=SHEET,
        )
        self.runners.append(runner)
        self.cloud.rows = [
            {"row_number": 3, "values": ["1", "2026/09/01", "沫德DL05", "101",
                                           "iPhone", "128GB", "黑色", "", "", "", ""]},
            {"row_number": 4, "values": ["2", "2026/09/01", "瑞盈8597", "102",
                                           "iPhone", "128GB", "黑色", "", "", "", ""]},
        ]
        runner.current_table_orders = runner.table_order_keys(
            self.cloud.result()["snapshot"]
        )

        with patch.object(api.collect, "run", return_value={}) as collect_call:
            runner._collect()

        args = collect_call.call_args.args[0]
        self.assertEqual(args.store, ["owner-store"])
        self.assertEqual(args.force_order, ["owner-store:102"])

    def test_feedback_summary_excludes_inactive_store_rows(self):
        (self.root / "monitor.json").write_text(json.dumps({
            "stores": ["owner-store"],
            "shipment_workflow": {"mode": api.PASSIVE_TRACKING_MODE},
        }), encoding="utf-8")
        runner = api.ApiSync(
            self.root, invoke=self.cloud, run_id="owner-only-feedback",
            target_sheet=SHEET,
        )
        self.runners.append(runner)
        runner.directory.mkdir(parents=True)
        self.cloud.rows = [
            {"row_number": 3, "values": ["1", "2026/09/01", "沫德DL05", "101",
                                           "iPhone", "128GB", "黑色", "", "", "", ""],
             "warehouse_feedback": "新店历史回复", "warehouse_feedback_formula": ""},
            {"row_number": 4, "values": ["2", "2026/09/01", "瑞盈8597", "102",
                                           "iPhone", "128GB", "黑色", "", "", "", ""],
             "warehouse_feedback": "老店当前回复", "warehouse_feedback_formula": ""},
        ]
        current = self.cloud.result()
        current["snapshot"]["warehouse_feedback_complete"] = True

        result = runner.feedback_summary(current)

        self.assertEqual([(entry["store_alias"], entry["order_id"])
                          for entry in result["entries"]],
                         [("owner-store", "102")])

    def test_manual_only_live_without_explicit_trigger_is_local_noop(self):
        (self.root / "monitor.json").write_text(json.dumps({
            "stores": ["owner-store"],
            "trigger_mode": "manual_only",
            "shipment_workflow": {"mode": api.PASSIVE_TRACKING_MODE},
        }), encoding="utf-8")

        def unexpected_collection():
            raise AssertionError("manual-only no-op must not collect WB")

        runner = api.ApiSync(
            self.root, invoke=lambda argv: (_ for _ in ()).throw(
                AssertionError("manual-only no-op must not read WPS")),
            collect_orders=unexpected_collection,
            run_id="manual-only-disabled", target_sheet=SHEET,
        )
        self.runners.append(runner)

        result = runner.run(live=True)

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["reason"], "manual_trigger_required")
        self.assertEqual(result["configured_stores"], ["owner-store"])
        self.assertEqual(result["request_counts"]["wps"], 0)
        self.assertFalse(runner.directory.exists())

    def test_manual_only_explicit_trigger_proceeds_for_owner_store(self):
        (self.root / "monitor.json").write_text(json.dumps({
            "stores": ["owner-store"],
            "trigger_mode": "manual_only",
            "shipment_workflow": {"mode": api.PASSIVE_TRACKING_MODE},
        }), encoding="utf-8")
        run_id = "manual-only-explicit"

        def owner_collection():
            return {
                "run_id": run_id, "all_sources_ok": True,
                "collected_at": api.ledger.now(), "orders": [],
                "store_results": [{"store_alias": "owner-store", "ok": True}],
            }

        runner = api.ApiSync(
            self.root, invoke=self.cloud, collect_orders=owner_collection,
            run_id=run_id, target_sheet=SHEET,
        )
        self.runners.append(runner)

        result = runner.run(live=True, manual_trigger=True)

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["configured_stores"], ["owner-store"])
        self.assertGreaterEqual(len(self.cloud.calls), 2)

    def test_production_constructor_rejects_active_mode_from_config(self):
        (self.root / "monitor.json").write_text(json.dumps({
            "shipment_workflow": {
                "mode": api.ACTIVE_FULFILLMENT_MODE,
                "enabled": True,
                "prepare_enabled": True,
            },
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "passive-only production policy"):
            api.ApiSync(self.root, invoke=self.cloud, collect_orders=lambda: {},
                        run_id="active-config-rejected", target_sheet=SHEET)

    def test_passive_restore_keeps_shipment_image_columns_empty(self):
        self.order.update(orderId="900000088", supplierStatus="new")
        self.assets()
        first = self.runner("active-row-before-passive")
        self.assertEqual(first.run(live=True)["status"], "ok")
        self.assertTrue(self.cloud.rows[0]["values"][8])
        self.assertTrue(self.cloud.rows[0]["values"][9])
        self.cloud.rows = []

        passive = self.runner("passive-restore", active_shipment=False)
        with patch.object(api.image_fill, "fill_existing") as image_call:
            result = passive.run(live=True)

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["committed"][0]["action"], "restore")
        self.assertEqual(self.cloud.rows[0]["values"][8:10], ["", ""])
        self.assertNotIn("repair_images", [call["action"] for call in self.cloud.calls])
        image_call.assert_not_called()

    def test_return_workflow_uses_full_collection_same_lock_and_runs_before_prepare(self):
        return_root = self.enable_returns()
        self.order.update(storeAlias="owner-store", orderId="900000090", supplierStatus="new")
        runner = self.runner("returns-stage-order")
        runner.prepare_enabled = True
        runner.order_status_sync_enabled = True
        stages = []
        complete_return_collector = self.return_result()

        def collect_returns(*, root, store_aliases, shipping_orders):
            stages.append("return_collection")
            self.assertEqual(Path(root), return_root)
            self.assertEqual(tuple(store_aliases), ("owner-store", "new-store"))
            self.assertEqual(self.cloud.reads, 1)
            runner.ledger.require_lock(runner.run_id)
            self.assertEqual(len(shipping_orders), 1)
            self.assertEqual(shipping_orders[0]["orderId"], "900000090")
            self.assertEqual(shipping_orders[0]["productModel"], "iPhone 14 Pro")
            return complete_return_collector

        def sync_returns(actual, current, return_collector, summary):
            stages.append("return_table_sync")
            actual.ledger.require_lock(actual.run_id)
            self.assertEqual(return_collector["pickup_records"][0]["returnKey"],
                             "owner-store:1234567890")
            linked = return_collector["records"][0]
            self.assertIs(linked["productIdentityVerified"], True)
            self.assertEqual(linked["metadataEvidencePath"],
                             "content-card-evidence.json")
            self.assertEqual(
                [linked["productModel"], linked["capacityGB"], linked["colorChinese"]],
                ["iPhone 16 Pro", 256, "白色"],
            )
            self.assertEqual(self.cloud.rows[0]["values"][3], "900000090")
            self.assertEqual(self.cloud.calls[-1]["action"], "read")
            summary["return_sync"] = {"status": "unchanged", "blockers": []}
            return current

        def prepare_after_returns(actual, current, collector, summary):
            stages.append("preparation")
            self.assertEqual(stages[-2], "return_table_sync")
            summary["preparation"] = []
            return current

        def status_after_prepare(actual, current, collector, summary,
                                 return_collector):
            stages.append("order_status")
            self.assertIs(return_collector, complete_return_collector)
            self.assertEqual(stages[-2], "preparation")
            summary["order_status_sync"] = {"status": "unchanged"}
            return current

        with patch.object(api.return_collect, "collect_returns", side_effect=collect_returns), \
                patch.object(return_sync, "process", side_effect=sync_returns), \
                patch.object(api.prepare, "process", side_effect=prepare_after_returns), \
                patch.object(api.status_sync, "process", side_effect=status_after_prepare):
            result = runner.run(live=True)

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(stages, ["return_collection", "return_table_sync", "preparation",
                                  "order_status"])
        self.assertEqual(result["return_blockers"], [])
        self.assertEqual(result["return_collection"]["pickup_record_count"], 1)
        self.assertEqual(result["return_collection"]["product_linked_record_count"], 1)
        self.assertEqual(result["return_collection"]["request_count"], 2)
        self.assertIn("initial_wps_state_sha256", result["return_collection"])
        self.assertIn("return_collection", result["timings_seconds"])
        self.assertIn("return_table_sync", result["timings_seconds"])
        self.assertEqual(result["request_counts"]["wb_returns"], 2)
        self.assertTrue(list((self.root / "api-runs/returns-stage-order").glob(
            "*-return-collection-summary.json")))

    def test_disabled_return_workflow_does_not_collect_or_sync(self):
        runner = self.runner("returns-disabled")
        with patch.object(api.return_collect, "collect_returns") as collect_call, \
                patch.object(return_sync, "process") as sync_call:
            result = runner.run(live=True)
        self.assertEqual(result["status"], "ok", result)
        collect_call.assert_not_called()
        sync_call.assert_not_called()
        self.assertNotIn("return_collection", result["timings_seconds"])
        self.assertNotIn("return_table_sync", result["timings_seconds"])
        self.assertEqual(result["request_counts"]["wb_returns"], 0)

    def test_content_verified_return_survives_unverified_shipping_product(self):
        self.enable_returns()
        self.order.update(
            storeAlias="owner-store",
            orderId="900000093",
            warehouseActionRequired=False,
            productIdentityVerified=False,
            productModel=None,
            capacityGB=None,
            colorChinese=None,
        )
        collector = self.return_result()
        record = collector["records"][0]
        runner = self.runner("returns-content-product-authority")

        def accept_verified_content(actual, current, returned, summary):
            self.assertIs(returned, collector)
            self.assertIs(returned["records"][0], returned["pickup_records"][0])
            item = returned["records"][0]
            self.assertIs(item["productIdentityVerified"], True)
            self.assertEqual(
                [item["productModel"], item["capacityGB"], item["colorChinese"]],
                ["iPhone 16 Pro", 256, "白色"],
            )
            self.assertEqual(item["metadataEvidencePath"],
                             "content-card-evidence.json")
            summary["return_sync"] = {"status": "unchanged", "blockers": []}
            return current

        with patch.object(api.return_collect, "collect_returns",
                          return_value=collector), patch.object(
                return_sync, "process", side_effect=accept_verified_content,
        ) as sync_call:
            result = runner.run(live=True)

        self.assertEqual(result["status"], "ok", result)
        self.assertIs(record["productIdentityVerified"], True)
        self.assertEqual(result["return_collection"]["product_linked_record_count"], 1)
        sync_call.assert_called_once()

    def test_read_only_check_never_collects_returns_even_when_enabled(self):
        self.enable_returns()
        runner = self.runner("returns-read-only")
        with patch.object(api.return_collect, "collect_returns") as collect_call, \
                patch.object(return_sync, "process") as sync_call:
            result = runner.run(live=False)
        self.assertEqual(result["status"], "ready", result)
        collect_call.assert_not_called()
        sync_call.assert_not_called()

    def test_return_collection_failure_isolated_from_shipping_and_prepare(self):
        self.enable_returns()
        self.order.update(orderId="900000091", supplierStatus="new")
        runner = self.runner("returns-collection-failure")
        runner.prepare_enabled = True

        def prepare_still_runs(actual, current, collector, summary):
            summary["preparation"] = []
            return current

        with patch.object(
                api.return_collect,
                "collect_returns",
                side_effect=api.return_collect.ReturnCollectError("fixture collection failed"),
        ), patch.object(return_sync, "process") as sync_call, patch.object(
                api.prepare, "process", side_effect=prepare_still_runs,
        ) as prepare_call:
            result = runner.run(live=True)

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(len(result["committed"]), 1)
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["return_blockers"][0]["reason"],
                         "return_collection_failed")
        self.assertEqual(result["return_blockers"][0]["detail"],
                         "fixture collection failed")
        self.assertIn("return_collection", result["timings_seconds"])
        self.assertEqual(result["timings_seconds"]["return_table_sync"], 0.0)
        self.assertEqual(result["request_counts"]["wb_returns"], 0)
        sync_call.assert_not_called()
        prepare_call.assert_called_once()

    def test_return_sync_failure_isolated_from_shipping_and_prepare(self):
        self.enable_returns()
        self.order.update(orderId="900000092", supplierStatus="new")
        runner = self.runner("returns-sync-failure")
        runner.prepare_enabled = True

        def prepare_still_runs(actual, current, collector, summary):
            summary["preparation"] = []
            return current

        with patch.object(api.return_collect, "collect_returns",
                          return_value=self.return_result()), patch.object(
                return_sync,
                "process",
                side_effect=return_sync.ReturnSyncBlocked("fixture WPS return failure"),
        ), patch.object(api.prepare, "process", side_effect=prepare_still_runs) as prepare_call:
            result = runner.run(live=True)

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(len(result["committed"]), 1)
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["return_blockers"][0]["reason"], "return_sync_failed")
        self.assertEqual(result["return_blockers"][0]["detail"],
                         "fixture WPS return failure")
        self.assertIn("return_table_sync", result["timings_seconds"])
        self.assertEqual(result["request_counts"]["wb_returns"], 2)
        prepare_call.assert_called_once()

    def test_old_airscript_return_notice_does_not_break_shipping(self):
        self.enable_returns()
        self.cloud.build = "20260907-r10"
        runner = self.runner("returns-old-build")
        with patch.object(api.return_collect, "collect_returns",
                          return_value=self.return_result()):
            result = runner.run(live=True)

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(len(result["committed"]), 1)
        self.assertEqual(result["return_sync"]["status"], "needs_script_update")
        self.assertIn("请保存主1.0脚本20260908-r11后重试",
                      result["return_sync"]["blockers"][0]["reason"])
        self.assertEqual(result["return_blockers"][0]["reason"],
                         "return_sync_needs_script_update")

    def test_prepare_runs_only_after_independent_table_commit(self):
        self.order.update(orderId="900000101", supplierStatus="new")
        runner = self.runner("prepare-after-table")
        runner.prepare_enabled = True

        def prepare_after_commit(actual, current, collector, summary):
            self.assertEqual(len(summary["committed"]), 1)
            self.assertEqual(self.cloud.calls[-1]["action"], "read")
            self.assertEqual(self.cloud.rows[0]["values"][3], "900000101")
            summary["preparation"] = [{"store_alias": "new-store", "order_id": "900000101", "stage": "PREPARED"}]
            return current

        with patch.object(api.prepare, "process", side_effect=prepare_after_commit) as prepare_call:
            result = runner.run(live=True)
        self.assertEqual(result["status"], "ok")
        prepare_call.assert_called_once()

    def test_verified_pre_shipment_cancel_is_deleted_before_restore_planning(self):
        oid = "5685827269"
        self.cloud.rows = [{"row_number": 3, "values": [
            "1", "2026/09/07", "沫德DL05", oid, "iPhone 16", "256GB",
            "黑色", "", "", "", "待仓库处理",
        ]}]
        self.order.update(
            orderId=oid, supplierStatus="confirm", wbStatus="canceled_by_client",
            warehouseActionRequired=False, statusCheckedAt=api.ledger.now(),
            statusBlockers=[], statusIsCachedTerminal=False, supplyScanDt=None,
            supplyDone=False, supplyClosedAt=None, imei=None, wbLabelCode=None,
            labelImagePath=None, supplyQrImagePath=None, metaDetails=None,
        )
        runner = self.runner("cancel-before-plan")
        runner.cancellation_cleanup_enabled = True
        waiting = {**copy.deepcopy(self.order), "supplierStatus": "new",
                   "wbStatus": "waiting", "warehouseActionRequired": True}
        with runner.ledger.transaction():
            runner.ledger.ingest_order(waiting, api.ledger.now())

        result = runner.run(live=True)

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(
            [item["order_id"] for item in result["cancellation_cleanup"]["deleted"]],
            [oid],
        )
        self.assertEqual(self.cloud.rows, [])
        self.assertEqual(result["committed"], [])
        self.assertIsNotNone(runner.ledger.retirement("new-store", oid))
        self.assertEqual(
            [call["action"] for call in self.cloud.calls
             if call["action"] != "read"],
            ["delete_pre_shipment_cancellations"],
        )

    def test_failed_table_readback_never_starts_preparation(self):
        self.order.update(orderId="900000102", supplierStatus="new")
        self.cloud.fail_read_after_write = True
        runner = self.runner("no-prepare-without-readback")
        runner.prepare_enabled = True
        with patch.object(api.prepare, "process") as prepare_call:
            result = runner.run(live=True)
        self.assertEqual(result["status"], "blocked")
        prepare_call.assert_not_called()

    def test_uncertain_preparation_blocks_that_orders_imei_progression(self):
        self.order.update(orderId="900000103", supplierStatus="new")
        runner = self.runner("prepare-unknown-no-imei")
        runner.prepare_enabled = runner.imei_workflow = True

        def uncertain(actual, current, collector, summary):
            summary["preparation"] = [{"store_alias": "new-store", "order_id": "900000103", "stage": "NEEDS_REVIEW"}]
            return current

        def imei_after_prepare(actual, current, collector, summary):
            self.assertEqual(collector["orders"], [])
            summary["shipment_progress"] = []
            return current, set()

        with patch.object(api.prepare, "process", side_effect=uncertain), \
                patch.object(api.imei_sync, "process", side_effect=imei_after_prepare), \
                patch.object(api.imei_sync, "fetch_verified_labels"):
            result = runner.run(live=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["workflow_attention"][0]["stage"], "NEEDS_REVIEW")

    def test_probe_is_rejected_before_collection(self):
        def probe(argv):
            return {"http_status": 200, "response": {"status": "finished"}, "result": {"mode": "readonly_probe", "values": []}}
        result = self.runner("probe", probe).run(live=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.collect_calls, 0)
        self.assertIn("install airscript-sync.js", result["error"])

    def test_independent_readback_before_commit(self):
        self.assets()
        runner = self.runner("success")
        result = runner.run(live=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(len(result["committed"]), 1)
        self.assertEqual([c["action"] for c in self.cloud.calls], ["read", "read", "read", "apply", "read"])
        self.assertEqual(runner.ledger.summary()["pending_operations"], 0)
        self.assertTrue(list((self.root / "api-runs/success").glob("*-response-apply.json")))

    def test_collaborator_change_aborts_before_write(self):
        def change_on_third_read(cloud):
            if cloud.reads == 3:
                cloud.rows.append({"row_number": 4, "values": ["", "", "", "", "", "", "", "", "", "", "人工备注"]})
        self.cloud.on_read = change_on_third_read
        result = self.runner("concurrent").run(live=True)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("changed after planning", result["error"])
        self.assertFalse(self.writes())

    def test_ambiguous_missing_row_never_retries_or_replans_payload(self):
        self.cloud.write_mode = "timeout_without_row"
        first = self.runner("timeout-1")
        result = first.run(live=True)
        self.assertEqual(result["status"], "blocked")
        old_payload = first.ledger.db.execute("SELECT payload FROM operations").fetchone()[0]
        self.cloud.write_mode = "normal"
        second = self.runner("timeout-2")
        result = second.run(live=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(second.ledger.db.execute("SELECT payload FROM operations").fetchone()[0], old_payload)

    def test_lost_commit_recovers_receipts_and_current_manual_fields(self):
        self.assets()
        self.cloud.fail_read_after_write = True
        result = self.runner("lost-commit-1").run(live=True)
        self.assertEqual(result["status"], "blocked")
        self.cloud.rows[0]["values"][7] = "356000000000001"
        self.cloud.rows[0]["values"][10] += "\n仓库补充备注"
        second = self.runner("lost-commit-2")
        result = second.run(live=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(second.ledger.summary()["pending_operations"], 0)
        saved = json.loads(second.ledger.state("new-store", "5589475494")["latest_row"])
        self.assertEqual(saved["values"][7], "356000000000001")
        self.assertIn("仓库补充备注", saved["values"][10])

    def test_timeout_image_without_receipt_remains_unverified(self):
        self.assets()
        self.cloud.write_mode = "timeout_with_row"
        self.assertEqual(self.runner("missing-receipt-1").run(live=True)["status"], "blocked")
        self.cloud.write_mode = "normal"
        second = self.runner("missing-receipt-2")
        result = second.run(live=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(second.ledger.summary()["pending_operations"], 1)

    def test_partial_image_receipt_repairs_only_missing_image(self):
        self.assets()
        self.cloud.write_mode = "partial_image"
        runner = self.runner("partial-image")
        result = runner.run(live=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual([c["action"] for c in self.writes()], ["apply", "repair_images"])
        self.assertEqual(self.cloud.rows[0]["values"][8], '=DISPIMG("ID_ORDER_STICKER",1)')
        self.assertEqual(runner.ledger.summary()["pending_operations"], 0)

    def test_later_labels_fill_committed_row_and_preserve_manual_cells(self):
        self.assertEqual(self.runner("without-images").run(live=True)["status"], "ok")
        self.cloud.rows[0]["values"][7] = "356000000000001"
        self.cloud.rows[0]["values"][10] = "仓库人工备注"
        before = copy.deepcopy(self.cloud.rows[0]["values"])
        self.assets()
        runner = self.runner("later-images")
        result = runner.run(live=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["committed"], [])
        self.assertEqual(len(result["image_fills"]), 1)
        self.assertEqual(self.writes()[-1]["action"], "repair_images")
        self.assertEqual(self.writes()[-1]["operation"]["values"], before)
        self.assertEqual(self.cloud.rows[0]["values"][:8], before[:8])
        self.assertEqual(self.cloud.rows[0]["values"][10], before[10])
        self.assertEqual(runner.ledger.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 1)
        self.assertEqual(runner.ledger.summary()["restorations"], 0)
        self.assertEqual(runner.ledger.db.execute("SELECT COUNT(*) FROM events WHERE kind='api_image_fill_verified'").fetchone()[0], 1)
        assets = json.loads(runner.ledger.state("new-store", self.order["orderId"])["assets"])
        self.assertEqual(assets["order_sticker"]["verified_reference"], self.cloud.rows[0]["values"][8])
        self.assertEqual(self.runner("repeat-fill").run(live=True)["image_fills"], [])
        self.assertEqual(len(self.writes()), 2)

    def test_existing_unverified_image_is_preserved_and_reported(self):
        self.assertEqual(self.runner("manual-image-1").run(live=True)["status"], "ok")
        self.cloud.rows[0]["values"][8] = '=DISPIMG("ID_MANUAL",1)'
        self.assets()
        result = self.runner("manual-image-2").run(live=True)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("conflicting provenance", result["error"])
        self.assertEqual(self.cloud.rows[0]["values"][8], '=DISPIMG("ID_MANUAL",1)')
        self.assertEqual(len(self.writes()), 1)

    def test_later_label_does_not_touch_nonpending_order(self):
        self.runner("shipped-1").run(live=True)
        self.assets()
        self.order["warehouseActionRequired"] = False
        result = self.runner("shipped-2").run(live=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["image_fills"], [])
        self.assertEqual(len(self.writes()), 1)

    def test_fill_readback_timeout_recovers_receipt_without_reupload(self):
        self.runner("fill-read-timeout-1").run(live=True)
        self.assets()
        def fail_after_repair(argv):
            result = self.cloud(argv)
            if argv["action"] == "repair_images":
                self.cloud.fail_read_after_write = True
            return result
        result = self.runner("fill-read-timeout-2", fail_after_repair).run(live=True)
        self.assertEqual(result["status"], "blocked")
        runner = self.runner("fill-read-timeout-3")
        result = runner.run(live=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(len(result["image_fills"]), 1)
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(runner.ledger.summary()["restorations"], 0)

    def test_fill_write_timeout_never_reuploads_even_when_cell_stays_empty(self):
        self.runner("fill-uncertain-1").run(live=True)
        self.assets()
        self.cloud.write_mode = "timeout_without_row"
        self.assertEqual(self.runner("fill-uncertain-2").run(live=True)["status"], "blocked")
        self.cloud.write_mode = "normal"
        result = self.runner("fill-uncertain-3").run(live=True)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("no definite receipt", result["error"])
        self.assertEqual(len(self.writes()), 2)

    def test_fill_write_timeout_after_insert_preserves_unreceipted_image(self):
        self.runner("fill-insert-timeout-1").run(live=True)
        self.assets()
        self.cloud.write_mode = "timeout_with_row"
        self.assertEqual(self.runner("fill-insert-timeout-2").run(live=True)["status"], "blocked")
        self.cloud.write_mode = "normal"
        result = self.runner("fill-insert-timeout-3").run(live=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(self.writes()), 2)
        self.assertTrue(self.cloud.rows[0]["values"][8])

    def test_partial_fill_commits_only_verified_images_and_next_run_fills_remaining(self):
        self.runner("partial-fill-1").run(live=True)
        self.assets()
        self.cloud.write_mode = "partial_fill"
        runner = self.runner("partial-fill-2")
        result = runner.run(live=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["image_fills"][0]["images"], ["WB标签"])
        self.assertEqual(result["image_fill_pending"][0]["images"], ["供货二维码"])
        label_ref = self.cloud.rows[0]["values"][8]
        self.cloud.write_mode = "normal"
        result = self.runner("partial-fill-3").run(live=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(self.writes()[-1]["operation"]["required_image_kinds"], ["supply_qr"])
        self.assertEqual(self.cloud.rows[0]["values"][8], label_ref)
        self.assertEqual(runner.ledger.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 1)

    def test_missing_row_with_uncertain_image_request_does_not_restore(self):
        self.runner("fill-deleted-1").run(live=True)
        self.assets()
        self.cloud.write_mode = "timeout_with_row"
        self.runner("fill-deleted-2").run(live=True)
        self.cloud.rows = []
        self.cloud.write_mode = "normal"
        result = self.runner("fill-deleted-3").run(live=True)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("before restoring", result["error"])
        self.assertEqual(len(self.writes()), 2)


if __name__ == "__main__":
    unittest.main()
