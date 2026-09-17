from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from wb_manager.finance_receipts import ReceiptImportError, import_receipt_manifest
from wb_manager.finance_service import FinanceService, FinanceServiceError


def _registry(path):
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "status": "active",
                "currency": "CNY",
                "governance": {
                    "appendOnly": True,
                    "storeSpecific": True,
                    "neverInferFromBalanceDelta": True,
                    "correctionsRequireNewEvent": True,
                },
                "events": [],
                "receiptCoverage": [],
            }
        ),
        encoding="utf-8",
    )


def _manifest(path, *, amount="100.00", store="owner-store"):
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "currency": "CNY",
                "events": [
                    {
                        "eventId": "portal-payment-1",
                        "store": store,
                        "eventType": "account_receipt",
                        "amountCny": amount,
                        "receivedAt": "2026-09-01T09:00:00+03:00",
                        "status": "readback_confirmed",
                        "sourceType": "wb_settlement_account_credit_history",
                        "evidenceReference": "evidence/payment-1",
                    }
                ],
                "receiptCoverage": [
                    {
                        "coverageId": "portal-owner-august",
                        "store": store,
                        "from": "2026-08-01",
                        "through": "2026-09-01",
                        "complete": True,
                        "evidence": "complete portal export range",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_receipt_import_is_append_only_and_idempotent(tmp_path):
    registry = tmp_path / "cash.json"
    manifest = tmp_path / "receipts.json"
    _registry(registry)
    _manifest(manifest)

    kwargs = {
        "enabled_stores": ["owner-store"],
        "store_openings": {"owner-store": "2026-07-15"},
    }
    first = import_receipt_manifest(registry, manifest, **kwargs)
    second = import_receipt_manifest(registry, manifest, **kwargs)
    saved = json.loads(registry.read_text(encoding="utf-8"))

    assert first["status"] == "IMPORTED"
    assert first["insertedEvents"] == first["insertedCoverage"] == 1
    assert second["status"] == "NOOP"
    assert second["unchangedEvents"] == second["unchangedCoverage"] == 1
    assert saved["events"][0]["amountCny"] == "100.00"
    assert saved["receiptCoverage"][0]["from"] == "2026-08-01"


def test_receipt_import_rejects_conflicting_identity_without_rewriting(tmp_path):
    registry = tmp_path / "cash.json"
    manifest = tmp_path / "receipts.json"
    _registry(registry)
    _manifest(manifest)
    kwargs = {
        "enabled_stores": ["owner-store"],
        "store_openings": {"owner-store": "2026-07-15"},
    }
    import_receipt_manifest(registry, manifest, **kwargs)
    before = registry.read_bytes()
    _manifest(manifest, amount="101.00")

    with pytest.raises(ReceiptImportError, match="Conflicting receipt event"):
        import_receipt_manifest(registry, manifest, **kwargs)

    assert registry.read_bytes() == before


def test_receipt_import_never_writes_an_example_registry(tmp_path):
    registry = tmp_path / "historical_cash_events.example.json"
    manifest = tmp_path / "receipts.json"
    _registry(registry)
    _manifest(manifest)
    before = registry.read_bytes()

    with pytest.raises(ReceiptImportError, match="example registry"):
        import_receipt_manifest(
            registry,
            manifest,
            enabled_stores=["owner-store"],
            store_openings={"owner-store": "2026-07-15"},
        )
    assert registry.read_bytes() == before


def test_service_rejects_configured_fallback_even_without_example_filename(tmp_path):
    registry = tmp_path / "config" / "finance" / "default_cash_registry.json"
    registry.parent.mkdir(parents=True)
    _registry(registry)
    before = registry.read_bytes()
    service = SimpleNamespace(
        repo_root=tmp_path,
        ledger=SimpleNamespace(path=tmp_path / "data" / "finance" / "wb_finance.sqlite3"),
        periodic_standard=SimpleNamespace(
            historical_cash_events_registry=registry,
            raw={"historicalCashEventsRegistry": "config/finance/default_cash_registry.json"},
        ),
        _resolve_path=lambda path: tmp_path / path,
    )

    with pytest.raises(FinanceServiceError, match="private local cash-event registry"):
        FinanceService.receipt_import(service, manifest_path=tmp_path / "missing.json")
    assert registry.read_bytes() == before


def test_receipt_import_rejects_duplicate_legacy_receipt(tmp_path):
    registry = tmp_path / "cash.json"
    manifest = tmp_path / "receipts.json"
    _registry(registry)
    _manifest(manifest)

    with pytest.raises(ReceiptImportError, match="legacy owner decisions"):
        import_receipt_manifest(
            registry,
            manifest,
            enabled_stores=["owner-store"],
            store_openings={"owner-store": "2026-07-15"},
            legacy_receipts=[{
                "store": "owner-store", "eventId": "portal-payment-1",
                "date": "2026-09-01", "amountCny": "100.00",
                "evidence": "previous owner decision",
            }],
        )
    assert json.loads(registry.read_text(encoding="utf-8"))["events"] == []


def test_receipt_import_rejects_inferred_source_type(tmp_path):
    registry = tmp_path / "cash.json"
    manifest = tmp_path / "receipts.json"
    _registry(registry)
    _manifest(manifest)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["events"][0]["sourceType"] = "balance_delta"
    manifest.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ReceiptImportError, match="unsupported sourceType"):
        import_receipt_manifest(
            registry,
            manifest,
            enabled_stores=["owner-store"],
            store_openings={"owner-store": "2026-07-15"},
        )


@pytest.mark.parametrize(
    ("amount", "store", "message"),
    [
        ("-1", "owner-store", "finite and positive"),
        ("100.005", "owner-store", "fractional cents"),
        ("100", "disabled-store", "not enabled"),
    ],
)
def test_receipt_import_rejects_unsafe_facts(tmp_path, amount, store, message):
    registry = tmp_path / "cash.json"
    manifest = tmp_path / "receipts.json"
    _registry(registry)
    _manifest(manifest, amount=amount, store=store)

    with pytest.raises(ReceiptImportError, match=message):
        import_receipt_manifest(
            registry,
            manifest,
            enabled_stores=["owner-store"],
            store_openings={"owner-store": "2026-07-15"},
        )
