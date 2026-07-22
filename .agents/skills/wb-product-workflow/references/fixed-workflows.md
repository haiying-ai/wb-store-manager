# Fixed WB Workflows

Use one of four resumable flows. Keep the state file with the task artifacts and update it after every material stage.

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
- Generate product-first media and add copy deterministically when required.
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

## State commands

```powershell
python .agents/skills/wb-product-workflow/scripts/workflow_state.py new --flow daily --task-id wb-daily-20260722 --subject store --output data/operations/wb-daily-20260722.json
python .agents/skills/wb-product-workflow/scripts/workflow_state.py set --file data/operations/wb-daily-20260722.json --stage collect --status passed --evidence data/snapshots/example.json
python .agents/skills/wb-product-workflow/scripts/workflow_state.py validate --file data/operations/wb-daily-20260722.json
```

Statuses are `pending`, `in_progress`, `waiting_approval`, `passed`, or `blocked`. A later stage cannot pass while an earlier stage is incomplete. Approval stages require `--approved-by` before passing.
