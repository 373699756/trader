"""Writers and restarted readers share per-file verification without rehashing bodies."""

import json
from pathlib import Path

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.infra import sqlite_publication as publication
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_verification_cache import SqliteVerificationCache


def _create(path: Path) -> None:
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE prices(price REAL)")
        connection.run("INSERT INTO prices VALUES(10)")


def test_construction_never_creates_runtime_files(tmp_path):
    directory = tmp_path / "runtime" / "sqlite-verification"
    SqliteVerificationCache(directory)
    assert not directory.exists()


def test_publish_records_proof_for_restart_without_another_database_hash(tmp_path, monkeypatch):
    paths = tuple(tmp_path / f"prices_{year}.sqlite3" for year in (2025, 2026))
    directory = tmp_path / "runtime" / "sqlite-verification"
    cache = SqliteVerificationCache(directory)
    for path in paths:
        _create(path)
        publication.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture(), verification_cache=cache)

    def forbidden(*_args):
        pytest.fail("publication already computed the unchanged annual file's hash")

    monkeypatch.setattr(publication, "_digest", forbidden)
    restarted = SqliteVerificationCache(directory)
    for path in paths:
        publication.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture(), verification_cache=restarted)
    assert len(tuple(directory.glob("*.json"))) == 2


def test_sync_rehashes_only_changed_year_and_startup_reuses_both(tmp_path, monkeypatch):
    first, second = (tmp_path / f"prices_{year}.sqlite3" for year in (2025, 2026))
    directory = tmp_path / "runtime"
    cache = SqliteVerificationCache(directory)
    for path in (first, second):
        _create(path)
        publication.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture(), verification_cache=cache)
    with SqliteSession(second) as connection, SqliteTransaction(connection):
        connection.run("UPDATE prices SET price=11")
    original = publication._digest
    calls = []

    def tracked(path, size, check):
        calls.append(path)
        return original(path, size, check)

    monkeypatch.setattr(publication, "_digest", tracked)
    restarted_writer = SqliteVerificationCache(directory)
    for path in (first, second):
        publication.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture(), verification_cache=restarted_writer)
    assert calls == [second]
    calls.clear()
    restarted_reader = SqliteVerificationCache(directory)
    for path in (first, second):
        publication.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture(), verification_cache=restarted_reader)
    assert calls == []


def test_wal_update_does_not_rehash_main_database_at_publish_or_restart(tmp_path, monkeypatch):
    path = tmp_path / "daily_window.sqlite3"
    directory = tmp_path / "runtime"
    cache = SqliteVerificationCache(directory)
    with SqliteSession(path, SqliteConnectionOptions(journal_mode="WAL")) as writer:
        with SqliteTransaction(writer):
            writer.run("CREATE TABLE prices(price REAL)")
            writer.run("INSERT INTO prices VALUES(10)")
        publication.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture(), verification_cache=cache)
        with SqliteTransaction(writer):
            writer.run("UPDATE prices SET price=11")
        original = publication._digest
        calls = []

        def tracked(source, size, check):
            calls.append(source)
            return original(source, size, check)

        monkeypatch.setattr(publication, "_digest", tracked)
        publication.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture(), verification_cache=cache)
        assert calls == [Path(str(path) + "-wal")]
        calls.clear()
        publication.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture(), verification_cache=SqliteVerificationCache(directory))
        assert calls == []


def test_cache_cannot_hide_unpublished_data_change(tmp_path):
    path = tmp_path / "prices_2026.sqlite3"
    directory = tmp_path / "runtime"
    _create(path)
    publication.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture(), verification_cache=SqliteVerificationCache(directory))
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE prices SET price=999")
    with pytest.raises(publication.SqlitePublicationError, match="checksum_mismatch"):
        publication.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture(), verification_cache=SqliteVerificationCache(directory))


def test_corrupt_cache_does_not_silently_trigger_a_full_rehash(tmp_path, monkeypatch):
    path = tmp_path / "prices_2026.sqlite3"
    directory = tmp_path / "runtime"
    _create(path)
    publication.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture(), verification_cache=SqliteVerificationCache(directory))
    cache_file, = directory.glob("*.json")
    document = json.loads(cache_file.read_text())
    document["content_hash"] = "0" * 64
    cache_file.write_text(json.dumps(document))

    def forbidden(*_args):
        pytest.fail("corrupt cache must not hide a full database rehash")

    monkeypatch.setattr(publication, "_digest", forbidden)
    with pytest.raises(publication.SqlitePublicationError, match="verification_cache_invalid"):
        publication.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture(), verification_cache=SqliteVerificationCache(directory))


def test_cache_write_failure_preserves_successful_publication_and_in_memory_proof(tmp_path, monkeypatch, caplog):
    path = tmp_path / "prices_2026.sqlite3"
    _create(path)
    cache = SqliteVerificationCache(tmp_path / "runtime")

    def unavailable(*_args):
        raise OSError("fixture_cache_write_failed")

    monkeypatch.setattr("trader.infra.sqlite_verification_cache.AtomicWriteJson", unavailable)
    fingerprint = publication.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture(), verification_cache=cache)

    def forbidden(*_args):
        pytest.fail("cache persistence failure must not discard the already computed file hash")

    monkeypatch.setattr(publication, "_digest", forbidden)
    assert publication.VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture(), verification_cache=cache).fingerprint == fingerprint
    assert len([record for record in caplog.records if "sqlite_verification_cache_write_failed" in record.message]) == 1
