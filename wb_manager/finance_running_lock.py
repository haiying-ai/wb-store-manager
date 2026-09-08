"""Non-blocking cross-process guard for one complete running-account snapshot.

The fixed lock file is coordination only, not a financial fact. Never unlink
it: replacing its inode would let another process lock a different file.
The operating system releases the lock when its descriptor/process closes.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path


class RunningAccountLockError(ValueError):
    """A consistent running-account snapshot is not currently available."""


@contextmanager
def running_account_snapshot_lock(database: Path):
    database = Path(database).resolve()
    lock_path = database.with_name(database.name + ".snapshot.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunningAccountLockError(
                "Running-account snapshot is busy or unavailable; retry after "
                "the active refresh or funding read completes"
            ) from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def serialized_running_account(function=None, *, writer=False):
    """Use the same database identity for both full refresh and funding reads."""
    if function is None:
        return lambda target: serialized_running_account(target, writer=writer)
    @wraps(function)
    def guarded(service, *args, **kwargs):
        # Import at call time because the adapter also uses this decorator.
        from .finance_running_adapter import _settings
        database = Path(_settings(service)["db"]).resolve()
        marker = database.with_name(database.name + ".refresh-in-progress")
        with running_account_snapshot_lock(database):
            if writer:
                # Persist before any event writes. A failed/crashed refresh
                # leaves this marker; only a successful replay clears it.
                with marker.open("wb") as stream:
                    stream.write(b"Running-account refresh has not completed.\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            elif marker.exists():
                raise RunningAccountLockError(
                    "Running-account snapshot has an incomplete refresh; "
                    "successfully replay account refresh before funding"
                )
            result = function(service, *args, **kwargs)
            if writer:
                marker.unlink()
            return result
    return guarded
