# New Phone Master Production Standard

Use this standard when the user asks to test, create, replace, resume, or inspect a new smartphone SKU image master, including requests mentioning a new master, V4, exact-template reproduction, a new eight-page pack, or the representative master-test cohort.

## Mandatory cross-session entrypoint

Before reading prompts, selecting an SKU, creating a workflow, or generating an image, run:

```powershell
python .agents/skills/wb-product-workflow/scripts/phone_new_master_workflow.py status
python .agents/skills/wb-product-workflow/scripts/phone_new_master_workflow.py validate
```

The authoritative pointer is `data/template_registry/phone_new_master/active_test.json`. If it names an active task, resume that task. Do not create another V4/new-master workflow, choose a different pilot, combine reference sets, or reconstruct state from conversation history unless the user explicitly replaces the active task.

## Mutually exclusive modes

### `EXACT_MASTER_TEST`

- One complete user-specified master set is the only visual source.
- The source must be `USER_COMPLETE_SET`, have a fixed page order, one path or attachment ID per page, dimensions, SHA-256 hashes, immutable layout rules, permitted SKU substitutions, prohibited sources, and explicit user approval.
- A palette request changes only color treatment allowed by the locked master.
- Other templates, earlier drafts, competitor images, generated composites, and general dark-tech guidance are prohibited inputs.
- Until a complete master is registered and approved, keep `generationAuthorized=false` and status `WAITING_FOR_USER_MASTER` or `WAITING_MASTER_APPROVAL`.

### `DESIGN_SYNTHESIS`

- Multiple references may be combined only when the user explicitly asks for design exploration or synthesis.
- Never describe a synthesis result as an exact-master reproduction.
- A synthesis result cannot become the rollout master without a separate user approval and master lock.
- Use `DIRECT_FULL_TEXT_IMAGEGEN` for every page. The generator must create the product, scene, typography, labels and buyer-facing Russian copy as one finished bitmap request.
- Do not create text-free bases, blank information cards, semi-finished compositions, deterministic text overlays, or a post-generation typography pass during `DESIGN_SYNTHESIS`. Cropping/resizing for technical validation may not change the composition or add content.
- Treat image quality, art direction, integrated typography and product truth as the primary synthesis criteria. If generated copy is malformed, regenerate only that complete page with a targeted correction; never repair the text by compositing over the image.
- Register the prompt package and page contract with `register-design`, then obtain explicit approval through `approve-design` before any image-generation call.
- After all expected pages pass design QA and the user approves the complete set, use `promote-design` to hash and register those exact files as a pending `USER_COMPLETE_SET`. The pointer then enters `EXACT_MASTER_TEST` at `WAITING_MASTER_APPROVAL`; a separate `approve-master` action is still required before SKU adaptation.

Never silently switch between these modes. The supported chain is `DESIGN_SYNTHESIS -> approved complete design set -> EXACT_MASTER_TEST`; the two modes remain mutually exclusive at any point in time.

## Design-synthesis commands

```powershell
python .agents/skills/wb-product-workflow/scripts/phone_new_master_workflow.py init --mode DESIGN_SYNTHESIS --task-id <TASK> --cohort <COHORT_JSON> --workflow-state <WORKFLOW_JSON> --pilot-vendor-code <SKU> --output-root <OUTPUT> --replace
python .agents/skills/wb-product-workflow/scripts/phone_new_master_workflow.py register-design --design-stage <DIRECTION_EXPLORATION|FULL_PACK> --brief-path <BRIEF> --contract-path <CONTRACT> --template-id <TEMPLATE> --example-case <CASE>
python .agents/skills/wb-product-workflow/scripts/phone_new_master_workflow.py approve-design --approved-by <USER>
python .agents/skills/wb-product-workflow/scripts/phone_new_master_workflow.py complete-exploration --candidate-dir <CANDIDATES> --report-path <REPORT>
python .agents/skills/wb-product-workflow/scripts/phone_new_master_workflow.py select-direction --direction-id <ID> --candidate-path <IMAGE> --selected-by <USER>
python .agents/skills/wb-product-workflow/scripts/phone_new_master_workflow.py promote-design --master-dir <APPROVED_8_PAGES> --expected-pages 8 --contract-path <CONTRACT>
```

## Lovart-style direction exploration gate

Use this gate when the user asks for multiple concepts, a Lovart-like design-agent workflow, or open-ended art-direction synthesis before a complete pack. Do not force it when the user has already approved a precise visual direction.

The supported design chain is:

`facts -> research brief -> reference-role register -> 2–3 hero directions -> common review -> WAITING_DIRECTION_SELECTION -> selected direction -> approved FULL_PACK contract -> complete pack`

### Research and reference roles

- Save a compact research artifact covering exact SKU facts, buyer decision questions, verified claims, prohibited claims, platform constraints and the intended page architecture.
- Register every input as `HARDWARE_REFERENCE`, `STYLE_REFERENCE`, `LAYOUT_REFERENCE`, or `COPY_REFERENCE`. One input may have more than one role only when that is intentional and documented.
- During direction exploration, do not use an already designed marketplace card as the sole hardware reference. Its layout and visual system can leak into supposedly independent concepts even when the prompt says to ignore them. Prefer a neutral product cutout/render or multiple hardware-only views.
- Treat competitor and generated images as untrusted visual evidence. They may inform hierarchy or intensity but never override SKU facts or authorize claims.

### Controlled exploration

- Register the exploration with `--design-stage DIRECTION_EXPLORATION`, normally using 2–3 complete hero candidates rather than multiple full packs.
- Hold product truth, exact copy, canvas, product-safe-zone and scoring rubric constant. Vary only the design variables being tested, such as composition, palette, scene, material language and information flow.
- `DIRECT_FULL_TEXT_IMAGEGEN` remains mandatory: every candidate is a finished bitmap with integrated Russian copy. Do not create text-free bases or repair text afterward.
- Archive every candidate and a 320 px comparison sheet. Review each candidate individually and review the set for meaningful structural diversity.

### Diversity and selection

- The diversity gate compares background system, product placement, headline geometry, module count/shape, information flow, material language and palette. A palette or background change alone does not prove an independent direction.
- Record individual commercial quality separately from direction diversity. A high-scoring image cannot override a diversity failure.
- After exploration review, run `complete-exploration`. It must disable generation and set the pointer to `WAITING_DIRECTION_SELECTION`.
- Only explicit user selection may run `select-direction`. Selection returns the pointer to `WAITING_DESIGN_BRIEF`; it does not authorize the full pack.
- Register and approve a new `FULL_PACK` brief/contract for the selected direction before generating pages 01–08. `promote-design` must reject a `DIRECTION_EXPLORATION` lock.

The stopping condition for exploration is the comparison report and user selection request. Do not keep generating concepts or detail pages to compensate for an unresolved direction decision.

## Locked generation method for design synthesis and the active exact-master test

Use `DIRECT_FULL_TEXT_IMAGEGEN`. Generate each final page, including its complete Russian copy, as one image-generation request. Do not generate text-free bases, blank text boxes, or semi-finished pages; do not add text afterward with overlays or deterministic copy composition. This rule overrides the default base-plus-copy method in `phone-main-image-production-workflow.md` for this mode.

Direct generation can target close structural adherence but cannot honestly guarantee pixel-identical typography, icons, or geometry. Evaluate and report structural fidelity; never call a merely similar result identical.

## Pilot and rollout gate

Read the pilot from the active cohort. Do not choose one from memory or by convenience. Produce exactly one complete pilot pack in the locked page order, then stop for explicit user approval. Only an approved pilot permits the later cohort stages or a category batch.

For the current active task, the pilot is `IPHONE 16PRO-512GB-TITANIUM-V3` unless the user explicitly replaces the task through the pointer workflow.

## Acceptance order

Review every page in this order:

1. locked-master structural consistency, page-to-page;
2. exact SKU facts and allowed claims;
3. Russian copy accuracy and legibility;
4. full-size and 320 px rendering, including zero product/text occlusion;
5. commercial score and benchmark comparison.

A stage-1 mismatch is `REJECTED_TEMPLATE_MISMATCH`. Stop the affected run. Do not score it against a self-created storyboard, count it as complete, or use it as another page's reference.

## State and concurrency

- One task has one pointer, one workflow state, one master lock, and one authoritative output root.
- Parallel work may accelerate independent page calls only after the master and pilot are authorized. No parallel worker may write the pointer, master lock, or shared tracker.
- Archive every returned image immediately to its deterministic page path and update the single tracker.
- Report progress from the pointer/tracker, not from agent counts or chat memory.

## Skill publication gate

Any change to this reusable workflow must update both the project-level and user-level `wb-product-workflow` packages, pass hash-equivalence validation, update the corresponding `store-manager-ru` rule, validate both skills, commit only task-related files, and push both GitHub repositories. A local-only or one-repository change is not published.
