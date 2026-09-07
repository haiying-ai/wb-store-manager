# WB Financial Analysis Methodology

This document is the reusable public method layer for WB financial analysis.
It describes evidence governance, reconciliation, reporting, and funding
boundaries only. Seller-specific prices, cost tables, exchange rates, rates,
thresholds, account identifiers, and credentials belong in the private active
policy/configuration and must not be copied into this Skill, source code,
documentation, logs, or a public repository.

The runtime calculator loads the active approved policy at execution time.
This document never replaces that policy and intentionally contains no
seller-specific calculation constants or price equations.

## Operating boundary and flow

For running cumulative balances, cross-period refund reversals, fixed owner
weekly/monthly templates or arbitrary cutoff reports, also read
`owner-running-reporting.md`. It defines the signed-event owner-account layer
and the distinction between confirmed balances and provisional estimates.

Use the read-only `finance` flow for:

`collect -> backfill -> reconcile -> calculate -> verify -> report -> archive`

Use a separate `funding` flow for any money-moving action:

`preflight -> plan -> approve -> execute -> readback -> archive`

A report may expose a proposed allocation, but publishing a report never
executes a withdrawal, balance-funded campaign-budget allocation, or campaign
mutation.

## Store scope and time

- Discover every enabled WB owner store from the active registry on every run;
  do not hard-code a store list.
- Keep tokens, raw responses, watermarks, normalized rows, evidence, and
  status isolated by store. Calculate each store first and consolidate only for
  display; funding and withdrawal decisions remain store-specific.
- Use the configured business timezone for financial event periods and the
  configured scheduler timezone for trigger dates. A weekly report covers the
  preceding complete Monday-to-Sunday period. A monthly report covers the
  preceding complete calendar month.
- The first initialization backfills from each store's effective opening point
  through the cutoff. The first monthly publication includes both the
  ordinary month view and an explicitly labelled opening-to-cutoff cumulative
  view.

## Platform-first evidence

The platform is the transaction fact source. The local SQLite ledger is a
persistent evidence, audit, report-history, and watermark store; it is not a
substitute for a newer platform response.

Source precedence is:

1. WB official financial and settlement rows;
2. WB official order, lifecycle, return, and cancellation state;
3. effective-dated private cost, price-policy, and reporting-policy records;
4. live prices and balances as reconciliation or execution-time evidence.

Preserve the raw payload, source timestamp, stable source identifier, request
coverage, and response status. A failed, unauthorized, rate-limited, or
ambiguous source remains visible as an open item; it must not become a zero.

## Synchronization and replay

- Resume each source from its durable successful watermark and backfill missed
  dates before calculating a period.
- Use a short ordinary correction window and a longer period-close window;
  expand either window to the earliest known gap. An explicit bootstrap ignores
  incremental watermarks and rereads the complete available opening history.
- Use source-specific cursors with a boundary overlap for inclusive APIs.
- Upsert by stable, store-scoped identities so repeated pages are idempotent;
  preserve corrections as new evidence rather than overwriting old payloads.
- Bound concurrency by store and platform token. Keep source throttling intact,
  serialize stores sharing a token, and commit database writes in deterministic
  store/source order after network reads. A transient platform failure is
  recorded and retried by a later run, not hidden by an unbounded in-process
  retry loop.

## Order evidence and historical snapshots

Group order, sale, return, cancellation, and finance rows by a stable WB
identity such as `srid`, `orderId`, or `shkId`, always together with the store.
Retain every contributing evidence ID and the linkage used.

For each order, lock the historical snapshots that were effective when the
order was created:

- transaction selling-price evidence and its policy/version reference;
- purchase-cost version and source;
- reporting-policy version and component-rate snapshots.

Later quote or policy changes create a new effective-dated version for later
orders. They do not silently rewrite an earlier order. If a historical version
is recovered or corrected, append a validation or restatement record.

The order row must expose lifecycle, signed transaction amount, platform
deductions, direct settlement amount, payment/collection costs, logistics,
policy-derived allocations, profit components, completeness state, and source
links. Actual platform deductions take precedence over planning estimates. A
missing actual cannot be silently replaced by a template estimate in a
confirmed cash figure.

The exact component calculations, rounding, and rates are supplied by the
active private financial policy. The methodology only requires that:

- the transaction amount is taken from order-time platform settlement
  evidence, not today's price, a converted display price, or the settlement
  amount itself;
- platform deductions and direct settlement reconcile to one another;
- effective-dated reporting policies are selected by `orderCreatedAt`; every
  operating, return-risk, promotion, or legacy shared reserve uses that same
  order-time basis and retains its policy ID, rates, and evidence;
- actual return logistics is linked from all applicable WB finance rows and is
  allocated once at store-and-period scope;
- when the selected policy separates return risk and promotion, actual return
  logistics reconciles only against the return-risk reserve, the promotion
  reserve remains independent, and any excess return loss is shown separately;
- when an older selected policy uses a shared advertising/return pool, retain
  that historical allocation behavior instead of restating the order under the
  newest policy;
- actual advertising spend is reconciliation evidence and is never deducted a
  second time outside the policy reserve;
- planned/template values and actual settlement values occupy separate fields.

## Returns and period corrections

A genuinely new refund or return adjustment is recognized in the period in
which the platform posts that financial event, even when the original sale was
reported earlier. Keep the original sale and the signed adjustment visible.

Use a new `RESTATED` version only when WB later corrects, rewrites, or
reassigns evidence for a period that was already published. Preserve the
earlier version and show the difference. Do not manufacture a return charge
when the platform linkage is missing; keep the row open instead.

## Report publication gates

- `PENDING_DATA` means evidence is incomplete. Publish the scheduled report,
  show the open items, and keep unavailable values unavailable.
- `FINAL` means every required store and every in-scope row has the required
  identity, historical version, settlement evidence, and reconciled lifecycle.
- `RESTATED` is a later auditable replacement of an already published period;
  it never overwrites history.

Settled, cash-reconciled rows may be used for confirmed allocation. Unsettled
or unresolved rows remain visible but are excluded from confirmed cash totals.
An incomplete auxiliary source must not hold the whole report hostage.

## Settlement-balance advertising, withdrawals, and legacy debt

Funding is calculated per store and is independent from order-profit history.
The report distinguishes:

- the policy-derived advertising/return reserve and the portion available for
  advertising allocation;
- the order-ledger allocation ceiling for withdrawal;
- the live platform balance and the fresh execution-time funding plan;
- confirmed historical withdrawals and pending withdrawal requests;
- balance-funded campaign-budget allocations, actual campaign consumption,
  and WB settlement-cycle deductions as three separate evidence layers;
- pre-cutover withdrawal-linked advertising debt, when it exists.

The live execution plan must use a fresh same-store balance readback and
reserve/lock checks. A report balance snapshot is not an execution preflight.
Historical withdrawals are separate cash events: a confirmed event is counted
once in the cumulative cash adjustment, never as an order expense. A request
reported as processing is not a settled withdrawal; keep it in a separate
pending view until platform success or settlement evidence arrives.

WB withdrawal remains a manual seller-portal action unless a supported public
write route is independently verified. Under the active policy from
2026-09-05, advertising allocation uses the same store's WB mutual-settlement
`balance` (`net`, request `type=1`) through `/adv/v1/budget/deposit`; it does
not require a prior withdrawal or an external cash recharge. The allocation
still requires an exact store/campaign/currency/amount dry run, a fresh balance
and reserve preflight, explicit approval of that same fingerprint, one
execution attempt, and campaign-budget readback. Do not select promotional
credits or silently fall back to another source.

Reconcile actual campaign consumption from official promotion evidence and
the later settlement-cycle charge from official finance/balance evidence.
Neither is a second expense outside the policy-derived advertising-and-return
pool. Exclude a finance deduction from generic other-platform cost only when a
stable operation code or an exact approved operation name identifies it as the
settlement-cycle advertising charge; retain the source row for reconciliation.

Withdrawals on or after the cutover have advertising fee state
`settlement_balance`: due, withheld, and debt are zero/not applicable because
the advertising obligation is independent of that withdrawal. Historical
pre-cutover withdrawal debt remains immutable and is never cleared merely by
the new policy. A new balance-funded campaign allocation, campaign consumption,
or settlement-cycle deduction must never clear it. For a legacy withdrawal with
missing fee facts, keep `unknown/PENDING_DATA`; only a successful, explicitly
identified same-store legacy-debt payment may be allocated FIFO. Corrections
append evidence; they never rewrite the original record.

## Workbook and archive

Maintain one canonical, append-only workbook and matching JSON/Markdown
artifacts. Keep order detail, financial-event detail, source checks, report
history, policy/version snapshots, funding snapshots, withdrawal records, and
update notes in separate auditable sections. Refresh the current cumulative
dashboard from the latest valid snapshot so corrections do not double count;
never delete or silently replace an earlier report version.

## Scheduler and safety

The daily controller should sync through the last complete financial day, run
read-only advertising consumption/settlement reconciliation plus any legacy
advertising-payment debt reconciliation, generate due weekly/monthly reports,
and bridge the canonical workbook. Period-close runs use the longer
repair window and publish open items on schedule. Record phase timings, source
coverage, watermarks, hashes, and failures.

No finance report may implicitly change price, stock, advertising, campaign,
withdrawal, or account state. Any write requires a separately scoped approval,
fresh preflight, exact readback, and archived evidence.
