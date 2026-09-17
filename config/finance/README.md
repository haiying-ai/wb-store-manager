# Phone financial reporting policy

`active_phone_financial_reporting_policy.json` is the active order-profit and cost-reporting policy for every enabled WB smartphone store in `stores.json`.

The owner-approved agency fee correction is recorded separately in
`agency_withdrawal_fee_policy.json`. For restated owner running reports, the
2% fee is calculated on each independently confirmed bank-credited withdrawal,
rounded to cents per withdrawal, and posted on its settlement date as a
store-level event. Orders, balances, withdrawal requests, and cancelled or
unconfirmed withdrawals do not create this fee. Historical order-policy files
remain unchanged for audit; their former sale-based management-fee formula is
superseded for this component only. Prior published workbooks remain immutable.

It does not control WB live prices. Live price strategy remains governed by `config/pricing/active_phone_pricing_policy.json` and `config/pricing/active_phone_pricing_store_scope.json`.

The order-time signed sale amount `G` and all platform deductions come from matched platform settlement evidence. Purchase cost, selling-price policy, and reporting-policy versions are selected using the order creation timestamp and are locked on the order row. A later quotation or price-policy change creates a new effective-dated version and never silently rewrites older orders.

Return logistics has no 8/16 CNY fallback. A final report requires the actual signed platform deduction linked by store plus `orderId`, `shkId`, or `srid`. Orders created before `2026-09-06T13:08:38+03:00` retain their historical shared 3% reserve. Orders created at or after that boundary use a separate 2% return-risk reserve and 3% promotion reserve. Actual return logistics does not consume the promotion reserve; only the amount above the 2% return-risk reserve becomes additional return loss. Actual advertising spend is reconciliation evidence only and is never deducted a second time.

Scheduled publication and evidence completeness are separate. The same canonical Excel workbook is published on time even when auxiliary evidence is incomplete; missing evidence is shown as an open item and unsettled orders stay outside confirmed cash allocation. `PENDING_DATA` therefore describes evidence status only, not permission to withhold the weekly or monthly report. `FINAL` means the evidence is complete, and `RESTATED` is a later auditable replacement caused by a late settlement, refund, correction, or recovered historical version. A restatement never overwrites the earlier report.

The periodic output contract is defined in `active_wb_periodic_reporting_standard.json`. Weekly reports cover the prior Monday through Sunday; monthly reports cover the prior calendar month. The first monthly report also includes the bootstrap cumulative period from each enabled store's opening date through the month-end cutoff. Both stores are calculated separately and then consolidated. Repository defaults resolve relative to the checkout; a scheduled installation may override them with `WB_FINANCE_CANONICAL_WORKBOOK`, `WB_FINANCE_REPORT_ROOT`, and `WB_FINANCE_CASH_EVENTS`. The committed cash-event file is an empty schema example, while the real `historical_cash_events.json` remains local and Git-ignored.

Actual settlement-account credits are not available from the public WB balance
API. Add them only from an independently evidenced seller-portal account-credit
export or equivalent account-transaction record through the normalized local
importer:

```text
python -m wb_manager finance receipt-import --file <receipt-manifest.json>
python -m wb_manager finance account-refresh --through <complete-day>
```

The private `historicalCashEventsLocalOverride` must exist before import. If
configuration falls back to the tracked example registry, the import stops
without writing any receipt data.

The manifest is CNY-only and uses stable source identities. A coverage interval
asserts that the cited source is complete for every day in that exact interval;
omit `receiptCoverage` when only selected known receipts are available. Partial
coverage is retained as partial and does not make an opening-to-cutoff total
complete.

```json
{
  "schemaVersion": 1,
  "currency": "CNY",
  "events": [
    {
      "eventId": "stable-source-payment-id",
      "store": "owner-store",
      "eventType": "account_receipt",
      "amountCny": "1000.00",
      "receivedAt": "2026-09-15T10:00:00+03:00",
      "status": "readback_confirmed",
      "sourceType": "wb_settlement_account_credit_history",
      "evidenceReference": "local evidence reference"
    }
  ],
  "receiptCoverage": [
    {
      "coverageId": "owner-store-payments-through-2026-09-15",
      "store": "owner-store",
      "from": "2026-07-15",
      "through": "2026-09-15",
      "complete": true,
      "evidence": "complete export range and bank reconciliation reference"
    }
  ]
}
```

Never populate this registry from order receivables, `bankPaymentSum`, a single
balance snapshot, balance differences, a withdrawal amount, or a bank payout
that belongs in `withdrawal_completed` instead.
Allowed `sourceType` values are `wb_settlement_account_credit_history` and
`verified_settlement_account_credit_statement`; this label and the cited
reference still require human verification of the underlying source. An
existing `cashReceipts` entry in owner decisions with the same store and ID or
evidence reference must be reconciled before import.

Workbook creation and synchronization use the Codex bundled Node spreadsheet runtime and its private `@oai/artifact-tool` package. On a fresh checkout, load the workspace dependencies and create a local, Git-ignored `node_modules` junction to the returned Node package directory. Do not vendor or commit that runtime.

Historical policies are append-only under `policies/`. Secrets, raw finance rows, the SQLite ledger, cost catalogs, and generated reports remain local and must not be committed.

## Settlement-balance advertising and legacy withdrawal debt

From 2026-09-05, advertising no longer depends on a withdrawal or an external
cash recharge. A separately approved funding action allocates an exact integer
CNY amount from the same store's WB mutual-settlement `balance` (`net`, request
`type=1`) to one campaign through `/adv/v1/budget/deposit`. WB then nets its
advertising service charges from future sales according to the settlement
cycle. The allocation still requires a fresh balance/reserve preflight, exact
store/campaign/amount approval, one non-retried write, and campaign-budget
readback. A finance report never performs that write implicitly.

Keep the three evidence layers separate: campaign-budget allocation comes from
`/adv/v1/budget/deposit`; actual campaign consumption comes from official
promotion evidence such as `/adv/v1/upd`; settlement-cycle charging comes from
official finance/balance evidence. Actual consumption and the settlement
deduction reconcile the policy-derived promotion reserve and must not be deducted
again through `otherPlatformCostCny` or another profit field. Return logistics
reconciles the separate return-risk reserve for v7 orders and the historical shared
reserve for older orders.

Withdrawal rows dated on or after the cutover use `--ad-fee-status
settlement_balance`: advertising cost is not linked to that withdrawal and the
due, withheld, and debt amounts are zero/not applicable. The withdrawal remains
an ordinary, immutable cash event. For example:

```text
python -m wb_manager finance withdrawal-record --store owner-store \
  --record-key wb-withdrawal-<portal-id> --amount-cny 1000 \
  --withdrawn-at 2026-09-05T12:00:00+03:00 \
  --ad-fee-status settlement_balance
```

Historical withdrawal-linked advertising debt recorded before the cutover is
immutable and is not cleared by this policy change. For those legacy records
only, missing fee facts remain `unknown/PENDING_DATA`; a confirmed
non-deduction supplies the due and withheld amounts explicitly. For example:

```text
python -m wb_manager finance withdrawal-record --store owner-store \
  --record-key wb-withdrawal-<portal-id> --amount-cny 1000 \
  --withdrawn-at 2026-09-01T12:00:00+03:00 \
  --ad-fee-due-cny 30 --ad-fee-withheld-cny 0 --ad-fee-status not_deducted
```

`ads-payment-reconcile` (alias `ads-debt-reconcile`) is retained only for that
legacy ledger. It reads normalized, read-only `adv/v1/payments` evidence;
positive timestamped funding rows are allocated FIFO against pre-cutover
withdrawal debt. It is not the source for settlement-cycle advertising charges
and it does not create new withdrawal debt. `ads-debt-status` shows the legacy
due, settled, unapplied, and outstanding amounts. No withdrawal or campaign
budget write is performed by these commands.

## Scheduled-run performance policy

The daily controller records `performance.phaseTimingsMs` in its JSON result so
platform synchronization, report calculation, historical refresh, and workbook
bridging can be measured separately. Ordinary daily captures use a 30-day
correction window; Monday and the first day of a month use the full 120-day
repair window. An explicit bootstrap backfills from each store's configured
history/opening date. Open synchronization gaps still force an earlier
backfill, so the shorter ordinary window does not hide a known gap. Within
that window, the statistics sales/orders change-log sources resume from their
own durable watermark with a one-day overlap for inclusive cursors; finance,
returns, and advertising period sources retain the rolling repair window.
Explicit bootstrap always overrides source watermarks and re-reads complete
opening history from the finance-detail source; WB's operational statistics
sales/orders feed is retained for about 90 days and is marked `bootstrap_90d`
when older history is unavailable there. Historical FINAL/RESTATED reports are
scanned for candidate counts on every run, but recalculated only when a
report-relevant persisted source row/status changes (or a same-window source
re-read detects a retraction). Advertising-payment readbacks update only the
legacy pre-cutover withdrawal/debt ledger and are excluded from the order-P&L
refresh gate. Advertising consumption and settlement deductions are separate
reconciliation evidence for the effective-dated policy reserves. A moving correction-window boundary by itself
does not trigger a refresh. Existing PENDING_DATA historical candidates are
rechecked until they become FINAL/RESTATED, even when the rolling source hash
is unchanged, so a late correction cannot remain stranded outside the next
day's API window. The daily result also reports
`historicalReportsRecalculated` for this audit trail.

Store API reads are bounded and token-isolated: up to two enabled stores are
collected concurrently by default, while the eight sources inside one store
remain sequential so each source keeps its documented throttle. The source
adapter sends each request with retries disabled; an HTTP 429 is recorded as a
failed/incomplete source and is retried by the next scheduled sync (or an
explicit replay), rather than by an unbounded in-process backoff loop. Stores
that resolve to the same token are automatically serialized.
API workers never write SQLite; source rows, coverage, and watermarks are
committed in deterministic store/source order after the read phase. The CLI
flag `--max-store-workers 1` provides a conservative fallback if WB applies a
cross-token/global rate limit. The active registry's
`governance.failClosedOnMissingStoreCredential` remains authoritative: when it
is true (the production setting), a missing token aborts the run before any
store is collected; when explicitly false, the missing-token store is recorded
as failed evidence while other stores continue.

The phase-2/3 speed path deliberately does not rewrite historical advertising
event keys.  WB's `updNum` may be `0`, while payment rows have a stable `id`; a
future identity migration must first add a canonical identity/quality column,
reconcile legacy hashes (including `updTime=null` current-value rows), and
preserve an immutable collision trail.  Until that migration is separately
reviewed, existing hashes are retained so a replay cannot silently change the
historical advertising, settlement-reconciliation, or legacy debt totals.

The canonical workbook bridge keeps a sidecar fingerprint next to the `.xlsx`.
When the selected report contents, store registry, cash-event registry, debt
snapshot, and workbook state are unchanged, it returns `NOOP` before importing
or exporting the workbook. Any input change falls back to the existing locked,
backup-and-readback-verified append flow.

## Confirmed-withdrawal agency policy and owner dashboard

Read `../../docs/wb-finance-daily-report-contract.md` for the approved owner dashboard: 18 core metrics, yesterday/week/month/cumulative columns, independent stores, and period-end cash positions.

The executable agency policy is local and Git-ignored: `agency_withdrawal_fee_policy.json`. Use `agency_withdrawal_fee_policy.example.json` as a schema only; it intentionally has no active status or rate. Supply the explicitly approved policy before running account refresh. Missing or invalid policy must fail; never infer it from the pricing policy, example, receipt, or balance. The current qualified engine enforces its specific fee contract. A different rate requires a separately authorized and qualified code/template release, including labels and tests.

Keep real receipts, settlement adjustments, evidence, balances and store allocations in private registries. Public test data is synthetic. Configure output/archive locations locally; the checked-in standard uses relative paths.
