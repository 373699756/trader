"""Locked migration of root-level BaoStock checkpoints into bounded partitions."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from trader.application.research.baostock_daily import BaoStockShardContext
from trader.domain.research.baostock_daily import BaoStockDailySpec
from trader.infra.research.baostock_catalog import partition_name
from trader.infra.research.baostock_daily import BaoStockDailyArtifactConflictError, SQLiteBaoStockDailyShard
from trader.infra.research.baostock_daily_codec import encode_json

_LEGACY_DAILY_INTEGRITY_QUERIES: tuple[tuple[str, str], ...] = (
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


def legacy_daily_database_sha256(path: Path) -> str:
    """Read the retired daily-only partition hash solely to migrate old manifests."""
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = hashlib.sha256()
    with sqlite3.connect(path) as connection:
        for table, query in _LEGACY_DAILY_INTEGRITY_QUERIES:
            digest.update(table.encode("ascii"))
            digest.update(b"\0")
            for row in connection.execute(query):
                digest.update(encode_json(list(row)).encode("utf-8"))
                digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class ArchiveRecoveryAssessment:
    corrupt_partitions: tuple[Path, ...]
    metadata_reseal_required: bool


def assess_archive_recovery(
    root: Path,
    manifest_path: Path,
    catalog_path: Path,
    *,
    repairable_codes: frozenset[str] = frozenset(),
) -> ArchiveRecoveryAssessment:
    """Classify invalid archive parts without ever targeting paths outside the archive."""
    corrupt: list[Path] = []
    reseal = False
    resolved_root = root.resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        partitions = payload.get("partitions")
        if not isinstance(partitions, list):
            raise ValueError("partitions missing")
        quality_repair_codes = _quality_repair_codes(payload, repairable_codes)
        expected_catalog = payload.get("catalog_sha256")
        if (
            not isinstance(expected_catalog, str)
            or not catalog_path.exists()
            or _sha256_file(catalog_path) != expected_catalog
        ):
            reseal = True
        for item in partitions:
            path, action = _partition_recovery_action(root, resolved_root, item, quality_repair_codes)
            if action == "reseal":
                reseal = True
                continue
            if action == "replace" and path is not None:
                corrupt.append(path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        reseal = True
    return ArchiveRecoveryAssessment(tuple(corrupt), reseal or bool(corrupt))


def _quality_repair_codes(payload: object, repairable_codes: frozenset[str]) -> frozenset[str]:
    if not isinstance(payload, dict):
        return frozenset()
    audit = payload.get("audit")
    if not isinstance(audit, dict):
        return frozenset()
    failed_codes = audit.get("failed_codes")
    if not isinstance(failed_codes, list):
        return frozenset()
    return repairable_codes.intersection(item for item in failed_codes if isinstance(item, str))


def _partition_recovery_action(
    root: Path,
    resolved_root: Path,
    item: object,
    quality_repair_codes: frozenset[str],
) -> tuple[Path | None, Literal["keep", "reseal", "replace"]]:
    if not isinstance(item, dict):
        raise ValueError("partition descriptor invalid")
    relative = item.get("relative_path")
    expected = item.get("database_sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        raise ValueError("partition identity invalid")
    path = (root / relative).resolve()
    if resolved_root not in path.parents:
        return None, "reseal"
    if not path.is_file():
        return path, "replace"
    codes = item.get("codes")
    repair_quality = isinstance(codes, list) and bool(
        quality_repair_codes.intersection(code for code in codes if isinstance(code, str))
    )
    if _sha256_file(path) == expected:
        return path, "replace" if repair_quality else "keep"
    try:
        legacy_matches = legacy_daily_database_sha256(path) == expected
    except (OSError, sqlite3.DatabaseError):
        legacy_matches = False
    if not legacy_matches:
        return path, "replace"
    return (path, "replace") if repair_quality else (None, "reseal")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checkpoint_paths(root: Path) -> tuple[Path, ...]:
    """Return current partitions plus read-only root-level checkpoints."""
    current = tuple((root / "shards").glob("*.sqlite3"))
    legacy = tuple(root.glob("shard-*.sqlite3"))
    return tuple(sorted((*current, *legacy)))


def migrate_legacy_archive(root: Path, spec: BaoStockDailySpec, context: BaoStockShardContext) -> None:
    """Preserve and migrate root-level pre-partition checkpoints under the caller's run lock."""
    legacy_shards = tuple(sorted(root.glob("shard-*.sqlite3")))
    if not legacy_shards:
        return
    boards = {item.code: item.board for item in context.universe}
    recovery = root / "recovery" / f"legacy-{time.time_ns()}"
    for path in legacy_shards:
        source = SQLiteBaoStockDailyShard(path)
        snapshot = source.snapshot(spec)
        if not source.context_matches(spec, context):
            raise BaoStockDailyArtifactConflictError("BaoStock legacy shard context does not match the active run")
        ready = source.training_ready_codes(spec)
        for batch in snapshot.batches:
            board = boards.get(batch.code)
            if board is None:
                raise BaoStockDailyArtifactConflictError("BaoStock legacy code is absent from active universe")
            target = SQLiteBaoStockDailyShard(root / "shards" / partition_name(board, batch.code))
            target.initialize(
                spec,
                context.calendar,
                context.universe,
                context.source_versions,
                context.industry_intervals,
            )
            target.save_batch(spec, batch)
            if batch.code in ready:
                facts, intervals = source.read_training_facts(spec, batch.code)
                target.save_training_facts(spec, batch.code, facts, intervals)
    recovery.mkdir(parents=True, exist_ok=False)
    for path in legacy_shards:
        shutil.move(str(path), recovery / path.name)
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists():
                shutil.move(str(sidecar), recovery / sidecar.name)


__all__ = [
    "ArchiveRecoveryAssessment",
    "assess_archive_recovery",
    "checkpoint_paths",
    "legacy_daily_database_sha256",
    "migrate_legacy_archive",
]
