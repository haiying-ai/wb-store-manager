# WB Live-State Sync

Whenever WB platform data has been written live and confirmed by readback, sync the confirmed state into the category `03` online-materials folder promptly.

This applies to:

- content-card publish or discovery results;
- SKU/vendorCode/barcode to `nmID`/`imtID`/`chrtID` mappings;
- media upload requests, responses, readbacks, and photo counts;
- price and discount upload requests, task IDs, quarantine checks, readbacks, and validations;
- warehouse and stock requests, responses, and readbacks;
- error-card checks and remaining sale-readiness blockers.

The `02` generated-materials folder is a working area. It can keep drafts, batches, and raw operation outputs, but the durable state for future operations must be available from `03`.

For batch operations, save both:

- a batch-level mapping/status file; and
- per-SKU or per-variant live-state records when later operations need stable identifiers.

Before starting follow-up work such as image upload, price refresh, parameter update, or stock write, prefer the latest synced `03` live-state files over older generated drafts.
