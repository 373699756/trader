from __future__ import annotations

import sqlite3
from contextlib import contextmanager


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_BUSY_TIMEOUT_MS = 30_000


def open_sqlite(
    db_path: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    row_factory=None,
) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=timeout)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = {}".format(DEFAULT_BUSY_TIMEOUT_MS))
    connection.execute("PRAGMA journal_mode = WAL")
    if row_factory is not None:
        connection.row_factory = row_factory
    return connection


@contextmanager
def sqlite_transaction(db_path: str, *, row_factory=None):
    connection = open_sqlite(db_path, row_factory=row_factory)
    try:
        with connection:
            yield connection
    finally:
        connection.close()
