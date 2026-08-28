from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from .finance_ledger import DEFAULT_FINANCE_DB, FinanceLedger


DEFAULT_FUNDS_LOCK_TTL_SECONDS = 1800


class FundsOperationLockedError(RuntimeError):
    """Raised when another store funding operation owns the shared lock."""


@dataclass
class StoreFundsLock:
    ledger: FinanceLedger
    lock_name: str
    owner_id: str
    ttl_seconds: int
    _released: bool = False

    def release(self) -> bool:
        if self._released:
            return False
        try:
            return self.ledger.release_lock(self.lock_name, self.owner_id)
        finally:
            self._released = True
            self.ledger.close()

    def __enter__(self) -> StoreFundsLock:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def acquire_store_funds_lock(
    store_id: str,
    *,
    db_path: Path | str = DEFAULT_FINANCE_DB,
    owner_id: str | None = None,
    ttl_seconds: int = DEFAULT_FUNDS_LOCK_TTL_SECONDS,
) -> StoreFundsLock:
    """Acquire the store funding lock shared by finance and advertising tasks."""

    normalized_store = str(store_id).strip()
    if not normalized_store:
        raise ValueError("store_id is required")
    ledger = FinanceLedger(db_path)
    lock_name = f"wb-advertising-funds:{normalized_store}"
    owner = owner_id or f"advertising-monitor-{uuid.uuid4()}"
    if not ledger.acquire_lock(
        lock_name,
        owner,
        ttl_seconds=ttl_seconds,
    ):
        ledger.close()
        raise FundsOperationLockedError(
            f"Another finance/advertising funding operation holds {lock_name}"
        )
    return StoreFundsLock(
        ledger=ledger,
        lock_name=lock_name,
        owner_id=owner,
        ttl_seconds=ttl_seconds,
    )


__all__ = [
    "DEFAULT_FUNDS_LOCK_TTL_SECONDS",
    "FundsOperationLockedError",
    "StoreFundsLock",
    "acquire_store_funds_lock",
]
