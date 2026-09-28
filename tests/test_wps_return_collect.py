"""Focused offline tests for the WB seller-return collector.

The suite uses an injected ``FinanceSources.goods_returns`` compatible source;
it never reads credentials or performs network requests.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from tools.wps_warehouse import return_collect as rc
from wb_manager.client import ApiResult
from wb_manager.finance_sources import SourceResult


NOW = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)
OWNER = "owner-store"
NEW = "new-store"
SECRET = "Bearer test-secret-that-must-never-be-archived"


def return_row(**changes):
    row = {
        "barcode": "2053238380992",
        "brand": "Apple",
        "completedDt": None,
        "dstOfficeAddress": "Москва, Новороссийская Улица 25к1",
        "dstOfficeId": 206860,
        "expiredDt": None,
        "isStatusActive": 1,
        "nmId": 1248939553,
        "orderDt": "2026-07-31",
        "orderId": 5369901737,
        "readyToReturnDt": "2026-09-08T09:55:37",
        "reason": "",
        "returnType": "Возврат товара, который приехал по МП, продавцу",
        "shkId": 56287024150,
        "srid": "mp.008bd81a1816464aa842041ef3042d97.r",
        "status": "Готов к выдаче",
        "stickerId": "56287024150",
        "subjectName": "Смартфоны",
        "techSize": "0",
    }
    row.update(changes)
    return row


def card_payload(*, nm_id=1248939553, article="IPHONE 17AIR-256GB-BLACK",
                 barcode="2053238380992", duplicate=False, color="Черный"):
    card = {
        "nmID": nm_id,
        "vendorCode": article,
        "title": "Apple iPhone 17 Air 256GB",
        "brand": "Apple",
        "sizes": [{"skus": [barcode]}],
        "characteristics": [
            {"id": 5023, "value": ["iPhone 17 Air"]},
            {"id": 4424, "value": ["256"]},
            {"id": 14177449, "value": [color]},
        ],
    }
    return {"cards": [card, dict(card)] if duplicate else [card]}


class FakeMetadataResolver:
    def __init__(self, payload=None, *, error=None):
        self.payload = card_payload() if payload is None else payload
        self.error = error
        self.calls = []

    def __call__(self, alias, record):
        self.calls.append((alias, dict(record)))
        if self.error:
            raise self.error
        return self.payload


class FakeSources:
    """Small FinanceSources-compatible test double with range-aware rows."""

    def __init__(self, rows=(), *, complete=True, blocker=None, leak_secret=False):
        self.rows = [dict(row) for row in rows]
        self.complete = complete
        self.blocker = blocker
        self.calls = []
        # A collector must never serialize source/client credential attributes.
        self.token = SECRET
        self.client = type("SecretClient", (), {"token": SECRET})()
        self.leak_secret = leak_secret

    def goods_returns(self, start, end):
        start_date = date.fromisoformat(str(start)[:10])
        end_date = date.fromisoformat(str(end)[:10])
        self.calls.append((start_date, end_date))
        rows = [
            dict(row)
            for row in self.rows
            if start_date <= date.fromisoformat(str(row["orderDt"])[:10]) <= end_date
        ]
        requests = [{"status": 200, "dateFrom": str(start), "dateTo": str(end)}]
        if self.leak_secret:
            requests[0].update(
                headers={"Authorization": SECRET}, token=SECRET
            )
        return SourceResult(
            "goods_returns",
            200 if self.complete else 503,
            self.complete,
            rows,
            requests,
            self.blocker,
        )


class ReturnClassificationTests(unittest.TestCase):
    def test_waiting_pickup_requires_every_official_predicate(self):
        exact = rc.classify_return(return_row())
        self.assertEqual(exact["code"], "WAITING_PICKUP")
        self.assertEqual(exact["category"], "待提货")
        self.assertTrue(exact["pickupActionRequired"])
        self.assertTrue(exact["definitive"])

        near_misses = (
            return_row(status="В пути"),
            return_row(isStatusActive=0),
            return_row(readyToReturnDt=None),
            return_row(completedDt="2026-09-08T12:00:00"),
            return_row(expiredDt="2026-09-08T12:00:00"),
        )
        for row in near_misses:
            with self.subTest(row=row):
                classified = rc.classify_return(row)
                self.assertNotEqual(classified["code"], "WAITING_PICKUP")
                self.assertFalse(classified["pickupActionRequired"])

    def test_other_states_are_separate_and_never_actionable_as_pickup(self):
        cases = (
            (
                return_row(status="В пути", readyToReturnDt=None),
                "IN_TRANSIT",
                "运输中",
            ),
            (
                return_row(status="Выдано", isStatusActive=0,
                           completedDt="2026-09-08T12:00:00"),
                "PICKED_UP",
                "已提货",
            ),
            (
                return_row(status="Истек срок хранения", isStatusActive=0,
                           expiredDt="2026-09-08T12:00:00"),
                "EXPIRED",
                "已过期",
            ),
            (return_row(status="Архив", isStatusActive=0,
                        readyToReturnDt=None), "HISTORICAL", "历史记录"),
            (return_row(status="Неизвестный новый статус",
                        readyToReturnDt=None), "UNKNOWN", "未知"),
        )
        for row, code, category in cases:
            with self.subTest(code=code):
                classified = rc.classify_return(row)
                self.assertEqual(classified["code"], code)
                self.assertEqual(classified["category"], category)
                self.assertFalse(classified["pickupActionRequired"])
                self.assertTrue(classified["reason"])

    def test_normalized_record_is_sheet_ready_and_article_match_is_store_scoped(self):
        evidence = [
            {"storeAlias": NEW, "orderId": "5369901737", "article": "WRONG-STORE"},
            {"storeAlias": OWNER, "orderId": "5369901737", "article": "IPHONE 17AIR-256GB-BLACK"},
        ]
        record = rc.normalize_return(
            return_row(),
            OWNER,
            shipping_orders=evidence,
            checked_at="2026-09-08T10:00:00+00:00",
            evidence_paths=["archive/raw-owner.json"],
        )

        expected = {
            "storeAlias": OWNER,
            "returnKey": f"{OWNER}:56287024150",
            "shkId": "56287024150",
            "stickerId": "56287024150",
            "orderId": "5369901737",
            "srid": "mp.008bd81a1816464aa842041ef3042d97.r",
            "orderDt": "2026-07-31",
            "nmId": "1248939553",
            "barcode": "2053238380992",
            "article": "IPHONE 17AIR-256GB-BLACK",
            "statusOriginal": "Готов к выдаче",
            "statusCode": "WAITING_PICKUP",
            "statusChinese": "待提货",
            "pickupActionRequired": True,
            "readyToReturnDt": "2026-09-08T09:55:37",
            "completedDt": None,
            "expiredDt": None,
            "dstOfficeId": "206860",
            "dstOfficeAddress": "Москва, Новороссийская Улица 25к1",
            "reason": "",
            "returnType": "Возврат товара, который приехал по МП, продавцу",
            "checkedAt": "2026-09-08T10:00:00+00:00",
            "evidencePaths": ["archive/raw-owner.json"],
        }
        for key, value in expected.items():
            self.assertEqual(record.get(key), value, key)

        no_cross_store_article = rc.normalize_return(
            return_row(), OWNER, shipping_orders=evidence[:1]
        )
        self.assertFalse(no_cross_store_article.get("article"))


class ReturnCollectorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def collector(self, sources, *, now=NOW, metadata_resolver=None):
        return rc.ReturnCollector(
            root=self.root,
            source_factory=lambda alias: sources[alias],
            now=now,
            lookback_days=31,
            request_throttle_seconds=0,
            metadata_resolver=metadata_resolver,
        )

    def test_same_shkid_in_two_stores_remains_two_isolated_identities(self):
        # Both dates fall within each store's configured opening window.  The
        # same physical id is valid because store alias is part of the key.
        owner_row = return_row(orderDt="2026-09-01")
        new_row = return_row(
            orderDt="2026-09-01", orderId=999, srid="new-store-srid"
        )
        sources = {
            OWNER: FakeSources([owner_row]),
            NEW: FakeSources([new_row]),
        }
        metadata = FakeMetadataResolver()
        shipping = [
            {"storeAlias": OWNER, "orderId": "5369901737",
             "article": "IPHONE 17AIR-256GB-BLACK"},
            {"storeAlias": NEW, "orderId": "999",
             "article": "IPHONE 17AIR-256GB-BLACK"},
        ]

        result = self.collector(
            sources, metadata_resolver=metadata
        ).collect([OWNER, NEW], shipping)

        self.assertTrue(result["all_sources_ok"])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(len(result["pickup_records"]), 2)
        self.assertEqual(
            {(r["storeAlias"], r["shkId"]) for r in result["records"]},
            {(OWNER, "56287024150"), (NEW, "56287024150")},
        )
        self.assertEqual(
            {r["returnKey"] for r in result["records"]},
            {f"{OWNER}:56287024150", f"{NEW}:56287024150"},
        )

    def test_same_store_primary_key_conflicts_are_blockers(self):
        for changed_field, changed_value in (
            ("orderId", 9999999999),
            ("srid", "conflicting-srid"),
        ):
            with self.subTest(field=changed_field):
                root = self.root / changed_field
                first = return_row(orderDt="2026-09-01")
                second = return_row(
                    orderDt="2026-09-01", **{changed_field: changed_value}
                )
                source = FakeSources([first, second])
                result = rc.ReturnCollector(
                    root=root,
                    source_factory=lambda _alias: source,
                    now=NOW,
                    request_throttle_seconds=0,
                ).collect([OWNER])

                self.assertTrue(
                    any(
                        b.get("store_alias") == OWNER
                        and b.get("return_key") == f"{OWNER}:56287024150"
                        and "conflict" in str(b.get("reason", "")).lower()
                        for b in result["blockers"]
                    ),
                    result["blockers"],
                )
                self.assertEqual(result["pickup_records"], [])

    def test_raw_and_normalized_archives_strip_credentials_recursively(self):
        source = FakeSources(
            [return_row(orderDt="2026-09-01")], leak_secret=True
        )
        result = self.collector({OWNER: source}).collect([OWNER])

        run_dir = Path(result["run_dir"])
        archived = list(run_dir.rglob("*.json"))
        self.assertTrue(archived)
        self.assertTrue(any("raw" in (part.lower() for part in path.parts)
                            for path in archived))
        self.assertTrue((run_dir / "collector.json").is_file())
        combined = "\n".join(path.read_text(encoding="utf-8") for path in archived)
        self.assertIn("5369901737", combined)
        self.assertNotIn(SECRET, combined)
        self.assertNotIn('"Authorization"', combined)
        self.assertNotIn('"token"', combined.lower())

    def test_active_order_date_anchor_is_persisted_and_rechecked_after_31_days(self):
        row = return_row(orderDt="2026-07-31")
        first_source = FakeSources([row])
        shipping = [{"storeAlias": OWNER, "orderId": "5369901737",
                     "article": "IPHONE 17AIR-256GB-BLACK"}]
        first = self.collector(
            {OWNER: first_source},
            now=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
            metadata_resolver=FakeMetadataResolver(),
        ).collect([OWNER], shipping)
        self.assertEqual(len(first["pickup_records"]), 1)

        state_path = Path(first["state_path"])
        state_text = state_path.read_text(encoding="utf-8")
        self.assertIn("2026-07-31", state_text)
        self.assertIn("56287024150", state_text)

        second_source = FakeSources([row])
        second = self.collector(
            {OWNER: second_source}, now=NOW,
            metadata_resolver=FakeMetadataResolver(),
        ).collect([OWNER], shipping)

        self.assertEqual(len(second["pickup_records"]), 1)
        self.assertTrue(
            any(start <= date(2026, 7, 31) <= end
                for start, end in second_source.calls),
            second_source.calls,
        )
        self.assertTrue(
            any(start >= date(2026, 8, 9) for start, _ in second_source.calls),
            second_source.calls,
        )
        for index, left in enumerate(second_source.calls):
            for right in second_source.calls[index + 1:]:
                self.assertTrue(left[1] < right[0] or right[1] < left[0])

    def test_incomplete_refresh_is_not_zero_and_does_not_erase_active_anchor(self):
        row = return_row(orderDt="2026-07-31")
        self.collector(
            {OWNER: FakeSources([row])},
            now=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
        ).collect([OWNER])
        before = (self.root / "return-collector-state.json").read_text(encoding="utf-8")

        failing = FakeSources([], complete=False, blocker="temporary source failure")
        result = self.collector({OWNER: failing}, now=NOW).collect([OWNER])

        self.assertFalse(result["all_sources_ok"])
        self.assertTrue(result["blockers"])
        after = (self.root / "return-collector-state.json").read_text(encoding="utf-8")
        self.assertIn("56287024150", after)
        self.assertIn("2026-07-31", after)
        # An incomplete read may update diagnostic metadata but cannot drop the anchor.
        self.assertEqual(
            json.loads(before)["stores"][OWNER]["items"],
            json.loads(after)["stores"][OWNER]["items"],
        )

    def test_waiting_pickup_metadata_is_triple_verified_and_archived(self):
        source = FakeSources([return_row(orderDt="2026-09-01")])
        metadata = FakeMetadataResolver()
        shipping = [{"storeAlias": OWNER, "orderId": "5369901737",
                     "article": "IPHONE 17AIR-256GB-BLACK"}]

        result = self.collector(
            {OWNER: source}, metadata_resolver=metadata
        ).collect([OWNER], shipping)

        self.assertEqual(len(metadata.calls), 1)
        called_alias, called = metadata.calls[0]
        self.assertEqual(called_alias, OWNER)
        self.assertEqual(
            (called["nmId"], called["article"], called["barcode"]),
            ("1248939553", "IPHONE 17AIR-256GB-BLACK", "2053238380992"),
        )
        self.assertEqual(len(result["pickup_records"]), 1)
        record = result["pickup_records"][0]
        self.assertEqual(record["productModel"], "iPhone 17 Air")
        self.assertEqual(record["capacityGB"], 256)
        self.assertEqual(record["colorChinese"], "黑色")
        self.assertTrue(record["productIdentityVerified"])
        self.assertTrue(record["tableReady"])
        self.assertTrue(Path(record["metadataEvidencePath"]).is_file())

    def test_metadata_lookup_requires_exact_same_store_order_article(self):
        source = FakeSources([return_row(orderDt="2026-09-01")])
        metadata = FakeMetadataResolver()
        cross_store = [{"storeAlias": NEW, "orderId": "5369901737",
                        "article": "IPHONE 17AIR-256GB-BLACK"}]

        result = self.collector(
            {OWNER: source}, metadata_resolver=metadata
        ).collect([OWNER], cross_store)

        self.assertEqual(metadata.calls, [])
        self.assertEqual(result["pickup_records"], [])
        self.assertEqual(len(result["blocked_pickup_records"]), 1)
        self.assertIn(
            "return_product_article_not_verified",
            result["blocked_pickup_records"][0]["metadataBlockers"],
        )

        transit_metadata = FakeMetadataResolver()
        transit = FakeSources([
            return_row(orderDt="2026-09-01", status="В пути",
                       readyToReturnDt=None)
        ])
        transit_result = self.collector(
            {OWNER: transit}, metadata_resolver=transit_metadata
        ).collect([OWNER], [{"storeAlias": OWNER, "orderId": "5369901737",
                             "article": "IPHONE 17AIR-256GB-BLACK"}])
        self.assertEqual(transit_metadata.calls, [])
        self.assertEqual(transit_result["pickup_records"], [])

    def test_metadata_network_structure_and_identity_failures_block_entry(self):
        cases = (
            ("network", FakeMetadataResolver(error=TimeoutError("secret token"))),
            ("structure", FakeMetadataResolver(payload={"unexpected": []})),
            ("nm", FakeMetadataResolver(payload=card_payload(nm_id=999))),
            ("article", FakeMetadataResolver(payload=card_payload(article="OTHER"))),
            ("barcode", FakeMetadataResolver(payload=card_payload(barcode="OTHER"))),
            ("duplicate", FakeMetadataResolver(payload=card_payload(duplicate=True))),
        )
        shipping = [{"storeAlias": OWNER, "orderId": "5369901737",
                     "article": "IPHONE 17AIR-256GB-BLACK"}]
        for name, resolver in cases:
            with self.subTest(case=name):
                root = self.root / ("metadata-" + name)
                collector = rc.ReturnCollector(
                    root=root,
                    source_factory=lambda _alias: FakeSources([
                        return_row(orderDt="2026-09-01")
                    ]),
                    now=NOW,
                    request_throttle_seconds=0,
                    metadata_resolver=resolver,
                )
                result = collector.collect([OWNER], shipping)
                self.assertEqual(result["pickup_records"], [])
                self.assertEqual(len(result["blocked_pickup_records"]), 1)
                record = result["blocked_pickup_records"][0]
                self.assertFalse(record["productIdentityVerified"])
                self.assertIsNone(record["metadataEvidencePath"])
                self.assertTrue(record["metadataBlockers"])
                self.assertTrue(any(
                    blocker.get("reason") == "return_product_metadata_unverified"
                    for blocker in result["blockers"]
                ))

    def test_production_requester_uses_owner_credentials_and_builds_table_ready_record(self):
        token = "owner-secret-token-used-only-by-mocked-client"
        token_file = self.root / "owner-token.env"
        token_file.write_text(f"WB_API_TOKEN={token}\n", encoding="utf-8")
        scope_file = self.root / "stores.json"
        scope_file.write_text(
            json.dumps(
                {
                    "stores": [
                        {
                            "alias": OWNER,
                            "displayName": "瑞盈8597",
                            "enabled": True,
                            "openedAt": "2026-07-15",
                            "tokenEnv": "WB_API_TOKEN",
                            "tokenFile": str(token_file),
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        class MockWbClient:
            instances = []

            def __init__(self, supplied_token):
                self.token = supplied_token
                self.calls = []
                self.__class__.instances.append(self)

            def request(self, method, url, *, body=None, retries=0):
                self.calls.append(
                    {"method": method, "url": url, "body": body, "retries": retries}
                )
                if urlsplit(url).netloc == "seller-analytics-api.wildberries.ru":
                    query = parse_qs(urlsplit(url).query)
                    start = date.fromisoformat(query["dateFrom"][0])
                    end = date.fromisoformat(query["dateTo"][0])
                    rows = (
                        [return_row()]
                        if start <= date(2026, 7, 31) <= end
                        else []
                    )
                    return ApiResult(200, True, {"report": rows}, {})
                if url == rc.CONTENT_CARD_URL:
                    return ApiResult(200, True, card_payload(), {})
                raise AssertionError(f"unexpected URL: {url}")

        shipping = [
            {
                "storeAlias": OWNER,
                "orderId": "5369901737",
                "article": "IPHONE 17AIR-256GB-BLACK",
                "nmId": "1248939553",
                "barcodes": ["2053238380992"],
            }
        ]
        with patch.object(rc, "WbClient", MockWbClient):
            result = rc.ReturnCollector(
                root=self.root / "live-path",
                now=NOW,
                store_scope=scope_file,
                request_throttle_seconds=0,
            ).collect([OWNER], shipping)

        self.assertTrue(result["all_sources_ok"], result["blockers"])
        self.assertEqual(len(MockWbClient.instances), 1)
        client = MockWbClient.instances[0]
        self.assertEqual(client.token, token)
        content_calls = [call for call in client.calls if call["url"] == rc.CONTENT_CARD_URL]
        self.assertEqual(len(content_calls), 1)
        self.assertEqual(content_calls[0]["method"], "POST")
        self.assertEqual(content_calls[0]["retries"], 0)
        self.assertEqual(
            content_calls[0]["body"],
            {
                "settings": {
                    "cursor": {"limit": 100},
                    "filter": {"textSearch": "1248939553", "withPhoto": -1},
                }
            },
        )
        self.assertEqual(len(result["pickup_records"]), 1)
        record = result["pickup_records"][0]
        self.assertEqual(record["returnKey"], f"{OWNER}:56287024150")
        self.assertEqual(record["productModel"], "iPhone 17 Air")
        self.assertEqual(record["capacityGB"], 256)
        self.assertEqual(record["colorChinese"], "黑色")
        self.assertTrue(record["productIdentityVerified"])
        self.assertTrue(record["tableReady"])
        metadata_path = Path(record["metadataEvidencePath"])
        self.assertTrue(metadata_path.is_file())
        self.assertIn("product-card", metadata_path.name)

        archived_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in Path(result["run_dir"]).rglob("*.json")
        )
        self.assertNotIn(token, archived_text)
        self.assertNotIn('"Authorization"', archived_text)


if __name__ == "__main__":
    unittest.main()
