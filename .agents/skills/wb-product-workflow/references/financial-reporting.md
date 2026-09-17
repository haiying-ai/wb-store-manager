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
Read the project's approved owner-report contract when supplied, including its
18-metric compact layout, cutoff-day/week/month/cumulative column order,
confirmed-withdrawal agency policy and period-end cash-position definition.
Private rates, receipts, settlement adjustments and store names remain private.

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
- order-linked reporting policies are selected by `orderCreatedAt`; return-risk,
  promotion and legacy shared reserves retain that original order-time basis,
  policy ID, rates and evidence. Confirmed-withdrawal agency accrual instead
  follows the approved private withdrawal policy, bank-credit business date
  and per-event rounding; do not accrue it from order sales or reverse it merely
  because an unrelated order was refunded;
- actual return logistics is linked from all applicable WB finance rows and is
  allocated once at store-and-period scope;
- when the selected policy separates return risk and promotion, actual return
  logistics reconciles only against the return-risk reserve, the promotion
  reserve remains independent, and any excess return loss is shown separately;
- when an older selected policy uses a shared advertising/return pool, retain
  that historical allocation behavior instead of restating the order under the
  newest policy;
- reference operating profit deducts actual seller-funded advertising, agency
  accrual and other actual costs once. A separately labelled policy-reserve
  profit uses its own allocation basis; never subtract both the reserve and the
  same cash consumption in one profit figure. Formal advertising balances
  exclude estimates even when approved reference profit includes them;
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

- `PENDING_DATA` means evidence is incomplete. If the template and artifact
  gates pass, publish the scheduled report with open items and keep unavailable
  values unavailable. Source incompleteness never permits bypassing a template
  fingerprint failure or publishing an unverified workbook.
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
The approved owner cash-position view includes unwithdrawn funds: cumulative
completed confirmed withdrawals plus the latest CNY WB balance snapshot at or
before each cutoff. Preserve negative balances and historical observation
dates, and mark missing or stale evidence. This period-end stock is distinct
from account-credit flows and live withdrawable funds. Processing withdrawals
stay separate and are not added again; never invent receipts from balance
changes. See `owner-running-reporting.md` for receipt and adjustment idempotency,
cutoff filtering and unsupported store-allocation treatment.

WB withdrawal remains a manual seller-portal action unless a supported public
write route is independently verified. Under the active policy from
2026-09-05, advertising allocation uses the same store's WB mutual-settlement
`balance` (`net`, request `type=1`) through `/adv/v1/budget/deposit`; it does
not require a prior withdrawal or an external cash recharge. The allocation
still requires an exact store/campaign/currency/amount dry run, a fresh balance
and reserve preflight, explicit approval of that same fingerprint, one
execution attempt, and independent campaign-budget and source-debit readback.
An explicitly enabled private funding rule may prefer an eligible promotional
bucket in a new mixed plan; this never authorizes a payment or a cash fallback.
Follow the daily funding contract below.

### Daily confirmed funding contract

Keep cash accounting separate from promotional-funding eligibility.
`fundingGovernance.promotionalCreditsExcludedFromCashAccounting=true` excludes
promotional credits from seller cash advertising expense and confirmed cash
reserves; `promotionalCreditsIgnored` is its deprecated compatibility alias,
not a ban on using promotional funds. New active configurations use the explicit
name. Old-only configurations remain valid; if both names are supplied, both
must be boolean true. `runningAccounts.funding.cashbackMode=prefer_eligible`
independently permits eligible promotional buckets in an exact approved v3
plan. These rules are compatible: do not report their coexistence as a conflict
or disable cash-accounting exclusion to enable promotional funding. A campaign
allocation consumes the cash allowance, but cash advertising expense is based
on actual later seller-funded consumption, not the deposit itself.

Use `finance funds-plan --through yesterday` for the durable daily confirmed
basis, without a weekly report ID. If source ingestion is behind, run
`finance funds-refresh --through yesterday --correction-window-days 30`:
bounded official source ingestion, account materialization, then funds-plan.
This path does not generate a workbook or execute a deposit. `account-refresh`
alone only materializes existing local evidence; it is not a source fetch.
Do not advance a date to disguise missing source coverage. Keep the existing
scheduled owner report/template unchanged; a funding refresh is not a report.

Compute each store's cash ceiling from confirmed CNY original-policy accruals,
linked refund reversals, applicable legacy shared-pool logistics, and all
current confirmed or locked cash allocations, even if those allocations are
later than the business-event cutoff. Subtract cash allocations, not both
allocations and consumption. Separately show the book budget after actual
cash consumption and its unspent campaign allocations. Unsettled positive
sales and reference exchange-rate estimates are excluded, not zeroed into
confirmed cash; unresolved negative adjustments or stale coverage block new
money. Historical report approvals never migrate to a daily basis.

`finance ads-deposit-plan --funds-plan <daily.json> ...` creates a GET-only
preflight and immutable v3 exact proposal, not approval. Freeze daily basis,
store, campaign, currency, total, cash amount, promotional amount, one eligible
bucket's available balance, percentage and expiry, live settlement balance,
approval actor and expiry in its fingerprint. With the approved private rule,
select one unambiguous eligible bucket; never combine buckets. The cash part
alone must fit confirmed cash and live `balance.net`. A valid 100% promotional
bucket may cover the total with zero cash; promotional spending is neither
seller cash expense nor cash reserve usage. Official deposit `sum` is the
total and `cashback_sum`/`cashback_percent` specify the promotional part.
`--cash-only` creates a distinct proposal; failed promo must never silently
be replaced with cash. Legacy v2 exact plans retain their original contract.

Before any approved write, serialize by store and revalidate current daily
evidence, currency, source balances, identity and both cash/promotional locks.
Persist execution before the single no-retry POST. Executing, unknown or
readback-pending actions retain their locks and only permit GET reconciliation.
Confirm only when campaign increment and separate cash/promo source debits
match; HTTP success is insufficient. Do not reuse a stale promotional snapshot
to resurrect confirmed spent funds. New grants reusing a bucket identity need
independent reconciliation. Insufficient cash produces a precise shortfall;
do not invent a reserve increase or assume that an account balance is budget.

Reconcile actual campaign consumption from official promotion evidence and
the later settlement-cycle charge from official finance/balance evidence.
Count actual seller-funded consumption once in reference operating profit and
as budget usage; do not also charge the deposit, reserve allocation or later
settlement deduction as the same expense. Keep policy-reserve profit separately
labelled. Exclude a finance deduction from generic other-platform cost only when a
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

Before any WB source call, the daily controller validates the configured
template version, builder path and approved SHA-256 against the release
manifest and actual builder. Revalidate that same fingerprint after workbook
generation and before publication or archive. A scheduled run never edits code,
configuration, tests, prompts or layout rules to bypass a failed gate; stop and
return a locally verified report with its real earlier cutoff when available.
Template changes belong to a separately authorized development run with
automated and visual qualification before the approved release is updated.

The daily controller freezes the last complete financial day in the business
timezone, syncs bounded correction windows and gaps, then refreshes running
accounts. Generate or reuse the primary as-of report before checking whether
it covers a due weekly/monthly report at the same cutoff. Keep optional legacy
workbook bridging separate. Read-only advertising/settlement and applicable
legacy-debt reconciliation use the same materialized evidence. Daily funds-plan
uses that same frozen cutoff and shared ledger without a weekly report ID,
another full source fetch, a workbook rebuild or any money-moving action.
Period-close runs use the longer repair window and publish source open items
after the artifact gates pass. Record phase timings, source coverage,
watermarks, hashes and failures. Preserve any owner-disabled proactive funding
monitor; the daily report is not authority to restore it.

No finance report may implicitly change price, stock, advertising, campaign,
withdrawal, or account state. Any write requires a separately scoped approval,
fresh preflight, exact readback, and archived evidence.
