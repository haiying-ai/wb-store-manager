# Image Workflow

Use staged image handling.

1. Lock confirmed product color and physical appearance.
2. Find the matching product images and extract only visible facts from them.
   - Visible facts include shape, color, materials that can be seen, included accessories, display/interface elements, package contents, and usage context shown in the source image.
   - Do not infer hidden functions, certifications, performance, medical effects, warranty, or compatibility unless confirmed by source facts.
3. Stop and send the visible-fact extraction to the user for confirmation.
   - Do not translate benefits, build screen concepts, generate prompts, or generate images before the user confirms steps 1-2.
4. Translate seller benefits into buyer concerns.
   - Turn each selling point into the question or hesitation it answers, such as safety, effectiveness, ease of use, fit, cleaning, portability, giftability, or setup effort.
   - Keep only concerns that the product facts and visible evidence can support.
5. Create or confirm the main image workspace.
6. Stop and send the buyer-concern mapping and main-image workspace/direction to the user for confirmation.
   - Do not build the first-screen sequence, generate prompts, or generate images before the user confirms steps 4-5.
7. Build the first-screen concept before detail pages.
   - The first screen must show the product clearly and give the primary purchase reason.
   - Use visible product evidence, not decorative claims, to explain why the buyer should keep looking.
8. Turn the visible window into picture evidence.
   - Use close-ups, interface/readout details, attachment views, before-use setup, scale references, or package contents when they are visible and confirmed.
   - Each evidence element must map back to a source image or confirmed product fact.
9. Generate one detail page per buyer concern.
   - Each page should answer one concern with one visual proof idea and one concise message.
   - Avoid mixing unrelated claims on the same screen.
10. Make parameter pages into purchase snippets.
   - Do not present specs as a static table only.
   - Pair key parameters with buyer meaning, such as size for storage, power/modes for use scenarios, package weight for delivery expectations, or included items for readiness to use.
11. Use menu/comparison pages to lower choice cost.
   - Show color, kit, mode, accessory, or usage choices only when those choices are confirmed for the SKU.
   - Make the recommended choice or SKU scope clear so the buyer does not confuse variants.
12. End with a final screen that ties the image set together.
   - Summarize the buying logic across the group: product identity, main reason, key evidence, included items, and next action.
   - Do not add new unsupported claims on the final screen.
13. Send the screen sequence and prompt package to the user for confirmation before any image generation.
   - If the user chooses to generate images on another platform, stop here and wait for externally generated files.
   - If images are generated externally, put them in the SKU batch `external_inbox`.
   - If Codex is asked to generate images directly, do so only after the user approves the prompts.
14. Inspect images for color, claims, text, and Russian copy before promotion.
15. Upload only approved images to WB.
16. After upload, read back photo count/order and check error-card list.

Reject images with unconfirmed colors, invented claims, unsupported certificates, wrong accessories, or unreadable/localized text.
