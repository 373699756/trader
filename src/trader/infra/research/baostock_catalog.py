"""Catalog and manifest assembly for partitioned BaoStock archives."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

from trader.domain.research.baostock_daily import (
    BaoStockBoard,
    BaoStockDailyManifest,
    BaoStockDailySpec,
    BaoStockPartitionRef,
    BaoStockSecurity,
)
from trader.infra.research.baostock_daily_codec import encode_json as _json
from trader.infra.research.baostock_daily_codec import json_object as _json_object
from trader.infra.research.baostock_daily_serialization import _decode_spec, _encode_security
from trader.infra.research.baostock_errors import BaoStockDailyArtifactConflictError


class _ShardPath(Protocol):
    @property
    def path(self) -> Path: ...


class _DailyIndex(Protocol):
    @property
    def codes(self) -> tuple[str, ...]: ...

    @property
    def row_count(self) -> int: ...

    @property
    def logical_records_hash(self) -> str: ...


def partition_ref(
    root: Path,
    shard: _ShardPath,
    index: _DailyIndex,
) -> BaoStockPartitionRef:
    try:
        relative = shard.path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("BaoStock partition must be inside the archive root") from exc
    stem = shard.path.stem
    if "-" not in stem:
        raise ValueError("BaoStock partition filename is invalid")
    board, prefix = stem.rsplit("-", 1)
    codes = index.codes
    checkpoint_database(shard.path)
    return BaoStockPartitionRef(
        relative,
        cast(BaoStockBoard, board),
        prefix,
        codes,
        index.row_count,
        index.logical_records_hash,
        daily_database_sha256(shard.path),
    )


def write_catalog(
    path: Path,
    references: tuple[BaoStockPartitionRef, ...],
    universe: tuple[BaoStockSecurity, ...],
    batch_hashes: Mapping[str, str],
) -> None:
    securities = {item.code: item for item in universe}
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE partitions (
                relative_path TEXT PRIMARY KEY,
                database_sha256 TEXT NOT NULL,
                logical_records_hash TEXT NOT NULL,
                row_count INTEGER NOT NULL
            );
            CREATE TABLE securities (
                code TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL,
                security_json TEXT NOT NULL,
                batch_hash TEXT NOT NULL,
                FOREIGN KEY(relative_path) REFERENCES partitions(relative_path)
            );
            """
        )
        for reference in references:
            connection.execute(
                "INSERT INTO partitions VALUES (?, ?, ?, ?)",
                (
                    reference.relative_path,
                    reference.database_sha256,
                    reference.logical_records_hash,
                    reference.row_count,
                ),
            )
            connection.executemany(
                "INSERT INTO securities VALUES (?, ?, ?, ?)",
                (
                    (
                        code,
                        reference.relative_path,
                        _json(_encode_security(securities[code])),
                        batch_hashes[code],
                    )
                    for code in reference.codes
                ),
            )


def manifest_spec(root: Path, manifest: BaoStockDailyManifest) -> BaoStockDailySpec:
    with sqlite3.connect(root / manifest.partitions[0].relative_path) as connection:
        row = connection.execute("SELECT spec_json FROM context WHERE singleton=1").fetchone()
    if row is None:
        raise BaoStockDailyArtifactConflictError("BaoStock partition context is missing")
    stored = _decode_spec(_json_object(row[0]))
    if stored.content_hash == manifest.spec_hash:
        return stored
    active = BaoStockDailySpec(sessions=stored.sessions)
    if (
        active.content_hash != manifest.spec_hash
        or stored.source_cutoff != active.source_cutoff
        or stored.production_authority != active.production_authority
        or stored.point_in_time_parity != active.point_in_time_parity
    ):
        raise BaoStockDailyArtifactConflictError("BaoStock partition spec hash mismatch")
    return active


def checkpoint_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


_DAILY_INTEGRITY_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "context",
        "SELECT spec_json, calendar_json, universe_json, versions_json, context_hash FROM context WHERE singleton=1",
    ),
    (
        "daily_cells",
        "SELECT code, trade_date, payload_json, content_hash FROM daily_cells ORDER BY code, trade_date",
    ),
    (
        "code_batches",
        "SELECT code, metadata_json, content_hash FROM code_batches ORDER BY code",
    ),
    (
        "completed_checkpoints",
        "SELECT code, state, error_code, batch_hash FROM checkpoints WHERE state='completed' ORDER BY code",
    ),
)


def daily_database_sha256(path: Path) -> str:
    """Hash only the immutable daily-owned rows in a mixed-purpose shard."""
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = hashlib.sha256()
    with sqlite3.connect(path) as connection:
        for table, query in _DAILY_INTEGRITY_QUERIES:
            digest.update(table.encode("ascii"))
            digest.update(b"\0")
            for row in connection.execute(query):
                digest.update(_json(list(row)).encode("utf-8"))
                digest.update(b"\n")
    return digest.hexdigest()


def write_immutable_json(path: Path, payload: dict[str, object], content_hash: str) -> None:
    document = dict(payload)
    document["content_hash"] = content_hash
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_json(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    except FileExistsError:
        raise BaoStockDailyArtifactConflictError("BaoStock merged manifest identity conflict") from None
    finally:
        temporary.unlink(missing_ok=True)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "checkpoint_database",
    "daily_database_sha256",
    "file_sha256",
    "manifest_spec",
    "partition_ref",
    "write_catalog",
    "write_immutable_json",
]
