"""Bounded, offline feedback runner tests; synthetic orders and a fake API only."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.wps_warehouse import feedback_apply
from tools.wps_warehouse.feedback_apply import Runner


SHEET = "2026年9月发货"
WORKBOOK_URL = "https://www.kdocs.cn/l/cof93WEpp0yG"
ORDER_IDS = ["910000001", "910000002", "910000003"]
REPLY_COLOR = 13395456


def font(index):
    return {
        "Name": "Arial" if index % 2 else "Microsoft YaHei",
        "Size": 10 + index % 2,
        "Bold": index % 2 == 0,
        "Italic": index % 3 == 0,
        "Underline": -4142,
        "Color": 255 if index % 2 else 0,
        "Strikethrough": False,
        "Subscript": False,
        "Superscript": False,
    }


def suffix(item):
    return "\n\n【核实回复 北京时间 " + item["reply_time_bj"] + "】\n" + item["reply"]


class FakeFeedback:
    def __init__(self, items, *, timeout_after_first=False, corrupt_old_font=False):
        self.calls = []
        self.timeout_after_first = timeout_after_first
        self.corrupt_old_font = corrupt_old_font
        self.pending_corruption = False
        self.append_count = 0
        rows = [
            {"row_number": 1, "values": ["合成仓库表"] + [""] * 11},
            {"row_number": 2, "values": ["序号", "日期", "店铺", "订单", "型号", "内存",
                                         "颜色", "IMEI", "标签", "二维码", "备注", "仓库反馈"]},
        ]
        for index, item in enumerate(items):
            text = item["warehouse_text"]
            store_name = "沫德DL05" if item["store_alias"] == "new-store" else "瑞盈8597"
            rows.append({
                "row_number": item["row_number"],
                "values": [str(index + 1), "2026/9/5", store_name, item["order_id"],
                           "Synthetic Phone", "128GB", "黑色", "synthetic-imei",
                           '=DISPIMG("SYNTHETIC_I",1)', '=DISPIMG("SYNTHETIC_J",1)',
                           "保留原备注", text],
                "styles": [font(n) for n in range(len(text))],
            })
        self.snapshot = {
            "workbook_url": WORKBOOK_URL,
            "target_sheet": SHEET,
            "sheet_names": [SHEET, "库存概览"],
            "sheets": [{"name": SHEET, "rows": rows, "used_row_end": 5}],
        }
        self.original = copy.deepcopy(self.snapshot)

    def result(self, action="probe", status="ok", **extra):
        snapshot = copy.deepcopy(self.snapshot)
        return {
            "version": "warehouse-feedback-v2", "action": action, "status": status,
            "read_only": action == "probe", "snapshot": snapshot,
            "state_token": json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            **extra,
        }

    def __call__(self, argv):
        self.calls.append(copy.deepcopy(argv))
        action = argv["action"]
        if action == "probe":
            if self.pending_corruption:
                self.snapshot["sheets"][0]["rows"][2]["styles"][0]["Bold"] = False
                self.pending_corruption = False
            return self.result()
        if action != "append_reply":
            raise AssertionError("Unexpected fake API action: " + action)
        if argv["expected_state_token"] != self.result()["state_token"]:
            raise AssertionError("An append used stale workbook evidence")
        row = next(row for row in self.snapshot["sheets"][0]["rows"]
                   if row["row_number"] == argv["row_number"])
        if row["values"][3] != argv["order_id"]:
            raise AssertionError("An append targeted the wrong order")
        if row["values"][11] != argv["expected_existing_text"]:
            raise AssertionError("An append used stale original text")
        extra_text = suffix(argv)
        new_font = copy.deepcopy(row["styles"][-1])
        new_font["Color"] = REPLY_COLOR
        row["values"][11] += extra_text
        row["styles"].extend(copy.deepcopy(new_font) for _ in extra_text)
        self.append_count += 1
        if self.corrupt_old_font and self.append_count == 1:
            self.pending_corruption = True
        if self.timeout_after_first and self.append_count == 1:
            raise TimeoutError("Synthetic timeout after the write completed")
        return self.result("append_reply", "verified", reply_color=REPLY_COLOR,
                           request_id=argv["request_id"], independent_http_readback_required=True)


class ColorPendingFeedback(FakeFeedback):
    """The first append expands text but leaves the appended font unchanged."""
    def __init__(self, items, *, repair_failure=None):
        super().__init__(items)
        self.repair_failure = repair_failure
        self.repair_count = 0
        self.archived_append = None

    def __call__(self, argv):
        if argv["action"] == "repair_reply_color":
            self.calls.append(copy.deepcopy(argv))
            expected = {**self.archived_append, "action": "repair_reply_color"}
            if argv != expected:
                raise AssertionError("Color repair changed the archived append parameters")
            self.repair_before = copy.deepcopy(self.snapshot)
            row = next(row for row in self.snapshot["sheets"][0]["rows"]
                       if row["row_number"] == argv["row_number"])
            length = len(argv["expected_existing_text"])
            for style in row["styles"][length:]:
                style["Color"] = REPLY_COLOR
            self.repair_after = copy.deepcopy(self.snapshot)
            self.repair_count += 1
            if self.repair_failure == "old_font":
                self.pending_corruption = True
            if self.repair_failure == "http_unknown":
                raise TimeoutError("Synthetic unknown HTTP outcome after color repair")
            return self.result("repair_reply_color", "applied_pending_readback", reply_color=REPLY_COLOR,
                               request_id=argv["request_id"], snapshot=None, state_token=None)

        result = super().__call__(argv)
        if argv["action"] == "append_reply" and self.append_count == 1:
            self.archived_append = copy.deepcopy(argv)
            row = next(row for row in self.snapshot["sheets"][0]["rows"]
                       if row["row_number"] == argv["row_number"])
            length = len(argv["expected_existing_text"])
            old_font = copy.deepcopy(row["styles"][length - 1])
            row["styles"][length:] = [copy.deepcopy(old_font) for _ in suffix(argv)]
            return self.result("append_reply", "applied_pending_readback", reply_color=REPLY_COLOR,
                               request_id=argv["request_id"])
        return result


class FeedbackApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def case(self, name, *, evidence_age=timedelta(0), **fake_options):
        directory = self.base / name
        directory.mkdir()
        captured_at = (datetime.now(timezone.utc) - evidence_age).isoformat()
        reply_time = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        items = [{
            "store_alias": "owner-store" if index == 1 else "new-store",
            "order_id": order_id,
            "row_number": index + 3,
            "warehouse_text": "请核实订单状态" + str(index + 1),
            "reply": "已核实，合成测试订单仍待仓库处理。",
            "reply_time_bj": reply_time,
            "request_id": "offline-feedback-" + str(index + 1),
        } for index, order_id in enumerate(ORDER_IDS)]
        evidence = directory / "wb-evidence.json"
        evidence.write_text(json.dumps({"captured_at": captured_at, "order_ids": ORDER_IDS,
                                        "orders": [{"order_id": oid} for oid in ORDER_IDS]}),
                            encoding="utf-8")
        plan = {
            "schema_version": 1, "plan_id": "offline-plan-" + name,
            "authorization": {"approved": True, "statement": "Approve these three synthetic replies."},
            "sheet": SHEET, "expected_order_ids": ORDER_IDS,
            "wb_evidence": [{"path": str(evidence), "captured_at": captured_at, "order_ids": ORDER_IDS}],
            "items": items,
        }
        plan_path = directory / "plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        fake = FakeFeedback(items, **fake_options)
        return plan_path, directory / "runs", fake, items

    def assert_serial_readbacks(self, calls):
        positions = [index for index, call in enumerate(calls)
                     if call["action"] in ("append_reply", "repair_reply_color")]
        for index in positions:
            self.assertLess(index + 1, len(calls), "Every append needs an independent readback")
            self.assertEqual(calls[index + 1]["action"], "probe")

    def fresh_summary(self, directory, **overrides):
        path = directory / "fresh-wb-summary.json"
        path.write_text(json.dumps({
            "all_verified": True, "captured_at": datetime.now(timezone.utc).isoformat(),
            "order_ids": ORDER_IDS, **overrides,
        }), encoding="utf-8")
        return path

    def test_dryrun_writes_nothing_and_live_applies_three_serial_replies(self):
        plan, root, fake, items = self.case("normal")
        summary = Runner(plan, root, invoke=fake).run(live=False)
        self.assertEqual(summary["status"], "dryrun")
        self.assertEqual(fake.append_count, 0)
        self.assertEqual(fake.snapshot, fake.original)

        summary = Runner(plan, root, invoke=fake).run(live=True)
        self.assertEqual(summary["status"], "complete")
        writes = [call for call in fake.calls if call["action"] == "append_reply"]
        self.assertEqual([call["order_id"] for call in writes], ORDER_IDS)
        self.assert_serial_readbacks(fake.calls)
        for old, row, item in zip(fake.original["sheets"][0]["rows"][2:],
                                  fake.snapshot["sheets"][0]["rows"][2:], items):
            original_length = len(item["warehouse_text"])
            self.assertEqual(row["values"][:11], old["values"][:11])
            self.assertEqual(row["values"][11], item["warehouse_text"] + suffix(item))
            self.assertEqual(row["styles"][:original_length], old["styles"])
            self.assertTrue(all(style["Color"] == REPLY_COLOR for style in row["styles"][original_length:]))

    def test_timeout_stops_after_readback_and_resume_never_repeats_the_write(self):
        plan, root, fake, _ = self.case("timeout", timeout_after_first=True)
        summary = Runner(plan, root, invoke=fake).run(live=True)
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(fake.append_count, 1)
        self.assert_serial_readbacks(fake.calls)
        resume_start = len(fake.calls)

        summary = Runner(plan, root, invoke=fake).run(live=True)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(fake.calls[resume_start]["action"], "probe")
        writes = [call for call in fake.calls if call["action"] == "append_reply"]
        self.assertEqual([call["order_id"] for call in writes], ORDER_IDS)
        self.assertEqual(len({call["request_id"] for call in writes}), 3)
        self.assert_serial_readbacks(fake.calls)

        self.assertEqual(Runner(plan, root, invoke=fake).run(live=True)["status"], "complete")
        self.assertEqual(fake.append_count, 3)

    def test_stale_wb_evidence_or_old_font_changes_prevent_further_writes(self):
        with self.subTest("stale WB evidence"):
            plan, root, fake, _ = self.case("stale", evidence_age=timedelta(days=2))
            summary = Runner(plan, root, invoke=fake).run(live=True)
            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(fake.append_count, 0)
            self.assertEqual(fake.snapshot, fake.original)

        with self.subTest("old font changed on independent readback"):
            plan, root, fake, _ = self.case("style", corrupt_old_font=True)
            summary = Runner(plan, root, invoke=fake).run(live=True)
            self.assertIn(summary["status"], ("stopped", "blocked"))
            self.assertEqual(fake.append_count, 1)
            self.assert_serial_readbacks(fake.calls)

    def test_http_failures_retain_status_and_redacted_response_body(self):
        token = "offline-synthetic-token-DO-NOT-PERSIST"
        url = "https://www.kdocs.cn/api/v3/ide/file/SYNTHETIC/script/SYNTHETIC/sync_task"
        cases = [
            ("unfinished execution", 200,
             json.dumps({"status": "running", "error": "", "diagnostic": token}), "body"),
            ("non-JSON server error", 503, "Synthetic upstream error: " + token, "body_text"),
            ("malformed inner result", 200,
             json.dumps({"status": "finished", "error": "", "data": {"result": "bad-json " + token}}), "body"),
        ]
        for name, http_status, body_text, body_key in cases:
            with self.subTest(name):
                response = SimpleNamespace(status_code=http_status, text=body_text)
                with patch.object(feedback_apply, "load_connection", return_value=(url, token)), \
                        patch.object(feedback_apply.requests, "post", return_value=response) as post:
                    with self.assertRaises(feedback_apply.FeedbackHttpFailure) as caught:
                        feedback_apply.invoke_live({"action": "probe"})
                post.assert_called_once()
                record = caught.exception.response_record
                self.assertEqual(record["http_status"], http_status)
                self.assertIsInstance(record["elapsed_seconds"], (int, float))
                self.assertGreaterEqual(record["elapsed_seconds"], 0)
                self.assertIsNotNone(datetime.fromisoformat(record["captured_at"]).tzinfo)
                redacted_text = body_text.replace(token, "[REDACTED]")
                expected_body = json.loads(redacted_text) if body_key == "body" else redacted_text
                self.assertEqual(record[body_key], expected_body)
                serialized = json.dumps(record, ensure_ascii=False)
                self.assertNotIn(token, serialized)
                self.assertIn("[REDACTED]", serialized)
                self.assertNotIn(token, str(caught.exception))

    def test_unfinished_http_after_write_is_archived_and_independently_reconciled(self):
        plan, root, fake, _ = self.case("http-failure")
        token = "offline-synthetic-token-DO-NOT-PERSIST"
        url = "https://www.kdocs.cn/api/v3/ide/file/SYNTHETIC/script/SYNTHETIC/sync_task"
        body = {"status": "running", "error": "", "data": {"logs": [
            {"level": "info", "args": ["Synthetic diagnostic: " + token]}
        ]}}
        response = SimpleNamespace(status_code=200, text=json.dumps(body))

        def write_then_receive_unfinished(argv):
            result = fake(argv)
            if argv["action"] == "append_reply":
                return feedback_apply.invoke_live(argv)
            return result

        with patch.object(feedback_apply, "load_connection", return_value=(url, token)), \
                patch.object(feedback_apply.requests, "post", return_value=response) as post:
            runner = Runner(plan, root, invoke=write_then_receive_unfinished)
            summary = runner.run(live=True)

        post.assert_called_once()
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(fake.append_count, 1)
        self.assert_serial_readbacks(fake.calls)
        self.assertEqual(fake.calls[-1]["action"], "probe")
        journal_text = (runner.directory / "journal.json").read_text(encoding="utf-8")
        journal = json.loads(journal_text)
        entries = [entry for entry in journal["http"] if entry["action"] == "append_reply"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["status"], "uncertain")
        self.assertEqual(entry["error_type"], "FeedbackHttpFailure")
        self.assertEqual(entry["http_status"], 200)
        response_path = Path(entry["response"])
        self.assertEqual(response_path.parent, runner.directory)
        self.assertRegex(response_path.name, r"^\d{3}-append_reply-response\.json$")
        archived_text = response_path.read_text(encoding="utf-8")
        archived = json.loads(archived_text)
        self.assertEqual(archived["http_status"], 200)
        self.assertEqual(archived["body"], json.loads(json.dumps(body).replace(token, "[REDACTED]")))
        self.assertIn("[REDACTED]", archived_text)
        self.assertNotIn(token, archived_text + journal_text)
        self.assertEqual(journal["operations"]["offline-feedback-1"]["status"], "verified")

    def test_explicit_color_repair_uses_archived_request_and_continues_only_after_readback(self):
        plan, root, _, items = self.case("repair-success")
        fake = ColorPendingFeedback(items)
        summary = Runner(plan, root, invoke=fake).run(live=True)
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(fake.append_count, 1)
        first = fake.snapshot["sheets"][0]["rows"][2]
        length = len(items[0]["warehouse_text"])
        self.assertEqual(first["values"][11], items[0]["warehouse_text"] + suffix(items[0]))
        self.assertTrue(all(style == first["styles"][length - 1] for style in first["styles"][length:]))

        summary = Runner(plan, root, invoke=fake).run(live=True)
        self.assertEqual(summary["status"], "blocked")
        self.assertEqual((fake.append_count, fake.repair_count), (1, 0))

        for invalid in ({"all_verified": False}, {"order_ids": ORDER_IDS[:2] + ["999999999"]}):
            with self.subTest(invalid):
                fresh = self.fresh_summary(plan.parent, **invalid)
                summary = Runner(plan, root, invoke=fake, wb_evidence=fresh).run(
                    live=True, repair_color_order=ORDER_IDS[0])
                self.assertEqual(summary["status"], "blocked")
                self.assertEqual((fake.append_count, fake.repair_count), (1, 0))

        fresh = self.fresh_summary(plan.parent)
        resume_start = len(fake.calls)
        summary = Runner(plan, root, invoke=fake, wb_evidence=fresh).run(
            live=True, repair_color_order=ORDER_IDS[0])
        self.assertEqual(summary["status"], "complete")
        self.assertEqual([call["action"] for call in fake.calls[resume_start:]],
                         ["probe", "repair_reply_color", "probe", "append_reply", "probe", "append_reply", "probe"])
        self.assertEqual((fake.append_count, fake.repair_count), (3, 1))
        writes = [call for call in fake.calls if call["action"] == "append_reply"]
        self.assertEqual([call["order_id"] for call in writes], ORDER_IDS)
        repair = next(call for call in fake.calls if call["action"] == "repair_reply_color")
        self.assertEqual(repair, {**writes[0], "action": "repair_reply_color"})
        permitted = copy.deepcopy(fake.repair_before)
        for style in permitted["sheets"][0]["rows"][2]["styles"][length:]:
            style["Color"] = REPLY_COLOR
        self.assertEqual(fake.repair_after, permitted, "Repair may change only the appended Color values")
        self.assert_serial_readbacks(fake.calls)

    def test_color_repair_old_font_change_or_unknown_http_stops_later_appends(self):
        for failure in ("old_font", "http_unknown"):
            with self.subTest(failure):
                plan, root, _, items = self.case("repair-" + failure)
                fake = ColorPendingFeedback(items, repair_failure=failure)
                self.assertEqual(Runner(plan, root, invoke=fake).run(live=True)["status"], "stopped")
                fresh = self.fresh_summary(plan.parent)
                resume_start = len(fake.calls)
                summary = Runner(plan, root, invoke=fake, wb_evidence=fresh).run(
                    live=True, repair_color_order=ORDER_IDS[0])
                self.assertEqual(summary["status"], "stopped")
                self.assertEqual((fake.append_count, fake.repair_count), (1, 1))
                self.assertEqual([call["action"] for call in fake.calls[resume_start:]],
                                 ["probe", "repair_reply_color", "probe"])
                self.assert_serial_readbacks(fake.calls)


if __name__ == "__main__":
    unittest.main()
