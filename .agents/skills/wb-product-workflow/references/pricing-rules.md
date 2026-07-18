# Pricing Rules

Profitability is a hard gate. Do not treat a source-material price as a planned sale price unless the user explicitly confirms it. If a review fails the profit gate, the SKU must not be listed or repriced live until the inputs are corrected.

## Reusable Category Pricing Method

For each category, keep four groups separate:

1. Product cost: confirmed purchase cost in one settlement currency.
2. Variable sale costs: each percentage of the buyer-paid price, such as WB commission, managed-operations commission, advertising reserve, return-risk reserve, payment fee, and taxes only when actually applicable.
3. Fixed per-unit costs: logistics, packaging, inspection, storage, or one consolidated amount. Never count an item both as a fixed cost and a percentage reserve.
4. Profit target: fixed profit per unit or target net-margin rate. Record whose profit it is; a managed-operations commission is revenue to the operator but a cost in the product owner's profit calculation.

Use the buyer-paid price as the base:

- fixed-profit mode: `minimumBuyerPrice = (productCost + fixedCost + targetProfit) / (1 - variableRateSum)`
- target-margin mode: `minimumBuyerPrice = (productCost + fixedCost) / (1 - variableRateSum - targetNetMarginRate)`
- net profit: `buyerPrice * (1 - variableRateSum) - productCost - fixedCost`

Only after calculating the buyer-paid price, derive the WB list/upload price:

- `wbUploadPrice = ceil(minimumBuyerPrice / (1 - plannedDiscountRate))`
- after WB accepts integer price and discount, recompute the actual discounted price and profit; block the write if profit falls below target.

Before reusing a category model, verify from the seller's own portal and reports:

- exact WB subject and sales-model commission;
- settlement currency and whether exchange risk actually exists;
- payment/acquiring and withdrawal fees;
- advertising DRR from real campaigns;
- return rate and return loss;
- logistics and other fixed costs;
- applicable tax treatment.

Use account-specific portal tariffs and actual settlement reports over generic API defaults when they conflict. Preserve assumptions as named fields; do not hide them in a single unexplained multiplier.

## Confirmed Phone Formula

The current phone model is owner-profit accounting for WB FBS smartphones under the Hong Kong seller account.

Variable costs based on buyer-paid price:

- WB FBS smartphone commission: 5%
- managed-operations commission: 2%
- advertising reserve: 2%
- return-risk reserve: 2%
- payment/withdrawal reserve: 0.8%
- total variable rate: 11.8%
- retained-price factor: 88.2% (`0.882`)

Fixed cost:

- 12 CNY per unit, represented as 138 RUB at the template rate of 11.5 RUB/CNY.

Explicit exclusions:

- no 6% tax reserve;
- no 3% exchange-loss reserve;
- no separate logistics, storage, packaging, or inspection charges in addition to the consolidated 12 CNY fixed cost.

Profit target:

- 220 CNY per unit for the product owner;
- the 2% managed-operations commission is separately retained as operator service revenue.

Final phone equations:

- `ownerNetProfitCny = buyerPriceCny * 0.882 - purchaseCostCny - 12`
- `minimumBuyerPriceCny = (purchaseCostCny + 12 + 220) / 0.882`
- with the current planned 40% WB discount: `wbUploadPriceCny = ceil(minimumBuyerPriceCny / 0.60)`

These values are business decisions as of 2026-07-12. Recalibrate them from live commission, settlement, advertising, return, and logistics data rather than silently carrying them into unrelated categories.
