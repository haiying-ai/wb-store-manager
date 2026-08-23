---
name: wb-product-workflow
description: Run end-to-end Wildberries/WB product and store operations from local source material through pricing, listing, media, publication readback, buyer audit, daily reporting, and periodic review. Use for WB daily operations, new listings, listing optimization, pricing, FBS sale readiness, images, videos, buyer questions, funnel analysis, store reports, or requests to prepare listings using the verified V2 SKU logic ("V2版本SKU逻辑").
---

# WB Product Workflow

Use this skill for Wildberries/WB product work rooted at `E:\电商运营\01平台资料\01WB\<category>` and implemented by `E:\CodexWorks\WB`.

## Fixed Workflow Router

Classify each request as `daily`, `listing`, `media`, or `review`. Read `references/fixed-workflows.md`, create or resume its workflow state, and then load only the domain references needed for that flow. Use spreadsheet, document, browser, image, and video skills as execution tools without weakening WB truth, approval, visual-review, or readback gates.

For every request to test, create, replace, resume, or inspect a **new smartphone image master**, first read `references/phone-new-master-production-standard.md`, then run `python .agents/skills/wb-product-workflow/scripts/phone_new_master_workflow.py status` and `validate`. The active pointer is authoritative across conversations. Resume it before selecting an SKU or creating any workflow. Never silently replace its pilot, source set, production mode, or task ID.

For every flow:

1. Start from manifests, summaries, and the requested SKU files.
2. Separate platform facts, inferences, recommendations, and proposed mutations.
3. Stop at `waiting_approval` before any marketplace mutation unless the user explicitly approved the exact action and scope.
4. After an approved mutation, read back the same live identity and archive the evidence.
5. Declare completion only when required stages pass and blockers are empty or explicitly accepted.

`E:\电商运营` is the cross-platform materials root. Never create a WB category directly under it. Shared product facts and assets belong in `E:\电商运营\00共享资料`; Ozon work belongs in `E:\电商运营\01平台资料\02OZON`.

## Directory Protocol

Each category uses:

- `00知识库`: generated source-material knowledge base.
- `01来源资料`: user-supplied files.
- `02生成资料`: working reviews, pricing, prompts, image batches, and inboxes.
- `03上线资料`: confirmed listing package and WB audit responses.
- `04运营资料`: operations snapshots and reports.

Read `references/directory-protocol.md` before changing folder behavior.
Read `references/live-state-sync.md` before or immediately after any WB live write, readback, or follow-up operation that depends on live identifiers.
For this workspace's GitHub publication, read `references/github-ssh.md` before committing or pushing.
Before publishing any reusable WB platform-logic change, read `references/dual-repository-sync.md` and update both `wb-store-manager` and `store-manager-ru` in the same work session.
For every change to this Skill package, also mirror the complete project package to `C:\Users\孙兵\.agents\skills\wb-product-workflow`, run `scripts/sync_wb_skill_layers.py check`, and require zero missing or hash-mismatched project files before Git publication.

## Default Workflow

1. Sync the knowledge base.
   - Run `python -m wb_manager kb sync --category <category>`.
   - Review source manifest, product facts, asset index, missing facts, and change log.
   - If source files changed, trust regenerated facts over older generated packages.

2. Qualify a new category template before rollout.
   - Read `references/category-template-benchmark-gate.md` whenever adding a category or materially changing a category template.
   - Require one timestamped competitor record and one complete representative prototype covering main image, gallery, title, characteristics, description, trust/fulfilment and compliance.
   - Score both with the same rubric. The prototype must have zero hard fails, a higher total, and a strictly higher score in every template-controlled dimension; ties do not pass.
   - Do not generate or publish the category-wide SKU batch until the user explicitly approves the passing prototype.

3. Confirm product details.
   - Run `python -m wb_manager product review --category <category> --sku <SKU>`.
   - Block progression if required fields are missing.
   - Record color, barcode, package dimensions, package gross weight, warehouse, certifications, and WB subject.

4. Generate official barcode when needed.
   - Use `python -m wb_manager barcode generate --category <category> --sku <SKU>`.
   - Never fabricate barcodes.
   - One independently sellable configuration needs one barcode.

5. Review pricing and profitability.
   - Read `references/pricing-rules.md` before creating or changing a category pricing model.
   - For every phone profit calculation, price recommendation, dry-run, or live price write, first run `python tools/validate_active_pricing_policy.py` and load `config/pricing/active_phone_pricing_policy.json`. Treat that JSON as the only active formula source. Never fall back to constants copied into an older report, script, spreadsheet, or chat.
   - For every smartphone price-strategy change, also load `config/pricing/active_phone_pricing_store_scope.json`. Treat every store marked `requiredForPriceStrategy` as mandatory: generate a per-store plan with store-specific nmIDs, preflight all stores before writing either one, request one explicit joint approval that lists each store's SKU count, and read back each store independently. Never silently fall back to one store.
   - If the active policy file is missing, invalid, not marked `active`, or disagrees with its component-rate sum, stop with a blocker. Do not infer the latest formula from file timestamps.
   - Run `python -m wb_manager pricing review --category <category> --sku <SKU> --input-file <pricing.json>` when pricing input is external.
   - Profitability is a hard gate.
   - Source-material price is not sale price unless the user explicitly confirms it.
   - Separate percentage-of-sale costs, fixed per-unit costs, product cost, and target profit. Solve for the minimum buyer-paid price first, then derive any WB list price and discount.
   - For the current phone category, the confirmed working formula is documented in `references/pricing-rules.md`; do not silently restore older tax, exchange-loss, or fixed-cost assumptions.

6. Stage images.
   - Create/confirm main image first.
   - Then generate prompts or inspect external inboxes.
   - Lock product color before selecting or generating images.
   - Reject unconfirmed colors.
   - When the user supplies image attachments or explicitly names a template directory for a phone image pack, treat those pages as the **single visual master**. Before any prompt, generation, batch, score, approval, or upload, read `references/phone-template-master-gate.md`.
   - A palette instruction (for example, "deep indigo structure + blue-violet electric") changes only the color system inside the approved master. It never authorizes a new layout, dark-theme redesign, different card system, or mixed reference set.
   - Freeze the master page paths/attachment IDs and a per-page change contract before generation. Every parallel worker must receive the same contract; invalidate all earlier prompts, drafts, and visual references that conflict with it.
   - Run the template-consistency gate before factual, Russian-copy, dimension, thumbnail, or sales-score gates. Any structural mismatch is `REJECTED_TEMPLATE_MISMATCH`; do not count it as generated, approved, or complete regardless of its visual score.
   - For a newly confirmed phone template, finish one complete representative pack and obtain user approval before starting a category-wide batch. If the representative pack fails template consistency, stop the batch and do not consume further image-generation capacity until the failure is resolved or the user explicitly changes the standard.
   - For IPL hair-removal devices / 脱毛仪, read `references/hair-removal-main-image.md` before planning, prompting, generating, scoring, approving, uploading, or verifying a main image. Treat its SKU-truth, Russian-copy, prompt-template, scoring, and WB readback gates as mandatory.
   - For mobile phones, also read `references/phone-image-playbook.md` before planning, generating, reviewing, or reporting image progress. It defines the required model-first workflow, image-set structure, factual guardrails, and resumable batch protocol.
   - For a new phone master or exact-master test, `references/phone-new-master-production-standard.md` takes precedence over generic phone-image guidance. In `EXACT_MASTER_TEST`, use only the locked complete user master, the pointer-selected pilot, and `DIRECT_FULL_TEXT_IMAGEGEN`; do not create text-free bases, post-fill text, mix references, or open a second workflow.
   - When the request uses the confirmed eight-page V3 phone master, asks for exact-template batch production, or resumes the 121-SKU V3 work, read `references/phone-v3-deterministic-production-standard.md` before inspecting prompts or images. Resume through its active run pointer and SQLite tracker; do not reconstruct state from chat history or restart completed pages.
   - Before producing a phone main image, read `references/phone-main-image-production-workflow.md`. Its text-free-base plus deterministic-copy method is the default only when no more specific approved production mode overrides it. The active new-master exact-test mode explicitly overrides it with direct complete text-bearing generation.
   - Before generating or approving any phone main image, read `references/phone-main-image-sales-score.md`. Require a score of at least 93/100, zero hard-fail defects, and a score above the active competitor/reference benchmark before presenting it as upload-ready.
   - Before auditing, packaging, uploading, or verifying a phone image batch, also read `references/phone-image-operations.md`. Use its four-stage tool chain and completion gates instead of ad hoc upload loops.
   - If a phone card-quality score is below 10, use the portal-led diagnostic and single-SKU pilot/remediation rules in `references/phone-image-playbook.md`; do not assume that an HTTP-successful image upload or a complete image count has resolved the score.

7. Validate listing package.
   - Run `python -m wb_manager listing validate --category <category> --sku <SKU>`.
   - Required files in `03上线资料\<SKU>`: `listing_manifest.json`, passing `pricing_review.json`, complete product review, and confirmed `images/`.

8. Produce product-promotion video when requested.
   - Read `references/product-video-workflow.md` before selecting reference media, writing a storyboard, generating video, or accepting a render.
   - Treat a video as a separate marketing asset: it must use approved final product images and verified product facts; it does not replace the WB image package or its validation gates.
   - Use Dreamina/即梦 only when the user explicitly asks for it. Follow the `dreamina-cli` skill for login, credit-consuming submissions, task IDs, polling, and downloads.
   - For a multi-beat product story, default to short independently generated clips (about 4–6 seconds each), inspect each clip, then concatenate approved clips locally. Do not attempt the whole story as one long multi-image generation unless the user explicitly requests it.
   - Keep generated clips, review frames, prompts, and the final render under `E:\CodexWorks\WB\videos\<product-slug>\` or the category's video work folder. Never overwrite an approved final render.

9. Publish or discover live WB card.
   - Prefer dry-run until the user explicitly approves live writes.
   - When the user manually created or edited a card, treat WB live data as source of truth and do not recreate the card.
   - Record live `nmID`, `imtID`, `chrtID`, `vendorCode`, barcode, title, dimensions, and photo count.
   - After any successful live publish or readback-confirmed live update, sync the confirmed WB payload, response, readback, mapping, and status summary into `03涓婄嚎璧勬枡` promptly. Do not leave live platform state only in `02鐢熸垚璧勬枡` or `data/operations/`.

10. Complete card-to-sale gates.
   - Upload approved images and verify count/order.
   - For phone batches, image upload is complete only when the package manifest, resumable upload state, live card readback, and tracker all agree. A successful HTTP response alone is not completion.
   - Set price/discount and read back.
   - Check error-card list.
   - After each live gate is read back successfully, update the corresponding `03涓婄嚎璧勬枡` package with the current WB state and remaining blockers.
   - Configure return pickup points when portal warehouse settings are disabled.
   - Create/select compatible FBS warehouse.
   - Write a tiny initial stock only after explicit user approval.
   - Read stock back from the same warehouse and barcode.
   - Wait for portal/front-end propagation before declaring buyer-visible sale status.

11. Run the mandatory post-publish buyer-perspective audit.
   - Read `references/buyer-perspective-post-publish-audit.md` after a new listing or material listing update is live and readable.
   - Audit the complete live buyer experience, not only the upload payload: title, attributes, description, every image position, configuration selectors, condition, included items, delivery promises, warranty, and model-specific claims.
   - Check each sellable SKU against its exact model, color, capacity, condition, and confirmed facts. Deduplicate shared images for efficiency, but map every finding back to all affected SKUs.
   - Separate common-sense contradictions from purchase doubts and backend-only risks. Treat buyer confusion or reasonable misinterpretation as a defect even when the API accepts the card.
   - Require human visual review of full-size assets and marketplace-size thumbnails. Automated hashes, OCR, image counts, and HTTP success are evidence aids, not substitutes for human judgment.
   - Save a timestamped report and affected-SKU list under the category `04运营资料`; sync any confirmed live remediation into `03上线资料`.
   - Do not declare final listing completion while an unresolved hard contradiction remains. Present proposed image/content fixes for user approval before any live write.

12. Analyze operations and sale readiness.
   - For store metrics, traffic, add-to-cart, conversion, product-problem, sale-readiness, or a daily report, read `references/operations-analytics.md` and `references/daily-report-template-v2.md` first.
   - Use the WB Analytics v3 sales-funnel endpoint for card totals and funnel data; do not use a single Content page or price-goods count as the total number of cards.
   - Treat cards listed for only 1–2 days as cold-start candidates unless other hard blockers are found. Use search-query analysis only when diagnosing search relevance or parameter matching.
   - For a card with add-to-cart but no order, verify the complete read-only chain: card errors, media, dimensions, price, active FBS warehouse, per-barcode stock readback, and public product-page buy controls plus delivery date.
   - Save a timestamped evidence report to `E:\电商运营\01平台资料\01WB\<category>\04运营资料\`; include data period, currency/region, source status, facts, inferences, and proposed actions. Never include tokens.
   - The fixed daily report is read-only and must collect the funnel, search, warehouse, price-readback, error-card, feedback/question, order/sale, advertising, promotion, inventory, and history status surfaces. Keep failed or rate-limited sources visible with HTTP status; never replace missing data with zero.
   - For Analytics Sales Funnel v3, use its current official request contract: `nmIds`, `subjectIds`, `tagIds`, required `orderBy`, `limit`, and `offset`; do not send legacy capitalized filter names or unrelated cursor fields. For per-product history, send `nmIds` in batches of at most 20. Honor the 20-second Analytics interval and do not accept a report as complete unless the core funnel read is HTTP 200.
## V2 SKU Listing Logic

When the user requests `按 V2 版本 SKU 逻辑` (or `V2 SKU`), treat it as the validated phone-new-SKU listing path. Read `references/phone-v2-sku-listing-logic.md` before planning, generating, reviewing, or publishing. Reuse its current live template and gates; do not reinterpret it as inspiration or create a new visual style.

## V1 SKU Listing Logic

When the user requests `按 V1 版本 SKU 逻辑` (or `V1 SKU`), use the existing original-SKU listing rules for source facts, parameters, prices, and card creation. Read `references/phone-v1-sku-listing-logic.md` for the final V1 phone image-template registry, snapshot, comparison, and fallback rules. Never substitute V2 assets or stale V1 exports.

## V3 Deterministic Phone Production

For the confirmed eight-page V3 smartphone system, use the persistent production entrypoint instead of ad hoc image loops:

```powershell
python .agents/skills/wb-product-workflow/scripts/phone_v3_production.py status
```

If `data/template_registry/phone_v3/active_production.json` points to a different run directory, use that exact directory. Page 01 uses fixed geometry and controlled fields on an approved SKU scene; pages 02–08 are complete approved model-color pages reused byte-for-byte across capacities. New pages 02–08 may be generated as complete text-bearing pages only before approval; after approval they are atomic and must not receive text boxes, product overlays, or other edits.

The production tool owns the template lock, SKU fact table, claim registry, golden hashes, SQLite task state, dashboard and regression report. `REGRESSION_PASS` proves identity to the frozen local golden asset, not WB upload or buyer-visible completion. Use `mark` with the page score floor and an approver for a new asset; use `approve-run` only after every task passes. Any WB upload remains a separate approval-gated flow with live readback.

## Variant And Inventory Rules

- Separate colors, kits, or package variants that need independent stock must use separate sellable identities.
- Each sellable identity needs its own seller article/vendorCode, barcode, image set, price, and stock amount.
- Do not reuse one barcode across colors.
- For color splits, verify each color's stock independently.
- For manually added goods, first discover live card identifiers, then continue only missing gates: images, price, warehouse, stock, and sale-readiness checks.

## WB Listing Payload Rules

- Query/read official subject characteristics before building payloads.
- Omit optional characteristics unless required by WB or explicitly confirmed by the user.
- Do not duplicate brand in the title when WB quality recommends removing it; keep the brand in the brand field.
- Package dimensions are centimeters and should be integer values; round decimal package dimensions up.
- `dimensions.weightBrutto` is required and is gross package weight in kg.
- Save every live request/response under the SKU listing folder or `data/operations/`.
- For batch publishes, also save a batch-level mapping file and per-SKU or per-variant live-state records under `03涓婄嚎璧勬枡` so future image uploads, price refreshes, parameter updates, and stock writes can target stable WB identifiers.

## FBS Warehouse And Stock Rules

- `GET /api/v3/warehouses` must return a usable seller warehouse before stock writes.
- Stock write endpoint: `PUT /api/v3/stocks/{warehouseId}` with `{"stocks":[{"sku":"<barcode>","amount":1}]}`.
- Stock read endpoint: `POST /api/v3/stocks/{warehouseId}` with `{"skus":["<barcode>"]}`.
- HTTP 204 from stock write is not enough. Always read back and require `amount > 0`.
- If WB returns `CargoWarehouseRestrictionMGT`, stop using that warehouse. Select/create a compatible MGT FBS warehouse without ODC/CD+ restrictions.
- For Russia-side FBS, prefer a Russian local MGT warehouse over China/Hong Kong cross-border consolidation offices.

## Sale-Readiness Checklist

Before saying a product is sellable, verify:

- For a phone batch, run a read-only combined check of cards, photos, dimensions, prices, warehouses, stock, and characteristic coverage.
- Add only model facts already verified in the local specification table; require dry-run, explicit execution approval, and delayed readback verification for every targeted card.
- For smartphone card-quality score, inspect the seller portal improvement view, resolve missing required classifications from verified regulatory sources, then require both API readback and the target portal score for every SKU.
- To activate a batch in FBS, first generate and review a dry-run stock plan, select the live warehouse by ID, then require explicit execution approval and exact per-barcode readback.

- Content card exists and live identifiers are recorded.
- Error-card list does not contain the SKU/nmID.
- Images are present and ordered.
- `dimensions.isValid` is true and `weightBrutto` exists.
- Price/discount readback matches planned values.
- FBS warehouse exists and is not deleting/processing.
- Stock readback returns the barcode with `amount > 0`.
- Portal/front-end propagation is checked or explicitly marked pending.
- The post-publish buyer-perspective audit has been completed against the live card.
- No unresolved hard contradiction exists across SKU, title, attributes, description, images, condition, configuration, included items, or delivery/warranty promises.
- Purchase-doubt findings are documented with severity, affected SKU count, evidence, and a proposed minimal remediation.

## Encoding Rules

- Write JSON/Markdown as UTF-8 without BOM.
- Read user-supplied JSON with `utf-8-sig` tolerance.
- Avoid PowerShell `Set-Content` for multilingual JSON/Markdown unless encoding is controlled.
- **WB fixed morning daily reports must be written in Simplified Chinese.** This applies to every report title, section heading, metric label, status explanation, finding, inference, and recommended read-only action. Preserve only proper nouns, API paths, SKU/vendor codes, HTTP status codes, currencies, and field names where translation would reduce precision.
- Treat English-only report text or Chinese mojibake as a blocking output defect: correct the report template and regenerate the local report before delivery. Verify generated Markdown is UTF-8 and that its fixed template headings are Chinese.
- Treat Chinese/Russian mojibake as a workflow blocker before moving materials into `03上线资料`.

## References

- `references/fixed-workflows.md`
- `references/wb-listing-safety.md`
- `references/pricing-rules.md`
- `references/image-workflow.md`
- `references/category-template-benchmark-gate.md`
- `references/hair-removal-main-image.md`
- `references/product-video-workflow.md`
- `references/phone-image-playbook.md`
- `references/phone-new-master-production-standard.md`
- `references/phone-main-image-sales-score.md`
- `references/phone-main-image-production-workflow.md`
- `references/phone-image-operations.md`
- `references/phone-v3-deterministic-production-standard.md`
- `references/directory-protocol.md`
- `references/live-state-sync.md`
- `references/operations-analytics.md`
- `references/daily-report-template-v2.md`
- `references/buyer-perspective-post-publish-audit.md`
- `references/github-ssh.md`
- `references/dual-repository-sync.md`
