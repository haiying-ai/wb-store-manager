# WB Listing Safety

Default all listing operations to dry-run.

## Live Write Rules

- Live writes require an explicit user request.
- Stock writes require extra care because they can activate sales.
- Use tiny initial stock amounts for first activation tests.
- Save every live request, response, and readback.
- After a live write is read back successfully, promptly sync the confirmed platform state into the category `03` online-materials folder. Do not leave WB identifiers, mapping files, price/media/stock readbacks, or gate summaries only in the `02` generated-materials folder.
- Stop on validation errors. Do not blindly retry writes.

## Before Card Publish

- Product detail review has no missing required fields.
- Pricing review is ready and passes the margin gate.
- Listing manifest exists and matches the SKU.
- Confirmed images exist.
- WB subject/category is present.
- Package dimensions and gross weight are present.
- Price is not below the minimum acceptable price.

## Variant Safety

- Separate sellable colors or kits need separate vendorCode/article, barcode, and stock amount.
- Do not share one barcode across colors when inventory must be managed independently.
- If the user manually edited or created a WB card, read live WB identifiers first and continue from missing gates instead of recreating the card.

## Warehouse And Stock Safety

- Do not write stock until a seller warehouse is visible from `GET /api/v3/warehouses`.
- After `PUT /api/v3/stocks/{warehouseId}`, always read back with `POST /api/v3/stocks/{warehouseId}`.
- Require the target barcode to return `amount > 0`.
- If WB returns `CargoWarehouseRestrictionMGT`, stop using that warehouse and select/create a compatible MGT warehouse without ODC/CD+ restrictions.

## Sale-Readiness Requires

- Card exists.
- No error-card entry.
- Images are present and ordered.
- Dimensions and gross package weight are valid.
- Price/discount readback is correct.
- Warehouse exists and is active.
- Stock readback is positive.
- Portal/front-end propagation has been checked or marked pending.
- The latest confirmed WB live state has been synced into the category `03` online-materials folder for future image, price, parameter, and stock operations.
