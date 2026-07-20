from __future__ import annotations

import sqlite3
from pathlib import Path

from trader.infrastructure.persistence.sqlite import SCHEMA_VERSION, connect, initialize_database


def test_initialize_database_sets_schema_to_current_version(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    initialize_database(database)

    with connect(database) as connection:
        value = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()

    assert value is not None
    assert int(str(value["value"])) == SCHEMA_VERSION


def test_initialize_database_is_idempotent_for_legacy_state(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    initialize_database(database)
    initialize_database(database)

    with connect(database) as connection:
        versions = [
            row["value"]
            for row in connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchall()
        ]

    assert versions == [str(SCHEMA_VERSION)]


def test_initialize_database_migrates_from_versioned_partial_schema(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1');

            CREATE TABLE frozen_snapshots(
                snapshot_id TEXT PRIMARY KEY,
                strategy TEXT NOT NULL
            );

            CREATE TABLE data_source_health(
                source TEXT PRIMARY KEY,
                planned_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                circuit_open INTEGER NOT NULL DEFAULT 0,
                p50_latency_ms REAL,
                p95_latency_ms REAL,
                data_age_seconds REAL
            );
            """
        )

    initialize_database(database)

    with connect(database) as connection:
        frozen_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(frozen_snapshots)")}
        health_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(data_source_health)")}
        version_row = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()

    assert version_row is not None
    assert int(str(version_row["value"])) == SCHEMA_VERSION
    assert "schema_version" in frozen_columns
    assert "anchor_json" in frozen_columns
    assert "last_error" in health_columns


def test_initialize_database_handles_corrupt_schema_version(tmp_path: Path) -> None:
    database = tmp_path / "corrupt.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES ('schema_version', 'N/A');

            CREATE TABLE frozen_snapshots(
                snapshot_id TEXT PRIMARY KEY,
                strategy TEXT NOT NULL
            );

            CREATE TABLE data_source_health(
                source TEXT PRIMARY KEY,
                planned_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                circuit_open INTEGER NOT NULL DEFAULT 0,
                p50_latency_ms REAL,
                p95_latency_ms REAL,
                data_age_seconds REAL
            );
            """
        )

    initialize_database(database)

    with connect(database) as connection:
        version_row = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        frozen_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(frozen_snapshots)")}
        health_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(data_source_health)")}

    assert version_row is not None
    assert int(str(version_row["value"])) == SCHEMA_VERSION
    assert "schema_version" in frozen_columns
    assert "anchor_json" in frozen_columns
    assert "last_error" in health_columns


def test_initialize_database_recovers_when_schema_version_row_is_blank(tmp_path: Path) -> None:
    database = tmp_path / "missing_schema_meta.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES ('schema_version', '');

            CREATE TABLE frozen_snapshots(
                snapshot_id TEXT PRIMARY KEY,
                strategy TEXT NOT NULL
            );

            CREATE TABLE data_source_health(
                source TEXT PRIMARY KEY,
                planned_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                circuit_open INTEGER NOT NULL DEFAULT 0,
                p50_latency_ms REAL,
                p95_latency_ms REAL,
                data_age_seconds REAL
            );
            """
        )

    initialize_database(database)

    with connect(database) as connection:
        version_row = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        frozen_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(frozen_snapshots)")}
        health_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(data_source_health)")}

    assert version_row is not None
    assert int(str(version_row["value"])) == SCHEMA_VERSION
    assert "schema_version" in frozen_columns
    assert "anchor_json" in frozen_columns
    assert "last_error" in health_columns
