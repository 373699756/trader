"""Thread-owned SQLite connections and explicit, bounded transaction lifetimes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class SqliteConnectionOptions:
    read_only: bool = False
    timeout_seconds: float = 5.0
    journal_mode: Literal["WAL", "DELETE"] | None = None
    foreign_keys: bool = False
    immutable: bool = False
    query_only: bool | None = None
    check_same_thread: bool = True
    isolation_level: Literal["DEFERRED", "IMMEDIATE", "EXCLUSIVE"] | None = "DEFERRED"

    def __post_init__(self) -> None:
        if (
            not isfinite(self.timeout_seconds)
            or self.timeout_seconds < 0
            or self.read_only
            and self.journal_mode is not None
            or self.immutable
            and not self.read_only
        ):
            raise ValueError("invalid_sqlite_connection_options")


_DEFAULT_OPTIONS = SqliteConnectionOptions()


def OpenSqliteConnection(
    path: Path | Literal[":memory:"],
    options: SqliteConnectionOptions = _DEFAULT_OPTIONS,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> sqlite3.Connection:
    """Caller closes the handle; immutable is only for offline, unchanging copies."""
    connection = sqlite3.connect(
        _connection_target(path, options),
        uri=options.read_only,
        timeout=options.timeout_seconds,
        check_same_thread=options.check_same_thread,
        isolation_level=options.isolation_level,
    )
    try:
        query_only = options.query_only if options.query_only is not None else options.read_only
        if query_only:
            connection.execute("PRAGMA query_only=ON")
        if not options.read_only:
            if options.journal_mode is not None:
                actual = connection.execute(f"PRAGMA journal_mode={options.journal_mode}").fetchone()[0]
                if actual.upper() != options.journal_mode:
                    raise sqlite3.OperationalError("sqlite_journal_mode_unavailable")
            connection.execute("PRAGMA synchronous=FULL")
        if options.foreign_keys:
            connection.execute("PRAGMA foreign_keys=ON")
        if cancel_requested is not None:
            connection.set_progress_handler(lambda: int(cancel_requested()), 10_000)
        return connection
    except BaseException:
        connection.close()
        raise


def _connection_target(path: Path | Literal[":memory:"], options: SqliteConnectionOptions) -> Path | str:
    if path == ":memory:":
        if options.read_only:
            raise ValueError("sqlite_memory_database_cannot_be_read_only")
        return path
    elif options.read_only:
        return f"{path.resolve().as_uri()}?mode=ro" + ("&immutable=1" if options.immutable else "")
    else:
        return path


@contextmanager
def SqliteTransaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """Commit a complete unit or roll it back, including cancellation; no nested transactions."""
    if connection.in_transaction:
        raise ValueError("sqlite_nested_transaction")
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    with connection:
        yield connection


__all__ = ["OpenSqliteConnection", "SqliteConnectionOptions", "SqliteTransaction"]
