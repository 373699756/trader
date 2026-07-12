import sqlite3

from stock_analyzer.sqlite_support import open_sqlite, sqlite_transaction


def test_open_sqlite_applies_shared_pragmas(tmp_path):
    connection = open_sqlite(str(tmp_path / "shared.sqlite3"))
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        connection.close()


def test_sqlite_transaction_commits_and_supports_row_factory(tmp_path):
    db_path = str(tmp_path / "transaction.sqlite3")
    with sqlite_transaction(db_path, row_factory=sqlite3.Row) as connection:
        connection.execute("CREATE TABLE samples (value TEXT NOT NULL)")
        connection.execute("INSERT INTO samples VALUES ('ok')")

    with sqlite_transaction(db_path, row_factory=sqlite3.Row) as connection:
        row = connection.execute("SELECT value FROM samples").fetchone()

    assert row["value"] == "ok"
