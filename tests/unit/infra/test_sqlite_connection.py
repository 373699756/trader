import sqlite3
from concurrent.futures import ThreadPoolExecutor
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


@pytest.mark.parametrize("timeout", (-1.0, float("nan"), float("inf")))
def test_invalid_timeout_is_rejected(timeout):
    with pytest.raises(ValueError, match="invalid_sqlite_connection_options"):
        SqliteConnectionOptions(timeout_seconds=timeout)


def test_immutable_copy_does_not_create_journals_and_escapes_uri_characters(tmp_path):
    path = tmp_path / "snapshot ?#%.sqlite3"
    with closing(OpenSqliteConnection(path, SqliteConnectionOptions(journal_mode="WAL"))) as connection:
        connection.execute("CREATE TABLE evidence(value INTEGER)")
        with SqliteTransaction(connection):
            connection.execute("INSERT INTO evidence VALUES(7)")
    before = {item.name: item.read_bytes() for item in tmp_path.iterdir()}
    with closing(OpenSqliteConnection(path, SqliteConnectionOptions(read_only=True, immutable=True))) as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == (7,)
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM evidence")
    assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == before
    with pytest.raises(ValueError, match="invalid_sqlite_connection_options"):
        SqliteConnectionOptions(immutable=True)


def test_zero_timeout_preserves_immediate_lock_rejection(tmp_path):
    path = tmp_path / "busy.sqlite3"
    with closing(OpenSqliteConnection(path)) as writer:
        writer.execute("CREATE TABLE evidence(value INTEGER)")
        with closing(OpenSqliteConnection(path, SqliteConnectionOptions(timeout_seconds=0))) as competitor:
            assert competitor.execute("PRAGMA busy_timeout").fetchone() == (0,)
            with SqliteTransaction(writer, immediate=True):
                with (
                    pytest.raises(sqlite3.OperationalError, match="locked"),
                    SqliteTransaction(competitor, immediate=True),
                ):
                    competitor.execute("INSERT INTO evidence VALUES(2)")


def test_maintenance_copy_can_write_destination_but_cannot_write_source(tmp_path):
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    with closing(OpenSqliteConnection(source)) as connection, SqliteTransaction(connection):
        connection.execute("CREATE TABLE evidence(value INTEGER)")
        connection.execute("INSERT INTO evidence VALUES(7)")
    original = source.read_bytes()
    with closing(OpenSqliteConnection(source, SqliteConnectionOptions(read_only=True, query_only=False))) as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM evidence")
        connection.rollback()
        connection.execute("VACUUM INTO ?", (str(target),))
    assert source.read_bytes() == original
    with closing(OpenSqliteConnection(target, SqliteConnectionOptions(read_only=True))) as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == (7,)


def test_read_only_connection_sees_committed_wal_rows_without_checkpoint(tmp_path):
    path = tmp_path / "active.sqlite3"
    with closing(OpenSqliteConnection(path, SqliteConnectionOptions(journal_mode="WAL"))) as writer:
        writer.execute("CREATE TABLE evidence(value INTEGER)")
        with SqliteTransaction(writer):
            writer.execute("INSERT INTO evidence VALUES(11)")
        assert path.with_name(f"{path.name}-wal").stat().st_size > 0
        with closing(OpenSqliteConnection(path, SqliteConnectionOptions(read_only=True))) as reader:
            assert reader.execute("SELECT value FROM evidence").fetchone() == (11,)


def test_explicit_cross_thread_and_immediate_transaction_options(tmp_path):
    with closing(
        OpenSqliteConnection(
            tmp_path / "thread.sqlite3", SqliteConnectionOptions(check_same_thread=False, isolation_level="IMMEDIATE")
        )
    ) as connection:
        assert connection.isolation_level == "IMMEDIATE"
        connection.execute("CREATE TABLE evidence(value INTEGER)")
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(lambda: connection.execute("SELECT COUNT(*) FROM evidence").fetchone()).result() == (
                0,
            )
        with connection:
            connection.execute("INSERT INTO evidence VALUES(3)")
            assert connection.in_transaction
        assert not connection.in_transaction
    with pytest.raises(ValueError, match="memory_database"):
        OpenSqliteConnection(":memory:", SqliteConnectionOptions(read_only=True))
