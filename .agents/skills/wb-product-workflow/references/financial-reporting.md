# WB Financial Reporting

Use this contract for completed-order accounting, first-run financial bootstrap, daily financial ingestion, weekly and monthly reports, return-charge reconciliation, managed-operations fees, withdrawals, and advertising funding. Financial reports are read-only. Any movement of money is a separate `funding` workflow.

## Store scope and periods

- Enumerate every enabled WB owner store from the active store registry on every run. Do not hard-code the current two stores; a newly enabled store joins the next collection and report automatically.
- Keep credentials, raw responses, watermarks, normalized rows, and source status isolated by `store_id`. Consolidate only after each store-level calculation passes.
- The daily collector records newly available completed orders and return/settlement changes. Resume from the last durable successful watermark and backfill every missed calendar day after network, permission, or scheduler failure. Re-fetch unresolved orders until their finance evidence settles; idempotent upserts must not duplicate orders or charges.
- A normal weekly report runs Monday for the prior Monday-through-Sunday period. A normal monthly report runs on day 1 for the prior calendar month. Derive the current date, cutoff, and half-open period boundaries from the actual current clock plus the configured scheduler and business timezones; never select a period from a user-described weekday or date label when it conflicts with those values.
- The first successful financial run is a bootstrap from each store's opening date through the cutoff, not only the prior week. The first monthly report also covers opening-to-cutoff history and must be labelled as a bootstrap rather than an ordinary one-month comparison.

## Source precedence and order identity

- Preserve immutable raw evidence and source status. Normalize by store plus stable WB order/sale/settlement identities; retain every source identifier and evidence path needed to reproduce a row.
- Use completed orders as the reporting population, then link sales, returns, cancellations, financial settlements, logistics, commissions, and advertising evidence. Never join across stores.
- Platform financial rows and confirmed settlement readbacks take precedence over a template assumption. Statistics or order APIs can establish lifecycle facts but cannot replace settlement amounts.
- Select purchase cost from the version whose effective interval contains the order creation timestamp. Completion, sale, return, report, or settlement time must not switch an old order to a newer cost.
- A return fee is the sum of actual linked return-logistics deductions in WB financial rows. Never default to 8 CNY, 16 CNY, one row, or two rows. If a return exists but the charge is missing, unsettled, duplicated, or cannot be linked confidently, keep the order and report non-final.

## Calculation contract

For every order, retain both the amount and evidence source for buyer-paid revenue, purchase cost, WB commission and logistics, payment/withdrawal cost, managed-operations fee, combined advertising-and-return pool, actual return logistics, advertising allocation, and owner profit.

- Managed-operations fee: `max(0, netRetainedBuyerPaidTurnover) * 2%`, using the signed settlement evidence defined by the active financial policy.
- Combined advertising-and-return pool: `max(0, netRetainedBuyerPaidTurnover) * 3%`.
- Return logistics: the actual linked WB financial deduction, never an assumed percentage or fixed amount.
- Advertising allocation: `max(0, combinedPool - actualReturnLogistics)`.
- Combined pool expense used in owner profit: `actualReturnLogistics + advertisingAllocation`, which equals the 3% pool unless actual return logistics exceed it.
- Actual campaign spend is reported as reconciliation evidence against the advertising allocation. Do not deduct it again from owner profit after the combined pool has already been charged.
- Use the active financial policy for all other components and rounding. Show planned/template values separately from actual settlement values; never silently mix them in one column.

The order-detail report must expose order creation/completion dates, store, SKU/nmID, stable source identities, lifecycle/return state, revenue, selected cost version, every expense, profit, completeness status, and evidence links. Weekly and monthly summaries must show per store and consolidated totals for revenue, purchase cost, WB/platform costs, owner profit, 2% managed-operations fee, actual return fee, advertising allocation, actual campaign-spend reconciliation, and unresolved amounts.

## Report gates

- Status is `FINAL` only when every required store was collected and every in-scope order has a resolved identity, applicable historical cost, settled revenue/fee evidence, and reconciled return status.
- Use `PENDING_DATA` whenever collection is unfinished, a required source is missing, or any in-scope row remains unresolved. Missing or unavailable values remain unavailable; never convert them to zero.
- A genuinely new refund event belongs to the reporting period in which the refund occurs. Record its signed negative delta in that current refund week, even if the linked sale was reported in an earlier week; the new refund alone does not restate the earlier sale period.
- Use `RESTATED` only when WB later corrects, rewrites, or reassigns evidence for a financial period that was already reported. Produce a new complete version of that same historical period, preserve the earlier `FINAL` or `RESTATED` version, and expose the difference; never overwrite it.
- A failure in one store may still produce clearly labelled per-store evidence, but the consolidated report cannot be `FINAL`.
- The weekly report includes order detail and the exact withdrawal/advertising-funding proposal, if any. The monthly owner summary states what the business completed, owner profit, and the managed-operations fee payable to the operator.
- Archive the period, policy IDs, cost-version IDs, source coverage, watermarks, unresolved list, calculations, and report hashes so a later settlement correction is visible as a new revision rather than a silent overwrite.

## Funding boundary

- A report never moves money. Create a separate `funding` state and fresh preflight for each withdrawal or advertising top-up.
- Approval must name the exact store/account, currency, amount, source balance, destination or campaign, policy cap, and evidence timestamp. Any changed balance, target, amount, or stale preflight invalidates the approval.
- An advertising top-up is two-stage: first persist an exact read-only dry-run proposal, then accept a separate live confirmation for that same fingerprint. A one-shot live call without the prior proposal is forbidden. The governed per-action cap comes from the current post-activation unallocated advertising reserve; it is not a caller-supplied number, and pre-activation deposits never consume that reserve.
- WB withdrawal has no supported public API. Provide the exact manual seller-portal step and wait for balance readback or settlement evidence; never claim automatic withdrawal.
- Advertising funding may use an official supported endpoint only when the same-store token proves the current seller-account currency and the current campaign, source balance, amount, reserve cap, and permission all pass preflight. Unknown or unsupported response shapes fail closed. Otherwise use a manual instruction. Never reuse an approval across stores or campaigns.
- After execution or the user's manual action, read back the same account/campaign balance, compare expected and observed deltas, archive evidence, and keep the flow blocked on any mismatch. An ambiguous write may only enter a readback-only resolution path; it must never send another top-up automatically.
