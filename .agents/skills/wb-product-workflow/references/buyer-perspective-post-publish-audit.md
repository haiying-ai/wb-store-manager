# Post-Publish Buyer-Perspective Audit

Run this audit after a product is live and readable on WB, and rerun it after any material change to title, attributes, description, images, configuration, condition, included items, warranty, or fulfilment promises.

## Objective

Review the listing as a normal buyer making a purchase decision. Detect both:

1. common-sense contradictions that can cause the buyer to order the wrong item or cancel; and
2. missing or unclear information that creates reasonable purchase doubt.

API acceptance, a full image count, or a high card-quality score does not satisfy this gate by itself.

## Required Scope

Audit every sellable SKU and the complete live card:

- seller SKU/vendorCode, nmID, barcode mapping, title, brand and subject;
- exact model, capacity, RAM, color and condition;
- characteristics and description;
- every live image position in order, at full size and marketplace thumbnail size;
- selected-looking color, memory, bundle or configuration controls in shared images;
- new/refurbished status and any conflicting wording;
- battery, display, camera, processor, SIM/region, connectivity and other model claims;
- included items, gifts, charger/cable/case/film claims;
- warranty identity, duration, coverage and process;
- dispatch/delivery promises and other seller-controlled claims;
- cosmetic grade, battery-health threshold, replaced/original-parts statements and refurbishment disclosure when applicable;
- public buy controls, delivery date and any visible mismatch with the seller card when relevant.

Use verified product facts and current live state as evidence. Do not infer region-dependent or inventory-dependent facts.

## Review Method

1. Read the current live cards and error-card list.
2. Reconcile the expected SKU set with unique nmIDs and exact variant facts.
3. Download or read every live image position. Record photo count and order.
4. Hash identical images and review unique groups for efficiency. Preserve member mappings so one shared-image finding expands to every affected SKU.
5. Inspect full-size images for factual content, Russian copy, product geometry, color, capacity, selected states and promises.
6. Inspect approximately 320 px thumbnails for readability and misleading emphasis.
7. Cross-check title, attributes, description and every image against the SKU and against one another.
8. Think through the buyer's questions: “What exactly will arrive?”, “Is it new or refurbished?”, “Which capacity/color did I select?”, “What condition and battery should I expect?”, “What is included?”, “Who provides warranty?”, and “When will it ship?”
9. Retry isolated CDN/read failures. Do not call the audit complete with unreviewed image positions.

## Classification

### P0 — Hard contradiction

Examples: wrong model/capacity/color, selected-looking option that differs from the SKU, new/refurbished conflict, false included-item claim, incompatible technical claim, or a live image in the wrong position.

- Block final completion.
- List every affected SKU and nmID.
- Propose the smallest truthful fix.
- Require explicit user approval before live remediation.

### P1 — High purchase doubt

Examples: missing battery-health standard for refurbished phones, unclear cosmetic grade, unknown screen/parts replacement, unverified bundle contents, or ambiguous regional hardware version.

- Quantify affected SKUs.
- Distinguish missing confirmed facts from missing presentation.
- Ask for the business fact when it cannot be safely discovered.

### P2 — Medium trust or expectation risk

Examples: vague warranty process, fixed dispatch promise not supported by fulfilment data, small unreadable copy, or redundant low-value imagery.

- Document evidence and propose an improvement.
- Do not silently invent stronger claims.

### P3 — Backend-only risk

Examples: seller-article spelling errors, alias collisions, stale mappings or automation keys that do not affect buyer-visible content yet.

- Preserve live identifiers when changing them would break nmID, barcode, inventory, price or order history.
- Prefer a tested internal alias mapping.

## Required Output

Save a timestamped audit package under `<category>/04运营资料` containing:

- scope and completion counts: expected/live SKUs, image positions, unique image groups and retry gaps;
- concise findings report organized by P0–P3;
- affected-SKU CSV/JSON with nmID, image position or field, evidence and recommended action;
- contact sheets or direct evidence paths for visual findings;
- facts versus inferences versus facts still requiring user confirmation;
- a statement of what was not changed.

After approved remediation, save the request, response, readback, visual verification, image count/order, error-card result and completion receipt under `03上线资料`.

## Completion Gate

The audit passes only when:

- every expected live SKU is covered;
- every image position is reviewed or a documented retry succeeds;
- no P0 issue remains unresolved;
- P1–P3 findings are documented with affected scope and next action;
- any live fix is visually read back rather than inferred from API success;
- the user has approved judgment-dependent media or copy before upload.
