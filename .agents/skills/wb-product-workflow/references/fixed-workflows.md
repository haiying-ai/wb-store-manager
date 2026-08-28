# Fixed WB Workflows

Use one of six resumable flows. Keep the state file with the task artifacts and update it after every material stage.

## Daily operations (`daily`)

`collect -> analyze -> verify -> report -> archive`

- Collect store health, orders, questions, prices, promotions, stock, and funnel signals.
- Compare compatible periods and retain source status.
- Verify anomalies through the API or seller portal before recommending action.
- Produce a priority queue and a timestamped evidence report.
- Do not mutate prices, stock, advertising, or content from this flow.

## Listing and optimization (`listing`)

`sync -> confirm -> pricing -> media -> validate -> approve -> publish -> readback -> buyer_audit -> archive`

- Sync the knowledge base and confirm every sellable identity.
- Require profitability, media, and package validation gates.
- Save proposed live payloads as dry-run output and set `approve` to `waiting_approval`.
- Record who approved the exact scope before passing `approve`.
- Publish only that scope and read back all affected live state.
- Run the buyer-perspective audit after the live state is readable.

## Media (`media`)

`facts -> brief -> generate -> review -> approve -> upload -> readback -> archive`

- Lock model, color, capacity, condition, included items, and verified claims.
- For a new phone master, resolve `data/template_registry/phone_new_master/active_test.json` before creating any state. Resume the named task, pilot, source contract and production mode; create a replacement only after explicit user direction.
- Generate product-first media and add copy deterministically when that is the active mode. If an approved workflow locks direct complete text-bearing generation, follow that mutually exclusive mode instead.
- Inspect full-size assets and marketplace-size thumbnails.
- Record explicit approval before upload. Never overwrite an approved final asset.
- Read back count, order, and buyer-visible rendering after upload.

## Periodic review (`review`)

`collect -> compare -> diagnose -> recommend -> report -> archive`

- Use comparable periods, currencies, regions, and source definitions.
- Separate facts, inferences, confidence, and recommended actions.
- Identify cold-start products before diagnosing low conversion.
- Produce SKU/store decisions, owners, target dates, and evidence paths.
- Start a separate approval-gated task for every recommended mutation.

## Financial reporting (`finance`)

`collect -> backfill -> reconcile -> calculate -> verify -> report -> archive`

- This flow is read-only. It may ingest and normalize financial evidence but must not withdraw money, fund advertising, or mutate a campaign.
- On the first run, backfill every enabled store from its opening date through the reporting cutoff. Later runs resume from the durable watermark and recover every missed day before processing the nominal period.
- Reconcile completed orders, financial settlements, returns, actual return-logistics deductions, purchase-cost versions, and advertising evidence before calculating profit.
- Mark a period `PENDING_DATA`, never `FINAL`, while a required store, order identity, historical cost, return charge, or settlement row is unresolved. Use `RESTATED` only for a later complete replacement version after all completeness gates pass.
- A weekly report may produce a funding plan; execution always starts a separate `funding` flow.

## Funding actions (`funding`)

`preflight -> plan -> approve -> execute -> readback -> archive`

- Treat withdrawals and advertising top-ups as money-moving mutations, never as an automatic continuation of a report.
- Freeze the exact store/account, currency, amount, source balance, destination or campaign, policy caps, and preflight evidence before approval.
- Record the approving actor before passing `approve`; reject stale balances, changed scope, or an amount outside the approved plan.
- Execute only a currently supported route. WB withdrawal has no supported public API and must remain a manual seller-portal step.
- Read back the account or campaign balance and archive request, response, manual evidence, and any discrepancy before completion.

## State commands

```powershell
python .agents/skills/wb-product-workflow/scripts/workflow_state.py new --flow daily --task-id wb-daily-20260722 --subject store --output data/operations/wb-daily-20260722.json
python .agents/skills/wb-product-workflow/scripts/workflow_state.py set --file data/operations/wb-daily-20260722.json --stage collect --status passed --evidence data/snapshots/example.json
python .agents/skills/wb-product-workflow/scripts/workflow_state.py validate --file data/operations/wb-daily-20260722.json
```

Statuses are `pending`, `in_progress`, `waiting_approval`, `passed`, or `blocked`. A later stage cannot pass while an earlier stage is incomplete. Approval stages require `--approved-by` before passing.
