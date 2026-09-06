from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_PHONE_POLICY = ROOT / "config" / "pricing" / "active_phone_pricing_policy.json"
ACTIVE_PHONE_STORE_SCOPE = ROOT / "config" / "pricing" / "active_phone_pricing_store_scope.json"
ACTIVE_PHONE_ONLINE_PRICE_FORMULA = ROOT / "config" / "pricing" / "active_phone_online_price_formula.json"


class PricingPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class PhonePricingPolicy:
    policy_id: str
    effective_from: str
    retained_price_factor: float
    fixed_cost_cny: float
    target_owner_profit_cny: float
    variable_rate_total: float
    raw: dict[str, Any]

    def owner_profit(self, buyer_price_cny: float, purchase_cost_cny: float) -> float:
        return buyer_price_cny * self.retained_price_factor - purchase_cost_cny - self.fixed_cost_cny

    def minimum_buyer_price(self, purchase_cost_cny: float) -> float:
        return (purchase_cost_cny + self.fixed_cost_cny + self.target_owner_profit_cny) / self.retained_price_factor


@dataclass(frozen=True)
class PhonePricingStore:
    alias: str
    token_env: str
    token_file: str
    required: bool


@dataclass(frozen=True)
class PhonePricingStoreScope:
    scope_id: str
    effective_from: str
    stores: tuple[PhonePricingStore, ...]
    execution_contract: dict[str, Any]
    raw: dict[str, Any]


@dataclass(frozen=True)
class PhoneOnlinePriceFormula:
    policy_id: str
    effective_from: str
    scope_id: str
    model_prices_cny: dict[str, int]
    discount_percent: int
    raw: dict[str, Any]

    def buyer_price(self, model_capacity: str) -> int:
        try:
            return self.model_prices_cny[model_capacity]
        except KeyError as exc:
            raise PricingPolicyError(
                f"Online price formula has no model-capacity key: {model_capacity}"
            ) from exc


def load_active_phone_policy(path: Path = ACTIVE_PHONE_POLICY) -> PhonePricingPolicy:
    if not path.exists():
        raise PricingPolicyError(f"Active phone pricing policy is missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("status") != "active":
        raise PricingPolicyError(f"Pricing policy is not active: {data.get('policyId')}")
    policy_id = str(data.get("policyId") or "").strip()
    if not policy_id:
        raise PricingPolicyError("Active pricing policy has no policyId")
    rates = data.get("variableRates") or {}
    if "advertisingAndReturnPool" in rates:
        components = [
            "wbCommission",
            "managedOperations",
            "advertisingAndReturnPool",
            "paymentWithdrawalReserve",
        ]
    else:
        components = [
            "wbCommission",
            "managedOperations",
            "advertisingReserve",
            "returnRiskReserve",
            "paymentWithdrawalReserve",
        ]
    calculated_total = sum(float(rates.get(name, math.nan)) for name in components)
    declared_total = float(rates.get("total", math.nan))
    retained = float(data.get("retainedPriceFactor", math.nan))
    if not math.isfinite(calculated_total) or not math.isclose(calculated_total, declared_total, abs_tol=1e-9):
        raise PricingPolicyError(f"Variable-rate components do not equal declared total in {policy_id}")
    if not math.isclose(1 - declared_total, retained, abs_tol=1e-9):
        raise PricingPolicyError(f"Retained factor does not equal 1 - variable rates in {policy_id}")
    fixed = float(data.get("fixedCostCny", math.nan))
    target = float(data.get("targetOwnerProfitCny", math.nan))
    if retained <= 0 or fixed < 0 or target < 0:
        raise PricingPolicyError(f"Invalid numeric values in {policy_id}")
    actual_events = data.get("actualEventCosts") or {}
    forbidden_return_keys = {
        "returnLogisticsCnyPerReturnedUnit",
        "actualReturnLogisticsPerUnitCny",
        "returnLogisticsFallbackCnyPerReturnedUnit",
    }
    if any(name in actual_events for name in forbidden_return_keys):
        raise PricingPolicyError(
            f"Pricing policy {policy_id} must not assume a fixed 8/16 CNY return-logistics fee"
        )
    source = str(actual_events.get("returnLogisticsSource") or "").lower()
    if actual_events.get("noFixedReturnLogisticsFallback") is not True or "finance" not in source:
        raise PricingPolicyError(
            f"Pricing policy {policy_id} must source actual return logistics from WB finance rows"
        )
    return PhonePricingPolicy(
        policy_id=policy_id,
        effective_from=str(data.get("effectiveFrom") or ""),
        retained_price_factor=retained,
        fixed_cost_cny=fixed,
        target_owner_profit_cny=target,
        variable_rate_total=declared_total,
        raw=data,
    )


def load_active_phone_store_scope(path: Path = ACTIVE_PHONE_STORE_SCOPE) -> PhonePricingStoreScope:
    if not path.exists():
        raise PricingPolicyError(f"Active phone pricing store scope is missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("status") != "active":
        raise PricingPolicyError(f"Phone pricing store scope is not active: {data.get('scopeId')}")
    scope_id = str(data.get("scopeId") or "").strip()
    if not scope_id:
        raise PricingPolicyError("Active phone pricing store scope has no scopeId")
    if data.get("platform") != "WB" or data.get("category") != "smartphones":
        raise PricingPolicyError(f"Unexpected phone pricing store scope: {scope_id}")
    raw_stores = data.get("stores") or []
    if not isinstance(raw_stores, list) or len(raw_stores) < 2:
        raise PricingPolicyError(f"Phone pricing requires at least two stores: {scope_id}")
    stores: list[PhonePricingStore] = []
    aliases: set[str] = set()
    token_envs: set[str] = set()
    for item in raw_stores:
        alias = str(item.get("alias") or "").strip()
        token_env = str(item.get("tokenEnv") or "").strip()
        token_file = str(item.get("tokenFile") or "").strip()
        required = item.get("requiredForPriceStrategy") is True
        if not alias or not token_env or not token_file or not required:
            raise PricingPolicyError(f"Invalid required store entry in {scope_id}: {alias or '<missing>'}")
        if alias in aliases or token_env in token_envs:
            raise PricingPolicyError(f"Duplicate store alias or token environment in {scope_id}: {alias}")
        if any(key.lower() in {"token", "tokenvalue", "apikey", "apikeyvalue"} for key in item):
            raise PricingPolicyError(f"Store scope must not contain credential values: {alias}")
        aliases.add(alias)
        token_envs.add(token_env)
        stores.append(PhonePricingStore(alias=alias, token_env=token_env, token_file=token_file, required=required))
    contract = data.get("executionContract") or {}
    required_contract_flags = (
        "planEveryRequiredStore",
        "preflightEveryStoreBeforeAnyWrite",
        "requireExplicitJointApproval",
        "approvalMustListPerStoreSkuCounts",
        "useStoreSpecificNmIds",
        "includeZeroStockSkusByDefault",
        "blockAndReportUnmatchedSkus",
        "readBackEveryStore",
        "reportPartialFailureImmediately",
    )
    missing_flags = [name for name in required_contract_flags if contract.get(name) is not True]
    if missing_flags:
        raise PricingPolicyError(f"Phone pricing store scope is missing required execution gates: {missing_flags}")
    governance = data.get("governance") or {}
    if governance.get("noSingleStoreFallback") is not True or governance.get("failClosedIfAnyRequiredStoreIsUnavailable") is not True:
        raise PricingPolicyError(f"Phone pricing store scope does not fail closed: {scope_id}")
    return PhonePricingStoreScope(
        scope_id=scope_id,
        effective_from=str(data.get("effectiveFrom") or ""),
        stores=tuple(stores),
        execution_contract=contract,
        raw=data,
    )


def load_active_phone_online_price_formula(
    path: Path = ACTIVE_PHONE_ONLINE_PRICE_FORMULA,
) -> PhoneOnlinePriceFormula:
    if not path.exists():
        raise PricingPolicyError(f"Active phone online-price formula is missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("status") != "active":
        raise PricingPolicyError(
            f"Phone online-price formula is not active: {data.get('policyId')}"
        )
    policy_id = str(data.get("policyId") or "").strip()
    scope_id = str(data.get("scopeId") or "").strip()
    if not policy_id or not scope_id:
        raise PricingPolicyError("Phone online-price formula requires policyId and scopeId")
    if data.get("platform") != "WB" or data.get("category") != "smartphones":
        raise PricingPolicyError(f"Unexpected online-price formula scope: {policy_id}")
    formula = data.get("formula") or {}
    discount = formula.get("discountPercent")
    if discount != 0 or formula.get("uploadListPriceCny") != "buyerPriceCny":
        raise PricingPolicyError(
            f"Online-price formula must use direct integer price and zero discount: {policy_id}"
        )
    if formula.get("buyerPriceCny") != "max(minimumSafePriceCny, preRepriceBuyerPriceCny)":
        raise PricingPolicyError(
            f"Online-price formula must use the approved cost/current-price rule: {policy_id}"
        )
    if formula.get("benchmarkAUsage") != "comparison evidence only; never overrides the approved cost/current-price formula":
        raise PricingPolicyError(
            f"Benchmark A must be evidence-only in the online-price formula: {policy_id}"
        )
    financial_gate = data.get("financialGate") or {}
    try:
        retained = float(financial_gate.get("retainedPriceFactor"))
        fixed_cost = float(financial_gate.get("fixedCostCny"))
        target_profit = float(financial_gate.get("targetOwnerProfitCny"))
    except (TypeError, ValueError) as exc:
        raise PricingPolicyError(
            f"Online-price formula has an invalid financial gate: {policy_id}"
        ) from exc
    if not 0 < retained <= 1 or fixed_cost < 0 or target_profit < 0:
        raise PricingPolicyError(
            f"Online-price formula has an invalid financial gate: {policy_id}"
        )
    model_rows = data.get("modelPriceTable") or []
    if not isinstance(model_rows, list) or not model_rows:
        raise PricingPolicyError(f"Online-price formula has no model price table: {policy_id}")
    model_prices: dict[str, int] = {}
    for row in model_rows:
        key = str(row.get("modelCapacity") or "").strip()
        benchmark = row.get("benchmarkAPriceCny")
        purchase_cost = row.get("purchaseCostCny")
        row_fixed_cost = row.get("fixedCostCny")
        pre_reprice = row.get("preRepriceBuyerPriceCny")
        floor = row.get("minimumSafePriceCny")
        calibrated = row.get("calibratedFormulaPriceCny")
        source = row.get("priceSource")
        if not key or key in model_prices:
            raise PricingPolicyError(f"Duplicate or missing model-capacity key in {policy_id}: {key}")
        if not isinstance(purchase_cost, (int, float)) or purchase_cost <= 0:
            raise PricingPolicyError(f"Invalid purchase cost for {key} in {policy_id}")
        if not isinstance(row_fixed_cost, (int, float)) or not math.isclose(
            float(row_fixed_cost), fixed_cost, abs_tol=1e-9
        ):
            raise PricingPolicyError(f"Row fixed cost does not match the financial gate for {key}")
        if not all(isinstance(value, int) and value > 0 for value in (pre_reprice, floor, calibrated)):
            raise PricingPolicyError(f"Invalid integer price for {key} in {policy_id}")
        expected_floor = math.ceil(
            (float(purchase_cost) + fixed_cost + target_profit) / retained
        )
        if floor != expected_floor:
            raise PricingPolicyError(f"Minimum safe price does not match the cost formula for {key}")
        if calibrated != max(floor, pre_reprice):
            raise PricingPolicyError(
                f"Calibrated price does not equal max(floor, pre-reprice price) for {key}"
            )
        expected_source = "PROFIT_FLOOR" if floor >= pre_reprice else "CURRENT_PRICE"
        if source != expected_source:
            raise PricingPolicyError(f"Invalid price source for {key} in {policy_id}")
        owner_profit = calibrated * retained - float(purchase_cost) - fixed_cost
        if owner_profit + 1e-9 < target_profit:
            raise PricingPolicyError(f"Calibrated price is below the owner-profit floor for {key}")
        if row.get("benchmarkRole") != "EVIDENCE_ONLY":
            raise PricingPolicyError(f"Benchmark A is not marked evidence-only for {key}")
        candidates = row.get("benchmarkCandidatePricesCny")
        if not isinstance(candidates, list) or any(
            not isinstance(value, int) or value <= 0 for value in candidates
        ):
            raise PricingPolicyError(f"Invalid benchmark evidence for {key} in {policy_id}")
        if benchmark is not None and (
            not isinstance(benchmark, int)
            or benchmark <= 0
            or benchmark not in candidates
        ):
            raise PricingPolicyError(f"Invalid benchmark summary for {key} in {policy_id}")
        model_prices[key] = calibrated
    validation = data.get("validation") or {}
    if validation.get("status") != "two_store_uploaded_verified":
        raise PricingPolicyError(f"Online-price formula lacks verified two-store readback: {policy_id}")
    if validation.get("modelCapacityCount") != len(model_prices):
        raise PricingPolicyError(f"Online-price formula model count is inconsistent: {policy_id}")
    validation_stores = validation.get("stores") or {}
    declared_stores = data.get("stores") or []
    if set(validation_stores) != set(declared_stores):
        raise PricingPolicyError(f"Online-price formula validation store scope is inconsistent: {policy_id}")
    planned_total = 0
    for alias in declared_stores:
        store_validation = validation_stores.get(alias) or {}
        planned = store_validation.get("plannedSkuCount")
        if not isinstance(planned, int) or planned <= 0:
            raise PricingPolicyError(f"Invalid planned SKU count for {alias} in {policy_id}")
        for name in (
            "uploadReadbackCount",
            "independentReadbackCount",
            "planMatchCount",
            "discountZeroCount",
        ):
            if store_validation.get(name) != planned:
                raise PricingPolicyError(
                    f"Incomplete {name} validation for {alias} in {policy_id}"
                )
        if store_validation.get("mismatchCount") != 0:
            raise PricingPolicyError(f"Price mismatches remain for {alias} in {policy_id}")
        planned_total += planned
    if validation.get("totalSkuCount") != planned_total:
        raise PricingPolicyError(f"Online-price formula total SKU count is inconsistent: {policy_id}")
    governance = data.get("governance") or {}
    required_flags = (
        "currentPriceIsTimeBound",
        "refreshPurchaseCostsBeforeEveryReprice",
        "refreshBenchmarkBeforeEveryReprice",
        "benchmarkAIsEvidenceOnly",
        "benchmarkACannotOverrideFormula",
        "noPriceDecreaseFromPreRepriceSnapshot",
        "preflightBothStores",
        "requireExplicitJointApprovalBeforeLiveWrite",
        "readBackBothStoresAfterLiveWrite",
        "doesNotAuthorizeLiveWrite",
    )
    if any(governance.get(name) is not True for name in required_flags):
        raise PricingPolicyError(f"Online-price formula governance is incomplete: {policy_id}")
    return PhoneOnlinePriceFormula(
        policy_id=policy_id,
        effective_from=str(data.get("effectiveFrom") or ""),
        scope_id=scope_id,
        model_prices_cny=model_prices,
        discount_percent=discount,
        raw=data,
    )
