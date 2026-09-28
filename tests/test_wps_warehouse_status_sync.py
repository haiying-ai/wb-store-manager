"""Offline checks for L-column WB lifecycle planning and readback."""
import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tools.wps_warehouse import cancellation_cleanup, ledger, status_sync


NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


def row(number, store, order_id, status=""):
    return {"row_number": number,
            "values": [str(number - 2), "2026/09/06", store, order_id,
                       "iPhone", "128GB", "黑色", "", "", "", "赠品随机"],
            "warehouse_feedback": status, "warehouse_feedback_formula": ""}


def snapshot(rows):
    return {"captured_at": NOW, "complete": True, "warehouse_feedback_complete": True,
            "read_columns": "A:L", "workbook_url": "https://www.kdocs.cn/l/cof93WEpp0yG",
            "sheets": [{"name": "2026年9月发货", "rows": rows}]}


class StatusSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.book = ledger.Ledger(Path(self.tmp.name))
        self.addCleanup(self.book.db.close)
        self.sync = SimpleNamespace(ledger=self.book, target_sheet="2026年9月发货",
                                    max_age_seconds=600)

    def collector(self):
        return {"orders": [
            {"storeAlias": "new-store", "orderId": "101", "supplierStatus": "complete",
             "wbStatus": "sorted", "statusCheckedAt": NOW, "statusBlockers": []},
            {"storeAlias": "owner-store", "orderId": "102", "supplierStatus": "confirm",
             "wbStatus": "waiting", "statusCheckedAt": NOW, "statusBlockers": []},
        ]}

    def test_blank_cells_plan_every_current_order(self):
        current = {"snapshot": snapshot([
            row(3, "沫德DL05", "101"), row(4, "瑞盈8597", "102")])}
        plan = status_sync._plan(self.sync, current, self.collector())
        self.assertEqual([(op["order_id"], op["row_number"]) for op in plan["operations"]],
                         [("101", 3), ("102", 4)])
        self.assertIn("【自动状态】已发货", plan["operations"][0]["new_status"])
        self.assertIn("【自动状态】待发货", plan["operations"][1]["new_status"])
        self.assertEqual(plan["blockers"], [])

    def test_inactive_store_rows_are_preserved_and_ignored(self):
        self.sync.store_aliases = ("owner-store",)
        current = {"snapshot": snapshot([
            row(3, "沫德DL05", "101", "仓库历史文本"),
            row(4, "瑞盈8597", "102"),
        ])}

        plan = status_sync._plan(
            self.sync, current, {"orders": self.collector()["orders"][1:]}
        )

        self.assertEqual([(op["store_alias"], op["order_id"])
                          for op in plan["operations"]],
                         [("owner-store", "102")])
        self.assertEqual(plan["blockers"], [])
        self.assertFalse(any(item["order_id"] == "101" for item in plan["facts"]))

    def test_same_lifecycle_does_not_rewrite_only_to_refresh_time(self):
        current = {"snapshot": snapshot([row(
            3, "沫德DL05", "101",
            "【自动状态】已发货（WB已接收并分拣）；原始：supplierStatus=complete，"
            "wbStatus=sorted；核验：2026-09-06 09:00（北京时间）")])}
        plan = status_sync._plan(self.sync, current, {"orders": self.collector()["orders"][:1]})
        self.assertEqual(plan["operations"], [])
        self.assertEqual([item["order_id"] for item in plan["unchanged"]], ["101"])

    def test_non_managed_l_text_is_preserved(self):
        current = {"snapshot": snapshot([row(3, "沫德DL05", "101", "仓库人工说明")])}
        plan = status_sync._plan(self.sync, current, {"orders": self.collector()["orders"][:1]})
        self.assertEqual(plan["operations"], [])
        self.assertIn("已保留", plan["blockers"][0]["reason"])

    def test_verified_pre_handover_cancel_is_not_mislabeled_as_refusal(self):
        current = {"snapshot": snapshot([row(3, "沫德DL05", "103")])}
        order = {
            "storeAlias": "new-store", "orderId": "103",
            "supplierStatus": "confirm", "wbStatus": "canceled_by_client",
            "statusCheckedAt": NOW, "statusBlockers": [],
            cancellation_cleanup.VERIFIED_PHASE_FIELD: "before_handover",
        }
        plan = status_sync._plan(self.sync, current, {"orders": [order]})
        self.assertEqual(plan["facts"][0]["category"], "发货前取消")
        self.assertIn("【自动状态】发货前取消", plan["operations"][0]["new_status"])

    def test_defect_after_archived_pickup_state_is_post_shipment_cancel(self):
        current = {"snapshot": snapshot([row(3, "沫德DL05", "103")])}
        self.book.event("platform_observed", {
            "before": None,
            "after": {
                "storeAlias": "new-store", "orderId": "103",
                "supplierStatus": "complete", "wbStatus": "ready_for_pickup",
                "statusBlockers": [], "statusIsCachedTerminal": False,
                "statusCheckedAt": "2026-09-11T00:00:00+00:00",
            },
            "collected_at": NOW,
        }, "new-store", "103")
        order = {
            "storeAlias": "new-store", "orderId": "103",
            "supplierStatus": "complete", "wbStatus": "defect",
            "statusCheckedAt": NOW, "statusBlockers": [],
        }
        plan = status_sync._plan(self.sync, current, {"orders": [order]})
        self.assertEqual(
            (plan["facts"][0]["category"], plan["facts"][0]["code"]),
            ("发货后取消", "DEFECT_AFTER_SHIPMENT"),
        )
        self.assertIn("【自动状态】发货后取消", plan["operations"][0]["new_status"])

    def test_archived_handover_statuses_prove_post_shipment_cancellation(self):
        handover_statuses = (
            "sorted", "accepted_by_carrier", "sent_to_carrier",
            "postponed_delivery", "ready_for_pickup", "sold",
        )
        for index, historical_wb in enumerate(handover_statuses, start=200):
            oid = str(index)
            with self.subTest(historical_wb=historical_wb):
                self.book.event("platform_observed", {
                    "before": None,
                    "after": {
                        "storeAlias": "new-store", "orderId": oid,
                        "supplierStatus": "complete", "wbStatus": historical_wb,
                        "statusBlockers": [], "statusIsCachedTerminal": False,
                        "statusCheckedAt": "2026-09-11T00:00:00+00:00",
                    },
                    "collected_at": NOW,
                }, "new-store", oid)
                current = {
                    "storeAlias": "new-store", "orderId": oid,
                    "supplierStatus": "complete", "wbStatus": "defect",
                    "statusCheckedAt": NOW, "statusBlockers": [],
                }
                plan = status_sync._plan(
                    self.sync,
                    {"snapshot": snapshot([row(3, "沫德DL05", oid)])},
                    {"orders": [current]},
                )
                self.assertEqual(plan["facts"][0]["code"], "DEFECT_AFTER_SHIPMENT")

    def test_archived_handover_proves_generic_post_shipment_cancel(self):
        oid = "210"
        self.book.event("platform_observed", {
            "before": None,
            "after": {
                "storeAlias": "new-store", "orderId": oid,
                "supplierStatus": "complete", "wbStatus": "sorted",
                "statusBlockers": [], "statusIsCachedTerminal": False,
                "statusCheckedAt": "2026-09-11T00:00:00+00:00",
            },
            "collected_at": NOW,
        }, "new-store", oid)
        current = {
            "storeAlias": "new-store", "orderId": oid,
            "supplierStatus": "complete", "wbStatus": "canceled",
            "statusCheckedAt": NOW, "statusBlockers": [],
        }
        plan = status_sync._plan(
            self.sync,
            {"snapshot": snapshot([row(3, "沫德DL05", oid)])},
            {"orders": [current]},
        )
        self.assertEqual(
            (plan["facts"][0]["category"], plan["facts"][0]["code"]),
            ("发货后取消", "CANCELED_AFTER_SHIPMENT"),
        )

    def test_unverified_archived_handover_observations_are_rejected(self):
        variants = (
            ("wrong_store", {"storeAlias": "owner-store"}),
            ("wrong_order", {"orderId": "999"}),
            ("missing_time", {"statusCheckedAt": None}),
            ("equal_time", {"statusCheckedAt": NOW}),
            ("future_time", {"statusCheckedAt": "2999-01-01T00:00:00+00:00"}),
            ("cached", {"statusIsCachedTerminal": True}),
            ("blocked", {"statusBlockers": ["status source incomplete"]}),
        )
        for index, (case, changes) in enumerate(variants, start=220):
            oid = str(index)
            with self.subTest(case=case):
                historical = {
                    "storeAlias": "new-store", "orderId": oid,
                    "supplierStatus": "complete", "wbStatus": "sold",
                    "statusBlockers": [], "statusIsCachedTerminal": False,
                    "statusCheckedAt": "2026-09-11T00:00:00+00:00",
                }
                historical.update(changes)
                self.book.event("platform_observed", {
                    "before": None, "after": historical, "collected_at": NOW,
                }, "new-store", oid)
                current = {
                    "storeAlias": "new-store", "orderId": oid,
                    "supplierStatus": "complete", "wbStatus": "defect",
                    "statusCheckedAt": NOW, "statusBlockers": [],
                }
                plan = status_sync._plan(
                    self.sync,
                    {"snapshot": snapshot([row(3, "沫德DL05", oid)])},
                    {"orders": [current]},
                )
                self.assertEqual(plan["facts"][0]["code"], "DEFECT_PHASE_UNKNOWN")

    def test_managed_l_text_alone_does_not_prove_handover_history(self):
        current = {"snapshot": snapshot([row(
            3, "沫德DL05", "103",
            "【自动状态】待收货（已到提货点，等待买家收货）；"
            "原始：supplierStatus=complete，wbStatus=ready_for_pickup；"
            "核验：2026-09-12 10:00（北京时间）",
        )])}
        order = {
            "storeAlias": "new-store", "orderId": "103",
            "supplierStatus": "complete", "wbStatus": "defect",
            "statusCheckedAt": NOW, "statusBlockers": [],
        }
        plan = status_sync._plan(self.sync, current, {"orders": [order]})
        self.assertEqual(plan["facts"][0]["code"], "DEFECT_PHASE_UNKNOWN")

    def test_definitive_exact_official_return_sets_return_without_sheet_fields(self):
        current = {"snapshot": snapshot([row(3, "瑞盈8597", "102")])}
        returns = {"records": [{
            "storeAlias": "owner-store", "orderId": "102",
            "storeIdentityMatches": True, "identityComplete": True,
            "statusDefinitive": True,
            "productIdentityVerified": False, "pickupActionRequired": False,
        }]}
        plan = status_sync._plan(
            self.sync, current, {"orders": self.collector()["orders"][1:]}, returns
        )
        self.assertEqual(plan["facts"][0]["category"], "退货")
        self.assertIn("【自动状态】退货", plan["operations"][0]["new_status"])

    def test_official_return_requires_exact_order_identity_and_definitive_flags(self):
        current = {"snapshot": snapshot([row(3, "瑞盈8597", "102")])}
        shipping = {"orders": self.collector()["orders"][1:]}
        base = {
            "storeAlias": "owner-store", "orderId": "102",
            "storeIdentityMatches": True, "identityComplete": True,
            "statusDefinitive": True,
        }
        variants = (
            {**base, "storeAlias": "new-store"},
            {**base, "orderId": "999"},
            {**base, "storeIdentityMatches": False},
            {**base, "identityComplete": False},
            {**base, "statusDefinitive": False},
        )
        for item in variants:
            with self.subTest(item=item):
                plan = status_sync._plan(
                    self.sync, current, shipping, {"records": [item]}
                )
                self.assertEqual(plan["facts"][0]["category"], "待发货")

    def test_missing_return_collection_preserves_legacy_return_evidence(self):
        current = {"snapshot": snapshot([row(3, "瑞盈8597", "102")])}
        order = self.collector()["orders"][1]
        order["returnEvidenceVerified"] = True
        fallback = status_sync._plan(
            self.sync, current, {"orders": [order]}, None
        )
        authoritative_empty = status_sync._plan(
            self.sync, current, {"orders": [order]}, {"records": []}
        )
        self.assertEqual(fallback["facts"][0]["category"], "退货")
        self.assertEqual(authoritative_empty["facts"][0]["category"], "待发货")

    def test_readback_requires_only_exact_planned_l_changes(self):
        before = {"snapshot": snapshot([row(3, "沫德DL05", "101")])}
        op = {"store_alias": "new-store", "order_id": "101", "row_number": 3,
              "expected_old_status": "", "new_status": "【自动状态】已发货"}
        after = {"snapshot": copy.deepcopy(before["snapshot"])}
        after["snapshot"]["captured_at"] = datetime.now(timezone.utc).isoformat()
        after["snapshot"]["sheets"][0]["rows"][0]["warehouse_feedback"] = op["new_status"]
        status_sync.verify_readback(self.sync, before, after, [op])
        after["snapshot"]["sheets"][0]["rows"][0]["values"][10] = "被并发修改"
        with self.assertRaises(status_sync.StatusSyncBlocked):
            status_sync.verify_readback(self.sync, before, after, [op])


if __name__ == "__main__":
    unittest.main()
