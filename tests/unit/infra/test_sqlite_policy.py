"""Nondefault storage configuration affects real SQLite without weakening publication atomicity."""

from dataclasses import replace
from pathlib import Path
import sqlite3
import json
import shutil
from unittest.mock import Mock, call

import pytest

from trader.bootstrap import BuildSqlitePolicy
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.models import StorageSettings
from trader.infra.sqlite_connection import OpenSqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_observation import SqliteObservation
from trader.infra.storage_types import StorageProfile


def test_storage_json_reaches_download_training_and_recommendation_adapters(tmp_path, monkeypatch):
    from trader.download.infra.reference_index import SQLiteReferenceIndex
    from trader.infra import sqlite_connection
    from trader.recommendation.infra.persistence.publication_sequence import PublicationSequence
    from trader.training.infra.sample_index import SQLiteTrainingSampleIndex

    root = Path(__file__).resolve().parents[3]
    shutil.copytree(root / "config", tmp_path / "config")
    runtime_path = tmp_path / "config/runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["storage"] = "ssd"
    runtime_path.write_text(json.dumps(runtime))
    storage_path = tmp_path / "config/storage.json"
    storage = json.loads(storage_path.read_text())
    storage["storage_policy"]["ssd"] = {
        "connection_timeout_seconds": 0.041,
        "read_window_rows": 3,
        "transaction_batch_rows": 2,
        "checkpoint_mode": "PASSIVE",
    }
    storage_path.write_text(json.dumps(storage))
    policy = BuildSqlitePolicy(LoadRuntimeSettings(runtime_path))
    seen = []
    configure = sqlite_connection._configure_database

    def observe(driver, options):
        configure(driver, options)
        seen.append(driver.execute("PRAGMA busy_timeout").fetchone()[0])

    monkeypatch.setattr(sqlite_connection, "_configure_database", observe)
    SQLiteReferenceIndex(tmp_path / "reference.sqlite3", sqlite_policy=policy).merge_st(())
    with SQLiteTrainingSampleIndex(tmp_path / "samples.sqlite3", ("fixture_feature",), sqlite_policy=policy):
        pass
    PublicationSequence(tmp_path / "sequence.sqlite3", sqlite_policy=policy).initialize()
    assert seen == [41, 41, 41]


def test_composition_selects_explicit_detected_and_unknown_medium(monkeypatch):
    settings = LoadRuntimeSettings(Path(__file__).resolve().parents[3] / "config/runtime.json")
    hdd = SqliteIoPolicy(0.03, 3, 2, "PASSIVE")
    ssd = SqliteIoPolicy(0.07, 7, 5, "RESTART")
    settings = replace(settings, storage_policy=StorageSettings(hdd, ssd))
    assert BuildSqlitePolicy(replace(settings, storage_medium="hdd")) is hdd
    assert BuildSqlitePolicy(replace(settings, storage_medium="ssd")) is ssd
    for medium, expected in (("ssd", ssd), ("hdd", hdd), ("unknown", hdd)):
        monkeypatch.setattr("trader.bootstrap.StorageProbe.inspect", lambda _self, _path, kind=medium: StorageProfile(kind, "test", "automatic", "test"))
        assert BuildSqlitePolicy(replace(settings, storage_medium=None)) is expected


def test_policy_bounds_driver_batches_but_keeps_one_atomic_transaction():
    connection = OpenSqliteConnection(":memory:", sqlite_policy=SqliteIoPolicy(0.05, 3, 2, "TRUNCATE"))
    statements = []
    changes_before_consumption = []

    def rows():
        for value in range(7):
            changes_before_consumption.append(connection.total_changes)
            yield (value,)

    connection.observe_statements(statements.append)
    try:
        connection.run("CREATE TABLE facts(value INTEGER PRIMARY KEY)")
        with SqliteTransaction(connection):
            result = connection.run_many("INSERT INTO facts VALUES (?)", rows())
            assert result.rowcount == 7
            result.close()
        assert changes_before_consumption == [0, 0, 2, 2, 4, 4, 6]
        assert statements.count("BEGIN") == statements.count("COMMIT") == 1
        result = connection.run("SELECT value FROM facts ORDER BY value")
        cursor = Mock(wraps=result._cursor)
        result._cursor = cursor
        assert [row["value"] for row in result.fetchmany(5)] == [0, 1, 2, 3, 4]
        assert [row["value"] for row in result.fetchall()] == [5, 6]
        assert cursor.fetchmany.call_args_list == [call(3), call(2), call(3), call(3)]
        with pytest.raises(sqlite3.IntegrityError), SqliteTransaction(connection):
            connection.run_many("INSERT INTO facts VALUES (?)", ((7,), (8,), (0,)))
        assert connection.run("SELECT COUNT(*) FROM facts").require_one()[0] == 7
        assert statements.count("COMMIT") == 1
        assert connection.run_many("INSERT INTO facts VALUES (?)", ()).rowcount == 0
        with pytest.raises(sqlite3.ProgrammingError):
            connection.run_many("SELECT 1", ())
    finally:
        connection.close()


@pytest.mark.parametrize("mode", ("PASSIVE", "RESTART", "TRUNCATE"))
def test_configured_timeout_and_checkpoint_reach_sqlite(tmp_path, mode):
    policy = SqliteIoPolicy(0.023, 3, 2, mode)
    with SqliteSession(tmp_path / "facts.sqlite3", SqliteConnectionOptions(journal_mode="WAL"), sqlite_policy=policy) as connection:
        assert connection.run("PRAGMA busy_timeout").require_one()[0] == 23
        statements = []
        connection.observe_statements(statements.append)
        connection.run("CREATE TABLE facts(value INTEGER)")
        with SqliteTransaction(connection):
            connection.run("INSERT INTO facts VALUES (1)")
        connection.checkpoint()
        assert f"PRAGMA wal_checkpoint({mode})" in statements


def test_passive_checkpoint_rejects_incomplete_wal_before_file_publication(tmp_path):
    policy = SqliteIoPolicy(0.01, 3, 2, "PASSIVE")
    path = tmp_path / "facts.sqlite3"
    with SqliteSession(path, SqliteConnectionOptions(journal_mode="WAL"), sqlite_policy=policy) as writer:
        writer.run("CREATE TABLE facts(value INTEGER)")
        writer.run("INSERT INTO facts VALUES (1)")
        with SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=policy) as reader:
            with SqliteTransaction(reader):
                assert reader.run("SELECT value FROM facts").require_one()[0] == 1
                writer.run("INSERT INTO facts VALUES (2)")
                with pytest.raises(sqlite3.OperationalError, match="checkpoint_incomplete"):
                    writer.checkpoint()
        writer.checkpoint()


def test_batched_failure_counts_only_rows_attempted_by_sqlite():
    observation = SqliteObservation()
    with SqliteSession(":memory:", sqlite_policy=SqliteIoPolicy(0.01, 3, 2, "PASSIVE")) as connection:
        connection.run("CREATE TABLE facts(value INTEGER PRIMARY KEY)")
        with pytest.raises(sqlite3.IntegrityError), SqliteTransaction(connection):
            with observation.bind():
                connection.run_many("INSERT INTO facts VALUES (?)", ((1,), (2,), (1,), (3,)))
        assert observation.snapshot().statements == 3
        assert connection.run("SELECT COUNT(*) FROM facts").require_one()[0] == 0


def test_batching_snapshots_reused_mutable_parameter_rows():
    def parameters():
        row = [0]
        for value in range(5):
            row[0] = value
            yield row

    with SqliteSession(":memory:", sqlite_policy=SqliteIoPolicy(0.01, 3, 2, "PASSIVE")) as connection:
        connection.run("CREATE TABLE facts(value INTEGER PRIMARY KEY)")
        with SqliteTransaction(connection):
            connection.run_many("INSERT INTO facts VALUES (?)", parameters())
        assert [row[0] for row in connection.run("SELECT value FROM facts ORDER BY value")] == list(range(5))
