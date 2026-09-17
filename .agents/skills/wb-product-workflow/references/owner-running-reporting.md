# Running owner accounts and fixed reports

Use this reference with `financial-reporting.md` for daily financial refreshes,
weekly/monthly owner reports, and an owner-requested historical cutoff. The
private reporting standard selects the active implementation and template.
Read the project's approved owner-report contract when supplied; its approved
layout and accounting basis take precedence over older generic examples.
This reporting method does not change price calculations or authorize funds.

## Signed events, not sums of snapshots

- Materialize a durable store-scoped event account after source ingestion and
  before expensive workbook/history work. Keep event date, observed date,
  source ID, evidence, original order, currency and original policy/cost version.
- Replaying the same event is a no-op. Changed evidence appends a revision with
  an audit reason. Published report snapshots stay immutable. Never sum every
  version of an order or add the previous cumulative report to today's total.
- Record a later refund in the refund's business period. Reverse the original
  order's revenue, proceeds and applicable advertising/return allocations by
  the refunded share, using its original amounts and rounding. Under the
  approved confirmed-withdrawal agency policy, an order refund does not reverse
  an unrelated bank-credit agency accrual; correct that accrual only against
  the withdrawal's own evidence. Do not use today's selling price or rate.
  An example supplied by the owner is not a source transaction. Missing
  original linkage remains an explicit exception.
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

- Agency accrual follows the approved private confirmed-withdrawal policy:
  calculate each confirmed bank credit using its effective rate and per-event
  rounding, then assign it to the bank-credit business date. Order sales,
  withdrawal applications, processing requests and WB balances do not accrue
  this fee. Load rates and rounding rules from private policy, never this skill.
- Show agency accrual and owner-confirmed receipts as period activity; payable
  is cumulative accrual minus cumulative registered receipts minus applicable
  explicitly approved noncash settlement adjustments at each period end.
  Preserve genuine overpayments as negative payable. Until receipt-history
  coverage is confirmed, mark the known payable incomplete; no record is not
  proof of zero. If the owner establishes a complete opening balance and agrees
  to report later receipts, maintain the labelled owner-reported register
  without asking for zero confirmation every day. Keep owner confirmation
  distinct from independently verified bank evidence.
- Receipts and noncash adjustments use stable event identities, provenance,
  business dates and cutoff filtering. A later receipt or adjustment cannot
  enter an earlier report. A consolidated receipt without supported store
  allocation stays unallocated; later allocation does not increase its total.
  A batch-specific rounding adjustment changes payable only, not the original
  accrual or actual cash receipt, and never creates a general write-off rule.
  Replay these events once; do not re-enter them on every daily run or declare
  each store settled merely because consolidated payable is zero.
- Net advertising accrual; actual seller-funded consumption; platform-funded
  consumption excluded; remaining policy budget after actual spend and any
  historically applicable shared-pool return charge. Preserve deficits rather
  than hiding them with a zero clamp. Unspent campaign allocations and live
  platform balances stay separate from total unspent policy budget.
- Order proceeds are not necessarily received cash. Keep official settlement
  proceeds, independently evidenced account credits, withdrawal requests,
  completed withdrawals and processing withdrawals as distinct evidence.
  Deduplicate the same withdrawal across registries and platform records.
  A processing request is not a completed cash withdrawal.
- Where the approved owner view includes unwithdrawn money, period-end cash
  position equals cumulative completed confirmed withdrawals plus the latest
  CNY WB account-balance snapshot observed on or before that period end in the
  business timezone. Retain historical snapshots, negative balances, observed
  dates and intraday qualifications. Missing, stale or conflicting snapshots
  stay explicit. With both components present, show their known total instead
  of requiring a separate account-credit ledger. This is a stock, not period
  cash income. Processing withdrawals stay separate and are not added again;
  do not infer their inclusion in the balance without evidence. Never create
  account-credit flows from balance changes or call this stock a live
  withdrawable amount.
- The main reference operating profit deducts seller-funded cash advertising,
  confirmed-withdrawal agency accrual and actual costs once. Keep profit after
  original policy allocations separately labelled in detail or reconciliation;
  never deduct both an advertising allocation and the same cash expense in
  one profit figure. Formal advertising accrual and period-end budget exclude
  provisional estimates even when the reference profit includes approved ones.

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

- Use the approved compact dashboard with 18 core metrics. Bind columns by
  named key in this order: cutoff day (`yesterday`), previous complete week
  (`last_week`), previous complete month (`last_month`), opening to cutoff
  (`cumulative`). Do not depend on incoming array position. Show exact date
  ranges; the cutoff-day column uses the report's anchor, not the machine's
  current date. Overlapping time columns must not be added together.
- Keep the same 18 metric rows in consolidated and store summaries, in order:
  sales, refunds, net revenue, settlement receivable, product cost, actual
  logistics, other cost, reference operating profit; confirmed net advertising
  accrual, cash advertising consumption, confirmed advertising budget balance;
  agency accrual, agency receipts, agency payable; total cash position,
  cumulative completed withdrawals, WB balance snapshot, processing withdrawals.
  Budget balance, payable and all four cash-position rows are stocks at each
  column's own period end; the remaining rows are activity within that period.
- For the approved two-store layout, retain eight worksheets in order:
  consolidated summary, first-store summary, second-store summary, first-store
  orders, first-store flows, second-store orders, second-store flows, period
  reconciliation. Resolve store identities and display names from configuration.
  Other store counts require an approved layout, never silently omit a store.
- Add focused store summaries to the main page showing each store's operating
  result, confirmed advertising budget, WB balance, processing withdrawals and
  evidence gaps. Retain and highlight losses and negative balances. Unsupported
  receipt allocations remain visibly pending at store scope. Put secondary
  profit views, provisional advertising balances, withdrawal requests,
  promotional consumption, return reserves and excess return losses in detail
  or reconciliation rather than adding duplicate core summary rows.
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

Before any WB source request, validate the active template version, builder
path and approved SHA-256 against the release manifest and actual builder.
Scheduled production requires a locked release. Revalidate the same builder
fingerprint after generation and before publishing a manifest or archive.
A mismatch stops live execution or publication; return the latest locally
verified report with its real cutoff when available. During scheduled runs,
never edit builders, services, configuration, tests, prompts or layout rules to
repair or bypass this gate. Template development requires a separate authorized
run, complete automated and visual qualification, then an approved release
version and fingerprint update.

Freeze the last complete financial day in the configured business timezone
once per run, including after-midnight recovery. Persist same-cutoff coverage
so a following day does not regenerate a weekly/monthly workbook already covered
by the as-of dashboard. Validate the current business/template fingerprint and
artifact hashes before reuse, including the workbook, report JSON and
verification JSON. Reuse is a no-op only when business, mapping and template
are unchanged and those hashes still match. Archive dated versions under the configured
private archive root with hash readback; archive or auxiliary debt-audit failure
must leave an already verified workspace report available with an explicit
phase failure. If fresh collection cannot complete, identify any fallback by
its real earlier cutoff instead of relabelling stale data as yesterday.

Before a template release, test cross-period refunds, same-day replay, late
evidence, partial refunds, cutoff boundaries, store isolation, promo exclusion,
agency receipts/adjustments, withdrawal lifecycle and null/zero distinctions.
For each delivery verify the approved worksheet order, 18 core metric rows,
named period columns and dates, store binding, period/stock distinction and
independent order-plus-store-to-flow reconciliation. Read back saved artifacts
and scan Excel formula errors; for changed report views also render and inspect
them. A formula scan alone does not prove financial correctness.

When the implementation needs owner evidence, ask a precise question listing
the store, event and effect. Store each answer in private configuration with
provenance. Do not keep asking about a rule the owner has already confirmed.
