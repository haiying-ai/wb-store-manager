# Phone V1 Image-Template Logic

Use this reference only for a request explicitly invoking the **V1 SKU logic**. V1 means the final approved first-version **image set** on WB. The existing WB original-SKU workflow remains authoritative for source facts, title, parameters, description, pricing, profitability, card payloads, barcodes, and stock; do not duplicate or override those rules here.

## Scope

1. Use the exact current live V1 source card selected for the target model/colour/capacity as the media-template anchor. For the first registered template, the user-confirmed anchor is `IPHONE 15-256GB-BLACK`, nmID `1248939649`.
2. Treat the final online image package and its exact order as the V1 template. Do not derive V1 from V2, a prior local working folder, an earlier V1 revision, or a visually similar SKU.
3. Parameterize only product renders and verified SKU-specific facts required by the approved V1 layout. Maintain the final V1 hierarchy, sequence, composition, and image count for the applicable template family.

## V1 template registry and fallback

1. Resolve V1 media through `E:\CodexWorks\WB\data\template_registry\phone_v1\<template-family>\current\template_manifest.json`; mirror the full version under the phone category's `00知识库\template_registry\phone_v1\<template-family>\`.
2. Store every active version as an immutable snapshot: anchor `nmID`/vendorCode, source configuration, live capture time, image count and positions, image files, source URLs, SHA-256 hashes, dimensions, visual-review result, approval record, and previous-version link.
3. Prefer the live anchor only after verifying that every expected position exists and hashes/count/order agree with the active template manifest.
4. If the live anchor is deleted, unavailable, or has incomplete media, use the complete approved snapshot for that same V1 template family and report `templateSource=fallback_snapshot`. This keeps V1 generation running without substituting another design.
5. If the snapshot is absent, incomplete, hash-mismatched, or for another model/template family, stop media generation and request a replacement anchor or template approval. Never recreate the V1 template from memory.
6. If live media differs from the active snapshot, capture a complete pending version and visual diff. Do not activate or use it until the user approves the V1 template upgrade; retain all previously approved versions for rollback.

## Media review and package gates

1. Inspect every V1 template image at full size and approximately 320 px before marking a version active. Confirm image order, product visibility, Russian text, model/colour/capacity facts where displayed, and no obstruction or degraded quality.
2. Before presenting a target SKU package, verify that no V2 backgrounds, icons, copy, image positions, or other V2-only elements have leaked into the V1 set.
3. Record the active V1 template version, anchor identity or fallback status, target media manifest/hashes, and review result in the SKU package.
4. Keep the existing original-SKU approval, upload, live-media readback, price, card-error, stock, and buyer-perspective-audit gates unchanged.

## Completion statement

Report V1 template version, template family, live-anchor versus fallback source, image count/order verification, visual-review result, and any pending template revision separately from the original-SKU listing result.
