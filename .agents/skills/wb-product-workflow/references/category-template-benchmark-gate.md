# New Category Competitor Benchmark Gate

Use this gate before promoting any generated listing template across a newly added category.

## Required benchmark input

Obtain at least one representative competitor product before creating the category template. Record the capture time and source URL or WB `nmID`, then preserve:

- exact category/subject, title, description, characteristics and variant structure;
- current main image and gallery sequence;
- price/discount, delivery promise, seller/brand presentation, rating/review count and visible trust signals;
- search terms, buyer objections, claims, included items and compliance risks visible on the card.

Do not bypass CAPTCHA or access controls. Use public data, an authenticated user-controlled browser session, official APIs, or user-provided exports/screenshots. Treat price, delivery, rating and sales rank as time-sensitive evidence.

## Representative prototype

Choose one representative SKU that exercises the category's main design and factual constraints. Build the complete first listing prototype before any batch rollout:

1. category mapping and verified fact sheet;
2. main image and full gallery/story sequence;
3. title and search-intent coverage;
4. characteristics and variant mapping;
5. description, trust/fulfilment information and compliance review;
6. pricing/readiness notes where the template controls presentation.

## Same-rubric comparison

Score the competitor and prototype independently using the same evidence and the following 100-point rubric. Record a reason and evidence path for every score.

| Dimension | Points |
| --- | ---: |
| Main-image recognition, hierarchy, product prominence and thumbnail conversion | 25 |
| Gallery completeness, narrative order and objection handling | 15 |
| Title clarity, search relevance and exact variant identification | 15 |
| Characteristic completeness, correctness and variant consistency | 15 |
| Description clarity, buyer usefulness and mobile readability | 10 |
| Verified value proposition, fulfilment and trust information | 10 |
| Cross-asset visual/copy consistency and differentiation | 5 |
| Factual integrity, policy safety and technical finish | 5 |

For phones, also apply `phone-main-image-sales-score.md` to the main image. Other categories must define an equivalent category-specific main-image rubric before scoring.

## Promotion rule

The category template is `APPROVED_FOR_ROLLOUT` only when:

- the prototype has zero factual, compliance, mapping, text-legibility or product-identity hard failures;
- the prototype total is strictly higher than the competitor total;
- the prototype is strictly higher in **every scored dimension**; ties do not pass;
- the main image beats the competitor/reference under the category's detailed main-image rubric;
- the user has reviewed the side-by-side prototype and explicitly approved rollout.

If any dimension is equal or lower, revise the weakest controlled component and rescore both unchanged competitor evidence and the new prototype. Do not average a weak dimension away with a strong one. External social proof, current price, delivery and sales rank must be recorded as context but never fabricated; if they are outside template control, do not award the prototype invented points for them.

## Required record

Save one versioned `category_template_benchmark.json` or equivalent record containing benchmark source/time, competitor and prototype evidence paths, dimension scores, hard-fail results, total scores, revision history, approver, and final status. Batch generation must reference the approved template version and stop if it changes materially.
