from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .barcode import generate_and_attach_barcode
from .client import WbApiError, WbClient
from .config import ConfigError, load_settings
from .image_workflow import create_image_prompts, create_main_image_workspace, inspect_external_inbox
from .kb import sync_knowledge_base
from .listing import publish_listing, validate_listing
from .ops import write_daily_report
from .paths import category_paths, ensure_category_dirs
from .pricing import create_pricing_review
from .product import create_product_review
from .reporting import build_summary
from .services import full_snapshot, ping_all
from .sku_alias import canonical_sku
from .storage import latest_snapshot, read_json, write_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wbm", description="Wildberries store management toolkit")
    parser.add_argument("--business-root", default="E:/WB", help="Business data root, default: E:/WB")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="Check API domain connectivity")
    snap = sub.add_parser("snapshot", help="Collect a read-only store snapshot")
    snap.add_argument("--json", action="store_true", help="Print the full snapshot JSON")

    summary = sub.add_parser("summary", help="Print a summary for the latest snapshot")
    summary.add_argument("--file", help="Snapshot file to summarize")

    kb = sub.add_parser("kb", help="Knowledge base commands")
    kb_sub = kb.add_subparsers(dest="kb_command", required=True)
    kb_sync = kb_sub.add_parser("sync", help="Sync category source materials into the local knowledge base")
    _add_category(kb_sync)

    product = sub.add_parser("product", help="Product detail review commands")
    product_sub = product.add_subparsers(dest="product_command", required=True)
    product_review = product_sub.add_parser("review", help="Create a product detail confirmation package")
    _add_category(product_review)
    _add_sku(product_review)

    pricing = sub.add_parser("pricing", help="Pricing and profitability commands")
    pricing_sub = pricing.add_subparsers(dest="pricing_command", required=True)
    pricing_review = pricing_sub.add_parser("review", help="Create a pricing gate review")
    _add_category(pricing_review)
    _add_sku(pricing_review)
    pricing_review.add_argument("--input-file", help="JSON file containing pricing inputs")

    barcode = sub.add_parser("barcode", help="WB official barcode commands")
    barcode_sub = barcode.add_subparsers(dest="barcode_command", required=True)
    barcode_generate = barcode_sub.add_parser("generate", help="Generate one official WB barcode and attach it to a SKU")
    _add_category(barcode_generate)
    _add_sku(barcode_generate)

    image = sub.add_parser("image", help="Image workflow commands")
    image_sub = image.add_subparsers(dest="image_command", required=True)
    image_main = image_sub.add_parser("main", help="Create main-image workspace")
    _add_category(image_main)
    _add_sku(image_main)
    image_prompts = image_sub.add_parser("prompts", help="Create post-main-image prompt package")
    _add_category(image_prompts)
    _add_sku(image_prompts)
    image_inbox = image_sub.add_parser("inspect-inbox", help="Inspect externally generated images")
    _add_category(image_inbox)
    _add_sku(image_inbox)
    image_inbox.add_argument("--batch", help="Specific generated batch folder, e.g. 20260705-144821")

    listing = sub.add_parser("listing", help="Listing validation and publish commands")
    listing_sub = listing.add_subparsers(dest="listing_command", required=True)
    listing_validate = listing_sub.add_parser("validate", help="Validate confirmed online materials")
    _add_category(listing_validate)
    _add_sku(listing_validate)
    listing_publish = listing_sub.add_parser("publish", help="Publish or dry-run a listing")
    _add_category(listing_publish)
    _add_sku(listing_publish)
    listing_publish.add_argument("--dry-run", action="store_true", help="Force dry-run mode")
    listing_publish.add_argument("--confirm-live", action="store_true", help="Required for live write calls")

    ops = sub.add_parser("ops", help="Operations reporting commands")
    ops_sub = ops.add_subparsers(dest="ops_command", required=True)
    ops_daily = ops_sub.add_parser("daily", help="Generate a daily operations report")
    _add_category(ops_daily)
    ops_daily.add_argument("--date", default="today", help="YYYY-MM-DD or today")
    ops_daily.add_argument("--snapshot-file", help="Optional snapshot JSON to summarize")

    args = parser.parse_args(argv)

    try:
        return _dispatch(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except WbApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "summary":
        path = latest_snapshot() if not args.file else Path(args.file)
        if path is None:
            print("No snapshot found. Run: python -m wb_manager snapshot", file=sys.stderr)
            return 2
        print(build_summary(read_json(path)))
        return 0

    if args.command in {"ping", "snapshot"}:
        settings = load_settings()
        client = WbClient(settings.token, verify_tls=settings.verify_tls)
        if args.command == "ping":
            print(json.dumps(ping_all(client), ensure_ascii=False, indent=2))
            return 0
        snapshot = full_snapshot(client)
        path = write_snapshot(snapshot)
        print(f"Wrote {path}")
        print(build_summary(snapshot))
        if args.json:
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0

    if args.command == "kb" and args.kb_command == "sync":
        paths = category_paths(args.category, business_root=args.business_root)
        ensure_category_dirs(paths)
        result = sync_knowledge_base(paths)
        print(json.dumps({"kb": str(paths.kb), "changes": result.changes}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "product" and args.product_command == "review":
        paths = category_paths(args.category, business_root=args.business_root)
        batch = create_product_review(paths, args.sku)
        print(f"Wrote product review package: {batch}")
        return 0

    if args.command == "pricing" and args.pricing_command == "review":
        paths = category_paths(args.category, business_root=args.business_root)
        out = create_pricing_review(paths, args.sku, input_file=Path(args.input_file) if args.input_file else None)
        print(f"Wrote pricing review: {out}")
        return 0

    if args.command == "barcode" and args.barcode_command == "generate":
        settings = load_settings()
        client = WbClient(settings.token, verify_tls=settings.verify_tls)
        paths = category_paths(args.category, business_root=args.business_root)
        print(json.dumps(generate_and_attach_barcode(client, paths, args.sku), ensure_ascii=False, indent=2))
        return 0

    if args.command == "image":
        paths = category_paths(args.category, business_root=args.business_root)
        if args.image_command == "main":
            batch = create_main_image_workspace(paths, args.sku)
        elif args.image_command == "prompts":
            batch = create_image_prompts(paths, args.sku)
        else:
            selected_batch = _generated_batch_path(paths, args.sku, args.batch) if args.batch else None
            batch = inspect_external_inbox(paths, args.sku, batch=selected_batch)
        print(f"Wrote image workflow batch: {batch}")
        return 0

    if args.command == "listing":
        paths = category_paths(args.category, business_root=args.business_root)
        if args.listing_command == "validate":
            print(json.dumps(validate_listing(paths, args.sku), ensure_ascii=False, indent=2))
            return 0
        settings = None
        client = None
        dry_run = args.dry_run or not args.confirm_live
        if args.confirm_live and not dry_run:
            settings = load_settings()
            client = WbClient(settings.token, verify_tls=settings.verify_tls)
        print(json.dumps(publish_listing(paths, args.sku, dry_run=dry_run, confirm_live=args.confirm_live, client=client), ensure_ascii=False, indent=2))
        return 0

    if args.command == "ops" and args.ops_command == "daily":
        paths = category_paths(args.category, business_root=args.business_root)
        day = date.today() if args.date == "today" else date.fromisoformat(args.date)
        snapshot = read_json(Path(args.snapshot_file)) if args.snapshot_file else None
        out = write_daily_report(paths, day=day, snapshot=snapshot)
        print(f"Wrote daily report: {out}")
        return 0

    return 2


def _add_category(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--category", required=True, help="Business category folder name, e.g. electronics")


def _add_sku(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sku", required=True, help="SKU/model, e.g. EXAMPLE-001")


def _generated_batch_path(paths, sku: str, batch_name: str) -> Path:
    if not batch_name or any(sep in batch_name for sep in ("/", "\\")):
        raise ValueError("--batch must be a generated batch folder name, not a path")
    batch = paths.generated / canonical_sku(sku, category=paths.category) / batch_name
    if not batch.exists():
        raise FileNotFoundError(f"Batch not found: {batch}")
    return batch


if __name__ == "__main__":
    raise SystemExit(main())
