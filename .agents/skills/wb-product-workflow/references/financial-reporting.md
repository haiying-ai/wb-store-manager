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

Use the read-only `finance` flow for:

`collect -> backfill -> reconcile -> calculate -> verify -> report -> archive`

Use a separate `funding` flow for any money-moving action:

`preflight -> plan -> approve -> execute -> readback -> archive`

A report may expose a proposed allocation, but publishing a report never
executes a withdrawal, advertising deposit, or campaign mutation.

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
- policy-derived operating and advertising/return reserves use the same
  order-time basis and are retained with their evidence;
- actual return logistics is linked from all applicable WB finance rows and is
  allocated once at store-and-period scope;
- the reserve remaining after actual return logistics is reported as the
  advertising allocation, while any excess return loss is shown separately;
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

## Funding, withdrawals, and advertising-fee debt

Funding is calculated per store and is independent from order-profit history.
The report distinguishes:

- the policy-derived advertising/return reserve and the portion available for
  advertising allocation;
- the order-ledger allocation ceiling for withdrawal;
- the live platform balance and the fresh execution-time funding plan;
- confirmed historical withdrawals, pending withdrawal requests, and
  advertising-fee debt.

The live execution plan must use a fresh same-store balance readback and
reserve/lock checks. A report balance snapshot is not an execution preflight.
Historical withdrawals are separate cash events: a confirmed event is counted
once in the cumulative cash adjustment, never as an order expense. A request
reported as processing is not a settled withdrawal; keep it in a separate
pending view until platform success or settlement evidence arrives.

WB withdrawal remains a manual seller-portal action unless a supported public
write route is independently verified. Advertising deposits, when supported,
require an exact store/campaign/currency/amount dry run, a fresh preflight, an
explicit approval of that same fingerprint, one execution attempt, and a
readback. Do not select promotional credits or other non-cash allowances
implicitly from a finance report. A separately approved advertising funding
plan may explicitly select an eligible promotional source after verifying its
currency, expiry, permitted share, balance and expected source debit. Never
fall back to cash or another source without a new approval. Promotional credit
is not withdrawable cash or realized order profit.

If a withdrawal record does not state the advertising amount due and the
amount withheld, mark its fee state `unknown/PENDING_DATA`; do not report a
numeric zero. Once actual, timestamped platform advertising-payment evidence
is available, apply it only to the same store and allocate it FIFO to eligible
withdrawal debt, preserving unapplied amounts. Corrected fee facts are added
as immutable correction evidence; the original user record remains intact.

## Workbook and archive

Maintain one canonical, append-only workbook and matching JSON/Markdown
artifacts. Keep order detail, financial-event detail, source checks, report
history, policy/version snapshots, funding snapshots, withdrawal records, and
update notes in separate auditable sections. Refresh the current cumulative
dashboard from the latest valid snapshot so corrections do not double count;
never delete or silently replace an earlier report version.

## Scheduler and safety

The daily controller should sync through the last complete financial day, run
read-only advertising-payment reconciliation, generate due weekly/monthly
reports, and bridge the canonical workbook. Period-close runs use the longer
repair window and publish open items on schedule. Record phase timings, source
coverage, watermarks, hashes, and failures.

No finance report may implicitly change price, stock, advertising, campaign,
withdrawal, or account state. Any write requires a separately scoped approval,
fresh preflight, exact readback, and archived evidence.
