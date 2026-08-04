# Phone V2 New-SKU Listing Logic

Use this reference only for a request explicitly invoking the **V2 SKU logic**. It codifies the successful second-version phone SKU rollout validated on WB in August 2026.

## Scope and trigger

Apply to new phone sellable identities that reuse a live source SKU's non-media product data while receiving the current V2 visual package. It is a listing workflow, not a request to change existing source cards. Ask for the source SKU set and the new configuration matrix only if they are not supplied.

## Source of truth and inheritance

1. Read the **current live WB source cards** immediately before package generation. Do not reuse old exported titles, prices, attributes, or images as truth.
2. Copy only confirmed current card data: title, category/subject, brand, characteristics, dimensions, description, certificates/compliance facts, price/discount policy, and compatible warehouse policy.
3. Do not copy a source video to V2 new cards unless the user explicitly asks.
4. Keep every independently sellable configuration as a separate seller article and a separate official WB barcode. Never create or upload a fabricated barcode.
5. Same model + same physical color may share one V2 **main image** across capacities/configurations; every image's model, color, capacity statement, kit and claim must still match the target SKU.

## Locked V2 visual system

1. Resolve the template through the V2 template registry before generating any SKU. The registry lives at `E:\CodexWorks\WB\data\template_registry\phone_v2\current\template_manifest.json`; its protected mirror belongs under the phone category's `00知识库\template_registry\phone_v2`. Do not rely on an unrecorded online card as the only template source.
2. The manifest must record: V2 version, approved timestamp, all anchor `nmID`/vendorCode values, all eight intended image positions, local snapshot paths, SHA-256 hashes, dimensions, visual-review status, and the user approval that made the version active.
3. Prefer the current live V2 anchor cards only after confirming the returned eight-image set is complete and agrees with the active manifest. Do not select a visually similar SKU, an earlier local folder, or a different image position merely because an anchor is unavailable.
4. If every required live anchor is unavailable, deleted, or has incomplete media, use the complete locally stored snapshot from the active manifest and explicitly report `templateSource=fallback_snapshot`. This is a valid continuation path, not a failure.
5. If the snapshot is missing, incomplete, hash-mismatched, or not explicitly approved, stop before generation and request a replacement anchor or template approval. Never recreate the template from memory or substitute a different layout.
6. When a live anchor differs from the active snapshot, create a proposed version containing the full downloaded eight-image set, hashes, source identities, and visual diff/review. Do not activate it or use it for new SKUs until the user approves the version upgrade. After approval, retain the former version as a rollback snapshot.
7. During the first V2 use after this rule is introduced, bootstrap the registry from the user-confirmed latest online V2 template: download and inspect all eight positions, save the immutable snapshot and mirror, create the manifest, and obtain the user's approval before marking it active.
8. Reuse the V2 layout directly. Parameterize only verified model, color, capacity/configuration, device renders, and colour swatches. Do not redesign the composition, icons, typography hierarchy, or image sequence.
9. V2 main-image baseline: bright mountain/lake environment; large model name in the upper region; capacity badge; slogan `ТВОЙ МИР — В ТВОИХ РУКАХ`; left feature strip; right 12-month warranty and included-items strip; two-view device hero; and bottom colour, pre-dispatch-check, and 24-hour-dispatch information. Preserve product-safe zones and keep all copy clear of the device.
10. Main-image quality target is the live V2 42-SKU batch: product dominant and fully visible; no excessive blank region; high contrast; factual copy; no border boxes around text; colour swatch agrees with the SKU; product does not merge into the ground; and no blocked/misaligned copy at full size or marketplace thumbnail size.
11. Create the remaining seven gallery positions by directly parameterizing the current approved V2 gallery templates. Maintain their order, information hierarchy, and product angles. Do not substitute a loosely similar visual system.
12. Do not state unverified condition, included items, delivery, warranty, camera, battery, or performance claims. The main-image wording must agree with live title/attributes and verified facts.

## Mandatory V2 production and review gates

1. Build a SKU matrix with source vendorCode/nmID, new vendorCode, new barcode, model, capacity/configuration, physical colour, title, current price/discount, source photo count, and template source nmID.
2. Generate all media into a versioned working folder. Run a full visual audit before any WB mutation:
   - review every image at full size and at approximately 320 px;
   - confirm exact model, device shape, cameras, colour, capacity, kit and Russian copy;
   - confirm text is centered, unblocked, legible, and uncut;
   - reject wrong colour, product obstruction, factual mismatch, copy error, layout drift, or lower quality than the V2 live baseline;
   - regenerate failed items and rerun review. Do not submit a batch with unresolved failures.
3. Create the listing package and preserve per-SKU manifests, source-live snapshots, media manifest/hashes, review scorecards, batch mapping, and intended image order.
4. Present the complete package to the user for approval before creating cards, uploading images, changing prices, assigning tags, or writing stock.
5. After approval, execute in this order: create cards and official barcodes; upload images; read back identity and photo count/order; copy and read back price/discount; run card-error check; then perform buyer-perspective audit.
6. Add the backend tag `第二版` only when the user requests activation/tagging. Reuse the exact existing tag if it exists; do not create duplicate tags.
7. Write initial FBS stock only after explicit user authorization. Default to 10 per SKU only when the user has confirmed the same initial-stock standard for the batch; otherwise ask. Read back every barcode from the same warehouse, then verify buyer-facing card availability, stock, price, and delivery data before saying sellable.
8. Archive every live request, response, readback, mapping, and remaining blocker under the relevant `03上线资料` and buyer/operational evidence under `04运营资料`.

## Completion statement

Report separately: generated/reviewed package count, WB cards created, images and order verified, price parity verified, tag result, warehouse and stock readback, buyer-facing sellability, and any propagation or compliance blocker. Never collapse a successful upload into a claim that the SKU is sellable without the final stock and buyer-facing readback.
