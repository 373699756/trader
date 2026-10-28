"""Recommendation-owned trace schema for immutable freeze observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteSession,
)

SCHEMA = SqliteSchema(
    1,
    (
        """
        CREATE TABLE committed_events (
            freeze_id TEXT NOT NULL PRIMARY KEY,
            strategy TEXT NOT NULL,
            trade_date INTEGER NOT NULL,
            observed_at INTEGER NOT NULL,
            freeze_hash BLOB NOT NULL CHECK(length(freeze_hash)=32),
            schema_version TEXT NOT NULL,
            event_hash BLOB NOT NULL CHECK(length(event_hash)=32),
            event_bytes BLOB NOT NULL
        )
        """,
        """
        CREATE INDEX committed_events_trade_date
        ON committed_events(trade_date DESC, strategy, observed_at DESC)
        """,
        """
        CREATE TABLE committed_event_quarantine (
            freeze_id TEXT NOT NULL PRIMARY KEY,
            reason TEXT NOT NULL,
            event_hash BLOB NOT NULL CHECK(length(event_hash)=32),
            event_bytes BLOB NOT NULL
        )
        """,
    ),
)


@dataclass(frozen=True)
class TraceIndexSummary:
    retained: int
    retained_bytes: int
    trade_dates: frozenset[date]
    legacy_retained: int


def PartitionDate(path: Path) -> date | None:
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None


def InitializeDatabase(database: Path, initialized: set[Path]) -> None:
    if database in initialized:
        return
    database.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSession(database, SqliteConnectionOptions(journal_mode="WAL")) as conn:
        EnsureSqliteSchema(conn, SCHEMA, allow_create=True)
    initialized.add(database)


def QuarantineRow(conn: SqliteConnection, freeze_id: str, reason: str) -> None:
    conn.run(
        """
        INSERT OR REPLACE INTO committed_event_quarantine (
            freeze_id, reason, event_hash, event_bytes
        )
        SELECT freeze_id, ?, event_hash, event_bytes
        FROM committed_events WHERE freeze_id = ?
        """,
        (reason, freeze_id),
    )
    conn.run("DELETE FROM committed_events WHERE freeze_id = ?", (freeze_id,))


__all__ = ["TraceIndexSummary", "SCHEMA", "InitializeDatabase", "PartitionDate", "QuarantineRow"]
