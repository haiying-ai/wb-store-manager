# Phone Main-Image Production Workflow

Use this workflow for every WB phone main image. It defines the reusable generation prompt, deterministic Russian copy composition, visual review and rollout gate. Pair it with `phone-main-image-sales-score.md` and `category-template-benchmark-gate.md`.

## 1. Lock SKU truth and benchmark

Before generation, record exact brand/model suffix (`Pro`, `Pro Max`, `Plus`, `Air`, foldable name), storage, color, condition, exterior hardware, verified specifications, dispatch promise, inspection wording, included items and warranty. Never infer a missing fact.

For the current seller-refurbished phone program, `24-hour dispatch` and the included charger, case and screen film may be used because the seller confirmed them. Warranty duration and all technical specifications still require an approved SKU/model fact source.

Capture the active competitor/reference main image and score it with the same 100-point rubric. A new-category prototype must also pass `category-template-benchmark-gate.md` before batch use.

## 2. Generate a clean product-first base

Generate a new commercial base rather than placing a few boxes over the old main image. Use the latest online-matching source image only to identify the correct hardware/color; do not use an intermediate rejected version.

Reusable base prompt template:

```text
Create a premium vertical Wildberries smartphone hero image, 2:3 aspect ratio.
Exact product: [MODEL], [COLOR], [CONDITION]. Show an accurate front-and-back
pair with the correct camera island, screen cutout/notch, buttons, proportions
and finish. Product occupies about 50–60% of visual weight and is fully visible
inside a protected right/central product zone. Use a dark premium technical
environment with one dominant accent color and one restrained secondary accent,
controlled rim light, halo/platform and rich but ordered detail. Reserve clean,
high-contrast information zones around—not over—the product. Do not render text,
letters, numbers, logos, badges, accessories, boxes, hands, extra phones, black
rectangles, shards or effects crossing the handset. No watermark. Marketplace
thumbnail must recognize the exact model and color immediately.
```

Adapt the palette to the handset color so the batch has variation while retaining one visual system. Do not let blue, purple, magenta and red compete at equal strength. Dark phones require a controlled bright edge/halo.

## 3. Add copy deterministically after generation

Treat AI-generated text as unusable. Add all Russian copy, icons and cards with deterministic layout/compositing tools after the base image is accepted.

Required reading path:

1. prominent condition label;
2. exact model, including `Pro` or `Pro Max` where applicable;
3. one storage/color line;
4. dominant handset;
5. fulfilment/trust cards;
6. compact bottom specification rail.

Use only confirmed items and avoid repetition:

- `ОТПРАВКА ЗА 24 ЧАСА`;
- `ПРОВЕРКА ПЕРЕД ОТПРАВКОЙ` with only verified check details;
- `В КОМПЛЕКТЕ` with charger, case and screen film where confirmed;
- seller warranty duration only when confirmed;
- 3–5 model/SKU specifications from the approved fact table.

Layout contract:

- Keep every card outside the handset silhouette; no black/dark panel may cover any part of the phone.
- Use consistent card geometry, margins, icon language and alignment.
- Make the model and storage readable at 320 px width. Do not repeat storage in multiple modules.
- Target 85–95% purposeful occupancy without filling space with decorative badges or unsupported claims.
- Keep the phone fully inside safe margins and retain separation from the background.

## 4. Manual QA and scoring loop

Inspect every output at full resolution and at 320 px width. Score it with `phone-main-image-sales-score.md`; record category scores, total, hard fails, benchmark score, reviewer and revision note.

Reject and regenerate when:

- any box, text, gradient or effect crosses the product;
- the hardware/model suffix/color/condition is wrong;
- Russian text is malformed, small, clipped or ambiguous;
- the image is merely the original main image with overlay boxes;
- visual density, hierarchy or finish is visibly below the approved category prototype;
- the total is below the approved iPhone 14/category-template score, does not exceed the active competitor, or any hard fail exists.

Fix the lowest-scoring dimension, render again and repeat until it passes. Automated geometry/OCR checks support review but never replace human visual inspection.

## 5. Prototype and batch rollout

For a new category or materially different product family, generate one representative complete listing first. Compare main image, gallery, title, characteristics, description and trust information against the competitor using `category-template-benchmark-gate.md`. Do not generate the category batch until the template status is `APPROVED_FOR_ROLLOUT` and the user approves it.

For the batch, substitute only verified model/SKU facts and palette treatment. Render each SKU from the approved production logic, score every main image individually, regenerate failures, build contact sheets, then request upload approval. Upload only photo position `1` when replacing main images; preserve detail pages and verify live readback according to `phone-image-operations.md`.
