"""Publication checks operate on files, including WAL, without scanning table rows."""

from pathlib import Path
import sqlite3
import json

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.infra import sqlite_publication as module
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction


def _create(path: Path) -> None:
    with SqliteSession(path, sqlite_policy=SqlitePolicyFixture()) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE prices(code TEXT PRIMARY KEY,price REAL)")
        connection.run("INSERT INTO prices VALUES('600001',10)")
        connection.run("PRAGMA user_version=2")


def test_publication_and_load_never_scan_business_rows(tmp_path, monkeypatch):
    path = tmp_path / "prices.sqlite3"
    _create(path)
    statements = []
    run = SqliteConnection.run

    def tracked(connection, sql, parameters=()):
        statements.append(sql)
        assert "prices" not in sql.lower()
        assert "integrity_check" not in sql.lower() and "quick_check" not in sql.lower()
        return run(connection, sql, parameters)

    monkeypatch.setattr(SqliteConnection, "run", tracked)
    published = module.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    loaded = module.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    assert loaded.fingerprint == published
    assert statements and all("sqlite_master" in statement for statement in statements)


def test_file_tampering_invalidates_previous_verification(tmp_path):
    path = tmp_path / "prices.sqlite3"
    _create(path)
    module.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    previous = module.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE prices SET price=11")
    with pytest.raises(module.SqlitePublicationError, match="checksum_mismatch"):
        module.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture(), previous=previous)
    module.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    assert module.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture()).fingerprint != previous.fingerprint


def test_receipts_require_current_fields_and_correct_content_digest(tmp_path):
    path = tmp_path / "prices.sqlite3"
    _create(path)
    fingerprint = module.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    receipt = Path(f"{path}.sha256")
    payload = json.loads(receipt.read_text())
    assert "schema_version" not in payload
    payload["schema_version"] = 999
    receipt.write_text(json.dumps(payload))
    with pytest.raises(module.SqlitePublicationError, match="receipt_invalid"):
        module.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    payload.pop("schema_version")
    receipt.write_text(json.dumps(payload))
    assert module.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture()).fingerprint == fingerprint
    payload["database_sha256"] = "0" * 64
    receipt.write_text(json.dumps(payload))
    with pytest.raises(module.SqlitePublicationError, match="checksum_mismatch"):
        module.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture())


def test_unchanged_load_uses_caller_owned_verification(tmp_path, monkeypatch):
    path = tmp_path / "prices.sqlite3"
    _create(path)
    module.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    previous = module.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture())

    def forbidden(*args):
        raise AssertionError("unchanged file must not be hashed again")

    monkeypatch.setattr(module, "_digest", forbidden)
    assert module.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture(), previous=previous) is previous


def test_wal_changes_are_checked_even_when_main_file_is_unchanged(tmp_path):
    path = tmp_path / "prices.sqlite3"
    with SqliteSession(path, SqliteConnectionOptions(journal_mode="WAL")) as writer:
        with SqliteTransaction(writer):
            writer.run("CREATE TABLE prices(price REAL)")
            writer.run("INSERT INTO prices VALUES(10)")
            writer.run("PRAGMA user_version=2")
        main_bytes = path.read_bytes()
        module.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture())
        previous = module.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture())
        assert previous.fingerprint.wal_size > 0
        with SqliteTransaction(writer):
            writer.run("UPDATE prices SET price=11")
        assert path.read_bytes() == main_bytes
        with pytest.raises(module.SqlitePublicationError, match="checksum_mismatch"):
            module.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture(), previous=previous)


def test_incomplete_or_cancelled_publication_never_creates_a_receipt(tmp_path):
    path = tmp_path / "prices.sqlite3"
    _create(path)
    with pytest.raises(module.SqlitePublicationError, match="file_missing"):
        module.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture())

    def cancelled():
        raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError):
        module.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture(), check=cancelled)
    assert not Path(f"{path}.sha256").exists()


def test_non_sqlite_replacement_reports_publication_error_with_cause(tmp_path):
    path = tmp_path / "prices.sqlite3"
    _create(path)
    module.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    previous = module.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(b"x" * path.stat().st_size)
    replacement.replace(path)

    with pytest.raises(module.SqlitePublicationError, match="database_unreadable") as raised:
        module.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture(), previous=previous)

    assert isinstance(raised.value.__cause__, sqlite3.DatabaseError)
