"""Prepared full-file fingerprints remain private until guarded publication."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.infra import sqlite_publication as publication
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction


def _create(path: Path) -> None:
    with SqliteSession(path, sqlite_policy=SqlitePolicyFixture()) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE prices(code TEXT PRIMARY KEY,price REAL)")
        connection.run("INSERT INTO prices VALUES('600001',10)")
        connection.run("PRAGMA user_version=2")


def _freeze_stamps(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Reproduce a coarse filesystem tick without sleeps or raw database edits."""
    stamp = publication._stamp
    wal_path = Path(f"{path}-wal")
    database, wal = stamp(path), stamp(wal_path, optional=True)

    def fixed(candidate: Path, *, optional: bool = False) -> publication.SqliteFileStamp | None:
        if candidate == path:
            return database
        if candidate == wal_path:
            return wal
        return stamp(candidate, optional=optional)

    monkeypatch.setattr(publication, "_stamp", fixed)


def test_prepare_does_not_create_receipt_and_final_result_matches_baseline(tmp_path: Path) -> None:
    path = tmp_path / "prices.sqlite3"
    _create(path)
    database = path.read_bytes()
    receipt = Path(f"{path}.sha256")
    prepared = publication.PrepareSqliteFilePublication(path, 2, sqlite_policy=SqlitePolicyFixture())
    assert not receipt.exists()
    assert path.read_bytes() == database
    actual = publication.PublishPreparedSqliteFile(prepared)
    receipt_bytes = receipt.read_bytes()
    assert actual == prepared.fingerprint
    assert actual == publication.PublishSqliteFile(path, 2, sqlite_policy=SqlitePolicyFixture())
    assert receipt.read_bytes() == receipt_bytes
    assert publication.VerifySqliteFile(path, 2, sqlite_policy=SqlitePolicyFixture()).fingerprint == actual


@pytest.mark.parametrize("existing_receipt", [False, True])
def test_changed_main_file_between_prepare_and_finalize_never_replaces_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_receipt: bool
) -> None:
    path = tmp_path / "prices.sqlite3"
    _create(path)
    receipt = Path(f"{path}.sha256")
    if existing_receipt:
        publication.PublishSqliteFile(path, 2, sqlite_policy=SqlitePolicyFixture())
    previous = receipt.read_bytes() if existing_receipt else None
    prepared = publication.PrepareSqliteFilePublication(path, 2, sqlite_policy=SqlitePolicyFixture())
    _freeze_stamps(monkeypatch, path)
    with SqliteSession(path, sqlite_policy=SqlitePolicyFixture()) as connection, SqliteTransaction(connection):
        connection.run("UPDATE prices SET price=11")
    assert path.stat().st_size == prepared.database.size
    assert publication._stamp(path) == prepared.database
    assert path.read_bytes()[:100] != prepared.database_header
    with pytest.raises(publication.SqlitePublicationError, match="changed_before_publish"):
        publication.PublishPreparedSqliteFile(prepared)
    assert (receipt.read_bytes() if receipt.exists() else None) == previous


@pytest.mark.parametrize("existing_receipt", [False, True])
def test_changed_wal_with_unchanged_main_rejects_prepared_receipt(
    tmp_path: Path, existing_receipt: bool
) -> None:
    path = tmp_path / "prices.sqlite3"
    receipt = Path(f"{path}.sha256")
    with SqliteSession(
        path, SqliteConnectionOptions(journal_mode="WAL"), sqlite_policy=SqlitePolicyFixture()
    ) as writer:
        with SqliteTransaction(writer):
            writer.run("CREATE TABLE prices(price REAL)")
            writer.run("INSERT INTO prices VALUES(10)")
            writer.run("PRAGMA user_version=2")
        if existing_receipt:
            publication.PublishSqliteFile(path, 2, sqlite_policy=SqlitePolicyFixture())
        previous = receipt.read_bytes() if existing_receipt else None
        prepared = publication.PrepareSqliteFilePublication(path, 2, sqlite_policy=SqlitePolicyFixture())
        main_bytes = path.read_bytes()
        assert prepared.fingerprint.wal_size > 0
        with SqliteTransaction(writer):
            writer.run("UPDATE prices SET price=11")
        assert path.read_bytes() == main_bytes
        with pytest.raises(publication.SqlitePublicationError, match="changed_before_publish"):
            publication.PublishPreparedSqliteFile(prepared)
        assert (receipt.read_bytes() if receipt.exists() else None) == previous


@pytest.mark.parametrize("existing_receipt", [False, True])
@pytest.mark.parametrize("cancel_phase", ["prepare", "finalize"])
def test_cancelled_preparation_or_finalization_preserves_previous_receipt(
    tmp_path: Path, existing_receipt: bool, cancel_phase: str
) -> None:
    path = tmp_path / "prices.sqlite3"
    _create(path)
    receipt = Path(f"{path}.sha256")
    if existing_receipt:
        publication.PublishSqliteFile(path, 2, sqlite_policy=SqlitePolicyFixture())
    previous = receipt.read_bytes() if existing_receipt else None

    def cancel() -> None:
        raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError):
        prepared = publication.PrepareSqliteFilePublication(
            path, 2, sqlite_policy=SqlitePolicyFixture(), check=cancel if cancel_phase == "prepare" else None
        )
        publication.PublishPreparedSqliteFile(prepared, check=cancel)
    assert (receipt.read_bytes() if receipt.exists() else None) == previous


def test_file_change_from_final_cancellation_check_is_detected_before_receipt_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "prices.sqlite3"
    _create(path)
    prepared = publication.PrepareSqliteFilePublication(path, 2, sqlite_policy=SqlitePolicyFixture())
    _freeze_stamps(monkeypatch, path)

    def mutate() -> None:
        with SqliteSession(path, sqlite_policy=SqlitePolicyFixture()) as connection, SqliteTransaction(connection):
            connection.run("UPDATE prices SET price=12")

    with pytest.raises(publication.SqlitePublicationError, match="changed_before_publish"):
        publication.PublishPreparedSqliteFile(prepared, check=mutate)
    assert not Path(f"{path}.sha256").exists()


def test_normal_wal_generation_reuse_is_detected_with_fixed_stamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "prices.sqlite3"
    with SqliteSession(
        path, SqliteConnectionOptions(journal_mode="WAL"), sqlite_policy=SqlitePolicyFixture()
    ) as writer:
        with SqliteTransaction(writer):
            writer.run("CREATE TABLE prices(price REAL)")
            writer.run("INSERT INTO prices VALUES(10)")
            writer.run("PRAGMA user_version=2")
        writer.run("PRAGMA wal_checkpoint(RESTART)").fetchall()
        prepared = publication.PrepareSqliteFilePublication(path, 2, sqlite_policy=SqlitePolicyFixture())
        main_bytes = path.read_bytes()
        _freeze_stamps(monkeypatch, path)
        with SqliteTransaction(writer):
            writer.run("UPDATE prices SET price=11")
        assert path.read_bytes() == main_bytes
        wal_path = Path(f"{path}-wal")
        assert wal_path.stat().st_size == prepared.fingerprint.wal_size
        assert wal_path.read_bytes()[:32] != prepared.wal_header
        with pytest.raises(publication.SqlitePublicationError, match="changed_before_publish"):
            publication.PublishPreparedSqliteFile(prepared)
        assert not Path(f"{path}.sha256").exists()


def test_prepare_rejects_sqlite_update_during_digest_with_fixed_stamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "prices.sqlite3"
    _create(path)
    _freeze_stamps(monkeypatch, path)
    digest = publication._digest
    calls = 0

    def mutate_after_digest(candidate: Path, size: int, check: Callable[[], None] | None) -> str:
        nonlocal calls
        value = digest(candidate, size, check)
        calls += 1
        with SqliteSession(path, sqlite_policy=SqlitePolicyFixture()) as connection, SqliteTransaction(connection):
            connection.run("UPDATE prices SET price=12")
        return value

    monkeypatch.setattr(publication, "_digest", mutate_after_digest)
    with pytest.raises(publication.SqlitePublicationError, match="changed_during_read"):
        publication.PrepareSqliteFilePublication(path, 2, sqlite_policy=SqlitePolicyFixture())
    assert calls == 1
    assert not Path(f"{path}.sha256").exists()


def test_verified_cache_checks_sqlite_headers_before_reusing_fixed_stamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "prices.sqlite3"
    _create(path)
    publication.PublishSqliteFile(path, 2, sqlite_policy=SqlitePolicyFixture())
    previous = publication.VerifySqliteFile(path, 2, sqlite_policy=SqlitePolicyFixture())
    _freeze_stamps(monkeypatch, path)
    with SqliteSession(path, sqlite_policy=SqlitePolicyFixture()) as connection, SqliteTransaction(connection):
        connection.run("UPDATE prices SET price=11")
    assert publication._stamp(path) == previous.database
    with pytest.raises(publication.SqlitePublicationError, match="checksum_mismatch"):
        publication.VerifySqliteFile(path, 2, sqlite_policy=SqlitePolicyFixture(), previous=previous)


def test_unchanged_verified_cache_does_not_repeat_digest_with_fixed_stamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "prices.sqlite3"
    _create(path)
    publication.PublishSqliteFile(path, 2, sqlite_policy=SqlitePolicyFixture())
    previous = publication.VerifySqliteFile(path, 2, sqlite_policy=SqlitePolicyFixture())
    _freeze_stamps(monkeypatch, path)

    def forbidden_digest(candidate: Path, size: int, check: Callable[[], None] | None) -> str:
        pytest.fail("unchanged verified files must not be hashed again")

    monkeypatch.setattr(publication, "_digest", forbidden_digest)
    assert publication.VerifySqliteFile(path, 2, sqlite_policy=SqlitePolicyFixture(), previous=previous) is previous
