from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wb_manager.pricing_policy import PricingPolicyError, load_active_phone_online_price_formula


class NoHistoricalBaselineTests(unittest.TestCase):
    """Explicit no-history approval never weakens existing price/profit gates."""

    def candidate(self):
        root = Path(__file__).parents[1]
        source = root / "config/pricing/phone_online_price_formula_20260912_v7.json"
        data = json.loads(source.read_text(encoding="utf-8-sig"))
        data["policyId"] = "phone-cost-floor-online-price-20260915-test"
        data["financialGate"].update({
            "policyId": "phone-hk-fbs-20260915-test",
            "retainedPriceFactor": 0.782,
            "fixedCostCny": 12.0,
            "targetOwnerProfitCny": 85.0,
        })
        row = data["modelPriceTable"][0]
        row.update({
            "modelCapacity": "HONOR 50 256GB",
            "purchaseCostCny": 840,
            "fixedCostCny": 12,
            "preRepriceBuyerPriceCny": None,
            "preRepriceBaselineKind": "NO_HISTORICAL_BASELINE_USER_APPROVED",
            "minimumSafePriceCny": 1199,
            "calibratedFormulaPriceCny": 1199,
            "priceSource": "PROFIT_FLOOR",
        })
        data["modelPriceTable"] = [row]
        data["validation"]["modelCapacityCount"] = 1
        return data

    def load(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return load_active_phone_online_price_formula(path)

    def test_explicit_approved_null_baseline_uses_profit_floor(self):
        formula = self.load(self.candidate())
        self.assertEqual(formula.buyer_price("HONOR 50 256GB"), 1199)

    def test_null_baseline_without_approval_is_rejected(self):
        for marker in (None, "PRE_20260906_LIVE_PRICE", "CURRENT_PRICE_KEEP_EXCEPTION_NO_PRE_SEP6_HISTORY"):
            with self.subTest(marker=marker):
                data = self.candidate()
                row = data["modelPriceTable"][0]
                if marker is None:
                    row.pop("preRepriceBaselineKind")
                else:
                    row["preRepriceBaselineKind"] = marker
                with self.assertRaises(PricingPolicyError):
                    self.load(data)

    def test_missing_baseline_is_not_approved_null(self):
        data = self.candidate()
        data["modelPriceTable"][0].pop("preRepriceBuyerPriceCny")
        with self.assertRaises(PricingPolicyError):
            self.load(data)

    def test_no_history_marker_rejects_contradictory_numeric_baseline(self):
        data = self.candidate()
        data["modelPriceTable"][0]["preRepriceBuyerPriceCny"] = 1234
        with self.assertRaises(PricingPolicyError):
            self.load(data)

    def test_null_baseline_does_not_allow_below_profit_price(self):
        data = self.candidate()
        data["modelPriceTable"][0]["calibratedFormulaPriceCny"] = 1198
        self.assertLess(1198 * 0.782 - 840 - 12, 85)
        with self.assertRaises(PricingPolicyError):
            self.load(data)

    def test_null_baseline_does_not_allow_falsified_floor(self):
        data = self.candidate()
        data["modelPriceTable"][0].update({
            "minimumSafePriceCny": 1198,
            "calibratedFormulaPriceCny": 1198,
        })
        with self.assertRaises(PricingPolicyError):
            self.load(data)

    def test_null_baseline_must_declare_profit_floor_source(self):
        data = self.candidate()
        data["modelPriceTable"][0]["priceSource"] = "CURRENT_PRICE"
        with self.assertRaises(PricingPolicyError):
            self.load(data)

    def test_valid_historical_baseline_remains_a_floor(self):
        data = self.candidate()
        data["modelPriceTable"][0].update({
            "preRepriceBuyerPriceCny": 1234,
            "preRepriceBaselineKind": "PRE_20260906_LIVE_PRICE",
            "calibratedFormulaPriceCny": 1234,
            "priceSource": "CURRENT_PRICE",
        })
        self.assertEqual(self.load(data).buyer_price("HONOR 50 256GB"), 1234)
        data["modelPriceTable"][0].update({
            "calibratedFormulaPriceCny": 1199,
            "priceSource": "PROFIT_FLOOR",
        })
        with self.assertRaises(PricingPolicyError):
            self.load(data)

    def test_previous_verified_registry_still_loads_unchanged(self):
        source = Path(__file__).parents[1] / "config/pricing/phone_online_price_formula_20260912_v7.json"
        formula = load_active_phone_online_price_formula(source)
        self.assertEqual(formula.policy_id, "phone-cost-floor-online-price-20260912-v7")
        self.assertEqual(len(formula.model_prices_cny), 60)


if __name__ == "__main__":
    unittest.main()
