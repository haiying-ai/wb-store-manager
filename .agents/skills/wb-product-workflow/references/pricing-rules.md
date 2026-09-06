# Pricing Rules

Profitability is a hard gate. Do not treat a source-material price as a planned sale price unless the user explicitly confirms it. If a review fails the profit gate, the SKU must not be listed or repriced live until the inputs are corrected.

## Reusable Category Pricing Method

For each category, keep four groups separate:

1. Product cost: confirmed purchase cost in one settlement currency.
2. Variable sale costs: each percentage of the buyer-paid price, such as WB commission, managed-operations commission, one combined advertising-and-return pool, payment fee, and taxes only when actually applicable.
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

Active policy ID: `phone-hk-fbs-20260906-v6`

Canonical machine-readable source: `config/pricing/active_phone_pricing_policy.json`.

Active multi-store pricing scope ID: `phone-wb-two-store-20260821-v1`.

Canonical store-scope source: `config/pricing/active_phone_pricing_store_scope.json`.

From 2026-08-21 onward, every smartphone price-strategy change applies to both required WB stores: `owner-store` and `new-store`. Each store has its own SKU catalog and nmIDs, so one formula/strategy change must produce separate per-store plans rather than reuse one store's upload payload. Preflight both stores before any write, present per-store SKU counts in one approval request, include zero-stock phone SKUs by default, block and report unmatched SKUs, and read back both stores independently. WB has no cross-account transaction, so a partial write failure must be reported immediately and never described as two-store completion.

Before any phone profit calculation, recommendation, dry-run, or live write, run `python tools/validate_active_pricing_policy.py` and load the canonical JSON through `wb_manager.pricing_policy.load_active_phone_policy()`. Values written in older scripts, spreadsheets, reports, chats, or archived run folders are historical evidence only and must never override the active policy. If validation fails, stop; there is no fallback formula.

Variable costs based on buyer-paid price:

- WB FBS smartphone commission: 15%
- managed-operations commission: 2%
- return-loss reserve: 2%
- promotion reserve: 3%
- payment/withdrawal reserve: 0.8%
- total variable rate: 22.8%
- retained-price factor: 77.2% (`0.772`)

Fixed cost:

- freight: 12 CNY per unit, represented as 138 RUB at the template rate of 11.5 RUB/CNY.

Promotion and return-loss allocation:

- reserve 2% of buyer-paid price for return loss and 3% for promotion as separate planning lines;
- read actual return logistics from WB finance rows for reconciliation; never assume a fixed 8 or 16 CNY charge;
- do not deduct actual return logistics a second time from planning profit after the 2% return-loss reserve has already been included;
- report any difference between the 2% reserve and actual return cost as reconciliation variance.

Explicit exclusions:

- no 6% tax reserve;
- no 3% exchange-loss reserve;
- no separate logistics, storage, packaging, or inspection charges in addition to the consolidated 12 CNY fixed cost.

Profit target:

- at least 100 CNY per unit for the product owner;
- the 2% managed-operations commission is separately retained as operator service revenue.

Final phone equations:

- `ownerNetProfitCny = buyerPriceCny * 0.772 - purchaseCostCny - 12`
- `minimumBuyerPriceCny = (purchaseCostCny + 12 + 100) / 0.772`
- current direct-price strategy: `wbUploadPriceCny = ceil(minimumBuyerPriceCny)` and `discountPercent = 0`

The 15% smartphone commission took effect in the confirmed 2026-07-22 repricing plan and supersedes the 5% / 0.892 policy from 2026-07-19. Recalibrate these values from live commission, settlement, advertising, return, and logistics data by publishing a new versioned policy file; never edit historical policy evidence in place.

The 2026-09-06 v6 policy supersedes v5. It separates a 2% return-loss reserve from the 3% promotion reserve, retains the 2% managed-operations commission, 15% WB commission, 0.8% payment/withdrawal reserve, and 12 CNY freight, and raises the owner's minimum profit target to 100 CNY. Total variable rate is 22.8% and the retained-price factor is 0.772. This policy activation does not itself authorize a live price write; refresh purchase costs, benchmark A, and both stores, then obtain joint approval before repricing.
