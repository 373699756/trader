"""Open read-only price connections with bounded retry for transient locks."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.infra.sqlite_connection import OpenSqliteConnection, SqliteConnection, SqliteConnectionOptions

# SQLite primary result codes are stable across supported Python versions.
_RETRYABLE_PRIMARY_CODES = frozenset({5, 6})  # SQLITE_BUSY, SQLITE_LOCKED
_LEGACY_LOCK_MESSAGES = frozenset({"database is locked", "database table is locked", "database schema is locked"})


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
    activity: HistorySqliteActivity | None,
) -> SqliteConnection:
    """Retry only SQLite lock contention; always close a failed connection."""
    for attempt in range(3):
        connection: SqliteConnection | None = None
        try:
            connection = OpenSqliteConnection(path, SqliteConnectionOptions(read_only=True, foreign_keys=True))
            if statement_trace is not None:
                connection.observe_statements(statement_trace)
            connection.run("PRAGMA cache_size=-8192")
            connection.run(f"PRAGMA mmap_size={mmap_bytes}")
            connection.run("PRAGMA temp_store=FILE")
            return connection
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            if not _retryable_lock(exc) or attempt == 2:
                raise
            if activity is not None:
                activity.tick()
            time.sleep(0.1 * (attempt + 1))
    raise AssertionError("unreachable month reader retry state")


__all__ = ["OpenHistoryPriceReader"]
