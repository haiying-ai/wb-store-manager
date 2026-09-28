"""Offline tests for strict WB lifecycle labels; no credentials or network."""
import unittest
from datetime import datetime, timezone

from tools.wps_warehouse import fulfillment_status as fs


class FulfillmentStatusTests(unittest.TestCase):
    def classify(self, supplier="complete", wb="waiting", **evidence):
        return fs.classify_fulfillment_status(supplier, wb, **evidence)

    def test_waiting_states_remain_pending_without_wb_receipt(self):
        expected_details = {
            "new": "新订单",
            "confirm": "配货中",
            "complete": "WB尚无接收或扫描证据",
        }
        for supplier, phrase in expected_details.items():
            with self.subTest(supplier=supplier):
                result = self.classify(supplier, "waiting")
                self.assertEqual(result.category, "待发货")
                self.assertIn(phrase, result.detail)

    def test_receipt_and_transport_evidence_advance_waiting_order(self):
        shipped = self.classify(wb_receipt_evidence=True)
        transit = self.classify(transport_evidence=True)
        self.assertEqual((shipped.category, shipped.code), ("已发货", "SHIPPED_RECEIPT_EVIDENCE"))
        self.assertEqual((transit.category, transit.code), ("运输中", "IN_TRANSIT_EVIDENCE"))

    def test_transport_evidence_advances_sorted_state(self):
        result = self.classify("complete", "sorted", transport_evidence=True)
        self.assertEqual((result.category, result.code), ("运输中", "IN_TRANSIT_EVIDENCE"))

    def test_later_pickup_and_receipt_states_override_transport_evidence(self):
        pickup = self.classify(
            "complete", "ready_for_pickup", transport_evidence=True
        )
        received = self.classify("complete", "sold", transport_evidence=True)
        self.assertEqual(pickup.category, "待收货")
        self.assertEqual(received.category, "已收货")

    def test_official_delivery_states_have_distinct_categories(self):
        cases = {
            "sorted": "已发货",
            "accepted_by_carrier": "运输中",
            "sent_to_carrier": "运输中",
            "postponed_delivery": "运输中",
            "ready_for_pickup": "待收货",
            "sold": "已收货",
        }
        for wb, category in cases.items():
            with self.subTest(wb=wb):
                self.assertEqual(self.classify(wb=wb).category, category)

    def test_buyer_cancellation_states_distinguish_early_cancel_and_refusal(self):
        self.assertEqual(self.classify("new", "declined_by_client").category, "发货前取消")
        self.assertEqual(self.classify("complete", "canceled_by_client").category, "已拒收")

    def test_canceled_by_client_with_verified_pre_handover_history_is_early_cancel(self):
        result = self.classify(
            "confirm", "canceled_by_client",
            cancellation_phase=fs.CANCEL_BEFORE_HANDOVER,
        )
        self.assertEqual(
            (result.category, result.code),
            ("发货前取消", "CANCELED_BEFORE_SHIPMENT"),
        )

    def test_canceled_by_client_pre_handover_conflicts_with_transport(self):
        result = self.classify(
            "confirm", "canceled_by_client",
            cancellation_phase=fs.CANCEL_BEFORE_HANDOVER,
            transport_evidence=True,
        )
        self.assertEqual(result.category, fs.UNKNOWN_STATUS)
        self.assertEqual(result.code, "STATUS_EVIDENCE_CONFLICT")

    def test_generic_cancel_requires_proven_event_order(self):
        unknown = self.classify("cancel", "canceled")
        before = self.classify(
            "cancel", "canceled", cancellation_phase=fs.CANCEL_BEFORE_HANDOVER
        )
        after = self.classify(
            "cancel", "canceled", cancellation_phase=fs.CANCEL_AFTER_HANDOVER
        )
        self.assertEqual(unknown.category, fs.UNKNOWN_STATUS)
        self.assertFalse(unknown.definitive)
        self.assertEqual(before.category, "发货前取消")
        self.assertEqual(after.category, "发货后取消")

    def test_defect_is_never_treated_as_return_without_return_evidence(self):
        unknown = self.classify("cancel", "defect")
        before = self.classify(
            "cancel", "defect", cancellation_phase=fs.CANCEL_BEFORE_HANDOVER
        )
        after = self.classify(
            "complete", "defect", cancellation_phase=fs.CANCEL_AFTER_HANDOVER
        )
        returned = self.classify("complete", "defect", return_evidence=True)
        self.assertEqual(unknown.category, fs.UNKNOWN_STATUS)
        self.assertEqual(before.category, "发货前取消")
        self.assertEqual(after.category, "发货后取消")
        self.assertNotEqual(before.category, "退货")
        self.assertNotEqual(after.category, "退货")
        self.assertEqual(returned.category, "退货")

    def test_explicit_return_evidence_has_precedence_over_marketplace_status(self):
        for wb in ("sold", "canceled_by_client", "waiting", None):
            with self.subTest(wb=wb):
                result = self.classify("complete", wb, return_evidence=True)
                self.assertEqual((result.category, result.code), ("退货", "RETURN_CONFIRMED"))

    def test_unknown_or_incomplete_status_is_visible_instead_of_guessed(self):
        for supplier, wb in ((None, None), ("complete", None), ("unexpected", "waiting")):
            with self.subTest(supplier=supplier, wb=wb):
                result = self.classify(supplier, wb)
                self.assertEqual(result.category, fs.UNKNOWN_STATUS)
                self.assertFalse(result.definitive)

    def test_non_boolean_evidence_is_rejected_instead_of_treated_as_truthy(self):
        for name in (
            "wb_receipt_evidence",
            "transport_evidence",
            "return_evidence",
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(TypeError, f"{name} must be bool"):
                    self.classify(**{name: "false"})

    def test_invalid_cancellation_phase_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cancellation_phase"):
            self.classify("cancel", "canceled", cancellation_phase="probably_after")

    def test_conflicting_early_cancel_and_transport_evidence_stays_unknown(self):
        result = self.classify("new", "declined_by_client", transport_evidence=True)
        self.assertEqual(result.category, fs.UNKNOWN_STATUS)
        self.assertEqual(result.code, "STATUS_EVIDENCE_CONFLICT")
        self.assertFalse(result.definitive)

    def test_l_cell_contains_marker_raw_states_and_beijing_time(self):
        value = fs.build_l_cell_text(
            "CONFIRM",
            "WAITING",
            verified_at=datetime(2026, 9, 6, 2, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            value,
            "【自动状态】待发货（配货中，WB尚未接收）；"
            "原始：supplierStatus=confirm，wbStatus=waiting；"
            "核验：2026-09-06 10:05（北京时间）",
        )

    def test_l_cell_rejects_naive_verification_time(self):
        status = self.classify("confirm", "waiting")
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            fs.render_l_cell(status, datetime(2026, 9, 6, 10, 5))


if __name__ == "__main__":
    unittest.main()
