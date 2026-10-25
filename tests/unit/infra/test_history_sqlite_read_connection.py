"""A transient SQLite lock cannot abort a resumable history download."""

import sqlite3

import pytest

from trader.download.infra import history_sqlite_read_connection
from trader.download.infra.history_sync_failure import HistoryFailureDetail
from trader.infra import sqlite_connection
from trader.infra.sqlite_connection import SqliteSession


def test_read_ahead_is_read_only_and_cancellable_between_bounded_chunks(tmp_path):
    path = tmp_path / "pages.sqlite3"
    original = b"p" * (2 * 1024 * 1024 + 11)
    path.write_bytes(original)
    checks = []
    history_sqlite_read_connection.PrefetchHistoryPricePages(path, lambda: checks.append(True))
    assert len(checks) == 4 and path.read_bytes() == original
    checks.clear()

    def cancelled():
        checks.append(True)
        if len(checks) == 3:
            raise RuntimeError("history_preload_cancelled")

    with pytest.raises(RuntimeError, match="history_preload_cancelled"):
        history_sqlite_read_connection.PrefetchHistoryPricePages(path, cancelled)
    assert len(checks) == 3 and path.read_bytes() == original


def test_read_budget_interrupts_sqlite_before_first_row_and_removes_callback(tmp_path):
    calls = 0

    def deadline():
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise RuntimeError("history_preload_deadline")

    with SqliteSession(tmp_path / "bounded.sqlite3") as connection:
        with pytest.raises(RuntimeError, match="history_preload_deadline") as failure:
            with history_sqlite_read_connection.WatchHistoryRead(connection, deadline):
                connection.run(
                    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) "
                    "SELECT sum(x) FROM n"
                ).fetchone()
        assert isinstance(failure.value.__cause__, sqlite3.OperationalError)
        assert tuple(connection.run("SELECT 42").fetchone()) == (42,)
    assert calls == 3


@pytest.mark.parametrize("code,attempts", ((sqlite3.SQLITE_BUSY, 2), (sqlite3.SQLITE_IOERR, 1), (None, 2)))
def test_month_reader_retries_only_lock_contention_and_closes_failed_handle(tmp_path, monkeypatch, code, attempts):
    opened = []

    original_connect = sqlite_connection.sqlite3.connect

    class Connection:
        def __init__(self, *args, **kwargs):
            self.inner = original_connect(":memory:", isolation_level=None)
            self.closed = False

        def execute(self, sql):
            if sql == "PRAGMA cache_size=-8192" and len(opened) == 1:
                error = sqlite3.OperationalError("database is locked" if code is None else "private payload")
                if code is not None:
                    error.sqlite_errorcode = code
                    error.sqlite_errorname = "SQLITE_BUSY" if code == sqlite3.SQLITE_BUSY else "SQLITE_IOERR"
                raise error
            return self.inner.execute(sql)

        def close(self):
            self.closed = True
            self.inner.close()

        def __getattr__(self, name):
            return getattr(self.inner, name)

    def connect(*args, **kwargs):
        connection = Connection()
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite_connection.sqlite3, "connect", connect)
    monkeypatch.setattr(history_sqlite_read_connection.time, "sleep", lambda seconds: None)
    if code != sqlite3.SQLITE_IOERR:
        connection = history_sqlite_read_connection.OpenHistoryPriceReader(
            tmp_path / "month.sqlite3", statement_trace=None, mmap_bytes=0, activity=None
        )
        connection.close()
        assert opened[1].closed
    else:
        with pytest.raises(sqlite3.OperationalError) as failure:
            history_sqlite_read_connection.OpenHistoryPriceReader(
                tmp_path / "month.sqlite3", statement_trace=None, mmap_bytes=0, activity=None
            )
        assert "SQLITE_IOERR" in HistoryFailureDetail(failure.value)
    assert len(opened) == attempts
    assert opened[0].closed
