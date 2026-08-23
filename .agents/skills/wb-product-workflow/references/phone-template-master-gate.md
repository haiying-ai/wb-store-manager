# Phone Image Template Master Gate

Use this gate whenever a user provides phone-image references or explicitly names a template directory.

For a new-master test, also read `phone-new-master-production-standard.md` and resolve its active pointer first. `EXACT_MASTER_TEST` accepts one complete `USER_COMPLETE_SET`; it forbids composite references, earlier drafts, competitor pages and a worker-selected pilot. Multiple references are permitted only in an explicitly named `DESIGN_SYNTHESIS` task, which is not an exact-master test.

## Rule hierarchy

1. Exact user-provided master pages and explicit page-by-page requirements.
2. Exact SKU facts and confirmed claims.
3. User-approved palette requirements.
4. General phone visual guidance and historical drafts.

Lower-priority rules may never override a higher-priority one. In particular, a color request does not permit a different layout.

## Freeze before prompting

Create a compact page register with:

- master path or attachment ID;
- page role and target dimensions;
- immutable items: background, grid, cards, title hierarchy, icon locations, margins, information density, and product-safe zone;
- permitted substitutions: model, visible color, storage, exact verified specifications, and user-approved claims;
- prohibited substitutions and invalid references.

Give the same frozen register to every parallel worker. Old prompts, earlier drafts, competing images, and generic dark-tech styles are invalid unless the user explicitly retains them.

Do not authorize generation until the register is represented by an approved immutable master lock containing ordered page paths or attachment IDs, dimensions and hashes. A waiting task must remain `generationAuthorized=false`.

## Acceptance sequence

1. Template-consistency review.
2. Exact-SKU fact and claim review.
3. Russian-copy review.
4. Full-size and 320 px review.
5. Commercial/sales score.

Any failure at stage 1 is `REJECTED_TEMPLATE_MISMATCH`. Do not score it as passing, count it in completion totals, stage it for WB, or use it as a reference for another batch.

## Batch stop line

For a new master, generate and review one representative complete pack before category-wide rollout. If it is structurally different from the master, stop all related batches. Resume only after the user approves a corrected representative pack or explicitly relaxes the matching standard.

## Direct image-generation limitation

Direct image generation can produce a complete text-bearing image and target close adherence, but it can redraw typography, icons, and geometry. It must not be represented as pixel-identical template preservation. If the user requires pixel-level preservation, request approval for a deterministic layout-preserving process instead.

When the active pointer locks `DIRECT_FULL_TEXT_IMAGEGEN`, generate every final text-bearing page in one image-generation call. Do not substitute a text-free base, blank boxes, post-generation text overlays, or deterministic copy composition. Report structural fidelity honestly rather than claiming pixel identity.
