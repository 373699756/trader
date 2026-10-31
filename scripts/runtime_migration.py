"""Explicit legacy-format decoding for isolated data migration; never an active read path."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import hashlib
import re
import shutil
from pathlib import Path

from trader.infra.sqlite_connection import (
    CheckSqliteIntegrity,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)

_READ = SqliteConnectionOptions(read_only=True, immutable=True)


def CopyFile(source: Path, target: Path) -> None:
    if not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if FileDigest(source) != FileDigest(target):
            raise ValueError("migration_file_identity_conflict")
        return
    shutil.copyfile(source, target)
    if FileDigest(source) != FileDigest(target):
        raise ValueError("migration_copy_digest_mismatch")


def FileDigest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def ValidateDatabase(path: Path, *, sqlite_policy: SqliteIoPolicy) -> None:
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("migration_requires_stopped_checkpointed_source")
    with SqliteSession(path, _READ, sqlite_policy=sqlite_policy) as connection:
        CheckSqliteIntegrity(connection)
        if connection.run("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("migration_foreign_key_invalid")


def MergeDatabase(source: Path, target: Path, *, sqlite_policy: SqliteIoPolicy) -> None:
    """Union unchanged table formats; duplicate keys must have identical entire rows."""
    if not source.is_file():
        return
    ValidateDatabase(source, sqlite_policy=sqlite_policy)
    if not target.exists():
        CopyFile(source, target)
        return
    with SqliteSession(source, _READ, sqlite_policy=sqlite_policy) as reader, SqliteSession(target, sqlite_policy=sqlite_policy) as writer:
        source_schema = tuple(
            tuple(row)
            for row in reader.run(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
            )
        )
        target_schema = tuple(
            tuple(row)
            for row in writer.run(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
            )
        )
        if source_schema != target_schema:
            raise ValueError("migration_database_schema_conflict")
        with SqliteTransaction(writer, immediate=True):
            for kind, table, _sql in source_schema:
                if kind == "table":
                    _merge_rows(reader, writer, str(table))
        CheckSqliteIntegrity(writer)


def _identifier(value: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
        raise ValueError("migration_identifier_invalid")
    return '"' + value + '"'


def _merge_rows(reader: SqliteConnection, writer: SqliteConnection, table: str) -> None:
    name = _identifier(table)
    columns = tuple(str(row[1]) for row in reader.run(f"PRAGMA table_info({name})"))
    for row in reader.run(f"SELECT * FROM {name}"):
        _insert_exact(writer, table, columns, tuple(row))


def _insert_exact(connection: SqliteConnection, table: str, columns: tuple[str, ...], row: tuple[object, ...]) -> None:
    name = _identifier(table)
    names = ",".join(_identifier(column) for column in columns)
    cursor = connection.run(f"INSERT OR IGNORE INTO {name} ({names}) VALUES ({','.join('?' for _ in row)})", row)
    if cursor.rowcount == 0:
        where = " AND ".join(f"{_identifier(column)} IS ?" for column in columns)
        if connection.run(f"SELECT 1 FROM {name} WHERE {where}", row).fetchone() is None:
            raise ValueError("migration_database_identity_conflict")


__all__ = [
    "CopyFile",
    "FileDigest",
    "ValidateDatabase",
    "MergeDatabase",
]
