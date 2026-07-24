"""SQLite schema and connection helpers for the v2 runtime store."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 9


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=10.0)
    try:
        connection.row_factory = sqlite3.Row
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
        connection.execute("PRAGMA journal_mode=WAL")
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
                board TEXT NOT NULL DEFAULT 'unsupported',
                board_policy_id TEXT NOT NULL DEFAULT '',
                board_rank INTEGER NOT NULL DEFAULT 0,
                board_data_reliability REAL NOT NULL DEFAULT 1.0,
                competition_group_id TEXT NOT NULL DEFAULT '',
                selection_skip_reason TEXT NOT NULL DEFAULT '',
                merge_epoch TEXT NOT NULL DEFAULT '',
                atr20_pct REAL,
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

            CREATE TABLE IF NOT EXISTS freeze_checkpoints(
                strategy TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                boundary_at TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ready', 'consumed', 'quarantined')),
                consumed_at TEXT,
                PRIMARY KEY(strategy, trade_date, boundary_at)
            );

            CREATE TABLE IF NOT EXISTS outcome_benchmarks(
                trade_date TEXT PRIMARY KEY,
                return_pct REAL NOT NULL,
                observed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recommendation_outcomes(
                snapshot_id TEXT NOT NULL REFERENCES frozen_snapshots(snapshot_id),
                strategy TEXT NOT NULL,
                recommend_date TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                horizon INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('complete', 'benchmark_missing', 'insufficient_data')),
                settled_at TEXT NOT NULL,
                anchor_price REAL NOT NULL,
                atr20_pct REAL NOT NULL,
                minimum_low REAL,
                end_close REAL,
                gross_return_pct REAL,
                benchmark_return_pct REAL,
                net_excess_return_pct REAL,
                mae_pct REAL,
                mae_atr REAL,
                severe_drawdown INTEGER,
                quality_reason TEXT NOT NULL DEFAULT '',
                version TEXT NOT NULL,
                PRIMARY KEY(snapshot_id, stock_code, horizon)
            );

            CREATE TABLE IF NOT EXISTS outcome_backlog(
                snapshot_id TEXT NOT NULL,
                strategy TEXT NOT NULL,
                recommend_date TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                anchor_price REAL NOT NULL,
                atr20_pct REAL NOT NULL,
                archive_relative_path TEXT NOT NULL,
                PRIMARY KEY(snapshot_id, stock_code)
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
        _ensure_column(connection, "recommendations", "board", "TEXT NOT NULL DEFAULT 'unsupported'")
        _ensure_column(connection, "recommendations", "board_policy_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "recommendations", "board_rank", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "recommendations", "board_data_reliability", "REAL NOT NULL DEFAULT 1.0")
        _ensure_column(connection, "recommendations", "competition_group_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "recommendations", "selection_skip_reason", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "recommendations", "merge_epoch", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "recommendations", "atr20_pct", "REAL")
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
    5: [
        "ALTER TABLE deepseek_calls ADD COLUMN model_role TEXT NOT NULL DEFAULT 'primary'",
        "ALTER TABLE deepseek_calls ADD COLUMN requested_model TEXT",
        "ALTER TABLE deepseek_calls ADD COLUMN actual_model TEXT",
        "ALTER TABLE deepseek_calls ADD COLUMN reasoning_effort TEXT",
        "ALTER TABLE deepseek_calls ADD COLUMN system_fingerprint TEXT",
        "ALTER TABLE deepseek_calls ADD COLUMN finish_reason TEXT",
        "ALTER TABLE deepseek_calls ADD COLUMN total_tokens INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE deepseek_calls ADD COLUMN prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE deepseek_calls ADD COLUMN prompt_cache_miss_tokens INTEGER NOT NULL DEFAULT 0",
    ],
    6: [
        "ALTER TABLE recommendations ADD COLUMN board TEXT NOT NULL DEFAULT 'unsupported'",
        "ALTER TABLE recommendations ADD COLUMN board_policy_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE recommendations ADD COLUMN board_rank INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE recommendations ADD COLUMN board_data_reliability REAL NOT NULL DEFAULT 1.0",
        "ALTER TABLE recommendations ADD COLUMN competition_group_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE recommendations ADD COLUMN selection_skip_reason TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE recommendations ADD COLUMN merge_epoch TEXT NOT NULL DEFAULT ''",
    ],
    7: [
        "ALTER TABLE recommendations ADD COLUMN atr20_pct REAL",
        """CREATE TABLE IF NOT EXISTS outcome_benchmarks(
            trade_date TEXT PRIMARY KEY,
            return_pct REAL NOT NULL,
            observed_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS recommendation_outcomes(
            snapshot_id TEXT NOT NULL REFERENCES frozen_snapshots(snapshot_id),
            strategy TEXT NOT NULL,
            recommend_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            horizon INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('complete', 'benchmark_missing', 'insufficient_data')),
            settled_at TEXT NOT NULL,
            anchor_price REAL NOT NULL,
            atr20_pct REAL NOT NULL,
            minimum_low REAL,
            end_close REAL,
            gross_return_pct REAL,
            benchmark_return_pct REAL,
            net_excess_return_pct REAL,
            mae_pct REAL,
            mae_atr REAL,
            severe_drawdown INTEGER,
            quality_reason TEXT NOT NULL DEFAULT '',
            version TEXT NOT NULL,
            PRIMARY KEY(snapshot_id, stock_code, horizon)
        )""",
    ],
    8: [
        """CREATE TABLE IF NOT EXISTS freeze_checkpoints(
            strategy TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            boundary_at TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ready', 'consumed', 'quarantined')),
            consumed_at TEXT,
            PRIMARY KEY(strategy, trade_date, boundary_at)
        )""",
    ],
    9: [
        "DROP TABLE IF EXISTS pipeline_events",
        "DROP TABLE IF EXISTS data_source_health",
        """CREATE TABLE IF NOT EXISTS outcome_backlog(
            snapshot_id TEXT NOT NULL,
            strategy TEXT NOT NULL,
            recommend_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            anchor_price REAL NOT NULL,
            atr20_pct REAL NOT NULL,
            archive_relative_path TEXT NOT NULL,
            PRIMARY KEY(snapshot_id, stock_code)
        )""",
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
                message = str(exc).lower()
                if "duplicate column name" not in message and "no such table" not in message:
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
