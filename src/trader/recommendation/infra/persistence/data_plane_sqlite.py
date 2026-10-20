"""Own the snapshots database schema, including durable publication coordinates."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteSession,
)

SCHEMA_VERSION = 3
SCHEMA_META_KEY = "schema_version"


def InitializeDatabase(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSession(
        database_path, SqliteConnectionOptions(journal_mode="WAL", foreign_keys=True, timeout_seconds=8)
    ) as connection:
        EnsureSqliteSchema(connection, _SCHEMA, allow_create=True, initialize=_initialize, validate=_validate)


def _initialize(connection: SqliteConnection) -> None:
    connection.run("INSERT INTO schema_meta(key,value) VALUES (?,?)", (SCHEMA_META_KEY, str(SCHEMA_VERSION)))
    connection.run("INSERT INTO publication_sequence VALUES (1,0)")


def _validate(connection: SqliteConnection) -> None:
    row = connection.run("SELECT value FROM schema_meta WHERE key=?", (SCHEMA_META_KEY,)).fetchone()
    if row is None or row[0] != str(SCHEMA_VERSION):
        raise sqlite3.DatabaseError("sqlite_schema_unsupported")


__all__ = ["SCHEMA_VERSION", "SCHEMA_META_KEY", "InitializeDatabase"]

_SCHEMA = SqliteSchema(
    None,
    (
        """
        CREATE TABLE IF NOT EXISTS schema_meta(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS security_master_recent(
            code TEXT PRIMARY KEY,
            observed_at TEXT NOT NULL,
            source_time TEXT NOT NULL,
            source TEXT NOT NULL,
            data_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'committed',
            error TEXT NOT NULL DEFAULT '',
            recovery_payload BLOB,
            recovery_sha256 TEXT NOT NULL DEFAULT '',
            CHECK(status IN ('staged', 'committed', 'quarantined'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS security_master_formal(
            freeze_id TEXT NOT NULL,
            code TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            source_time TEXT NOT NULL,
            source TEXT NOT NULL,
            data_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'committed',
            error TEXT NOT NULL DEFAULT '',
            recovery_payload BLOB,
            recovery_sha256 TEXT NOT NULL DEFAULT '',
            CHECK(status IN ('staged', 'committed', 'quarantined')),
            PRIMARY KEY(freeze_id, code)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_cursor_recent(
            cursor_name TEXT PRIMARY KEY,
            observed_at TEXT NOT NULL,
            source_time TEXT NOT NULL,
            source TEXT NOT NULL,
            data_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload TEXT NOT NULL,
            cursor_value TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'committed',
            error TEXT NOT NULL DEFAULT '',
            recovery_payload BLOB,
            recovery_sha256 TEXT NOT NULL DEFAULT '',
            CHECK(status IN ('staged', 'committed', 'quarantined'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_cursor_formal(
            freeze_id TEXT NOT NULL,
            cursor_name TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            source_time TEXT NOT NULL,
            source TEXT NOT NULL,
            data_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload TEXT NOT NULL,
            cursor_value TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'committed',
            error TEXT NOT NULL DEFAULT '',
            recovery_payload BLOB,
            recovery_sha256 TEXT NOT NULL DEFAULT '',
            CHECK(status IN ('staged', 'committed', 'quarantined')),
            PRIMARY KEY(freeze_id, cursor_name)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS trading_calendar_recent(
            calendar_name TEXT PRIMARY KEY,
            observed_at TEXT NOT NULL,
            source_time TEXT NOT NULL,
            source TEXT NOT NULL,
            data_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'committed',
            error TEXT NOT NULL DEFAULT '',
            recovery_payload BLOB,
            recovery_sha256 TEXT NOT NULL DEFAULT '',
            CHECK(status IN ('staged', 'committed', 'quarantined'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS trading_calendar_formal(
            freeze_id TEXT NOT NULL,
            calendar_name TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            source_time TEXT NOT NULL,
            source TEXT NOT NULL,
            data_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'committed',
            error TEXT NOT NULL DEFAULT '',
            recovery_payload BLOB,
            recovery_sha256 TEXT NOT NULL DEFAULT '',
            CHECK(status IN ('staged', 'committed', 'quarantined')),
            PRIMARY KEY(freeze_id, calendar_name)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS data_plane_quarantine_audit(
            audit_key TEXT PRIMARY KEY,
            record_kind TEXT NOT NULL,
            record_identity TEXT NOT NULL,
            reason TEXT NOT NULL,
            observed_at TEXT NOT NULL
        )
        """,
        "CREATE TABLE IF NOT EXISTS publication_sequence (singleton INTEGER PRIMARY KEY CHECK(singleton=1), reserved_until INTEGER NOT NULL)",
    ),
)
