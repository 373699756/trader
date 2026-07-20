"""SQLite schema and connection helpers for the v2 runtime store."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 4


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=10.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
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
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS frozen_snapshots(
                snapshot_id TEXT PRIMARY KEY,
                strategy TEXT NOT NULL,
                recommend_date TEXT NOT NULL,
                frozen_at TEXT NOT NULL,
                fusion_version TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                config_version TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                data_version TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                record_count INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('staged', 'committed', 'quarantined')),
                error TEXT NOT NULL DEFAULT '',
                anchor_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(strategy, recommend_date)
            );

            CREATE TABLE IF NOT EXISTS recommendations(
                strategy TEXT NOT NULL,
                recommend_date TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                rank INTEGER NOT NULL,
                anchor_price REAL NOT NULL,
                anchor_daily_return_pct REAL,
                snapshot_id TEXT NOT NULL REFERENCES frozen_snapshots(snapshot_id),
                PRIMARY KEY(strategy, recommend_date, stock_code)
            );

            CREATE TABLE IF NOT EXISTS published_snapshots(
                strategy TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                published_at TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                sha256 TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pipeline_events(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                subject_key TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                phase TEXT NOT NULL,
                strategy TEXT NOT NULL,
                priority INTEGER NOT NULL,
                data_version TEXT NOT NULL,
                config_version TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                deadline TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                UNIQUE(trade_date, phase, strategy, event_type, subject_key, data_version)
            );

            CREATE TABLE IF NOT EXISTS data_source_health(
                source TEXT PRIMARY KEY,
                planned_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                circuit_open INTEGER NOT NULL DEFAULT 0,
                p50_latency_ms REAL,
                p95_latency_ms REAL,
                data_age_seconds REAL,
                last_error TEXT NOT NULL DEFAULT '',
                route_json TEXT NOT NULL DEFAULT '{}',
                route_status TEXT NOT NULL DEFAULT 'idle',
                route_fallback_reason TEXT NOT NULL DEFAULT '',
                route_degraded INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deepseek_calls(
                call_id TEXT PRIMARY KEY,
                strategy TEXT NOT NULL,
                phase TEXT NOT NULL,
                model TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                completed_at TEXT,
                http_status INTEGER,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                latency_ms REAL,
                outcome TEXT NOT NULL,
                error_code TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS live_overlays(
                strategy TEXT NOT NULL,
                recommend_date TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                version TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                closing INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(strategy, recommend_date)
            );
            """
        )
        _ensure_column(
            connection,
            "frozen_snapshots",
            "schema_version",
            "TEXT NOT NULL DEFAULT 'recommendation_snapshot_v2'",
        )
        _ensure_column(connection, "frozen_snapshots", "anchor_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(connection, "data_source_health", "last_error", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "data_source_health", "route_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(connection, "data_source_health", "route_status", "TEXT NOT NULL DEFAULT 'idle'")
        _ensure_column(connection, "data_source_health", "route_fallback_reason", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "data_source_health", "route_degraded", "INTEGER NOT NULL DEFAULT 0")
        apply_migrations(connection)
        if _current_schema_version(connection) < SCHEMA_VERSION:
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


# Registered migrations keyed by target schema version.
# Each migration is a list of SQL statements that bring the database from the
# previous version to the keyed version.  Migrations are applied in ascending
# version order.
MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE frozen_snapshots ADD COLUMN schema_version TEXT NOT NULL DEFAULT 'recommendation_snapshot_v2'",
        "ALTER TABLE frozen_snapshots ADD COLUMN anchor_json TEXT NOT NULL DEFAULT '{}'",
    ],
    3: [
        "ALTER TABLE data_source_health ADD COLUMN last_error TEXT NOT NULL DEFAULT ''",
    ],
    4: [
        "ALTER TABLE data_source_health ADD COLUMN route_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE data_source_health ADD COLUMN route_status TEXT NOT NULL DEFAULT 'idle'",
        "ALTER TABLE data_source_health ADD COLUMN route_fallback_reason TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE data_source_health ADD COLUMN route_degraded INTEGER NOT NULL DEFAULT 0",
    ],
}


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Apply any pending migrations in version order."""
    current = _current_schema_version(connection)
    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        for statement in MIGRATIONS[version]:
            try:
                connection.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
            (str(version),),
        )
        current = version


def _current_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return 0
    return _parse_schema_version(row[0])


def _parse_schema_version(raw: object) -> int:
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return 0
    try:
        return int(text)
    except (ValueError, TypeError):
        return 0


__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "apply_migrations", "connect", "initialize_database"]
