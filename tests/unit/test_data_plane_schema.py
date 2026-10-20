"""Snapshot schemas are created atomically and never implicitly upgraded."""

import sqlite3

import pytest

from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.recommendation.infra.persistence.data_plane_sqlite import SCHEMA_VERSION, InitializeDatabase


def test_initialize_is_idempotent_and_preserves_publication_sequence(tmp_path):
    path = tmp_path / "snapshots.sqlite3"
    InitializeDatabase(path)
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE publication_sequence SET reserved_until=1234")
    InitializeDatabase(path)
    with SqliteSession(path) as connection:
        assert connection.run("SELECT value FROM schema_meta WHERE key='schema_version'").require_one()[0] == str(
            SCHEMA_VERSION
        )
        assert connection.run("SELECT reserved_until FROM publication_sequence").require_one()[0] == 1234


@pytest.mark.parametrize("version", ("1", "N/A", None))
def test_partial_or_obsolete_schema_is_rejected_without_rewriting(tmp_path, version):
    path = tmp_path / "snapshots.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        if version is not None:
            connection.run("INSERT INTO schema_meta VALUES ('schema_version',?)", (version,))
    with pytest.raises(sqlite3.DatabaseError):
        InitializeDatabase(path)
    # Opening the database may enable WAL; it must not change logical contents.
    with SqliteSession(path) as connection:
        assert [row[0] for row in connection.run("SELECT name FROM sqlite_master WHERE type='table'")] == [
            "schema_meta"
        ]
        assert connection.run("SELECT COUNT(*) FROM schema_meta").require_one()[0] == int(version is not None)
