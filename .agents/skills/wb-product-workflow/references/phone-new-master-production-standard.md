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

Never silently switch between these modes.

## Locked generation method for the active exact-master test

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
