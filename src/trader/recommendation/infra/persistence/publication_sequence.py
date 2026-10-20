"""Durable reservation of publication coordinates, independent of payload contents."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from trader.infra.sqlite_connection import (
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)
from trader.recommendation.infra.persistence.data_plane_sqlite import InitializeDatabase


class PublicationSequence:
    """Reserve blocks before publishing; a crash may skip numbers, never reuse them."""

    def __init__(self, path: Path, *, block_size: int = 1_048_576) -> None:
        if block_size < 2:
            raise ValueError("publication sequence block must contain at least two numbers")
        self._path = path
        self._block_size = block_size
        self._lock = threading.Lock()
        self._next = 0
        self._limit = 0
        self._initialized = False

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            try:
                InitializeDatabase(self._path)
                self._reserve()
            except (OSError, sqlite3.Error) as exc:
                raise RuntimeError("publication_sequence_unavailable") from exc
            self._initialized = True

    def allocate(self, *, width: int = 1, minimum: int = 1) -> int:
        if not 1 <= width <= self._block_size:
            raise ValueError("publication sequence allocation width is invalid")
        if minimum < 1 or minimum > 2**63 - 1 - self._block_size:
            raise ValueError("publication sequence minimum is invalid")
        with self._lock:
            if not self._initialized:
                raise RuntimeError("publication_sequence_not_initialized")
            self._next = max(self._next, minimum)
            if self._next + width > self._limit:
                self._reserve(minimum=minimum)
            value = self._next
            self._next += width
            return value

    def _reserve(self, *, minimum: int = 1) -> None:
        try:
            with (
                SqliteSession(self._path, SqliteConnectionOptions(timeout_seconds=5.0)) as connection,
                SqliteTransaction(connection, immediate=True),
            ):
                row = connection.run("SELECT reserved_until FROM publication_sequence WHERE singleton=1").fetchone()
                if row is None or not isinstance(row[0], int) or row[0] < 0 or row[0] > 2**63 - 1 - self._block_size:
                    raise RuntimeError("publication_sequence_invalid")
                start = max(row[0] + 1, minimum)
                limit = start + self._block_size
                connection.run("UPDATE publication_sequence SET reserved_until=? WHERE singleton=1", (limit - 1,))
            self._next = start
            self._limit = limit
        except sqlite3.Error as exc:
            raise RuntimeError("publication_sequence_unavailable") from exc
