"""History failure diagnostics keep SQLite codes without leaking payloads."""

import sqlite3

from trader.download.infra.history_sync_failure import HistoryFailureDetail


def test_sqlite_failure_detail_keeps_safe_error_name_without_message() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        try:
            connection.execute("SELECT * FROM missing_private_table")
        except sqlite3.OperationalError as exc:
            detail = HistoryFailureDetail(exc)
        else:
            raise AssertionError("missing table should fail")
    finally:
        connection.close()
    assert "SQLITE_ERROR" in detail
    assert "missing_private_table" not in detail


def test_sqlite_failure_detail_uses_numeric_code_if_name_is_unavailable() -> None:
    error = sqlite3.OperationalError("private error text")
    error.sqlite_errorcode = 5
    assert HistoryFailureDetail(error) == "OperationalError:SQLITE_CODE_5"
