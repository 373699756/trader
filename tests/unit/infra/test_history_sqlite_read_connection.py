"""A transient SQLite lock cannot abort a resumable history download."""

import sqlite3

import pytest

from trader.download.infra import history_sqlite_read_connection
from trader.download.infra.history_sync_failure import HistoryFailureDetail


@pytest.mark.parametrize("code,attempts", ((sqlite3.SQLITE_BUSY, 2), (sqlite3.SQLITE_IOERR, 1), (None, 2)))
def test_month_reader_retries_only_lock_contention_and_closes_failed_handle(tmp_path, monkeypatch, code, attempts):
    original_connect = sqlite3.connect
    opened = []

    class Connection:
        def __init__(self):
            self.inner = original_connect(":memory:")
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

    monkeypatch.setattr(history_sqlite_read_connection.sqlite3, "connect", connect)
    monkeypatch.setattr(history_sqlite_read_connection.time, "sleep", lambda seconds: None)
    if code != sqlite3.SQLITE_IOERR:
        connection = history_sqlite_read_connection.OpenHistoryMonthReader(
            tmp_path / "month.sqlite3", statement_trace=None, mmap_bytes=0, activity=None
        )
        connection.close()
        assert opened[1].closed
    else:
        with pytest.raises(sqlite3.OperationalError) as failure:
            history_sqlite_read_connection.OpenHistoryMonthReader(
                tmp_path / "month.sqlite3", statement_trace=None, mmap_bytes=0, activity=None
            )
        assert "SQLITE_IOERR" in HistoryFailureDetail(failure.value)
    assert len(opened) == attempts
    assert opened[0].closed
