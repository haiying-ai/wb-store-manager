from __future__ import annotations

from datetime import datetime, timezone

import pytest

from wb_manager.advertising_funds_lock import (
    DEFAULT_FUNDS_LOCK_TTL_SECONDS,
    FundsOperationLockedError,
    acquire_store_funds_lock,
)
from wb_manager.finance_ledger import FinanceLedger


LOCK_NAME = "wb-advertising-funds:new-store"


def test_advertising_monitor_uses_the_finance_store_funds_lock(tmp_path) -> None:
    db_path = tmp_path / "finance.sqlite3"
    lock = acquire_store_funds_lock(
        "new-store",
        db_path=db_path,
        owner_id="advertising-test",
    )
    competing_finance = FinanceLedger(db_path)
    try:
        assert lock.lock_name == LOCK_NAME
        assert DEFAULT_FUNDS_LOCK_TTL_SECONDS >= 300
        assert not competing_finance.acquire_lock(
            LOCK_NAME,
            "finance-test",
            ttl_seconds=300,
            now=datetime.now(timezone.utc),
        )
    finally:
        assert lock.release()

    try:
        assert competing_finance.acquire_lock(
            LOCK_NAME,
            "finance-after-release",
            ttl_seconds=300,
            now=datetime.now(timezone.utc),
        )
    finally:
        competing_finance.close()


def test_advertising_monitor_fails_closed_when_finance_holds_lock(tmp_path) -> None:
    db_path = tmp_path / "finance.sqlite3"
    finance = FinanceLedger(db_path)
    assert finance.acquire_lock(
        LOCK_NAME,
        "finance-live",
        ttl_seconds=300,
        now=datetime.now(timezone.utc),
    )
    try:
        with pytest.raises(
            FundsOperationLockedError,
            match="finance/advertising funding operation",
        ):
            acquire_store_funds_lock(
                "new-store",
                db_path=db_path,
                owner_id="advertising-blocked",
            )
    finally:
        finance.close()
