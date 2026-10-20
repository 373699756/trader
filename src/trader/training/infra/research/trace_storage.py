"""SQLite storage primitives for the research trace archive."""

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
    None,
    (
        """
        CREATE TABLE IF NOT EXISTS committed_events (
            decision_version TEXT PRIMARY KEY,
            strategy TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            decision_hash TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload BLOB NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS committed_events_trade_date
        ON committed_events(trade_date DESC, strategy, observed_at DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS committed_event_quarantine (
            decision_version TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload BLOB NOT NULL
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


def QuarantineRow(conn: SqliteConnection, decision_version: str, reason: str) -> None:
    conn.run(
        """
        INSERT OR REPLACE INTO committed_event_quarantine (
            decision_version, reason, payload_hash, payload
        )
        SELECT decision_version, ?, payload_hash, payload
        FROM committed_events WHERE decision_version = ?
        """,
        (reason, decision_version),
    )
    conn.run("DELETE FROM committed_events WHERE decision_version = ?", (decision_version,))


__all__ = ["TraceIndexSummary", "SCHEMA", "InitializeDatabase", "PartitionDate", "QuarantineRow"]
