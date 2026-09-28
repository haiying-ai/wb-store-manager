"""Offline behavioral coverage for WB warehouse collection; no real credentials/network."""
import base64
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from tools.wps_warehouse import collect as c

NOW = datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc)
STORE = SimpleNamespace(alias="new-store")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def order(**changes):
    value = {"storeAlias": "new-store", "orderId": "101", "nmId": "901", "article": "SKU-128-BLACK", "barcodes": ["000123"], "createdAt": "2026-09-04T01:02:03Z", "supplierStatus": "confirm", "wbStatus": "waiting", "supplyId": None}
    value.update(changes)
    return value


def card(**changes):
    value = {"nmID": 901, "vendorCode": "SKU-128-BLACK", "sizes": [{"skus": ["000123"]}], "characteristics": [{"id": 5023, "value": ["iPhone 14 Pro"]}, {"id": 4424, "value": ["128"]}, {"id": 14177449, "value": ["черный"]}]}
    value.update(changes)
    return {"cards": [value]}


class FakeClient:
    def __init__(self, statuses=None, history=None, fresh=None, card_data=None, supply=None):
        self.statuses = [{"id": 101, "supplierStatus": "confirm", "wbStatus": "waiting"}] if statuses is None else statuses
        self.history = history or [{"orders": [], "next": 0}]
        self.fresh, self.card_data, self.supply = fresh or [], card_data or card(), supply
        self.calls = []

    def request(self, method, url, body=None):
        self.calls.append((method, url, body))
        status, data = 200, None
        if url.endswith("/orders/new"):
            data = {"orders": self.fresh}
        elif "/api/v3/orders?" in url:
            response = self.history.pop(0) if len(self.history) > 1 else self.history[0]
            if response is None:
                status = 503
            else:
                data = response
        elif url.endswith("/orders/status"):
            data = {"orders": self.statuses}
        elif url == c.CONTENT:
            data = self.card_data
        elif "/supplies/" in url and not "/barcode?" in url:
            data = self.supply
            status = 200 if data else 503
        elif "/stickers?" in url or "/barcode?" in url:
            status = 409
        else:
            raise AssertionError(f"Unexpected test request {url}")
        return SimpleNamespace(status=status, ok=status == 200, data=data)


class CollectorTests(unittest.TestCase):
    def run_store(self, client, rows=None, **kwargs):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        saved = {"orders": {r["orderId"]: copy.deepcopy(r) for r in (rows if rows is not None else [order()])}, "seller": {"sid": "seller1", "name": "Store Ltd", "tradeMark": "Shop"}, "watermark": c.stamp(NOW - timedelta(hours=2)), "last_daily_backfill": c.stamp(NOW)}
        original_watermark = saved["watermark"]
        recorder = c.Recorder(client, root / "raw", delay=0)
        result, observed = c.collect_store(STORE, saved, recorder, root, NOW, **kwargs)
        return result, observed, saved, original_watermark

    def test_dry_run_can_select_one_configured_owner_store(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        config = root / "stores.json"
        config.write_text(json.dumps({"stores": [
            {"alias": "owner-store", "role": "owner_readonly", "token_env": "WB_API_TOKEN"},
            {"alias": "new-store", "role": "owner_readonly", "token_env": "WB_NEW_STORE_API_TOKEN"},
        ]}), encoding="utf-8")
        args = c.parser().parse_args([
            "--dry-run", "--root", str(root / "archive"),
            "--store-config", str(config), "--store", "owner-store",
        ])

        result = c.run(args)

        self.assertEqual([item["store_alias"] for item in result["stores"]],
                         ["owner-store"])

    def test_requested_store_must_exist_in_registry(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        config = root / "stores.json"
        config.write_text(json.dumps({"stores": [
            {"alias": "owner-store", "role": "owner_readonly", "token_env": "WB_API_TOKEN"},
        ]}), encoding="utf-8")
        args = c.parser().parse_args([
            "--dry-run", "--root", str(root / "archive"),
            "--store-config", str(config), "--store", "new-store",
        ])

        with self.assertRaisesRegex(ValueError, "not present"):
            c.run(args)

    def test_lifecycle_and_unknown_are_distinct(self):
        for supplier in ("new", "confirm", "complete"):
            self.assertIs(c.classify(order(supplierStatus=supplier)), True)
        self.assertIs(c.classify(order(wbStatus="sorted")), False)
        self.assertIs(c.classify(order(wbStatus="canceled_by_client")), False)
        self.assertIs(c.classify(order(supplyScanDt="2026-09-04T01:00:00Z")), False)
        self.assertIsNone(c.classify(order(wbStatus=None)))
        self.assertIsNone(c.classify(order(supplierStatus="unexpected")))
        self.assertIsNone(c.classify(order(), fresh_status=False))

    def test_long_outage_windows_have_no_gaps_and_keep_overlap(self):
        watermark = NOW - timedelta(days=70)
        windows, daily = c.plan_windows({"watermark": c.stamp(watermark)}, NOW)
        self.assertEqual(windows[0][0], watermark - timedelta(hours=48))
        self.assertEqual(windows[-1][1], NOW)
        self.assertTrue(daily)
        self.assertTrue(all(b - a <= timedelta(days=29) for a, b in windows))
        self.assertTrue(all(windows[i][1] == windows[i + 1][0] for i in range(len(windows) - 1)))

    def test_daily_backfill_once_per_beijing_day(self):
        saved = {"watermark": c.stamp(NOW - timedelta(hours=2)), "last_daily_backfill": c.stamp(NOW)}
        windows, due = c.plan_windows(saved, NOW)
        self.assertFalse(due)
        self.assertEqual(windows[0][0], NOW - timedelta(hours=50))
        saved["last_daily_backfill"] = c.stamp(NOW - timedelta(days=1))
        windows, due = c.plan_windows(saved, NOW)
        self.assertTrue(due)
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0][0], NOW - timedelta(days=30))

    def test_missing_status_remains_unknown_and_watermark_does_not_advance(self):
        result, rows, saved, original = self.run_store(FakeClient(statuses=[]))
        self.assertFalse(result["ok"])
        self.assertIsNone(rows[0]["warehouseActionRequired"])
        self.assertIsNone(rows[0]["supplierStatus"])
        self.assertEqual(saved["watermark"], original)

    def test_pagination_failure_never_skips_window(self):
        history = [{"orders": [{"id": 101}], "next": 77}, None]
        result, _, saved, original = self.run_store(FakeClient(history=history))
        self.assertFalse(result["ok"])
        self.assertEqual(saved["watermark"], original)

    def test_cached_terminal_ids_are_not_refreshed_by_daily_discovery(self):
        client = FakeClient(history=[{"orders": [{"id": 101}], "next": 0}])
        result, rows, _, _ = self.run_store(client, [order(wbStatus="sold")])
        self.assertTrue(result["ok"])
        self.assertFalse(any(url.endswith("/orders/status") for _, url, _ in client.calls))
        self.assertFalse(rows[0]["warehouseActionRequired"])

    def test_current_table_order_forces_fresh_terminal_status(self):
        client = FakeClient(
            statuses=[{"id": 101, "supplierStatus": "complete", "wbStatus": "ready_for_pickup"}],
            history=[{"orders": [], "next": 0}],
        )
        result, rows, _, _ = self.run_store(
            client, [order(supplierStatus="complete", wbStatus="sorted")],
            force_order_ids={"101"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["forced_status_order_ids"], ["101"])
        self.assertTrue(any(url.endswith("/orders/status") for _, url, _ in client.calls))
        self.assertEqual(rows[0]["wbStatus"], "ready_for_pickup")
        self.assertIn("statusCheckedAt", rows[0])
        self.assertNotIn("statusIsCachedTerminal", rows[0])

    def test_newly_confirmed_order_is_found_via_history_not_new_endpoint(self):
        client = FakeClient(history=[{"orders": [{"id": 101, "nmId": 901, "article": "SKU-128-BLACK", "skus": ["000123"], "createdAt": "2026-09-04T01:02:03Z"}], "next": 0}])
        result, rows, _, _ = self.run_store(client, [])
        self.assertTrue(result["ok"])
        self.assertTrue(rows[0]["warehouseActionRequired"])
        self.assertTrue(rows[0]["readyForWps"])
        self.assertIn("labelImagePath", rows[0]["pendingFields"])
        self.assertIsNone(rows[0]["labelImagePath"])
        self.assertNotEqual(rows[0].get("wbLabelCode"), "000123")

    def test_new_without_supply_or_available_sticker_stays_actionable(self):
        client = FakeClient(statuses=[{"id": 101, "supplierStatus": "new", "wbStatus": "waiting"}])
        result, rows, _, _ = self.run_store(client)
        self.assertTrue(result["ok"])
        self.assertTrue(rows[0]["warehouseActionRequired"])
        self.assertIn("supplyId", rows[0]["pendingFields"])
        self.assertFalse(any("stickers?" in url for _, url, _ in client.calls))

    def test_supply_scan_failure_is_not_treated_as_unscanned(self):
        result, rows, saved, original = self.run_store(FakeClient(), [order(supplyId="WB-GI-100")])
        self.assertFalse(result["ok"])
        self.assertIsNone(rows[0]["warehouseActionRequired"])
        self.assertEqual(saved["watermark"], original)

    def test_receipt_scan_excludes_complete_waiting(self):
        client = FakeClient(supply={"id": "WB-GI-100", "scanDt": c.stamp(NOW), "done": True})
        result, rows, _, _ = self.run_store(client, [order(supplyId="WB-GI-100")])
        self.assertTrue(result["ok"])
        self.assertFalse(rows[0]["warehouseActionRequired"])

    def test_card_identity_checks_every_identifier_and_leading_zero_barcode(self):
        for data in (card(nmID=902), card(vendorCode="OTHER"), card(sizes=[{"skus": ["123"]}]), {"cards": card()["cards"] * 2}):
            values, issues = c.verify_card(order(), data)
            self.assertEqual(values, {})
            self.assertTrue(issues)
        values, issues = c.verify_card(order(), card())
        self.assertEqual(issues, [])
        self.assertEqual(values["capacityGB"], 128)
        self.assertEqual(values["colorChinese"], "黑色")

    def test_exact_black_titanium_card_color_is_verified(self):
        source = card(characteristics=[
            {"id": 5023, "value": ["iPhone 16 Pro Max"]},
            {"id": 4424, "value": ["512"]},
            {"id": 14177449, "value": ["черный титан"]},
        ])
        values, issues = c.verify_card(order(), source)
        self.assertEqual(issues, [])
        self.assertEqual(values["colorOriginal"], "черный титан")
        self.assertEqual(values["colorChinese"], "黑色钛金属")

    def test_bad_metadata_blocks_identity_but_does_not_hide_order(self):
        result, rows, _, _ = self.run_store(FakeClient(card_data=card(vendorCode="WRONG")))
        self.assertTrue(result["ok"])
        self.assertTrue(rows[0]["warehouseActionRequired"])
        self.assertFalse(rows[0]["readyForWps"])
        self.assertTrue(rows[0]["metadataBlockers"])
        self.assertIsNone(rows[0].get("productModel"))

    def test_verified_metadata_is_reused_for_identical_identity(self):
        row = order()
        values, _ = c.verify_card(row, card())
        row.update(values)
        client = FakeClient()
        result, rows, _, _ = self.run_store(client, [row])
        self.assertTrue(result["ok"])
        self.assertFalse(any(url == c.CONTENT for _, url, _ in client.calls))

    def test_png_requires_complete_valid_chunks(self):
        self.assertTrue(c.valid_png(PNG))
        self.assertFalse(c.valid_png(PNG[:-5]))
        self.assertFalse(c.valid_png(PNG[:45] + b"bad" + PNG[48:]))

    def test_policy_rejects_mutation_and_redirect_target(self):
        for method, url in [("PATCH", c.MARKET + "/api/v3/orders/101/cancel"), ("POST", c.MARKET + "/api/v3/supplies"), ("GET", "https://example.com/api/v3/orders/new")]:
            with self.assertRaises(Exception):
                c.POLICY.authorize(method, url)


if __name__ == "__main__":
    unittest.main()
