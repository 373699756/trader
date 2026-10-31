import sqlite3

import pytest

from trader.infra.sqlite_connection import OpenSqliteConnection, SqliteConnectionOptions, SqliteSession
from trader.infra.sqlite_observation import IndependentSqliteWork, SqliteObservation
from trader.infra.workers import BoundedExecutor, SubmitOrReject


def test_counts_reused_connections_and_worker_calls_without_context_leaks():
    observation = SqliteObservation()
    other = SqliteObservation()
    workers = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="sqlite-observation")
    workers.start()

    def query():
        with SqliteSession(":memory:") as connection:
            connection.run("SELECT 1").close()

    try:
        with SqliteSession(":memory:") as connection:
            connection.run("CREATE TABLE facts(value INTEGER)")
            with observation.bind():
                connection.run_many("INSERT INTO facts VALUES (?)", ((1,), (2,))).close()
                connection.observe_statements(None)
                connection.run("SELECT * FROM facts").fetchall()
                with other.bind():
                    SubmitOrReject(workers, query).result(timeout=2)
                SubmitOrReject(workers, query).result(timeout=2)
            SubmitOrReject(workers, query).result(timeout=2)
        assert observation.snapshot().connection_attempts == 1
        assert observation.snapshot().statements == 5  # reused statements + worker PRAGMA/SELECT
        assert other.snapshot().connection_attempts == 1
        assert other.snapshot().statements == 2
    finally:
        assert workers.stop().completed


def test_records_failed_connection_attempt(tmp_path):
    observation = SqliteObservation()
    with observation.bind(), pytest.raises(sqlite3.OperationalError):
        OpenSqliteConnection(tmp_path / "absent.sqlite3", SqliteConnectionOptions(read_only=True))
    assert observation.snapshot().connection_attempts == 1
    assert observation.snapshot().statements == 0


def test_cancelled_queue_and_detached_work_release_only_their_own_observation():
    from threading import Event

    release = Event()
    drained = Event()
    observation = SqliteObservation(on_drained=drained.set)
    workers = BoundedExecutor(worker_count=1, queue_capacity=2, thread_name_prefix="scope-cancel")
    workers.start()
    try:
        with observation.bind():
            running = SubmitOrReject(workers, release.wait, 3)
            pending = SubmitOrReject(workers, lambda: None)
            with IndependentSqliteWork():
                detached = SubmitOrReject(workers, lambda: None)
        assert observation.snapshot().worker_inflight == 2
        assert pending.cancel()
        assert observation.snapshot().worker_inflight == 1
        assert not drained.is_set()
        release.set()
        running.result(timeout=3)
        detached.result(timeout=3)
        assert drained.wait(2)
        assert observation.snapshot().worker_inflight == 0
        assert observation.snapshot().worker_peak == 2
    finally:
        release.set()
        workers.stop()
