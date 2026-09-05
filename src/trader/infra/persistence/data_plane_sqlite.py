"""SQLite migration and schema helpers for unified data-plane persistence."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 3
SCHEMA_META_KEY = "schema_version"


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=10.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=8000")
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def connection_scope(database_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(database_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with connection_scope(database_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

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
            );

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
            );

            CREATE TABLE IF NOT EXISTS historical_feature_recent(
                code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
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
                PRIMARY KEY(code, trade_date)
            );

            CREATE TABLE IF NOT EXISTS historical_feature_formal(
                freeze_id TEXT NOT NULL,
                code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
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
                PRIMARY KEY(freeze_id, code, trade_date)
            );

            CREATE TABLE IF NOT EXISTS risk_evidence_recent(
                code TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
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
                PRIMARY KEY(code, evidence_id)
            );

            CREATE TABLE IF NOT EXISTS risk_evidence_formal(
                freeze_id TEXT NOT NULL,
                code TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
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
                PRIMARY KEY(freeze_id, code, evidence_id)
            );

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
            );

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
            );

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
            );

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
            );

            CREATE TABLE IF NOT EXISTS data_plane_quarantine_audit(
                audit_key TEXT PRIMARY KEY,
                record_kind TEXT NOT NULL,
                record_identity TEXT NOT NULL,
                reason TEXT NOT NULL,
                observed_at TEXT NOT NULL
            );
            """
        )
        apply_migrations(connection)
        if _current_schema_version(connection) < SCHEMA_VERSION:
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                (SCHEMA_META_KEY, str(SCHEMA_VERSION)),
            )
        if _current_schema_version(connection) == 0:
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                (SCHEMA_META_KEY, str(SCHEMA_VERSION)),
            )


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Apply migrations for all known schema versions in order."""
    current = _current_schema_version(connection)
    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        for statement in MIGRATIONS[version]:
            try:
                connection.execute(statement)
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if "duplicate column name" not in text and "no such table" not in text:
                    raise
        connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (SCHEMA_META_KEY, str(version)),
        )


def _current_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT value FROM schema_meta WHERE key = ?", (SCHEMA_META_KEY,)).fetchone()
    if row is None:
        return 0
    return _parse_schema_version(row[0])


def _parse_schema_version(raw: object) -> int:
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return 0


MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE security_master_recent ADD COLUMN status TEXT NOT NULL DEFAULT 'committed'",
        "ALTER TABLE security_master_recent ADD COLUMN error TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE security_master_recent ADD COLUMN recovery_payload BLOB",
        "ALTER TABLE security_master_recent ADD COLUMN recovery_sha256 TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE security_master_formal ADD COLUMN status TEXT NOT NULL DEFAULT 'committed'",
        "ALTER TABLE security_master_formal ADD COLUMN error TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE security_master_formal ADD COLUMN recovery_payload BLOB",
        "ALTER TABLE security_master_formal ADD COLUMN recovery_sha256 TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE historical_feature_recent ADD COLUMN status TEXT NOT NULL DEFAULT 'committed'",
        "ALTER TABLE historical_feature_recent ADD COLUMN error TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE historical_feature_recent ADD COLUMN recovery_payload BLOB",
        "ALTER TABLE historical_feature_recent ADD COLUMN recovery_sha256 TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE historical_feature_formal ADD COLUMN status TEXT NOT NULL DEFAULT 'committed'",
        "ALTER TABLE historical_feature_formal ADD COLUMN error TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE historical_feature_formal ADD COLUMN recovery_payload BLOB",
        "ALTER TABLE historical_feature_formal ADD COLUMN recovery_sha256 TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE risk_evidence_recent ADD COLUMN status TEXT NOT NULL DEFAULT 'committed'",
        "ALTER TABLE risk_evidence_recent ADD COLUMN error TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE risk_evidence_recent ADD COLUMN recovery_payload BLOB",
        "ALTER TABLE risk_evidence_recent ADD COLUMN recovery_sha256 TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE risk_evidence_formal ADD COLUMN status TEXT NOT NULL DEFAULT 'committed'",
        "ALTER TABLE risk_evidence_formal ADD COLUMN error TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE risk_evidence_formal ADD COLUMN recovery_payload BLOB",
        "ALTER TABLE risk_evidence_formal ADD COLUMN recovery_sha256 TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE source_cursor_recent ADD COLUMN status TEXT NOT NULL DEFAULT 'committed'",
        "ALTER TABLE source_cursor_recent ADD COLUMN error TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE source_cursor_recent ADD COLUMN recovery_payload BLOB",
        "ALTER TABLE source_cursor_recent ADD COLUMN recovery_sha256 TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE source_cursor_formal ADD COLUMN status TEXT NOT NULL DEFAULT 'committed'",
        "ALTER TABLE source_cursor_formal ADD COLUMN error TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE source_cursor_formal ADD COLUMN recovery_payload BLOB",
        "ALTER TABLE source_cursor_formal ADD COLUMN recovery_sha256 TEXT NOT NULL DEFAULT ''",
    ),
    3: (
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
    ),
}


__all__ = ["SCHEMA_VERSION", "apply_migrations", "connection_scope", "connect", "initialize_database"]
