# Active Pricing Policy Registry

This directory is the only source of truth for current pricing formulas.

- Current phone policy: `active_phone_pricing_policy.json`
- Current phone store scope: `active_phone_pricing_store_scope.json`
- Current phone online-price formula: `active_phone_online_price_formula.json`
- Loader: `wb_manager.pricing_policy.load_active_phone_policy()`
- Store-scope loader: `wb_manager.pricing_policy.load_active_phone_store_scope()`
- Online-price formula loader: `wb_manager.pricing_policy.load_active_phone_online_price_formula()`
- Preflight: `python tools/validate_active_pricing_policy.py`

Rules:

1. Never select a formula by newest filename, folder timestamp, report date, or chat history.
2. Historical spreadsheets and run folders are evidence, not active policy.
3. A policy change creates a new `policyId`, updates the active JSON atomically, updates `pricing-rules.md`, and must pass preflight tests before any recommendation or write.
4. Missing, invalid, or internally inconsistent active policy blocks calculation. There is no fallback.
5. Every calculation artifact must record `policyId`, effective date, live-price snapshot path, and cost-source path.
6. Every smartphone price-strategy change must use every required store in the active store scope. Single-store fallback is blocked.
7. A joint dry-run and explicit approval must list each store's SKU count before any live write; each store requires an independent readback.
8. Current online price is resolved by the active cost/current-price formula: normalize model-capacity, calculate `ceil((purchaseCostCny + fixedCostCny + targetOwnerProfitCny) / retainedPriceFactor)`, then use `max(minimumSafePriceCny, preRepriceBuyerPriceCny)` as the integer direct price with 0% discount. Only a explicitly approved null baseline marked `NO_HISTORICAL_BASELINE_USER_APPROVED` uses the minimum price alone; never use a keep-current placeholder as historical truth.
9. Benchmark A is comparison evidence only. Preserve its compatibility fields and refresh the evidence when useful, but it must never override the approved cost/current-price formula.
10. The active online-price formula's `financialGate` policy ID, retained-price factor, fixed cost, and target owner profit must match the active phone pricing policy. A mismatch blocks use even when the model-price table is otherwise valid.
11. The active policy separates the return-loss reserve and the promotion reserve, applies the platform commission, a managed-operations fee, a payment/withdrawal reserve and a fixed freight cost, and enforces a minimum owner profit per unit. Actual rates exist only in the local active policy file and are never committed. Previous order-time policies are retained in the financial-policy timeline.
12. Before replacing an active policy or online-price formula, preserve the prior version as append-only historical evidence. Never overwrite a historical policy or calibrated model-price table.
13. The active online-price formula carries verified model-capacity rows. A repricing run covers every SKU in the required stores and requires an independent readback per store. Costs and non-target price records are preserved.
14. Purchase-cost and pre-reprice price snapshots are time-bound. Refresh both required stores and the authorized cost source before a future repricing; an already verified table is historical evidence, not permission to replay a write.
