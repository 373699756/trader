"""Thread-owned SQLite connections and explicit, bounded transaction lifetimes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class SqliteConnectionOptions:
    read_only: bool = False
    timeout_seconds: float = 5.0
    journal_mode: Literal["WAL", "DELETE"] | None = None
    foreign_keys: bool = False

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.read_only and self.journal_mode is not None:
            raise ValueError("invalid_sqlite_connection_options")


def OpenSqliteConnection(
    path: Path,
    options: SqliteConnectionOptions = SqliteConnectionOptions(),
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> sqlite3.Connection:
    """Caller owns and closes the connection; never create directories or retry writes."""
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro" if options.read_only else path,
        uri=options.read_only,
        timeout=options.timeout_seconds,
    )
    try:
        if options.read_only:
            connection.execute("PRAGMA query_only=ON")
        else:
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


@contextmanager
def SqliteTransaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """Commit a complete unit or roll it back, including cancellation; no nested transactions."""
    if connection.in_transaction:
        raise ValueError("sqlite_nested_transaction")
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    with connection:
        yield connection


__all__ = ["OpenSqliteConnection", "SqliteConnectionOptions", "SqliteTransaction"]
