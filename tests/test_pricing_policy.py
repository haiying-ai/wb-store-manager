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
        self.assertEqual(policy.policy_id, "phone-hk-fbs-20260828-v5")
        self.assertAlmostEqual(policy.variable_rate_total, 0.208)
        self.assertAlmostEqual(policy.retained_price_factor, 0.792)
        self.assertAlmostEqual(policy.raw["variableRates"]["wbCommission"], 0.15)
        self.assertAlmostEqual(policy.raw["variableRates"]["advertisingAndReturnPool"], 0.03)
        self.assertNotIn("returnRiskReserve", policy.raw["variableRates"])
        self.assertTrue(policy.raw["actualEventCosts"]["noFixedReturnLogisticsFallback"])
        self.assertIn("finance", policy.raw["actualEventCosts"]["returnLogisticsSource"].lower())
        self.assertNotIn("returnLogisticsCnyPerReturnedUnit", policy.raw["actualEventCosts"])

    def test_profit_and_floor_use_active_policy(self):
        policy = load_active_phone_policy()
        purchase_cost = 1956.52
        self.assertAlmostEqual(policy.owner_profit(2647.04, purchase_cost), 127.93568, places=5)
        self.assertAlmostEqual(policy.minimum_buyer_price(purchase_cost), 2586.51515, places=4)

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
        self.assertEqual(formula.policy_id, "phone-benchmark-a-online-price-20260828-v4")
        self.assertEqual(formula.scope_id, "phone-wb-two-store-20260821-v1")
        self.assertEqual(formula.raw["supersedes"], "phone-benchmark-a-online-price-20260824-v3")
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
        self.assertEqual(len(formula.model_prices_cny), 54)
        self.assertEqual(formula.discount_percent, 0)
        self.assertEqual(formula.buyer_price("IPHONE 13 128GB"), 2653)
        self.assertEqual(formula.raw["validation"]["totalSkuCount"], 325)
        self.assertEqual(formula.raw["validation"]["mismatchCount"], 0)

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
        active = load_active_phone_online_price_formula().raw
        self.assertEqual(historical["modelPriceTable"], active["modelPriceTable"])
        self.assertEqual(historical["validation"], active["validation"])

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
