"""One file-level checksum boundary for published SQLite databases and their WAL."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from trader.infra.atomic_files import AtomicReadJson, AtomicWriteJson
from trader.infra.sqlite_connection import OpenSqliteConnection, SqliteConnectionOptions
from trader.infra.sqlite_policy import SqliteIoPolicy

_CHUNK_BYTES = 4 * 1024 * 1024
_SQLITE_HEADER = b"SQLite format 3\x00"


class SqlitePublicationError(RuntimeError):
    """A publication is missing, changed, or cannot be read as the expected SQLite file."""


@dataclass(frozen=True)
class SqliteFileStamp:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class SqlitePublicationFingerprint:
    schema_version: int
    database_size: int
    database_sha256: str
    wal_size: int
    wal_sha256: str | None


@dataclass(frozen=True)
class VerifiedSqlitePublication:
    database: SqliteFileStamp
    wal: SqliteFileStamp | None
    receipt: SqliteFileStamp
    fingerprint: SqlitePublicationFingerprint


def _stamp(path: Path, *, optional: bool = False) -> SqliteFileStamp | None:
    if path.is_symlink():
        raise SqlitePublicationError("sqlite_publication_path_unsafe")
    try:
        value = path.stat()
    except FileNotFoundError:
        if optional:
            return None
        raise SqlitePublicationError("sqlite_publication_file_missing") from None
    if not path.is_file():
        raise SqlitePublicationError("sqlite_publication_path_unsafe")
    if optional and value.st_size == 0:
        return None
    return SqliteFileStamp(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _digest(path: Path, size: int, check: Callable[[], None] | None) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as source:
        buffer = bytearray(min(_CHUNK_BYTES, size))
        remaining = size
        while remaining:
            if check is not None:
                check()
            count = source.readinto(memoryview(buffer)[:remaining])
            if not count:
                raise SqlitePublicationError("sqlite_publication_changed_during_read")
            digest.update(memoryview(buffer)[:count])
            remaining -= count
    return digest.hexdigest()


def _fingerprint(
    path: Path,
    schema_version: int,
    check: Callable[[], None] | None,
    sqlite_policy: SqliteIoPolicy,
) -> tuple[SqliteFileStamp, SqliteFileStamp | None, SqlitePublicationFingerprint]:
    if check is not None:
        check()
    with closing(OpenSqliteConnection(
        path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy,
    )) as connection:
        if connection.run("PRAGMA user_version").require_one()[0] != schema_version:
            raise SqlitePublicationError("sqlite_publication_schema_invalid")
    before = _stamp(path)
    if before is None:
        raise AssertionError("required SQLite file stamp missing")
    wal_path = Path(f"{path}-wal")
    wal = _stamp(wal_path, optional=True)
    with path.open("rb") as source:
        header = source.read(100)
    if (
        len(header) != 100
        or header[:16] != _SQLITE_HEADER
    ):
        raise SqlitePublicationError("sqlite_publication_header_invalid")
    value = SqlitePublicationFingerprint(
        schema_version,
        before.size,
        _digest(path, before.size, check),
        wal.size if wal is not None else 0,
        _digest(wal_path, wal.size, check) if wal is not None else None,
    )
    if before != _stamp(path) or wal != _stamp(wal_path, optional=True):
        raise SqlitePublicationError("sqlite_publication_changed_during_read")
    return before, wal, value


def _read_receipt(path: Path, schema_version: int) -> SqlitePublicationFingerprint:
    if _stamp(path) is None or path.stat().st_size > 4096:
        raise SqlitePublicationError("sqlite_publication_receipt_invalid")
    try:
        raw = AtomicReadJson(path)
    except (OSError, ValueError) as exc:
        raise SqlitePublicationError("sqlite_publication_receipt_invalid") from exc
    fields = {"schema_version", "database_size", "database_sha256", "wal_size", "wal_sha256"}
    if not isinstance(raw, dict) or set(raw) != fields:
        raise SqlitePublicationError("sqlite_publication_receipt_invalid")
    version, size, digest, wal_size, wal_digest = (raw[name] for name in (
        "schema_version", "database_size", "database_sha256", "wal_size", "wal_sha256"
    ))
    if (
        type(version) is not int or version != schema_version
        or type(size) is not int or size < 100
        or type(wal_size) is not int or wal_size < 0
        or not _valid_digest(digest)
        or (wal_size == 0 and wal_digest is not None)
        or (wal_size > 0 and not _valid_digest(wal_digest))
    ):
        raise SqlitePublicationError("sqlite_publication_receipt_invalid")
    return SqlitePublicationFingerprint(version, size, digest, wal_size, wal_digest)


def _valid_digest(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def PublishSqliteFile(
    path: Path,
    schema_version: int,
    *,
    sqlite_policy: SqliteIoPolicy,
    check: Callable[[], None] | None = None,
) -> SqlitePublicationFingerprint:
    """Seal closed writer output once; this never reads a business table or changes the database."""
    before, wal, value = _fingerprint(path, schema_version, check, sqlite_policy)
    receipt = Path(f"{path}.sha256")
    if receipt.is_symlink():
        raise SqlitePublicationError("sqlite_publication_path_unsafe")
    AtomicWriteJson(receipt, {
        "schema_version": value.schema_version,
        "database_size": value.database_size,
        "database_sha256": value.database_sha256,
        "wal_size": value.wal_size,
        "wal_sha256": value.wal_sha256,
    })
    if before != _stamp(path) or wal != _stamp(Path(f"{path}-wal"), optional=True):
        raise SqlitePublicationError("sqlite_publication_changed_during_publish")
    return value


def VerifySqliteFile(
    path: Path,
    schema_version: int,
    *,
    sqlite_policy: SqliteIoPolicy,
    previous: VerifiedSqlitePublication | None = None,
    check: Callable[[], None] | None = None,
) -> VerifiedSqlitePublication:
    """Verify a published file on load, reusing an unchanged caller-owned verification."""
    if check is not None:
        check()
    database = _stamp(path)
    receipt_path = Path(f"{path}.sha256")
    receipt = _stamp(receipt_path)
    wal = _stamp(Path(f"{path}-wal"), optional=True)
    if database is None or receipt is None:
        raise AssertionError("required publication file stamp missing")
    if previous is not None and (
        previous.database == database and previous.wal == wal and previous.receipt == receipt
        and previous.fingerprint.schema_version == schema_version
    ):
        return previous
    expected = _read_receipt(receipt_path, schema_version)
    actual_database, actual_wal, actual = _fingerprint(path, schema_version, check, sqlite_policy)
    if expected != actual or receipt != _stamp(receipt_path):
        raise SqlitePublicationError("sqlite_publication_checksum_mismatch")
    return VerifiedSqlitePublication(actual_database, actual_wal, receipt, actual)


__all__ = [
    "PublishSqliteFile",
    "SqliteFileStamp",
    "SqlitePublicationError",
    "SqlitePublicationFingerprint",
    "VerifiedSqlitePublication",
    "VerifySqliteFile",
]
