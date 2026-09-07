# Running owner accounts and fixed reports

Use this reference with `financial-reporting.md` for daily financial refreshes,
weekly/monthly owner reports, and an owner-requested historical cutoff. The
private reporting standard selects the active implementation and template.
This reporting method does not change price calculations or authorize funds.

## Signed events, not sums of snapshots

- Materialize a durable store-scoped event account after source ingestion and
  before expensive workbook/history work. Keep event date, observed date,
  source ID, evidence, original order, currency and original policy/cost version.
- Replaying the same event is a no-op. Changed evidence appends a revision with
  an audit reason. Published report snapshots stay immutable. Never sum every
  version of an order or add the previous cumulative report to today's total.
- Record a later refund in the refund's business period. Reverse the original
  order's revenue, proceeds, agency accrual and advertising/return allocations
  by the refunded share, using its original amounts and rounding. Do not use
  today's selling price or rate. An example supplied by the owner is not a
  source transaction. Missing original linkage remains an explicit exception.
- When the owner or warehouse confirms saleable return inventory, reverse the
  original product cost for that quantity and return its value to inventory.
  This is not new cash or income. Keep actual nonrefundable shipping/return
  charges once, with platform evidence. Do not assume every return is saleable.
- Do not pretend that already consumed cash advertising is refunded when an
  order returns. Reverse its policy accrual, not historical campaign expenses.
  Exclude platform promotional/cashback consumption from seller expense and
  budget usage. Transfers into campaigns are not consumption; settlement
  deductions are reconciliation of the same expense, not another expense.

## Fixed dimensions and balances

Every report uses the same metric names, row order, worksheet order and units.
Only its reporting period, store scope and data change. Calculate each enabled
store independently, then show the consolidated total. Every weekly, monthly
and cutoff report includes current-period activity and opening-to-cutoff totals.

Keep these views explicit:

- Net agency accrual; owner-confirmed agency receipts in the period and to date;
  agency payable equals cumulative net accrual less confirmed paid/received.
  Until receipt-history coverage is confirmed, display the known accrued amount
  and mark the payable balance incomplete. No payment record is not proof of zero.
  If the owner explicitly establishes a complete opening balance and undertakes
  to report each later receipt, maintain a labelled owner-reported receipt
  register from that point. Do not ask them to confirm zero every day. Keep its
  registered balance distinct from independently verified bank receipts.
- Net advertising accrual; actual seller-funded consumption; platform-funded
  consumption excluded; remaining policy budget after actual spend and any
  historically applicable shared-pool return charge. Preserve deficits rather
  than hiding them with a zero clamp. Unspent campaign allocations and live
  platform balances stay separate from total unspent policy budget.
- Order proceeds are not necessarily received cash. Show official settlement
  proceeds, independently evidenced account credits, withdrawal requests,
  completed withdrawals, processing withdrawals and the live available balance
  separately. Deduplicate the same withdrawal across registries and platform
  records. A processing request is not a completed cash withdrawal.
- Profit after the original policy allocations and actual-cost operating
  contribution are separately labelled. Never deduct both an advertising
  allocation and the same cash expense in one profit figure.

Shared-pool return charges use only the original legacy shared allocations;
separate-policy return charges use only the return-risk reserve. Charge any
excess return loss to policy profit once. Period excess is the change between
the cumulative start and end balances; sum the per-store excess, never net one
store's loss against another store's unused reserve.

A missing source affects only its dependent metrics. Show verified subtotals
and known activity beside the precise uncovered dates; do not turn missing
amounts into zero or an all-zero summary. Optional same-order currency estimates
must be explicitly labelled. When the owner approves a readable combined
reference dashboard, use exactly one selected amount per event and retain
official amounts plus estimate differences in separate detail columns. Never
sum actual and provisional views. Estimates cannot increase confirmed payout
or funding balances. Retain native currency facts on sale/refund rows only;
do not repeat original sale income on logistics rows.

## Compact owner dashboard and order reconciliation

- Use the owner's approved fixed dashboard layout: metrics down rows; opening
  to cutoff, previous complete month, previous complete week and cutoff day
  across columns. State the exact date range. The cutoff-day column is the
  prior day relative to the dashboard's reporting anchor, not the machine's
  current date. Different time columns overlap and must not be added together.
- Show consolidated and independent store summaries, followed by each store's
  order events and financial flows, then a terminal period-reconciliation view.
  Label activity separately from balances at each column's own period end.
  Show each flow's principal amount near its description; preserve its metric
  breakdown, original IDs, official/estimated basis and provenance to the right.
  Keep monetary headings aligned with their amounts. The approved owner view
  uses right alignment and one calculation explanation beside each summary
  metric, including the time basis and missing-data treatment. Bind named-store
  subtotals by store identity, not registry position. A book balance derived
  from receipts and withdrawal requests is not the platform's live withdrawable
  balance and must not be labelled as such.
- Join orders by store-scoped typed platform identifiers, not SKU/name alone.
  Support archived marketplace order IDs and exact full order-line identifiers.
  Keep ambiguous order charges visible as pending, with their amount. Explicit
  trusted store-level fees and campaign/funds events remain store-level; missing
  order identity alone does not establish store-level scope.
- Order detail is one financial event per row, so one order can have several
  dates for sale, shipping and refund. For each store and time column, verify
  order-event amounts plus store-level amounts equal all financial flows.
  Never force a zero difference by fabricating or duplicating an order.
- Keep the latest confirmed receipt rules in private data, not fixed dated
  prose embedded in the template. Unknown histories stay unknown even if all
  component cells would otherwise sum to zero.

## Stable execution and delivery

The daily job syncs source correction windows and repairs durable gaps, then
refreshes running accounts. Due reports and ad-budget checks read this materialized
state, never a stale weekly workbook. Materialize or reuse the daily as-of
dashboard first. It may explicitly cover a due weekly/monthly report with the
same cutoff, avoiding duplicate workbooks; track older closed-period catchup
separately. Mapping or template changes invalidate only the report fingerprint,
not financial events. Querying a temporary cutoff is read-only
against that state; disclose each source's coverage and the latest refresh.
Use one deterministic builder for weekly/monthly/as-of exports and a stable
template version. Keep human-readable Chinese filenames and immutable report
history. An auxiliary history/workbook failure must not discard a report that
has already been successfully generated.

Freeze the last complete financial day in the configured business timezone
once per run, including after-midnight recovery. Persist same-cutoff coverage
so a following day does not regenerate a weekly/monthly workbook already covered
by the as-of dashboard. Validate the current business/template fingerprint and
artifact hashes before reuse. Archive dated versions under the configured
private archive root with hash readback; archive or auxiliary debt-audit failure
must leave an already verified workspace report available with an explicit
phase failure. If fresh collection cannot complete, identify any fallback by
its real earlier cutoff instead of relabelling stale data as yesterday.

Before delivery test the owner's cross-period refund example, same-day replay,
late evidence, partial refund, cutoff boundary, store isolation, promo exclusion,
agency payment, withdrawal lifecycle and null/zero distinctions. Compare key
amounts to independent source totals, render all changed report views and scan
Excel formula errors. A formula scan alone does not prove financial correctness.

When the implementation needs owner evidence, ask a precise question listing
the store, event and effect. Store each answer in private configuration with
provenance. Do not keep asking about a rule the owner has already confirmed.
