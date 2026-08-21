# Phone V3 Deterministic Production Standard

Read this reference whenever the user locks the eight-page V3 phone master, requests batch V3 SKU creation, asks for generation progress, or resumes an interrupted V3 run.

## Canonical entrypoint

Do not infer progress from chat messages, old subagents, generation caches, or image counts. Read the active pointer, then query the persistent run:

```powershell
Get-Content data/template_registry/phone_v3/active_production.json
python .agents/skills/wb-product-workflow/scripts/phone_v3_production.py status --run-dir <RUN_DIR>
python .agents/skills/wb-product-workflow/scripts/phone_v3_production.py next --run-dir <RUN_DIR> --limit 20
```

The current frozen standard is `v3_20260821_deterministic_production`. Its source visual master remains `v3_20260807_benchmark_c_trust_reference`; never overwrite that source directory.

## Page production contract

| Page | Production unit | Allowed operation after approval |
| ---: | --- | --- |
| 01 | exact SKU | Fixed-geometry title/capacity/status rendering on the approved SKU scene. Product and bottom trust regions must have zero changed pixels. No dynamic text boxes or layout reflow. |
| 02–08 | model + color | Byte-identical complete-page reuse for every capacity SKU in the group. No text rendering, product overlay, crop, recolor, or post-approval edit. |

For a new model-color, pages 02–08 may use direct image generation only to create a complete text-bearing candidate before approval, matching the frozen V3 page exactly. Archive it immediately, inspect the full 900×1200 image and 320 px preview, verify Russian text/model facts, score it against the page-specific V3 floor, and then mark it approved. Once approved, treat the whole page as an atomic file.

The page score floors are: `01=93`, `02=91`, `03=92`, `04=91`, `05=89`, `06=91`, `07=90`, `08=90`. Any wrong model/color/capacity/specification, malformed Russian, product/text overlap, template drift, unsupported claim, crop, white edge, or artifact is a hard failure regardless of score.

## Persistent artifacts

Every run directory must contain:

- `pipeline_config.json`: run identity, counts, page modes and score floors;
- `template_lock.json`: hashes of the eight master pages, contracts, renderer, pipeline and locked fonts;
- `sku_facts.csv` and `sku_facts.jsonl`: exact sellable identities and fact status;
- `claim_registry.json` and `claim_registry.csv`: owner-confirmed claims, page scope and prohibited claims;
- `golden_assets.jsonl`: one source path, dimension, mode and SHA-256 per SKU/page assignment;
- `progress.sqlite3`: authoritative task, approval and event state;
- `progress.json`, `progress.md` and `dashboard.html`: human-readable current progress;
- `regression_report.json`: no-generation golden comparison;
- `validation_report.json`: artifact/count/hash integrity.

Facts, claims, assets and live marketplace state are different layers. A fact or claim must be confirmed before an image may use it. An approved local image does not authorize WB upload. A successful upload does not complete the task until live photo count/order and buyer-visible rendering are read back.

## Commands

Initialize a new run only after the manifest, template directory and approval scope are explicit:

```powershell
python .agents/skills/wb-product-workflow/scripts/phone_v3_production.py init `
  --manifest <121-or-new-manifest.json> `
  --template-dir data/template_registry/phone_v3/versions/v3_20260821_deterministic_production `
  --run-dir <RUN_DIR> `
  --approved-by user
```

Run a zero-generation regression and integrity validation:

```powershell
python .agents/skills/wb-product-workflow/scripts/phone_v3_production.py regress --run-dir <RUN_DIR>
python .agents/skills/wb-product-workflow/scripts/phone_v3_production.py validate --run-dir <RUN_DIR>
```

Render one new page 01 candidate from the locked SKU facts and an approved product scene. The command derives model and capacity from the run database; it does not accept free-form replacement facts:

```powershell
python .agents/skills/wb-product-workflow/scripts/phone_v3_production.py render-page01 `
  --run-dir <RUN_DIR> --vendor-code <SKU> --source-scene <APPROVED_900x1200_SCENE.png>
```

This step may only produce `ARCHIVED` or `BLOCKED`; it never auto-approves the image. Full-size/320 px review and `mark --status APPROVED` are still mandatory.

Record a newly archived or reviewed asset:

```powershell
python .agents/skills/wb-product-workflow/scripts/phone_v3_production.py mark `
  --run-dir <RUN_DIR> --vendor-code <SKU> --page <1-8> `
  --status APPROVED --asset-path <900x1200.png> --score <SCORE> `
  --approved-by user --note <QA_NOTE>
```

`APPROVED` requires the page score floor, a real 900×1200 asset and an approver. Rejected or blocked pages remain visible on the dashboard and are never counted as complete.

After every task passes, local production approval is explicit and still excludes WB mutation:

```powershell
python .agents/skills/wb-product-workflow/scripts/phone_v3_production.py approve-run `
  --run-dir <RUN_DIR> --approved-by user --note <NOTE>
```

Serve the five-second auto-refresh dashboard when the user wants continuous visibility:

```powershell
python .agents/skills/wb-product-workflow/scripts/phone_v3_production.py serve `
  --run-dir <RUN_DIR> --host 127.0.0.1 --port 8765
```

The browser URL is `http://127.0.0.1:8765/dashboard.html`. The static `dashboard.html` and `progress.md` remain readable even when the server is not running.

## Resume and failure rules

1. Query `status`, then `next`; never restart the batch from a conversation estimate.
2. Keep one writer per run database. Parallel image generation may create candidates, but only the production tool archives and changes task state.
3. Reuse completed hashes. Regenerate only rejected or missing deterministic page paths.
4. If the template lock, input manifest or a golden asset hash changes, stop. Create a new version/run or obtain explicit approval for the change; do not silently update the old lock.
5. Report SKU count, page-assignment count, archived count, reviewed count, approved/regression-pass count, rejected/blocked count and last event separately.
6. Direct image-generation success, temporary cache output, HTTP success and subagent activity are never progress by themselves.
7. Before any WB upload, return to `phone-image-operations.md`, build a dry-run package, obtain exact-scope approval and read back every affected card.

## Current regression baseline

The `phone-v3-production-standard-20260821` run freezes 121 SKUs and 968 page assignments. Its regression is valid only when `regression_report.json` says `968/968`, `failureCount=0`, the template-lock hash matches the run database, and `validate` passes. Hash identity proves the resolver returns the frozen current assets; human approval is still required before declaring the production standard approved or performing any marketplace action.
