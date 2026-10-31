"""Open read-only price connections with bounded retry for transient locks."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.infra.sqlite_connection import OpenSqliteConnection, SqliteConnection, SqliteConnectionOptions

# SQLite primary result codes are stable across supported Python versions.
_RETRYABLE_PRIMARY_CODES = frozenset({5, 6})  # SQLITE_BUSY, SQLITE_LOCKED
_LEGACY_LOCK_MESSAGES = frozenset({"database is locked", "database table is locked", "database schema is locked"})


def PrefetchHistoryPricePages(path: Path, check: Callable[[], None] | None = None) -> None:
    """Read annual pages sequentially before many interleaved monthly B-tree walks.

    The operating system owns the evictable pages; Python retains only one MiB.
    This does not establish trust: logical proofs and payload checks still follow.
    """
    if check is not None:
        check()
    with path.open("rb", buffering=0) as source:
        remaining = os.fstat(source.fileno()).st_size
        buffer = bytearray(min(1024 * 1024, remaining))
        while remaining:
            if check is not None:
                check()
            count = source.readinto(memoryview(buffer)[:remaining])
            if not count:
                raise OSError("history_partition_truncated")
            remaining -= count


def _retryable_lock(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return code & 0xFF in _RETRYABLE_PRIMARY_CODES
    # Python 3.10 does not expose sqlite_errorcode on exceptions.
    return str(exc) in _LEGACY_LOCK_MESSAGES


def OpenHistoryPriceReader(
    path: Path,
    *,
    statement_trace: Callable[[str], None] | None,
    mmap_bytes: int,
    activity: HistorySqliteActivity | None, sqlite_policy: SqliteIoPolicy,
) -> SqliteConnection:
    """Retry only SQLite lock contention; always close a failed connection."""
    for attempt in range(3):
        try:
            return OpenSqliteConnection(
                path,
                SqliteConnectionOptions(
                    read_only=True, foreign_keys=True, cache_kib=8192, mmap_bytes=mmap_bytes, temp_storage="FILE"
                ),
                statement_trace=statement_trace, sqlite_policy=sqlite_policy,
            )
        except sqlite3.Error as exc:
            if not _retryable_lock(exc) or attempt == 2:
                raise
            if activity is not None:
                activity.tick()
            time.sleep(0.1 * (attempt + 1))
    raise AssertionError("unreachable month reader retry state")


@contextmanager
def WatchHistoryRead(connection: SqliteConnection, check: Callable[[], None] | None) -> Iterator[None]:
    """Interrupt SQLite work and propagate the caller's bounded-read failure."""
    if check is None:
        yield
        return
    failure: RuntimeError | None = None

    def progress() -> int:
        nonlocal failure
        try:
            check()
        except RuntimeError as exc:
            failure = exc
            return 1
        return 0

    check()
    connection.observe_progress(progress, 10_000)
    try:
        yield
        check()
    except sqlite3.OperationalError as exc:
        if failure is not None:
            raise failure from exc
        raise
    finally:
        connection.observe_progress(None, 0)


__all__ = ["OpenHistoryPriceReader", "PrefetchHistoryPricePages", "WatchHistoryRead"]
