# WB multi-store read-only intelligence

`wb-store-intelligence` collects isolated WB seller snapshots and compares SKU structure, media coverage, prices, inventory and funnel performance without exposing any mutation route.

## Effective capability

- Compare owner and benchmark stores on the same subject and time window.
- Extract cards, variants, attributes, photo/video metadata and prices.
- Compare FBS and WB warehouse inventory.
- Compare views, carts, orders, buyouts and conversion stages.
- Query search, advertising, promotion, feedback, question and seller surfaces when authorized.
- Produce local JSON snapshots, a normalized metrics file and a Markdown action report.

Missing, forbidden or rate-limited sources remain visible with their HTTP status and are never replaced by zero.

## Safety structure

The registry stores aliases, roles and environment-variable names only. Token values live in a Git-ignored local env file. `ReadOnlyPolicy` checks the exact HTTPS method, host and anchored path before the client loads a token. Query-style `POST` endpoints are individually allowlisted; every other operation is denied. Benchmark findings can enter `wb-product-workflow` only as sanitized recommendations, and owner-store changes remain a separate approval-gated task.

## Token configuration

Copy `config/store-intelligence.example.json` to `config/store-intelligence.local.json`. Give every store a unique alias, role and token variable. Put values only in `config/store-intelligence.tokens.local.env`:

```text
WB_INTEL_BENCHMARK_A_TOKEN=replace_with_store_a_token
WB_INTEL_BENCHMARK_B_TOKEN=replace_with_store_b_token
```

Tokens with WB write permission are accepted, but the intelligence client remains application-level read-only. Never commit, print or paste tokens into reports or chat.

## Commands

```powershell
python -m wb_store_intelligence validate-config --config config/store-intelligence.local.json --token-file config/store-intelligence.tokens.local.env
python -m wb_store_intelligence policy-check --method POST --url https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products
python -m wb_store_intelligence collect-baseline --config config/store-intelligence.local.json --token-file config/store-intelligence.tokens.local.env --output data/store_intelligence/snapshots --days 7
python -m wb_store_intelligence analyze-three-stores --snapshot data/store_intelligence/snapshots/<run-id> --output data/store_intelligence/comparisons/<run-id>
```

Collection output contains response data and status metadata, never request headers or tokens.
