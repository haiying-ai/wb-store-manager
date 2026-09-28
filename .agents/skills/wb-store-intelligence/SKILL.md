---
name: wb-store-intelligence
description: Safely collect, compare, and analyze multiple Wildberries seller stores in strict read-only mode. Use for WB store benchmarking, SKU structure analysis, price comparison, sales-funnel comparison, image audits, and converting strengths from reference stores into proposed actions for the owner's store. Never use it to modify cards, prices, stock, media, advertising, promotions, orders, or any other live WB state.
---

# WB Store Intelligence

Analyze WB stores without permitting marketplace mutations.

## Non-negotiable boundary

1. Read `references/read-only-safety.md` before any live API collection.
2. Use only `wb_store_intelligence.ReadOnlyWbClient`.
3. Never invoke `wb_manager.client.WbClient` or any listing, price, stock, media, advertising, promotion, or order mutation code.
4. Load tokens only through per-store environment-variable names declared in a local registry.
5. Accept WB tokens regardless of their platform permission scope; never treat token write permission as authorization to mutate.
6. Treat every store in this skill as read-only, including the owner's store.
7. Refuse endpoints absent from `ReadOnlyPolicy`; do not bypass or widen the policy during collection.
8. If a required read endpoint is missing, stop, verify its official contract, add one exact rule and a blocking test, then rerun.
9. Never include tokens in saved requests, logs, reports, exceptions, shell commands, or chat. If an exposure is discovered, report it without repeating the token and recommend rotation; an exposure alone does not block a user-authorized read-only connection check.

## Workflow

Use:

`scope -> validate -> collect -> normalize -> compare -> inspect_media -> recommend -> archive`

- **Scope:** name stores, period, currency, region, categories, and SKU cohort.
- **Validate:** validate the registry and safety policy without loading token values.
- **Collect:** call only allowlisted query endpoints. Record HTTP status and collection time.
- **Normalize:** distinguish list price, discount, buyer price, region, stock, and funnel periods.
- **Compare:** compare like-for-like SKUs and compatible periods. Do not infer causation from correlation.
- **Inspect media:** visually assess full images and marketplace-size thumbnails.
- **Recommend:** separate verified facts, inferences, confidence, and proposed experiments.
- **Archive:** keep raw snapshots local and out of Git.

## Configuration

Copy `config/store-intelligence.example.json` to ignored `config/store-intelligence.local.json`.
Set each token outside the repository using its declared `WB_INTEL_*_TOKEN` variable.
Tokens with broader WB permissions are accepted, but the skill remains application-level read-only.

```powershell
python -m wb_store_intelligence validate-config --config config/store-intelligence.local.json
python -m wb_store_intelligence policy-check --method POST --url https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products
```

## Output contract

Write local evidence below `data/store_intelligence/`:

- `snapshots/<store>/<timestamp>/`: raw and normalized snapshots.
- `comparisons/<period>/`: comparable store and SKU tables.
- `recommendations/action_plan_<date>.md`: proposed actions only.

For each recommendation include source store/SKU, verified fact, inference, confidence, target owner SKU, expected metric, test period, stop condition, and missing data. Classify it as `adopt`, `experiment`, `observe`, `reject`, or `needs_data`.

## Handoff

Pass only sanitized reports and selected recommendations to `wb-product-workflow`. Never pass benchmark-store tokens or authenticated clients. Any owner-store change must start as a separate `wb-product-workflow` task and pass its dry-run, explicit-approval, write, and readback gates.
