---
name: wb-product-workflow
description: End-to-end Wildberries/WB product and operations workflow for E:\WB category folders, including source-material sync, pricing, image staging, listing publish, FBS sale-readiness, store metrics, sales-funnel analysis, and operational diagnosis.
---

# WB Product Workflow

Use this skill for Wildberries/WB product work rooted at `E:\WB\<category>` and implemented by `E:\CodexWorks\WB`.

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

## Default Workflow

1. Sync the knowledge base.
   - Run `python -m wb_manager kb sync --category <category>`.
   - Review source manifest, product facts, asset index, missing facts, and change log.
   - If source files changed, trust regenerated facts over older generated packages.

2. Confirm product details.
   - Run `python -m wb_manager product review --category <category> --sku <SKU>`.
   - Block progression if required fields are missing.
   - Record color, barcode, package dimensions, package gross weight, warehouse, certifications, and WB subject.

3. Generate official barcode when needed.
   - Use `python -m wb_manager barcode generate --category <category> --sku <SKU>`.
   - Never fabricate barcodes.
   - One independently sellable configuration needs one barcode.

4. Review pricing and profitability.
   - Read `references/pricing-rules.md` before creating or changing a category pricing model.
   - Run `python -m wb_manager pricing review --category <category> --sku <SKU> --input-file <pricing.json>` when pricing input is external.
   - Profitability is a hard gate.
   - Source-material price is not sale price unless the user explicitly confirms it.
   - Separate percentage-of-sale costs, fixed per-unit costs, product cost, and target profit. Solve for the minimum buyer-paid price first, then derive any WB list price and discount.
   - For the current phone category, the confirmed working formula is documented in `references/pricing-rules.md`; do not silently restore older tax, exchange-loss, or fixed-cost assumptions.

5. Stage images.
   - Create/confirm main image first.
   - Then generate prompts or inspect external inboxes.
   - Lock product color before selecting or generating images.
   - Reject unconfirmed colors.
   - For mobile phones, also read `references/phone-image-playbook.md` before planning, generating, reviewing, or reporting image progress. It defines the required model-first workflow, image-set structure, factual guardrails, and resumable batch protocol.
   - Before auditing, packaging, uploading, or verifying a phone image batch, also read `references/phone-image-operations.md`. Use its four-stage tool chain and completion gates instead of ad hoc upload loops.
   - If a phone card-quality score is below 10, use the portal-led diagnostic and single-SKU pilot/remediation rules in `references/phone-image-playbook.md`; do not assume that an HTTP-successful image upload or a complete image count has resolved the score.

6. Validate listing package.
   - Run `python -m wb_manager listing validate --category <category> --sku <SKU>`.
   - Required files in `03上线资料\<SKU>`: `listing_manifest.json`, passing `pricing_review.json`, complete product review, and confirmed `images/`.

7. Publish or discover live WB card.
   - Prefer dry-run until the user explicitly approves live writes.
   - When the user manually created or edited a card, treat WB live data as source of truth and do not recreate the card.
   - Record live `nmID`, `imtID`, `chrtID`, `vendorCode`, barcode, title, dimensions, and photo count.
   - After any successful live publish or readback-confirmed live update, sync the confirmed WB payload, response, readback, mapping, and status summary into `03涓婄嚎璧勬枡` promptly. Do not leave live platform state only in `02鐢熸垚璧勬枡` or `data/operations/`.

8. Complete card-to-sale gates.
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

9. Analyze operations and sale readiness.
   - For store metrics, traffic, add-to-cart, conversion, product-problem, or sale-readiness requests, read `references/operations-analytics.md` and `references/daily-report-template.md` first.
   - Use the WB Analytics v3 sales-funnel endpoint for card totals and funnel data; do not use a single Content page or price-goods count as the total number of cards.
   - Treat cards listed for only 1–2 days as cold-start candidates unless other hard blockers are found. Use search-query analysis only when diagnosing search relevance or parameter matching.
   - For a card with add-to-cart but no order, verify the complete read-only chain: card errors, media, dimensions, price, active FBS warehouse, per-barcode stock readback, and public product-page buy controls plus delivery date.
   - Save a timestamped evidence report to `E:\WB\<category>\04运营资料\`; include data period, currency/region, source status, facts, inferences, and proposed actions. Never include tokens.

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

## Encoding Rules

- Write JSON/Markdown as UTF-8 without BOM.
- Read user-supplied JSON with `utf-8-sig` tolerance.
- Avoid PowerShell `Set-Content` for multilingual JSON/Markdown unless encoding is controlled.
- Treat Chinese/Russian mojibake as a workflow blocker before moving materials into `03上线资料`.

## References

- `references/wb-listing-safety.md`
- `references/pricing-rules.md`
- `references/image-workflow.md`
- `references/phone-image-playbook.md`
- `references/phone-image-operations.md`
- `references/directory-protocol.md`
- `references/live-state-sync.md`
- `references/operations-analytics.md`
- `references/daily-report-template.md`
- `references/github-ssh.md`
