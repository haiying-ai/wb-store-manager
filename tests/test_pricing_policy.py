from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wb_manager.pricing_policy import (
    PricingPolicyError,
    load_active_phone_online_price_formula,
    load_active_phone_policy,
    load_active_phone_store_scope,
)


class PricingPolicyTests(unittest.TestCase):
    def test_active_phone_policy_is_current_and_consistent(self):
        policy = load_active_phone_policy()
        self.assertEqual(policy.policy_id, "phone-hk-fbs-20260906-v6")
        self.assertAlmostEqual(policy.variable_rate_total, 0.228)
        self.assertAlmostEqual(policy.retained_price_factor, 0.772)
        self.assertAlmostEqual(policy.raw["variableRates"]["wbCommission"], 0.15)
        self.assertAlmostEqual(policy.raw["variableRates"]["advertisingReserve"], 0.03)
        self.assertAlmostEqual(policy.raw["variableRates"]["returnRiskReserve"], 0.02)
        self.assertAlmostEqual(policy.raw["variableRates"]["paymentWithdrawalReserve"], 0.008)
        self.assertAlmostEqual(policy.fixed_cost_cny, 12.0)
        self.assertAlmostEqual(policy.target_owner_profit_cny, 100.0)
        self.assertTrue(policy.raw["actualEventCosts"]["noFixedReturnLogisticsFallback"])
        self.assertIn("finance", policy.raw["actualEventCosts"]["returnLogisticsSource"].lower())
        self.assertNotIn("returnLogisticsCnyPerReturnedUnit", policy.raw["actualEventCosts"])

    def test_profit_and_floor_use_active_policy(self):
        policy = load_active_phone_policy()
        purchase_cost = 1956.52
        self.assertAlmostEqual(policy.owner_profit(2700, purchase_cost), 115.88, places=5)
        self.assertAlmostEqual(policy.minimum_buyer_price(purchase_cost), 2679.43005, places=4)

    def test_inconsistent_policy_fails_closed(self):
        source = load_active_phone_policy().raw
        broken = json.loads(json.dumps(source))
        broken["retainedPriceFactor"] = 0.892
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(PricingPolicyError):
                load_active_phone_policy(path)

    def test_active_phone_store_scope_requires_two_stores(self):
        scope = load_active_phone_store_scope()
        self.assertEqual(scope.scope_id, "phone-wb-two-store-20260821-v1")
        self.assertEqual([store.alias for store in scope.stores], ["owner-store", "new-store"])
        self.assertTrue(all(store.required for store in scope.stores))
        self.assertTrue(scope.execution_contract["preflightEveryStoreBeforeAnyWrite"])
        self.assertTrue(scope.execution_contract["readBackEveryStore"])

    def test_single_store_scope_fails_closed(self):
        source = load_active_phone_store_scope().raw
        broken = json.loads(json.dumps(source))
        broken["stores"] = broken["stores"][:1]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "single-store.json"
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(PricingPolicyError):
                load_active_phone_store_scope(path)

    def test_active_online_price_formula_matches_calibration(self):
        formula = load_active_phone_online_price_formula()
        pricing_policy = load_active_phone_policy()
        self.assertEqual(formula.policy_id, "phone-cost-floor-online-price-20260906-v6")
        self.assertEqual(formula.scope_id, "phone-wb-two-store-20260821-v1")
        self.assertEqual(formula.raw["supersedes"], "phone-benchmark-a-online-price-20260906-v5")
        self.assertEqual(formula.raw["financialGate"]["policyId"], pricing_policy.policy_id)
        self.assertAlmostEqual(
            formula.raw["financialGate"]["retainedPriceFactor"],
            pricing_policy.retained_price_factor,
        )
        self.assertAlmostEqual(
            formula.raw["financialGate"]["fixedCostCny"],
            pricing_policy.fixed_cost_cny,
        )
        self.assertAlmostEqual(
            formula.raw["financialGate"]["targetOwnerProfitCny"],
            pricing_policy.target_owner_profit_cny,
        )
        self.assertEqual(len(formula.model_prices_cny), 56)
        self.assertEqual(formula.discount_percent, 0)
        self.assertEqual(formula.buyer_price("IPHONE 13 128GB"), 2866)
        self.assertEqual(formula.buyer_price("IPHONE 12 MINI 128GB"), 1830)
        self.assertEqual(formula.buyer_price("IPHONE 12 MINI 256GB"), 2089)
        self.assertEqual(formula.raw["validation"]["status"], "two_store_uploaded_verified")
        self.assertEqual(formula.raw["validation"]["totalSkuCount"], 355)
        self.assertEqual(
            formula.raw["formula"]["buyerPriceCny"],
            "max(minimumSafePriceCny, preRepriceBuyerPriceCny)",
        )
        self.assertTrue(formula.raw["governance"]["benchmarkACannotOverrideFormula"])

    def test_previous_online_price_formula_is_preserved_as_historical_evidence(self):
        path = (
            Path(__file__).parents[1]
            / "config"
            / "pricing"
            / "phone_online_price_formula_20260824_v3.json"
        )
        historical = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(historical["policyId"], "phone-benchmark-a-online-price-20260824-v3")
        self.assertEqual(historical["status"], "historical")
        self.assertEqual(historical["financialGate"]["policyId"], "phone-hk-fbs-20260824-v4")
        self.assertEqual(historical["supersededBy"], "phone-benchmark-a-online-price-20260828-v4")
        v4 = json.loads(
            (
                Path(__file__).parents[1]
                / "config"
                / "pricing"
                / "phone_online_price_formula_20260828_v4.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(historical["modelPriceTable"], v4["modelPriceTable"])
        self.assertEqual(historical["validation"], v4["validation"])

    def test_v5_policy_and_v4_online_formula_are_preserved(self):
        root = Path(__file__).parents[1] / "config" / "pricing"
        policy = json.loads((root / "phone_pricing_policy_20260828_v5.json").read_text(encoding="utf-8"))
        online = json.loads((root / "phone_online_price_formula_20260828_v4.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["status"], "historical")
        self.assertEqual(policy["supersededBy"], "phone-hk-fbs-20260906-v6")
        self.assertEqual(online["status"], "historical")
        self.assertEqual(online["supersededBy"], "phone-benchmark-a-online-price-20260906-v5")

    def test_previous_v5_online_formula_is_preserved_as_historical_evidence(self):
        root = Path(__file__).parents[1] / "config" / "pricing"
        historical = json.loads(
            (root / "phone_online_price_formula_20260906_v5.json").read_text(encoding="utf-8")
        )
        self.assertEqual(historical["policyId"], "phone-benchmark-a-online-price-20260906-v5")
        self.assertEqual(historical["status"], "historical")
        self.assertEqual(historical["supersededBy"], "phone-cost-floor-online-price-20260906-v6")
        self.assertEqual(historical["validation"]["status"], "requires_two_store_reprice_refresh")
        self.assertEqual(len(historical["modelPriceTable"]), 54)

    def test_benchmark_evidence_cannot_override_cost_current_price_formula(self):
        source = load_active_phone_online_price_formula().raw
        changed = json.loads(json.dumps(source))
        changed["modelPriceTable"][0]["benchmarkAPriceCny"] = 999999
        changed["modelPriceTable"][0]["benchmarkCandidatePricesCny"].append(999999)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "benchmark-evidence.json"
            path.write_text(json.dumps(changed), encoding="utf-8")
            formula = load_active_phone_online_price_formula(path)
        self.assertEqual(
            formula.model_prices_cny[source["modelPriceTable"][0]["modelCapacity"]],
            source["modelPriceTable"][0]["calibratedFormulaPriceCny"],
        )

    def test_online_price_formula_rejects_cost_floor_mismatch(self):
        source = load_active_phone_online_price_formula().raw
        broken = json.loads(json.dumps(source))
        broken["modelPriceTable"][0]["purchaseCostCny"] += 100
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken-cost-floor.json"
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(PricingPolicyError):
                load_active_phone_online_price_formula(path)

    def test_inconsistent_online_price_formula_fails_closed(self):
        source = load_active_phone_online_price_formula().raw
        broken = json.loads(json.dumps(source))
        broken["modelPriceTable"][0]["calibratedFormulaPriceCny"] += 1
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken-online-formula.json"
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(PricingPolicyError):
                load_active_phone_online_price_formula(path)


if __name__ == "__main__":
    unittest.main()
