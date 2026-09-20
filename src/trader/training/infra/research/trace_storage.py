"""SQLite storage primitives for the research trace archive."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
CREATE TABLE IF NOT EXISTS committed_events (
    decision_version TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    decision_hash TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS committed_events_trade_date
ON committed_events(trade_date DESC, strategy, observed_at DESC);
CREATE TABLE IF NOT EXISTS committed_event_quarantine (
    decision_version TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload BLOB NOT NULL
);
"""


@dataclass(frozen=True)
class ArchiveSummary:
    retained: int
    retained_bytes: int
    trade_dates: frozenset[date]
    legacy_retained: int


def partition_date(path: Path) -> date | None:
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None


@contextmanager
def connection(database: Path, *, write: bool) -> Iterator[sqlite3.Connection]:
    target = str(database) if write else f"file:{database.resolve()}?mode=ro"
    conn = sqlite3.connect(target, timeout=5.0, uri=not write)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize_database(database: Path, initialized: set[Path]) -> None:
    if database in initialized:
        return
    database.parent.mkdir(parents=True, exist_ok=True)
    with connection(database, write=True) as conn:
        conn.executescript(SCHEMA)
    initialized.add(database)


def quarantine_row(conn: sqlite3.Connection, decision_version: str, reason: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO committed_event_quarantine (
            decision_version, reason, payload_hash, payload
        )
        SELECT decision_version, ?, payload_hash, payload
        FROM committed_events WHERE decision_version = ?
        """,
        (reason, decision_version),
    )
    conn.execute("DELETE FROM committed_events WHERE decision_version = ?", (decision_version,))


__all__ = ["ArchiveSummary", "SCHEMA", "connection", "initialize_database", "partition_date", "quarantine_row"]
