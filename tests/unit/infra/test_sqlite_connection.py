import sqlite3
from contextlib import closing

import pytest

from trader.infra.sqlite_connection import OpenSqliteConnection, SqliteConnectionOptions, SqliteTransaction


def test_cancelled_transaction_rolls_back_and_connection_remains_usable(tmp_path):
    with closing(OpenSqliteConnection(tmp_path / "index.sqlite3")) as connection:
        connection.execute("CREATE TABLE evidence(value INTEGER)")
        with pytest.raises(KeyboardInterrupt), SqliteTransaction(connection, immediate=True):
            connection.execute("INSERT INTO evidence VALUES(1)")
            raise KeyboardInterrupt
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone() == (0,)
        with SqliteTransaction(connection):
            connection.execute("INSERT INTO evidence VALUES(2)")
        assert connection.execute("SELECT value FROM evidence").fetchone() == (2,)


def test_read_only_never_initializes_missing_database_or_accepts_writes(tmp_path):
    path = tmp_path / "index.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        OpenSqliteConnection(path, SqliteConnectionOptions(read_only=True))
    assert not path.exists()
    with closing(OpenSqliteConnection(path, SqliteConnectionOptions(journal_mode="DELETE"))) as connection:
        connection.execute("CREATE TABLE evidence(value INTEGER)")
    with closing(OpenSqliteConnection(path, SqliteConnectionOptions(read_only=True))) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        with pytest.raises(sqlite3.OperationalError), SqliteTransaction(connection):
            connection.execute("INSERT INTO evidence VALUES(1)")
