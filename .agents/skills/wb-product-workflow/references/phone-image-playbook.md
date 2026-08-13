# Phone SKU Image Playbook

Use this playbook for smartphone and foldable-phone listings on Wildberries. It applies to new, refurbished, and replacement-device SKU work. It supplements, rather than replaces, `image-workflow.md`.

## Outcome And Scope

Create a buyer-facing image system that is visually premium, technically clear, and honest about the exact SKU. The product must look like its official physical model, but do not copy or upload manufacturer images unless the seller has rights to use them. Official pages may be used only as a visual reference for externally visible form, camera layout, hinge, ports, display ratio, colors, and official specifications.

For a batch of related phone SKUs, work in this order:

1. Build a model fact sheet from confirmed listing data and official specifications.
2. Group SKUs by `model`; only then identify each model's memory, RAM, connectivity, color, and condition variants.
3. Generate one shared image pack per model, reusing it only where every displayed fact is true for every SKU that will use it.
4. Generate or compose SKU-specific main images only after the shared pack is ready.
5. Keep a machine-readable tracker and reconcile generated, archived, reviewed, and WB-uploaded states separately.

Never claim that images are complete merely because prompts were sent or images appeared in a temporary generation cache.

## Required Facts Before Prompting

For each model, record the source and confidence for:

- brand, exact model, model family, foldable/non-foldable form;
- colors available in the batch and the color tied to each SKU;
- RAM/storage, SIM/eSIM, 5G status, screen size/type, cameras, battery capacity, charging, dimensions, and weight;
- condition: new, officially refurbished, or seller-refurbished;
- confirmed refurbishment process, included accessories, warranty/return terms, and any verified inspection steps;
- capabilities that vary by region or SKU, such as NFC, eSIM, charging power, software/AI features, water resistance, accessories, and bundled gifts.

Use the official standard value for each model-specific specification. Never use a single battery capacity, camera specification, or display value across all phones by default. If a specification is unverified, omit it from both copy and image prompts.

## Refurbished Phone Positioning

Use the exact seller-approved Russian term. Do not disguise a used device as new. If the listing is confirmed as refurbished, use `Восстановленный` consistently in listing copy and images, and explain only verifiable measures.

The buyer concerns to address across the pack are:

- whether the device is genuinely refurbished rather than an unverified used phone;
- cosmetic and functional condition;
- battery, display, cameras, hinge/buttons/ports, and network operation;
- what inspection occurs before dispatch;
- transparency on configuration, color, and model;
- warranty, return, delivery, and included items, only when confirmed.

Allowed proof language must be factual and scoped, for example `Проверка перед отправкой`, `Функциональная проверка`, `Выберите память и цвет`, or an approved warranty term. Do not invent multi-point tests, percentages, certifications, repair steps, gifts, service centers, or warranty duration.

## Image Architecture

### User-Specified Template Master Gate (mandatory)

When the user supplies image pages or names a template directory, those pages are the only visual master for that request. This requirement overrides the default visual language in this playbook, including its dark technical-background guidance.

Before generating any asset:

1. Create a master register: page number, immutable layout elements, allowed SKU substitutions, source path/attachment ID, and target dimensions.
2. Separate the user's requirements into `template`, `palette`, `SKU facts`, and `claims`. A palette is not a template and cannot justify a layout redesign.
3. Freeze one prompt contract for every worker. The contract must state that prior drafts and other references are invalid unless the user explicitly keeps them.
4. Generate a complete representative pack first. Do not start a parallel batch until the user approves that pack when the request requires an exact or near-exact template match.

For each page, review template consistency before commercial scoring. Reject the page immediately if any of these differ from the master without explicit user approval:

- background system, page hierarchy, module count, card geometry, element positions, margins, icon logic, or information density;
- title/capacity/status placement or the model of product-versus-text balance;
- an added/removed banner, card, promise, accessory, graphic motif, or claim;
- use of an old template, a competitor layout, or a freely invented dark/light design.

Record the rejection as `REJECTED_TEMPLATE_MISMATCH`. It is not a passing image, cannot receive an upload-ready label, and must not be included in completion counts. A high visual score, correct model, correct copy, correct dimensions, or successful image-generation response cannot override this gate.

If the user requires direct image generation with text, state the limitation accurately: direct generation can aim for strong template adherence but cannot promise pixel-identical typography or geometry. Do not describe a visually similar result as template-identical. If that limitation is unacceptable, pause and request permission to use a deterministic layout-preserving method.

Use a vertical marketplace canvas unless WB requirements for the target card differ. Keep the phone large and immediately recognizable. Use a dark graphite/navy/black technical visual language with restrained cyan or electric-blue accents by default. Accent color may follow the phone/model palette, but must not reduce text legibility or conceal phone color.

### SKU Main Image

Follow the complete generation, deterministic-copy and review sequence in `phone-main-image-production-workflow.md`, then apply the mandatory commercial scoring gate in `phone-main-image-sales-score.md`. A technically valid image is not approved unless it reaches at least 93/100, has zero hard-fail defects, and beats the active benchmark. Use the approved iPhone 14 V5 reference asset as the minimum density, hierarchy, and sales-readiness standard; adapt model facts and color treatment without cloning its exact composition.

The main image is the buyer's first decision point. It should include:

- exact model name and the confirmed condition label, such as `Восстановленный`;
- an accurate front/back or folded/unfolded product view matching the official model silhouette;
- the SKU's key memory/RAM configuration and relevant color options; show multiple colors together only when they belong to the same shared offer and the selected SKU scope is clear;
- 3 to 5 high-value, confirmed specifications or buyer-trust points, prioritizing storage, display, camera, battery, connectivity, and pre-dispatch inspection;
- an energetic but controlled visual motif, such as light trails or a restrained breakthrough effect, that never hides the product or produces a gaming claim unsupported by the model facts.

Do not overload the main image. Avoid a wall of small text, competing product renders, misleading accessories, fake app screens, manufacturer logos, EAC/official-store claims, or free-gift claims unless confirmed and permitted.

#### Main-image composition safety (mandatory)

Treat text/product occlusion as a release blocker. A visually attractive image still fails this gate if the buyer cannot see the complete phone immediately.

- Reserve a fixed product-safe zone on the right: the phone (or foldable-phone pair) should occupy roughly 55–60% of the canvas height/visual weight, be fully visible, and never sit behind a text box, card, banner, or dark overlay.
- Reserve the left 35–40% for hierarchy: exact model name, refurbished-condition label, then three equal-sized cards for the SKU storage, pre-dispatch inspection, and only a confirmed warranty term.
- Keep text cards entirely inside the left zone. Do not allow a long configuration line to extend into the product zone; shorten or wrap within the card instead.
- Use a restrained dark gradient and one subtle halo/light trail behind the product. Reject shards, explosions, heavy fog, busy particles, or background panels that cut through the handset edge or make the product look cropped/covered.
- Do not add a fourth/fifth claim merely to fill space. In particular, do not introduce battery, accessories, color lists, capacity alternatives, certifications, or guarantees that are not verified for the exact SKU.
- The product must be recognizable at a small marketplace thumbnail: correct color, camera island, display/notch/hole, hinge where relevant, and no accidental extra device or accessory.

When adapting an approved style to another SKU, keep this layout contract fixed and substitute only model, storage, truthful color rendering, and approved seller facts. Do not let generative variation move the product into the text/card area.

### Shared Model Detail Pack

Create these ten shared pages per model. Rename/omit a page only where it is not factual for the model; keep the sequence and tracker slot stable.

1. `01_formats`: model identity, form factor, folded/unfolded or front/back views, and confirmed colors/configurations.
2. `02_configurations`: SKU selection guide showing only available RAM/storage/colors and a clear note to choose the required variant.
3. `03_display`: confirmed display type, size, refresh rate, brightness, or foldable display benefit. Use only verified values.
4. `04_camera`: camera modules and verified camera specifications; avoid performance promises that need test evidence.
5. `05_lifestyle`: one composition showing believable office, travel/fitness, family, and social use. The phone must remain accurate and readable.
6. `06_pre_shipment_check`: the actual approved inspection/check-before-dispatch message. It is a process diagram, not a claim of a factory certification.
7. `07_conceptual_structure`: a detailed dark-theme exploded assembly illustration, inspired by technical product documentation. Show coherent layers such as display, frame, cameras, motherboard, battery, speakers, charging module, and hinge where applicable. Use numbered callouts only for confirmed components; do not print fabricated chip names, repair details, or capacity values.
8. `08_battery_warranty`: official battery capacity plus only confirmed charging, warranty, return, and support information.
9. `09_transparency`: comparison of the seller's confirmed refurbishment/inspection process against generic unverified-refurbished risks. Do not make unsubstantiated competitor accusations.
10. `10_selection_summary`: close with exact model identity, buyer-fit summary, variant-selection reminder, and approved trust information. Do not add new claims.

For a foldable device, make the hinge and both screens visible in pages 1, 3, and 7. For non-foldables, use front/back/side views instead.

## Visual And Copy Rules

- All buyer-facing copy for WB Russia must be Russian. Prepare a Chinese review translation next to every page's Russian copy before generation.
- Keep type large, short, and high-contrast. Treat generated Russian text as untrusted: it must be visually reviewed and corrected/recomposed before upload if it is misspelled, malformed, too small, or clipped.
- Prefer a dense, premium product-presentation layout over blank white space. Dark technical backgrounds are encouraged, including for exploded views, but reserve enough contrast for product edges and labels.
- Use high-fidelity hardware rendering derived from confirmed external appearance. Never show the wrong camera island, screen hole/notch, hinge, port, button arrangement, color, or unrealistic ultra-thin proportions.
- Do not put official brand marks, proprietary badges, test scores, certifications, carrier logos, or unsupported claims in generated images. Model names may be used as factual identifiers when permitted by the listing.
- Do not copy competitor layouts verbatim. References are used for hierarchy, intensity, contrast, and evidence presentation only.
- Do not use a visible "official store" claim, fabricated gifts, fake discounts, invented guarantees, unsupported waterproof ratings, generic "original product" badges, or model-year claims.
- Avoid calling any phone `новый` if its condition is refurbished. Avoid language that conceals the condition.

## Prompt Package And Approval Gate

Before generation, create a compact review package for one representative model containing:

- factual source table and unresolved facts;
- ten-page storyboard with Russian on-image text and Chinese translation;
- one main-image prompt and ten shared-page prompts;
- model/variant mapping and planned image reuse;
- explicit prohibited claims list.

Ask the user to approve the visual direction and first representative pack. Once a direction has been approved, reuse the approved structure across models while substituting only confirmed model facts. Return to the user for approval when the product family, condition, visual style, or evidence claims materially change.

For a newly added category, approval of visual direction alone is insufficient. Complete the side-by-side listing comparison in `category-template-benchmark-gate.md`; require the prototype to beat the competitor in every scored dimension and total before setting the template to `APPROVED_FOR_ROLLOUT`.

## Batch Generation And Resumability

Use one output directory and one tracker for the full project, for example:

```text
data/analysis/phone_image_generation_<YYYYMMDD>/
  sku_image_tracker.csv
  <Model>/shared/01_formats.png ... 10_selection_summary.png
  <SKU>/main/01_main.png
```

The tracker must contain at least:

```text
artNo,model,variant,color,main_image_status,shared_image_status,review_status,archive_path,last_verified_at,notes
```

Status meanings:

- `PENDING`: no accepted asset exists.
- `GENERATED`: image was returned by the generator but has not been copied into the permanent project directory.
- `ARCHIVED`: expected file exists at the tracked permanent path.
- `REVIEW_REQUIRED`: archive exists but factual/text/visual QA is outstanding.
- `APPROVED`: passed review and is eligible for WB staging.
- `UPLOADED`: uploaded to WB and readback photo count/order was verified.

Batch protocol:

1. Process complete model packs, not arbitrary individual SKUs, so related SKUs reuse a single truthful shared set.
2. Generate in bounded batches that fit the image tool's reliable output limit; keep a batch manifest with model, page numbers, expected count, output IDs/paths, and timestamp.
3. After each batch, immediately archive each returned image to its deterministic page path and update the tracker. Do not wait until the end of a long run.
4. Reconcile before starting the next batch: compare expected page filenames against actual archive files and tracker statuses.
5. If generation stops, resume from the first missing page of the first incomplete model. Do not regenerate completed pages unless QA rejects them.
6. When asked for progress, report three separate figures: SKU main images, shared model packs (models and affected SKUs), and archived/reviewed/uploaded counts. Never combine temporary generated images with permanent archived images.
7. At final completion, run a deterministic missing-file audit for every SKU row and model pack in the local manifest, then report missing models/pages explicitly. Only then say the generation phase is complete.

## Quality Gate Before WB Upload

Review at least one full representative pack for every product family and inspect every main image. Verify:

- correct model silhouette, camera layout, hinge/ports, visible color, and selected configuration;
- all Russian text is spelled correctly, readable on a mobile card, and translated correctly in the Chinese review copy;
- every number matches the confirmed official/model-specific fact sheet;
- the condition `Восстановленный` is clear where required and never contradicted;
- no unsupported certification, warranty, gift, official-store, competitor, battery-health, or performance claim exists;
- dark backgrounds retain legibility and the phone remains the visual focus;
- variants are not confused and shared images do not state a configuration unavailable to one of their linked SKUs.

Only move approved assets into the SKU `images/` staging directory. Upload to WB only after explicit user approval, then read back image count and order.

### Main-image visual QA checklist

For every SKU main image, inspect the actual full-resolution output and a compact contact sheet before upload. Record `APPROVED` only when all checks pass:

1. The exact handset is completely visible and unobscured; text, cards, gradients, and background effects do not cover the phone.
2. The product occupies the intended right-side safe zone; all left-side copy/cards remain outside the handset silhouette.
3. Model, condition, storage, inspection, and warranty copy is readable, spelled correctly in Russian, and does not overflow, clip, or merge with the product.
4. No unapproved fourth claim, alternate storage/color list, battery fact, accessory, logo, fake badge, watermark, or unsupported promise appears.
5. Hardware identity and selected color match the SKU, including camera layout, front design, hinge, and foldable state when applicable.
6. The image preserves the intended WB main-image role: one clear buyer benefit hierarchy rather than a detail-page composition.

If any check fails, regenerate only that deterministic SKU main-image path, repeat the visual QA, and replace only photo position `1` after a dry run. Do not compensate by deleting or reordering detail images.

## Card-quality score diagnosis and remediation

WB's seller-portal card-quality score is a portal-derived result, not a field that can be inferred reliably from a successful media API response. Use the portal improvement view as the authoritative diagnosis and treat image/layout correlation as a tested hypothesis until a score refresh proves it.

When a phone card remains below 10 after a normal upload, diagnose in this order:

1. Capture the current below-10 SKU/nmID list and score; do not assume every SKU in a model family has the same issue.
2. Compare the card with same-model sibling cards that have already reached 10: required characteristic IDs/counts, non-variant values, price/dimension gates, and live image count/order.
3. Verify the expected image package count for that batch. For the current phone program it is 11 images per SKU (main image + 10 detail pages); use the batch manifest rather than assuming 11 for future programs.
4. Inspect main image #1 at full size for visual-quality defects: text over product, cropped/covered handset, unreadable/tiny Russian copy, wrong hardware/color, busy background, missing buyer-benefit hierarchy, or a main image that is only a bare product render.
5. If characteristics and image count/order match good siblings, treat the main-image visual defect as the leading remediation path. First run a single-SKU pilot: regenerate and QA the main image, dry-run upload position `1`, upload it, wait for portal propagation, then confirm that SKU's portal score changed before rolling out.
6. If the score does not improve after propagation, return to the portal's stated improvement reasons and compare all live fields again; do not repeatedly overwrite images without a new diagnosis.

For a confirmed visual-layout issue affecting multiple cards, generate and visually approve every replacement first, use a manifest that targets only photo `1`, dry-run the whole batch, upload, read back mapping/error status and package count/order, then recheck portal scores after propagation. Never declare the score fixed based solely on HTTP 200 or an unchanged cached portal view.

For the executable audit, package, resumable upload, recovery, and live verification sequence, follow `phone-image-operations.md`.
