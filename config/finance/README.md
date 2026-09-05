# Phone financial reporting policy

`active_phone_financial_reporting_policy.json` is the active order-profit and cost-reporting policy for every enabled WB smartphone store in `stores.json`.

It does not control WB live prices. Live price strategy remains governed by `config/pricing/active_phone_pricing_policy.json` and `config/pricing/active_phone_pricing_store_scope.json`.

The buyer-paid amount and platform deductions come from settlement evidence. Non-purchase-cost accounting rules use the latest active template; purchase cost is selected from an effective-dated cost version using the order creation timestamp.

Return logistics has no 8/16 CNY fallback. A final report requires the actual signed platform deduction linked by store plus `orderId`, `shkId`, or `srid`. The fixed 3% advertising-and-return pool remains the P&L convention: actual return logistics consumes it first and the remainder is booked advertising provision. Actual advertising spend is reconciliation evidence only and is never deducted a second time.

Report lifecycle is `PENDING_DATA` while required evidence is incomplete, `FINAL` after all completeness and stability gates pass, and `RESTATED` for a later complete replacement version caused by a late settlement, refund, or correction. A restatement never overwrites the earlier report.

Historical policies are append-only under `policies/`. Secrets, raw finance rows, the SQLite ledger, cost catalogs, and generated reports remain local and must not be committed.

## Settlement-balance advertising and legacy withdrawal debt

Advertising campaign budgets are allocated directly from the same store's WB
mutual-settlement `balance` (`net`, request `type=1`); no prior withdrawal or
external cash recharge is required. The allocation remains a separate funding
action with a fresh balance/reserve preflight, exact store/campaign/amount
approval, one write attempt, and campaign-budget readback. A finance report
never performs that action implicitly.

Keep campaign-budget allocation, actual campaign consumption, and later WB
settlement-cycle charging as separate evidence layers. Settlement-cycle
advertising deductions reconcile the configured 3% advertising-and-return pool
and must not be deducted again through `otherPlatformCostCny` or another profit
field.

The private policy supplies the cutover date. Withdrawal-linked advertising
debt recorded before that cutover remains immutable and separately
reconciled. Withdrawals on or after the cutover use `settlement_balance`: the
advertising due, withheld, and debt fields are zero/not applicable, so those
withdrawals do not create new advertising-fee debt. New campaign allocations,
campaign consumption, and settlement-cycle deductions never offset legacy
debt; only successful, explicitly identified same-store legacy-debt payment
evidence can settle it.
