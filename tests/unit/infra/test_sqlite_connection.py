import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    OpenSqliteConnection,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteSession,
    SqliteTransaction,
)


def test_schema_installation_is_atomic_and_driver_api_is_hidden(tmp_path):
    with SqliteSession(tmp_path / "index.sqlite3") as connection:
        assert not hasattr(connection, "execute")
        assert not hasattr(connection, "cursor")
        broken = SqliteSchema(1, ("CREATE TABLE facts(value INTEGER)", "CREATE INVALID STATEMENT"))
        with pytest.raises(sqlite3.OperationalError):
            EnsureSqliteSchema(connection, broken, allow_create=True)
        assert connection.run("PRAGMA user_version").require_one()[0] == 0
        assert connection.run("SELECT name FROM sqlite_master WHERE type='table'").fetchone() is None
        valid = SqliteSchema(1, ("CREATE TABLE facts(value INTEGER)",))
        EnsureSqliteSchema(connection, valid, allow_create=True)
        EnsureSqliteSchema(connection, valid)
        assert connection.run("PRAGMA user_version").require_one()[0] == 1


def test_schema_rejects_nonempty_unversioned_database(tmp_path):
    with SqliteSession(tmp_path / "index.sqlite3") as connection:
        connection.run("CREATE TABLE legacy(value INTEGER)")
        with pytest.raises(sqlite3.DatabaseError, match="unsupported"):
            EnsureSqliteSchema(connection, SqliteSchema(1, ("CREATE TABLE facts(value INTEGER)",)), allow_create=True)
        assert connection.run("PRAGMA user_version").require_one()[0] == 0


def test_concurrent_schema_initializers_share_one_valid_schema(tmp_path):
    path = tmp_path / "index.sqlite3"
    schema = SqliteSchema(1, ("CREATE TABLE facts(value INTEGER)",))

    def initialize():
        with SqliteSession(path) as connection:
            EnsureSqliteSchema(connection, schema, allow_create=True)
            return connection.run("PRAGMA user_version").require_one()[0]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: initialize(), range(8))) == [1] * 8


def test_cancelled_transaction_rolls_back_and_connection_remains_usable(tmp_path):
    with SqliteSession(tmp_path / "index.sqlite3") as connection:
        connection.run("CREATE TABLE evidence(value INTEGER)")
        with pytest.raises(KeyboardInterrupt), SqliteTransaction(connection, immediate=True):
            connection.run("INSERT INTO evidence VALUES(1)")
            raise KeyboardInterrupt
        assert tuple(connection.run("SELECT COUNT(*) FROM evidence").fetchone()) == (0,)
        with SqliteTransaction(connection):
            connection.run("INSERT INTO evidence VALUES(2)")
        assert tuple(connection.run("SELECT value FROM evidence").fetchone()) == (2,)


def test_read_only_never_initializes_missing_database_or_accepts_writes(tmp_path):
    path = tmp_path / "index.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        OpenSqliteConnection(path, SqliteConnectionOptions(read_only=True))
    assert not path.exists()
    with SqliteSession(path, SqliteConnectionOptions(journal_mode="DELETE")) as connection:
        connection.run("CREATE TABLE evidence(value INTEGER)")
    with SqliteSession(path, SqliteConnectionOptions(read_only=True)) as connection:
        assert tuple(connection.run("PRAGMA journal_mode").fetchone()) == ("delete",)
        with pytest.raises(sqlite3.OperationalError), SqliteTransaction(connection):
            connection.run("INSERT INTO evidence VALUES(1)")


@pytest.mark.parametrize("timeout", (-1.0, float("nan"), float("inf")))
def test_invalid_timeout_is_rejected(timeout):
    with pytest.raises(ValueError, match="invalid_sqlite_connection_options"):
        SqliteConnectionOptions(timeout_seconds=timeout)


def test_immutable_copy_does_not_create_journals_and_escapes_uri_characters(tmp_path):
    path = tmp_path / "snapshot ?#%.sqlite3"
    with SqliteSession(path, SqliteConnectionOptions(journal_mode="WAL")) as connection:
        connection.run("CREATE TABLE evidence(value INTEGER)")
        with SqliteTransaction(connection):
            connection.run("INSERT INTO evidence VALUES(7)")
    before = {item.name: item.read_bytes() for item in tmp_path.iterdir()}
    with SqliteSession(path, SqliteConnectionOptions(read_only=True, immutable=True)) as connection:
        assert tuple(connection.run("SELECT value FROM evidence").fetchone()) == (7,)
        with pytest.raises(sqlite3.OperationalError):
            connection.run("DELETE FROM evidence")
    assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == before
    with pytest.raises(ValueError, match="invalid_sqlite_connection_options"):
        SqliteConnectionOptions(immutable=True)


def test_zero_timeout_preserves_immediate_lock_rejection(tmp_path):
    path = tmp_path / "busy.sqlite3"
    with SqliteSession(path) as writer:
        writer.run("CREATE TABLE evidence(value INTEGER)")
        with SqliteSession(path, SqliteConnectionOptions(timeout_seconds=0)) as competitor:
            assert tuple(competitor.run("PRAGMA busy_timeout").fetchone()) == (0,)
            with SqliteTransaction(writer, immediate=True):
                with (
                    pytest.raises(sqlite3.OperationalError, match="locked"),
                    SqliteTransaction(competitor, immediate=True),
                ):
                    competitor.run("INSERT INTO evidence VALUES(2)")


def test_maintenance_copy_can_write_destination_but_cannot_write_source(tmp_path):
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    with SqliteSession(source) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE evidence(value INTEGER)")
        connection.run("INSERT INTO evidence VALUES(7)")
    original = source.read_bytes()
    with SqliteSession(source, SqliteConnectionOptions(read_only=True, query_only=False)) as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.run("DELETE FROM evidence")
        connection.run("VACUUM INTO ?", (str(target),))
    assert source.read_bytes() == original
    with SqliteSession(target, SqliteConnectionOptions(read_only=True)) as connection:
        assert tuple(connection.run("SELECT value FROM evidence").fetchone()) == (7,)


def test_read_only_connection_sees_committed_wal_rows_without_checkpoint(tmp_path):
    path = tmp_path / "active.sqlite3"
    with SqliteSession(path, SqliteConnectionOptions(journal_mode="WAL")) as writer:
        writer.run("CREATE TABLE evidence(value INTEGER)")
        with SqliteTransaction(writer):
            writer.run("INSERT INTO evidence VALUES(11)")
        assert path.with_name(f"{path.name}-wal").stat().st_size > 0
        with SqliteSession(path, SqliteConnectionOptions(read_only=True)) as reader:
            assert tuple(reader.run("SELECT value FROM evidence").fetchone()) == (11,)


def test_explicit_cross_thread_and_immediate_transaction_options(tmp_path):
    with SqliteSession(tmp_path / "thread.sqlite3", SqliteConnectionOptions(check_same_thread=False)) as connection:
        connection.run("CREATE TABLE evidence(value INTEGER)")
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(
                lambda: tuple(connection.run("SELECT COUNT(*) FROM evidence").fetchone())
            ).result() == (0,)
        with SqliteTransaction(connection, immediate=True):
            connection.run("INSERT INTO evidence VALUES(3)")
            assert connection.in_transaction
        assert not connection.in_transaction
    with pytest.raises(ValueError, match="memory_database"):
        OpenSqliteConnection(":memory:", SqliteConnectionOptions(read_only=True))


def test_schema_seed_failure_rolls_back_ddl_and_can_retry(tmp_path):
    schema = SqliteSchema(1, ("CREATE TABLE facts(value INTEGER NOT NULL)",))

    def seed(connection):
        connection.run("INSERT INTO facts VALUES(7)")
        raise RuntimeError("seed interrupted")

    with SqliteSession(tmp_path / "index.sqlite3") as connection:
        with pytest.raises(RuntimeError, match="seed interrupted"):
            EnsureSqliteSchema(connection, schema, allow_create=True, initialize=seed)
        assert not connection.in_transaction
        assert connection.run("SELECT name FROM sqlite_master").fetchall() == []
        assert connection.run("PRAGMA user_version").require_one()[0] == 0
        EnsureSqliteSchema(
            connection,
            schema,
            allow_create=True,
            initialize=lambda conn: conn.run("INSERT INTO facts VALUES(9)"),
        )
        assert connection.run("SELECT value FROM facts").require_one()[0] == 9


def test_deferred_constraint_commit_failure_rolls_back_and_releases_writer(tmp_path):
    path = tmp_path / "index.sqlite3"
    with SqliteSession(path, SqliteConnectionOptions(foreign_keys=True)) as connection:
        connection.run("CREATE TABLE parents(id INTEGER PRIMARY KEY)")
        connection.run("CREATE TABLE children(parent_id INTEGER REFERENCES parents(id) DEFERRABLE INITIALLY DEFERRED)")
        with pytest.raises(sqlite3.IntegrityError), SqliteTransaction(connection):
            connection.run("INSERT INTO children VALUES(42)")
        assert not connection.in_transaction
        assert connection.run("SELECT COUNT(*) FROM children").require_one()[0] == 0
        with (
            SqliteSession(path, SqliteConnectionOptions(timeout_seconds=0)) as other,
            SqliteTransaction(other, immediate=True),
        ):
            other.run("INSERT INTO parents VALUES(42)")


def test_session_closes_and_rolls_back_unfinished_transaction(tmp_path):
    path = tmp_path / "index.sqlite3"
    with SqliteSession(path) as connection:
        connection.run("CREATE TABLE facts(value INTEGER)")
        transaction = SqliteTransaction(connection)
        transaction.begin()
        connection.run("INSERT INTO facts VALUES(7)")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.run("SELECT 1")
    with SqliteSession(path) as reopened:
        assert reopened.run("SELECT COUNT(*) FROM facts").require_one()[0] == 0


def test_nested_transaction_is_rejected_without_finishing_outer_transaction(tmp_path):
    with SqliteSession(tmp_path / "index.sqlite3") as connection, SqliteTransaction(connection):
        with pytest.raises(ValueError, match="nested_transaction"):
            with SqliteTransaction(connection):
                pytest.fail("nested transaction entered")
        assert connection.in_transaction


def test_schema_validation_failure_rolls_back_initial_records(tmp_path):
    schema = SqliteSchema(None, ("CREATE TABLE facts(value INTEGER)",))

    def reject(connection):
        assert connection.run("SELECT value FROM facts").require_one()[0] == 7
        raise ValueError("invalid initial identity")

    with SqliteSession(tmp_path / "index.sqlite3") as connection:
        with pytest.raises(ValueError, match="invalid initial identity"):
            EnsureSqliteSchema(
                connection,
                schema,
                allow_create=True,
                initialize=lambda conn: conn.run("INSERT INTO facts VALUES(7)"),
                validate=reject,
            )
        assert connection.run("SELECT name FROM sqlite_master").fetchall() == []


@pytest.mark.parametrize(
    "options",
    (
        {"journal_mode": "OFF"},
        {"durability": "rebuildable"},
        {"cache_kib": 0},
        {"mmap_bytes": -1},
        {"durability": "invalid"},
        {"journal_mode": "off"},
        {"temp_storage": "invalid"},
    ),
)
def test_invalid_durability_and_cache_options_are_rejected(options):
    with pytest.raises(ValueError, match="invalid_sqlite_connection_options"):
        SqliteConnectionOptions(**options)


def test_cache_configuration_and_durability_are_applied_before_returning(tmp_path):
    with SqliteSession(
        tmp_path / "index.sqlite3",
        SqliteConnectionOptions(cache_kib=8192, mmap_bytes=0, temp_storage="FILE"),
    ) as connection:
        assert connection.run("PRAGMA synchronous").require_one()[0] == 2
        assert connection.run("PRAGMA cache_size").require_one()[0] == -8192
        assert connection.run("PRAGMA mmap_size").require_one()[0] == 0
        assert connection.run("PRAGMA temp_store").require_one()[0] == 1


@pytest.mark.parametrize("named", (False, True))
def test_parameters_and_row_iteration_preserve_supported_values(named):
    values = ("文本", 3, 1.25, b"bytes", None, True)
    parameters = {f"p{index}": value for index, value in enumerate(values)} if named else values
    placeholders = ",".join(f":p{index}" if named else "?" for index in range(len(values)))
    with SqliteSession(":memory:") as connection:
        rows = list(connection.run(f"SELECT {placeholders}", parameters))
        assert len(rows) == 1
        assert tuple(rows[0]) == values
        assert rows[0].keys() == placeholders.split(",")


@pytest.mark.parametrize("parameters", ((object(),), {"value": object()}))
def test_unsupported_parameter_is_rejected_before_sql_execution(parameters):
    with SqliteSession(":memory:") as connection:
        with pytest.raises(ValueError, match="sqlite_parameter_type_invalid"):
            connection.run("SELECT :value", parameters)
