from __future__ import annotations

from contextlib import closing
from pathlib import Path

from trader.infra.sqlite_connection import OpenSqliteConnection
from trader.recommendation.infra.persistence.data_plane_sqlite import (
    SCHEMA_VERSION,
    ConnectionScope,
    InitializeDatabase,
)


def test_initialize_database_sets_schema_to_current_version(tmp_path: Path) -> None:
    database = tmp_path / "market-data.sqlite3"

    InitializeDatabase(database)

    with ConnectionScope(database) as connection:
        version = connection.run("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()

    assert version is not None
    assert int(str(version["value"])) == SCHEMA_VERSION


def test_initialize_database_is_idempotent_for_partial_schema(tmp_path: Path) -> None:
    database = tmp_path / "legacy-data.sqlite3"
    with closing(OpenSqliteConnection(database)) as connection, connection:
        connection.run("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.run("INSERT INTO schema_meta(key, value) VALUES('schema_version', '1')")
        connection.run(
            """
            CREATE TABLE security_master_recent(
                code TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                source_time TEXT NOT NULL,
                source TEXT NOT NULL,
                data_version TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )

    InitializeDatabase(database)
    InitializeDatabase(database)

    with ConnectionScope(database) as connection:
        columns = {str(row["name"]) for row in connection.run("PRAGMA table_info(security_master_recent)")}
        tables = {
            str(row["name"]) for row in connection.run("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        version = connection.run("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()

    assert version is not None
    assert int(str(version["value"])) == SCHEMA_VERSION
    assert "status" in columns
    assert "error" in columns
    assert "recovery_payload" in columns
    assert "recovery_sha256" in columns
    assert {"trading_calendar_recent", "trading_calendar_formal"} <= tables


def test_initialize_database_recovers_from_invalid_schema_version(tmp_path: Path) -> None:
    database = tmp_path / "invalid-version.sqlite3"
    with closing(OpenSqliteConnection(database)) as connection, connection:
        connection.run("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.run("INSERT INTO schema_meta(key, value) VALUES('schema_version', 'N/A')")

    InitializeDatabase(database)

    with ConnectionScope(database) as connection:
        version = connection.run("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()

    assert version is not None
    assert int(str(version["value"])) == SCHEMA_VERSION


def test_initialize_database_handles_data_plane_schema_without_schema_meta(tmp_path: Path) -> None:
    database = tmp_path / "without-meta.sqlite3"
    with closing(OpenSqliteConnection(database)) as connection, connection:
        connection.apply_script(
            """
            CREATE TABLE security_master_recent(
                code TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                source_time TEXT NOT NULL,
                source TEXT NOT NULL,
                data_version TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )

    InitializeDatabase(database)

    with ConnectionScope(database) as connection:
        version = connection.run("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        rows = {str(row["name"]) for row in connection.run("PRAGMA table_info(security_master_recent)")}

    assert version is not None
    assert int(str(version["value"])) == SCHEMA_VERSION
    assert "status" in rows
