"""Durable reservation of publication coordinates, independent of payload contents."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


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
            self._path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with sqlite3.connect(self._path, timeout=5.0) as connection:
                    connection.execute("PRAGMA synchronous=FULL")
                    connection.execute("BEGIN IMMEDIATE")
                    exists = (
                        connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_sequence'"
                        ).fetchone()
                        is not None
                    )
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS publication_sequence "
                        "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), reserved_until INTEGER NOT NULL)"
                    )
                    if not exists:
                        connection.execute("INSERT INTO publication_sequence VALUES (1, 0)")
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
            with sqlite3.connect(self._path, timeout=5.0) as connection:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT reserved_until FROM publication_sequence WHERE singleton=1").fetchone()
                if row is None or not isinstance(row[0], int) or row[0] < 0 or row[0] > 2**63 - 1 - self._block_size:
                    raise RuntimeError("publication_sequence_invalid")
                start = max(row[0] + 1, minimum)
                limit = start + self._block_size
                connection.execute("UPDATE publication_sequence SET reserved_until=? WHERE singleton=1", (limit - 1,))
            self._next = start
            self._limit = limit
        except sqlite3.Error as exc:
            raise RuntimeError("publication_sequence_unavailable") from exc
