"""SQLite schema and connection helpers for the v2 runtime store."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as connection:
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
                data_version TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                record_count INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('staged', 'committed', 'quarantined')),
                error TEXT NOT NULL DEFAULT '',
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
                updated_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


__all__ = ["SCHEMA_VERSION", "connect", "initialize_database"]
