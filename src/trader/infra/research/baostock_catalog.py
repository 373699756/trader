"""Catalog and manifest assembly for partitioned BaoStock archives."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import cast

from trader.application.research.baostock_daily import BaoStockShardContext
from trader.domain.research.baostock_daily import (
    BaoStockBoard,
    BaoStockCodeBatch,
    BaoStockDailyManifest,
    BaoStockDailySpec,
    BaoStockPartitionRef,
    BaoStockSecurity,
)
from trader.domain.research.h1_point_in_time import canonical_hash
from trader.infra.research.baostock_daily import (
    BaoStockDailyArtifactConflictError,
    BaoStockShardSnapshot,
    SQLiteBaoStockDailyShard,
)
from trader.infra.research.baostock_daily_codec import encode_json as _json
from trader.infra.research.baostock_daily_codec import json_object as _json_object
from trader.infra.research.baostock_daily_serialization import _decode_spec, _encode_security


def partition_ref(
    root: Path,
    spec: BaoStockDailySpec,
    shard: SQLiteBaoStockDailyShard,
    snapshot: BaoStockShardSnapshot,
) -> BaoStockPartitionRef:
    try:
        relative = shard.path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("BaoStock partition must be inside the archive root") from exc
    stem = shard.path.stem
    if "-" not in stem:
        raise ValueError("BaoStock partition filename is invalid")
    board, prefix = stem.rsplit("-", 1)
    codes = tuple(item.code for item in snapshot.batches)
    if frozenset(codes) != shard.training_ready_codes(spec):
        raise BaoStockDailyArtifactConflictError("BaoStock partition training facts are incomplete")
    checkpoint_database(shard.path)
    return BaoStockPartitionRef(
        relative,
        cast(BaoStockBoard, board),
        prefix,
        codes,
        sum(len(item.cells) for item in snapshot.batches),
        canonical_hash(
            (
                tuple((item.code, item.content_hash) for item in snapshot.batches),
                shard.training_facts_hash(spec),
            )
        ),
        file_sha256(shard.path),
    )


def write_catalog(
    path: Path,
    references: tuple[BaoStockPartitionRef, ...],
    universe: tuple[BaoStockSecurity, ...],
    batches: tuple[BaoStockCodeBatch, ...],
) -> None:
    securities = {item.code: item for item in universe}
    batch_hashes = {item.code: item.content_hash for item in batches}
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
    spec = _decode_spec(_json_object(row[0]))
    if spec.content_hash != manifest.spec_hash:
        raise BaoStockDailyArtifactConflictError("BaoStock partition spec hash mismatch")
    return spec


def common_context(spec: BaoStockDailySpec, snapshots: tuple[BaoStockShardSnapshot, ...]) -> BaoStockShardContext:
    first = snapshots[0].context
    for snapshot in snapshots:
        if (
            snapshot.spec.content_hash != spec.content_hash
            or snapshot.context.calendar != first.calendar
            or snapshot.context.universe != first.universe
            or snapshot.context.source_versions != first.source_versions
        ):
            raise BaoStockDailyArtifactConflictError("BaoStock shard contexts do not match")
    intervals = tuple(
        sorted(
            {item for snapshot in snapshots for item in snapshot.context.industry_intervals},
            key=lambda item: (item.code, item.effective_from),
        )
    )
    return BaoStockShardContext(first.calendar, first.universe, first.source_versions, intervals)


def merged_batches(snapshots: tuple[BaoStockShardSnapshot, ...]) -> tuple[BaoStockCodeBatch, ...]:
    batches: dict[str, BaoStockCodeBatch] = {}
    for snapshot in snapshots:
        for batch in snapshot.batches:
            previous = batches.get(batch.code)
            if previous is not None and previous.content_hash != batch.content_hash:
                raise BaoStockDailyArtifactConflictError("BaoStock duplicate shard code identity conflict")
            batches[batch.code] = batch
    return tuple(sorted(batches.values(), key=lambda item: item.code))


def checkpoint_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


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
    "common_context",
    "file_sha256",
    "manifest_spec",
    "merged_batches",
    "partition_ref",
    "write_catalog",
    "write_immutable_json",
]
