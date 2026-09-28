"""Offline regression coverage for CPM query collection and exclusion guards.

All API traffic uses an in-memory fake. Evidence, state and journals are redirected
to a temporary directory; these tests never load credentials or live policies.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import monitor_new_store_stable_cpm_20260909 as monitor  # noqa: E402


def fixture_policy():
    targets = []
    for aid, nm in ((39841968, 1452973747), (40045316, 1452974279)):
        vendor, imt, generation, suffix = monitor.IDENTITIES[nm]
        targets.append({
            "advertId": aid, "nmID": nm, "imtID": imt, "vendorCode": vendor,
            "modelGeneration": generation, "modelSuffix": suffix,
            "currency": "CNY", "bidType": "manual", "paymentType": "cpm",
            "placements": {"search": True, "recommendations": False},
            "searchBidKopecks": 2000, "dailySoftTargetCny": 15,
        })
    return {
        "policyId": monitor.POLICY_ID,
        "targets": targets,
        "monitor": {"statisticsFrom": "2026-09-09", "startedAt": "2026-09-09T12:00:00+08:00"},
        "wrongModelExclusion": {"maxMinusTerms": 1000, "emptyMinusResponseMeansEmpty": False},
    }


def query_row(query, spend=1.25):
    return {"norm_query": query, "spend": spend, "views": 12, "clicks": 2,
            "atbs": 0, "orders": 0, "currency": "CNY"}


class FakeClient:
    """Model replacement-style set-minus with controllable read/write failures."""

    def __init__(self, policy):
        self.targets = {t["advertId"]: t for t in policy["targets"]}
        self.queries = {aid: [query_row("iphone")] for aid in self.targets}
        self.minus_terms = {aid: ["existing exclusion"] for aid in self.targets}
        self.minus_responses = {}
        self.fullstats_responses = {}
        self.fail_write = set()
        self.apply_unknown_write = False
        self.calls = []
        self.today = "2026-09-10"

    def request(self, method, url, body=None, retries=0):
        parsed = urlsplit(url)
        path, params = parsed.path, parse_qs(parsed.query)
        if path.endswith("/get/cards/list"):
            nm = int(body["settings"]["filter"]["textSearch"])
            target = next(t for t in self.targets.values() if t["nmID"] == nm)
            aid = target["advertId"]
        elif body and "items" in body:
            aid = body["items"][0]["advert_id"]
        elif body and "advert_id" in body:
            aid = body["advert_id"]
        else:
            aid = int((params.get("id") or params.get("ids"))[0])
        target = self.targets[aid]
        self.calls.append({"path": path, "method": method, "advertId": aid,
                           "body": copy.deepcopy(body), "retries": retries})
        if retries != 0:
            raise AssertionError("Monitor must not blindly retry API calls")
        if path.endswith("/adverts"):
            data = {"adverts": [{
                "id": aid, "status": 9, "currency": "CNY", "bid_type": "manual",
                "settings": {"payment_type": "cpm", "placements": target["placements"]},
                "nm_settings": [{"nm_id": target["nmID"],
                                 "bids_kopecks": {"search": target["searchBidKopecks"]}}],
            }]}
        elif path.endswith("/get/cards/list"):
            data = {"cards": [{k: target[k] for k in ("nmID", "imtID", "vendorCode")}]}
        elif path.endswith("/budget"):
            data = {"total": 80, "currency": "CNY"}
        elif path.endswith("/fullstats"):
            override = self.fullstats_responses.get(aid)
            if override is not None:
                return self.response(override)
            metrics = {"views": 100, "clicks": 10, "sum": 5, "atbs": 0, "orders": 0}
            data = [{"advertId": aid, **metrics,
                     "days": [{"date": self.today, **metrics}]}]
        elif path.endswith("/get-minus"):
            sequence = self.minus_responses.get(aid, [])
            if sequence:
                return self.response(sequence.pop(0))
            data = {"items": [{"advert_id": aid, "nm_id": target["nmID"],
                               "norm_queries": self.minus_terms[aid]}]}
        elif path.endswith("/set-minus"):
            if aid not in self.fail_write or self.apply_unknown_write:
                self.minus_terms[aid] = list(body["norm_queries"])
            if aid in self.fail_write:
                raise TimeoutError("set-minus response lost after submission")
            data = {}
        elif path.endswith("/normquery/stats"):
            data = {"stats": [{"advert_id": aid, "nm_id": target["nmID"],
                               "stats": self.queries[aid]}]}
        else:
            raise AssertionError(f"Unexpected fake route: {method} {path}")
        return self.response(data)

    @staticmethod
    def response(value):
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            status, data = value
        else:
            status, data = 200, value
        return SimpleNamespace(status=status, ok=200 <= status < 300, data=copy.deepcopy(data))

    def writes(self):
        return [c for c in self.calls if c["path"].endswith("/set-minus")]


class StableCpmMonitorRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wb-monitor-regression-")
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        for name, value in (("OUT", self.out), ("STATE_PATH", self.out / "state.json")):
            patcher = mock.patch.object(monitor, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # An accidental fallback to the production client or credential loader fails locally.
        for target in ("token", "WbClient"):
            patcher = mock.patch.object(monitor, target, side_effect=AssertionError("No live access in tests"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.policy = fixture_policy()
        self.target = self.policy["targets"][1]
        self.aid = self.target["advertId"]
        self.client = FakeClient(self.policy)
        self.state = {}
        self.runs = 0

    def runner(self, execute=True):
        self.runs += 1
        runner = monitor.Monitor(self.policy, self.client, self.state,
                                 self.out / f"report-{self.runs}.json", execute=execute,
                                 sleep=lambda _: None)
        runner.checked_at = datetime(2026, 9, 10, 18, 0, tzinfo=monitor.TZ)
        runner.report["checkedAt"] = runner.checked_at.isoformat()
        return runner

    def test_missing_or_failed_minus_preserves_query_decisions_and_blocks_writes(self):
        scenarios = (
            ({"items": []}, "minus"),
            ((503, {"message": "temporarily unavailable"}), "503"),
            (TimeoutError("minus read timed out"), "timed out"),
        )
        for response, error_fragment in scenarios:
            with self.subTest(response=response):
                self.state = {}
                self.client.minus_responses[self.aid] = [response]
                self.client.queries[self.aid] = [query_row("айфон 18", 7.5), query_row("iphone")]
                result = self.runner().monitor_target(self.target)
                self.assertFalse(result["ok"])
                self.assertFalse(result["minusComplete"])
                self.assertIn(error_fragment, result["minusError"])
                self.assertTrue(result["queryStatsComplete"])
                self.assertEqual(result["queryDecisions"][0]["classification"], "wrong_model")
                self.assertEqual(result["queryDecisions"][0]["spend"], 7.5)
                self.assertNotEqual(result.get("minusBefore"), [])
                self.assertEqual(self.client.writes(), [])
                self.assertTrue(result["notify"])
                self.assertTrue(result["notificationReasons"])
                self.assertIsNone(result["wrongModelTraffic"]["unexcludedSpendCny"])

    def test_union_preserves_prior_terms_and_requires_independent_readback(self):
        self.client.queries[self.aid] = [query_row("айфон 18"), query_row("iphone 16 512")]
        result = self.runner().monitor_target(self.target)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["action"], "excluded_verified")
        writes = self.client.writes()
        self.assertEqual(len(writes), 1)
        self.assertEqual(set(writes[0]["body"]["norm_queries"]), {"existing exclusion", "айфон 18"})
        write_index = self.client.calls.index(writes[0])
        subsequent = self.client.calls[write_index + 1:]
        self.assertTrue(any(c["path"].endswith("/get-minus") for c in subsequent))
        self.assertTrue(any(c["path"].endswith("/adverts") for c in subsequent))
        self.assertEqual(set(result["minusAfter"]), {"existing exclusion", "айфон 18"})
        self.assertNotIn("pendingExclusion", self.state["targets"][str(self.aid)])
        journal = (self.out / "wrong_model_exclusion_journal.jsonl").read_text(encoding="utf-8")
        self.assertIn("write_readback_confirmed", journal)

    def test_changed_prewrite_minus_aborts_without_overwriting(self):
        self.client.queries[self.aid] = [query_row("iphone 12")]
        self.client.minus_responses[self.aid] = [
            {"items": [{"advert_id": self.aid, "nm_id": self.target["nmID"],
                        "norm_queries": ["existing exclusion"]}]},
            {"items": [{"advert_id": self.aid, "nm_id": self.target["nmID"],
                        "norm_queries": ["existing exclusion", "concurrent addition"]}]},
        ]
        result = self.runner().monitor_target(self.target)
        self.assertFalse(result["ok"])
        self.assertIn("concurrently", result["error"])
        self.assertEqual(self.client.writes(), [])

    def test_unknown_write_is_reconciled_without_replay(self):
        self.client.queries[self.aid] = [query_row("iphone 12")]
        self.client.fail_write.add(self.aid)
        self.client.apply_unknown_write = True
        first = self.runner().monitor_target(self.target)
        self.assertFalse(first["ok"])
        self.assertIn("pendingExclusion", self.state["targets"][str(self.aid)])
        self.assertEqual(len(self.client.writes()), 1)
        second = self.runner().monitor_target(self.target)
        self.assertTrue(second["ok"], second.get("error"))
        self.assertTrue(second["reconciledPriorExclusion"])
        self.assertNotIn("pendingExclusion", self.state["targets"][str(self.aid)])
        self.assertEqual(len(self.client.writes()), 1)

    def test_unresolved_unknown_write_never_replays(self):
        self.client.queries[self.aid] = [query_row("iphone 12")]
        self.client.fail_write.add(self.aid)
        self.runner().monitor_target(self.target)
        result = self.runner().monitor_target(self.target)
        self.assertFalse(result["ok"])
        self.assertIn("unknown", result["error"])
        self.assertIn("pendingExclusion", self.state["targets"][str(self.aid)])
        self.assertEqual(len(self.client.writes()), 1)

    def test_missing_postwrite_minus_keeps_pending_until_reconciled(self):
        self.client.queries[self.aid] = [query_row("iphone 12")]
        before = {"items": [{"advert_id": self.aid, "nm_id": self.target["nmID"],
                             "norm_queries": ["existing exclusion"]}]}
        self.client.minus_responses[self.aid] = [before, before, {"items": []}]
        result = self.runner().monitor_target(self.target)
        self.assertFalse(result["ok"])
        self.assertIn("pendingExclusion", self.state["targets"][str(self.aid)])
        self.assertNotIn("lastConfirmedExclusion", self.state["targets"][str(self.aid)])
        self.assertNotEqual(result["action"], "excluded_verified")
        self.assertEqual(len(self.client.writes()), 1)
        reconciled = self.runner().monitor_target(self.target)
        self.assertTrue(reconciled["reconciledPriorExclusion"])
        self.assertEqual(len(self.client.writes()), 1)

    def test_generic_mixed_capacity_and_accessory_terms_are_preserved(self):
        terms = ["iphone", "iphone 15 или 16", "iphone 16 512", "iphone 16 белый",
                 "iphone 16 с чехлом", "чехол для iphone 12", "iphone samsung 12"]
        self.client.queries[self.aid] = [query_row(q) for q in terms]
        result = self.runner().monitor_target(self.target)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual([d["action"] for d in result["queryDecisions"]], ["keep"] * len(terms))
        self.assertEqual(self.client.writes(), [])
        for query in ("iphone 14", "айфон 14 256"):
            self.assertEqual(monitor.classify_model_query(query, 14, "pro")[0], "broad_or_uncertain")

    def test_fullstats_failure_does_not_hide_queries_or_allow_exclusion(self):
        self.client.fullstats_responses[self.aid] = (502, {"message": "stats unavailable"})
        self.client.queries[self.aid] = [query_row("iphone 12")]
        result = self.runner().monitor_target(self.target)
        self.assertFalse(result["ok"])
        self.assertFalse(result["fullstatsComplete"])
        self.assertTrue(result["queryStatsComplete"])
        self.assertEqual(result["queryDecisions"][0]["classification"], "wrong_model")
        self.assertIsNone(result["todayMetrics"])
        self.assertEqual(self.client.writes(), [])

    def test_one_campaign_failure_does_not_prevent_other_campaign_collection(self):
        first_target = self.policy["targets"][0]
        self.client.minus_responses[first_target["advertId"]] = [{"items": []}]
        report = self.runner().run()
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["targets"]), 2)
        self.assertFalse(report["targets"][0]["ok"])
        self.assertTrue(report["targets"][1]["ok"])
        self.assertTrue(all(r["queryStatsComplete"] for r in report["targets"]))
        self.assertEqual(report["combinedTodaySpendCny"], 10)

    def test_unknown_exclusion_risk_notifies_only_for_new_or_growing_spend(self):
        self.client.queries[self.aid] = [query_row("iphone 12", 2)]
        self.client.minus_responses[self.aid] = [{"items": []}] * 4
        first = self.runner().monitor_target(self.target)
        self.assertTrue(first["notify"])
        second = self.runner().monitor_target(self.target)
        self.assertFalse(second["notify"], second["notificationReasons"])
        self.client.queries[self.aid][0]["spend"] = 3
        third = self.runner().monitor_target(self.target)
        self.assertTrue(third["notify"])
        fourth = self.runner().monitor_target(self.target)
        self.assertFalse(fourth["notify"], fourth["notificationReasons"])
        self.assertEqual(self.client.writes(), [])

    def test_already_excluded_historical_cost_does_not_create_risk_notification(self):
        self.client.queries[self.aid] = [query_row("iphone 12", 100)]
        self.client.minus_terms[self.aid].append("iphone 12")
        result = self.runner().monitor_target(self.target)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertFalse(result["notify"], result["notificationReasons"])
        self.assertEqual(result["wrongModelTraffic"]["unexcludedSpendCny"], 0)
        self.assertEqual(self.client.writes(), [])

    def test_excluded_cluster_cost_growth_requests_timing_check_without_rewriting(self):
        self.client.queries[self.aid] = [query_row("iphone 12", 428.94)]
        self.client.minus_terms[self.aid].append("iphone 12")
        for spend in (428.94, 428.94, 420, 428.94):
            self.client.queries[self.aid][0]["spend"] = spend
            unchanged = self.runner().monitor_target(self.target)
            self.assertTrue(unchanged["ok"], unchanged.get("error"))
            self.assertFalse(unchanged["notify"], unchanged["notificationReasons"])
            self.assertEqual(unchanged["wrongModelTraffic"]["excludedClusterStatisticsIncreases"], [])
        self.client.queries[self.aid][0]["spend"] = 455.46
        grown = self.runner().monitor_target(self.target)
        self.assertTrue(grown["ok"], grown.get("error"))
        self.assertTrue(grown["notify"])
        self.assertEqual([r["kind"] for r in grown["notificationReasons"]],
                         ["excluded_cluster_statistics_increased_needs_timing_check"])
        increases = grown["wrongModelTraffic"]["excludedClusterStatisticsIncreases"]
        self.assertEqual(increases, [{"query": "iphone 12", "spendCny": 455.46,
                                      "increaseSinceObservedHighWaterCny": 26.52}])
        self.assertEqual(grown["wrongModelTraffic"]["candidates"], [])
        self.assertEqual(grown["wrongModelTraffic"]["unexcludedSpendCny"], 0)
        for spend in (455.46, 450, 455.46):
            self.client.queries[self.aid][0]["spend"] = spend
            repeated = self.runner().monitor_target(self.target)
            self.assertFalse(repeated["notify"], repeated["notificationReasons"])
            self.assertEqual(repeated["wrongModelTraffic"]["excludedClusterStatisticsIncreases"], [])
        self.policy["monitor"]["statisticsFrom"] = "2026-09-10"
        self.client.queries[self.aid][0]["spend"] = 460
        new_period = self.runner().monitor_target(self.target)
        self.assertFalse(new_period["notify"], new_period["notificationReasons"])
        self.assertEqual(self.client.writes(), [])

    def test_known_unexcluded_risk_uses_high_water_and_detects_new_queries(self):
        self.client.queries[self.aid] = [query_row("iphone 12", 5)]
        first = self.runner(execute=False).monitor_target(self.target)
        self.assertTrue(first["notify"])
        self.assertEqual(first["wrongModelTraffic"]["unexcludedSpendCny"], 5)
        for spend in (4, 5):
            self.client.queries[self.aid][0]["spend"] = spend
            result = self.runner(execute=False).monitor_target(self.target)
            self.assertFalse(result["notify"], result["notificationReasons"])
        self.client.queries[self.aid][0]["spend"] = 6
        grown = self.runner(execute=False).monitor_target(self.target)
        self.assertTrue(grown["notify"])
        self.client.queries[self.aid].append(query_row("айфон 18", 1))
        added = self.runner(execute=False).monitor_target(self.target)
        self.assertTrue(added["notify"])
        self.assertEqual(added["wrongModelTraffic"]["unexcludedSpendCny"], 7)
        self.assertEqual(self.client.writes(), [])

    def test_read_only_mode_never_submits_exclusions(self):
        self.client.queries[self.aid] = [query_row("iphone 12")]
        result = self.runner(execute=False).monitor_target(self.target)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result["exclusionPlan"]["write_required"])
        self.assertEqual(self.client.writes(), [])
        persisted = json.loads((self.out / "report-1.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["mode"], "read_only")


if __name__ == "__main__":
    unittest.main()
