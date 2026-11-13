# Product Promotion Video Workflow

Use this workflow to make a reusable short product-promotion video from approved SKU assets. It is suitable for WB product content and other product marketing, but does not authorize claims beyond the verified product facts.

## 1. Scope and source lock

1. Identify the SKU/model, target market, aspect ratio, target duration, and intended placement.
2. Select only final, approved reference images. Exclude drafts, rejected revisions, obsolete designs, and images with contradictory product details.
3. Create a compact reference pack:
   - all confirmed colors or sellable variants that need to be shown;
   - one clean hero image for the selected lead color;
   - one verified detail, feature, inspection, or packaging image when applicable;
   - one credible lifestyle/context image when available.
4. Extract only supported facts from the product record and selected images. Do not turn a generic inspection image into a certification claim, fabricate a performance test, or show accessories/configurations that are not in the SKU.

## 2. Storyboard and approval

Use a small number of explicit beats. For a 12–18 second product video, the preferred default is three clips of about 4–6 seconds:

| Clip | Purpose | Typical action |
| --- | --- | --- |
| 1. Variant reveal | Establish range and hero product | Multiple confirmed colors appear; lead color moves forward as the single hero product. |
| 2. Trust/detail | Give visible product evidence | Controlled exploded/detail view resolves to an intact device on a neutral inspection platform; use only factual overlays added locally. |
| 3. Lifestyle and close | Put the product in context, then finish cleanly | Product appears in credible use context and resolves to the chosen hero colour with the final message. |

- Use one action and one composition change per clip. Do not require the model to transform through many unrelated reference images in one shot.
- Lock a lead colour, front/back orientation, camera arrangement, materials, and key geometry in every applicable prompt.
- Write platform-ready copy separately. When exact Russian or other localized text is required, add it in local post-production rather than asking the generation model to render it.
- Obtain user approval before submitting any credit-consuming generation where model, duration, resolution, prompt, or reference assets are not already clear.

## 3. Generation with Dreamina/即梦 (only if explicitly requested)

1. Follow `dreamina-cli` instructions: verify the CLI, verify login/credits as practical, inspect the relevant subcommand help, submit with `--poll=30`, and record each `submit_id`.
2. Prefer the user's requested eligible fast/VIP model when available. If queue time or model availability changes, state it before submitting another paid task.
3. Generate each storyboard clip independently. Use the smallest approved reference set necessary for the shot.
4. Save each prompt and metadata beside its clip, including model, duration, resolution, task ID, reference filenames, and generation date.
5. Download only the clips needed for local review or final assembly.

## 4. Clip acceptance gate

Before assembly, review the first, middle, and last frame of every clip. Reject or regenerate only the failed clip if it has any of the following:

- product colour, model, camera layout, proportions, ports, or materials inconsistent with locked references;
- warped body, duplicate lenses, impossible folding/opening, unstable hand/product interaction, or accidental product substitution;
- inherited source-image text, unintelligible generated text, false inspection marks, invented specifications, or unsupported claims;
- the chosen hero colour/orientation does not survive into the end frame;
- a transition that makes the inspection/detail story ambiguous (for example, leaving an opened device when the intended outcome is an intact device under inspection).

Do not regenerate a complete multi-clip video because one segment failed; revise the prompt/reference set and replace that segment only.

## 5. Assembly and localized copy

1. Concatenate accepted clips in storyboard order. Keep source clips unchanged and create a new versioned final file.
2. Do not hard-concatenate the generation model's native clip audio. It is generated independently and will usually create audible discontinuities at each scene boundary.
3. Create one continuous licensed or self-created background bed for the full edit, with a short fade-in and fade-out. When the source clips establish a useful electronic, mechanical, or ambient sound style, retain their best details as low-level stems: crossfade adjacent clips (about 0.3–0.6 seconds), layer them over the continuous bed, and keep the bed running after their overlap. This preserves scene-specific content without audible cut points.
4. Add optional low-level transition whooshes at the visual cut points; do not use abrupt hard cuts in audio.
5. If no licensed music is available, use a restrained self-created ambient bed or deliver a silent master. Do not substitute an unknown web track. Preserve the no-music master so a licensed track can be added later.
6. Apply exact Russian or other localized copy locally with a font that supports the language. Use a readable size, contrast box or shadow, and a safe margin; do not cover critical product detail.
7. Keep claims conservative and supportable. Price comparisons, superiority claims, guarantees, certifications, and test-pass statements need an explicit verified basis. A safer generic closing line is a value-oriented message without unverified comparison.
8. Export the agreed format and confirm duration, dimensions, codec, and audio presence. For vertical product content, retain the requested portrait output rather than forcing a landscape conversion.

## 6. Deliverables and resumability

Use a versioned folder such as `%WORKSPACE%\videos\<product-slug>\`:

```text
assets/reference/                 approved final references
prompts/                          storyboard and per-clip prompt files
generated_pending_review/         raw downloaded clips
review/                           contact sheets and selected check frames
final/                            versioned final renders and subtitle text
manifest.json                     sources, tasks, settings, decisions, status
```

The manifest must record accepted/rejected clips, replacement reasoning, source filenames, Dreamina task IDs (if used), final caption text, and final export path. This lets a later run reuse approved clips and replace only what needs correction.

## iPhone 14 Pilot Lessons (Reusable)

- The reliable structure was: colour lineup to lead-colour hero → product detail/inspection → lifestyle to clean hero with local Russian closing line.
- Three independent 5-second fast/VIP clips were more stable than a single 15-second multi-image chain.
- Generate without source-image labels; add final text locally. This prevents inherited or malformed Russian text.
- Do not approve a clip solely because it finishes: inspect the middle frame, where product deformation and ambiguous exploded-view transitions are most likely.
