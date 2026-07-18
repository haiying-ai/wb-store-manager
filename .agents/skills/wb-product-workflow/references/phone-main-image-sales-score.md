# Phone Main-Image Commercial Score

Use this gate for every WB phone main image before user review or upload. The approved iPhone 14 V5 asset in `../assets/phone-main-image-reference/iphone14_v5_sales_reference.png` is the minimum reference for information density, visual hierarchy, product prominence, and thumbnail readability.

## Passing rule

Approve only when all three conditions hold:

1. Total score is at least **93/100**.
2. Every hard-fail check passes.
3. The image scores higher than the current competitor/reference image under the same rubric. If tied, revise it.

Record the category scores, total, hard-fail result, benchmark score, and revision note. Do not round a score upward to pass.

## Hard-fail checks

Reject immediately when any item is true:

- phone is covered, cropped, visually merged with the background, or not identifiable at thumbnail size;
- model, storage, color, condition, hardware silhouette, camera layout, or Russian text is wrong;
- text overlaps the phone, clips, overflows, is malformed, or is too small to read on a WB card;
- an unconfirmed claim, accessory, warranty, dispatch promise, gift, certification, official-store claim, or performance promise appears;
- condition is hidden or contradicted, especially describing a refurbished phone as new;
- capacity or another low-value fact is repeated while a higher-value confirmed selling point is omitted;
- layout copies a competitor image substantially instead of using it only as a benchmark.

## 100-point rubric

### 1. Instant recognition and hierarchy — 20

- In the first glance, read in this order: condition, exact model, storage/color, phone, fulfillment/trust benefits, key specifications.
- Model and storage remain readable in a compact thumbnail.
- One dominant headline, one dominant product, and one clear supporting-information system; no competing focal points.

### 2. Product prominence and separation — 20

- Phone occupies roughly 45–60% of visual weight and remains fully visible.
- Product edges contrast clearly against the background; dark phones require a controlled rim light/halo.
- Text/cards/icons never cross the handset silhouette. Hardware and selected color are immediately recognizable.

### 3. Selling information and density — 20

- Show exact model, storage, color, condition, and the strongest confirmed trust/fulfillment facts.
- For the current seller-refurbished phone program, use confirmed facts such as 24-hour dispatch, pre-dispatch inspection, seller warranty, and included charger/case/screen film only where true for that SKU.
- Fill the canvas with useful hierarchy rather than decorative emptiness; target roughly 85–95% purposeful visual occupancy while preserving breathing room.
- Avoid duplicated capacity, decorative badges without information, and low-value filler.

### 4. Readability and information architecture — 15

- Use short Russian copy, large type, strong contrast, consistent card dimensions, and one icon language.
- Keep a deliberate reading path and group related benefits. Match icon size to information importance.
- Verify full-resolution output and a 320 px-wide thumbnail. All primary claims must survive the thumbnail check.

### 5. Visual order and differentiation — 10

- Use one dominant dark base, one primary luminous accent, and one restrained secondary accent; white remains the main text color.
- Avoid simultaneous competition among blue, purple, magenta, and red. Use accents to encode hierarchy, not decoration.
- Preserve premium energy and rich detail without imitating the benchmark's exact palette or placement.

### 6. Trust, factual integrity, and buyer confidence — 10

- Every claim is confirmed for the exact SKU and condition is prominent.
- Inspection, included items, dispatch, warranty, and specifications are stated precisely without implying unsupported evidence.
- No fake badge, manufacturer endorsement, certification, or misleading originality/new-product claim.

### 7. Technical finish — 5

- Canvas meets WB dimensions; image is sharp, clean, and free from watermark, artifacts, malformed hands/ports/cameras, or unintended extra devices.
- Margins and alignment are consistent; no element touches unsafe edges.

## Required review loop

1. Generate and compose the image using `phone-main-image-production-workflow.md`; do not score a shortcut overlay on the old main image as a new candidate.
2. Score the first output at full size and 320 px width.
3. Compare it with the active benchmark using the same rubric.
4. Revise the lowest-scoring category with one targeted change.
5. Repeat until the image reaches at least 93, has zero hard fails, and exceeds the benchmark. For a new-category template, also require a strictly higher score in every dimension under `category-template-benchmark-gate.md`.
6. Present the scored candidate to the user. Keep it in `REVIEW_REQUIRED` until explicit approval; never upload merely because the internal score passed.
