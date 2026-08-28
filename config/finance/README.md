# Phone financial reporting policy

`active_phone_financial_reporting_policy.json` is the active order-profit and cost-reporting policy for every enabled WB smartphone store in `stores.json`.

It does not control WB live prices. Live price strategy remains governed by `config/pricing/active_phone_pricing_policy.json` and `config/pricing/active_phone_pricing_store_scope.json`.

The buyer-paid amount and platform deductions come from settlement evidence. Non-purchase-cost accounting rules use the latest active template; purchase cost is selected from an effective-dated cost version using the order creation timestamp.

Return logistics has no 8/16 CNY fallback. A final report requires the actual signed platform deduction linked by store plus `orderId`, `shkId`, or `srid`. The fixed 3% advertising-and-return pool remains the P&L convention: actual return logistics consumes it first and the remainder is booked advertising provision. Actual advertising spend is reconciliation evidence only and is never deducted a second time.

Report lifecycle is `PENDING_DATA` while required evidence is incomplete, `FINAL` after all completeness and stability gates pass, and `RESTATED` for a later complete replacement version caused by a late settlement, refund, or correction. A restatement never overwrites the earlier report.

Historical policies are append-only under `policies/`. Secrets, raw finance rows, the SQLite ledger, cost catalogs, and generated reports remain local and must not be committed.
