# Phone Image Audit, Packaging, Upload, And Verification

Use this runbook after phone images have passed the creative approval gate in `phone-image-playbook.md`. It turns an approved model/SKU image catalog into traceable WB media assignments.

## Required Tool Chain

Run the stages in this order:

1. `python tools/audit_phone_images.py`
   - Reconcile every SKU main image and every model shared page against the tracker.
   - Verify that files are readable, record dimensions, byte size, SHA-256, missing files, and exact duplicate groups.
   - Generate compact contact sheets for visual review.
   - Do not continue while the audit reports missing, unreadable, wrongly mapped, rejected, or unexplained duplicate assets.

2. `python tools/build_phone_image_upload_packages.py`
   - Resolve each seller SKU to one unique live WB `nmID`.
   - Build one deterministic package per SKU: `01_main.png` followed by the approved shared model pages.
   - Validate WB limits before creating the manifest: supported raster file, no more than 32 MB, and at least 700 x 900 pixels.
   - Require the expected package count, exact images-per-SKU count, unique `nmID` values, and zero missing mappings.

3. `python tools/upload_phone_images.py` first as a dry run, then with `--live` only after explicit approval.
   - The dry run must print the intended SKU and image-assignment totals without writing to WB.
   - Live upload uses `X-Nm-Id` and `X-Photo-Number`; preserve the manifest order.
   - Save a result after every image in `upload_state.json`. Resume by SKU, photo number, and SHA-256 so an interrupted batch skips already verified uploads.
   - Retry rate limits, TLS failures, timeouts, remote disconnects, connection resets, aborted connections, and broken pipes with bounded backoff.
   - Stop on a persistent item failure. Fix the cause and resume from state; do not restart completed images or run competing upload processes against the same state file.

4. `python tools/readback_phone_cards_and_map.py`
   - Read live cards and the error-card list after WB has had time to persist media.
   - Verify every requested SKU maps to exactly one `nmID`, the error list has no matching card, and live photo count matches the package manifest.
   - Inspect order when the main image or page sequence changed; photo count alone cannot prove order.
   - Mark tracker rows `UPLOADED` only after successful live readback.

## Completion Contract

Do not report a batch complete until all of the following agree:

- tracker row count and expected SKU count;
- package directory count and batch manifest SKU count;
- package image assignments and successful upload-state records;
- unique live `nmID` count;
- live card photo count/order and zero matching error cards;
- final tracker status `UPLOADED` for every SKU.

Read the expected SKU count and images-per-SKU from the local batch manifest. Treat those values as batch-specific expectations, not universal defaults.

## Operational Records

Keep these artifacts together and sync the verified copy into `E:\WB\手机\03上线资料`:

- image audit report and contact-sheet location;
- batch upload manifest and per-SKU manifests;
- resumable upload state and concise uploader logs;
- refreshed SKU-to-WB mapping;
- WB card/error readback summary;
- final verification summary and tracker.

Never expose the API token in commands, logs, manifests, or reports. Keep local/generated state separate from confirmed live state until readback passes.

After media verification, use `tools/audit_phone_sale_readiness.py` to identify remaining content, price, dimension, warehouse, and stock gates. Fill only characteristics backed by the approved per-model specification table; never infer region-dependent SIM, network, NFC, OS-version, or warranty values.

## Recovery Rules

- If generation is incomplete, return to the first missing model/page in the tracker.
- If one image is corrupt, restore or regenerate only that deterministic path, rerun the audit, rebuild affected packages, and let SHA-256 invalidate only changed upload-state entries.
- If the uploader stops, inspect stderr and the last successful state entry, add a bounded retry only for a recognized transient network class, and resume.
- If WB readback is short, wait for propagation and read again. If still short, re-upload only missing/mismatched positions.
- If mapping is ambiguous or duplicate, stop before any upload and repair the mapping source.
