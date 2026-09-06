from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from wb_manager.finance_service import FinanceService


def _dynamic_registry(path: Path) -> Path:
    data = json.loads(Path("config/finance/stores.json").read_text(encoding="utf-8"))
    data["fundingActivationRule"] = {
        "mode": "first_completed_or_return_event",
        "unresolvedBehavior": "PENDING_DATA",
        "noPriorAdvertisingRecharge": True,
    }
    data.pop("fundingActivationAt", None)
    data["stores"] = [data["stores"][0]]
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_dynamic_funding_activation_is_pending_then_earliest_platform_event(tmp_path):
    registry = _dynamic_registry(tmp_path / "stores.json")
    service = FinanceService(
        db_path=tmp_path / "finance.sqlite3",
        registry_path=registry,
        repo_root=Path.cwd(),
        now=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    try:
        pending = service._funding_activation_info("owner-store")
        assert pending["status"] == "PENDING_DATA"

        service.ledger.upsert_return(
            "owner-store",
            {"returnId": "return-1"},
            {
                "returnId": "return-1",
                "returnDate": "2026-08-03T10:00:00+03:00",
                "docTypeName": "退货",
            },
            occurred_at="2026-08-03T10:00:00+03:00",
        )
        service.ledger.upsert_finance(
            "owner-store",
            {"rrdId": "sale-1"},
            {
                "rrdId": "sale-1",
                "docTypeName": "销售",
                "retailPriceWithDisc": "100",
                "saleDt": "2026-08-05T10:00:00+03:00",
            },
            occurred_at="2026-08-05T10:00:00+03:00",
        )
        resolved = service._funding_activation_info("owner-store", required=True)
        assert resolved["status"] == "RESOLVED"
        assert resolved["source"].startswith("raw_returns:")
        assert resolved["atIso"].startswith("2026-08-03T10:00:00")
    finally:
        service.close()
