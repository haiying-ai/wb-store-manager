from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import CategoryPaths
from .sku_alias import canonical_sku


REQUIRED_PRICING_FIELDS = [
    "productCost",
    "currencyRate",
    "firstMileFreight",
    "fulfillmentCost",
    "commissionRate",
    "plannedSalePrice",
    "plannedDiscountRate",
    "targetNetMarginRate",
]


@dataclass(frozen=True)
class PricingInput:
    product_cost: float
    currency_rate: float
    first_mile_freight: float
    fulfillment_cost: float
    commission_rate: float
    planned_sale_price: float
    planned_discount_rate: float
    target_net_margin_rate: float
    payment_fee_rate: float = 0.0
    ads_reserve_rate: float = 0.0
    tax_rate: float = 0.0
    return_loss_rate: float = 0.0
    safety_buffer: float = 0.0


def calculate_pricing(data: PricingInput) -> dict[str, Any]:
    final_price = data.planned_sale_price * (1 - data.planned_discount_rate)
    variable_rate = data.commission_rate + data.payment_fee_rate + data.ads_reserve_rate + data.tax_rate + data.return_loss_rate
    fixed_cost = data.product_cost * data.currency_rate + data.first_mile_freight + data.fulfillment_cost + data.safety_buffer
    variable_cost = final_price * variable_rate
    net_profit = final_price - fixed_cost - variable_cost
    net_margin = net_profit / final_price if final_price else -1
    break_even = fixed_cost / (1 - variable_rate) if variable_rate < 1 else math.inf
    minimum_acceptable = fixed_cost / (1 - variable_rate - data.target_net_margin_rate) if variable_rate + data.target_net_margin_rate < 1 else math.inf
    return {
        "finalSalePrice": round(final_price, 2),
        "fixedCostRub": round(fixed_cost, 2),
        "variableCostRub": round(variable_cost, 2),
        "netProfitRub": round(net_profit, 2),
        "netMarginRate": round(net_margin, 4),
        "breakEvenPriceRub": round(break_even, 2) if math.isfinite(break_even) else None,
        "minimumAcceptablePriceRub": round(minimum_acceptable, 2) if math.isfinite(minimum_acceptable) else None,
        "passesGate": net_margin >= data.target_net_margin_rate,
    }


def create_pricing_review(paths: CategoryPaths, sku: str, *, input_file: Path | None = None) -> Path:
    sku = canonical_sku(sku, category=paths.category)
    out_dir = paths.generated / sku
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = _load_pricing_data(paths, sku, input_file)
    missing = [field for field in REQUIRED_PRICING_FIELDS if raw.get(field) in (None, "")]
    review: dict[str, Any] = {
        "sku": sku,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "status": "blocked" if missing else "ready",
        "missingRequiredFields": missing,
        "input": raw,
        "result": None,
        "riskNotes": [],
    }
    if missing:
        review["riskNotes"].append("Critical pricing fields are missing; do not move this SKU to online listing materials.")
    else:
        result = calculate_pricing(_pricing_input_from_dict(raw))
        review["result"] = result
        if not result["passesGate"]:
            review["status"] = "blocked"
            review["riskNotes"].append("Net margin is below the target minimum.")

    (out_dir / "pricing_review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "pricing_review.md").write_text(_pricing_markdown(review), encoding="utf-8")
    return out_dir


def _load_pricing_data(paths: CategoryPaths, sku: str, input_file: Path | None) -> dict[str, Any]:
    if input_file and input_file.exists():
        return json.loads(input_file.read_text(encoding="utf-8-sig"))
    kb = paths.kb / "product_facts.json"
    if not kb.exists():
        return {}
    facts = json.loads(kb.read_text(encoding="utf-8-sig"))
    for product in facts.get("products", {}).values():
        if str(product.get("sku", "")).upper() == sku.upper() or str(product.get("sourceFolder", "")).upper() == sku.upper():
            pricing = dict(product.get("pricing", {}))
            if "plannedSalePriceRub" in pricing and "plannedSalePrice" not in pricing:
                pricing["plannedSalePrice"] = pricing["plannedSalePriceRub"]
            return pricing
    return {}


def _pricing_input_from_dict(data: dict[str, Any]) -> PricingInput:
    return PricingInput(
        product_cost=float(data["productCost"]),
        currency_rate=float(data["currencyRate"]),
        first_mile_freight=float(data["firstMileFreight"]),
        fulfillment_cost=float(data["fulfillmentCost"]),
        commission_rate=float(data["commissionRate"]),
        planned_sale_price=float(data["plannedSalePrice"]),
        planned_discount_rate=float(data["plannedDiscountRate"]),
        target_net_margin_rate=float(data["targetNetMarginRate"]),
        payment_fee_rate=float(data.get("paymentFeeRate", 0)),
        ads_reserve_rate=float(data.get("adsReserveRate", 0)),
        tax_rate=float(data.get("taxRate", 0)),
        return_loss_rate=float(data.get("returnLossRate", 0)),
        safety_buffer=float(data.get("safetyBuffer", 0)),
    )


def _pricing_markdown(review: dict[str, Any]) -> str:
    lines = [f"# Pricing Review - {review['sku']}", "", f"Status: {review['status']}", ""]
    if review["missingRequiredFields"]:
        lines.append("## Missing Required Fields")
        lines.extend(f"- {field}" for field in review["missingRequiredFields"])
        lines.append("")
    if review["result"]:
        lines.append("## Result")
        for key, value in review["result"].items():
            lines.append(f"- {key}: {value}")
        lines.append("")
    if review["riskNotes"]:
        lines.append("## Risk Notes")
        lines.extend(f"- {note}" for note in review["riskNotes"])
        lines.append("")
    return "\n".join(lines)
