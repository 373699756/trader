"""Locked migration of root-level BaoStock checkpoints into bounded partitions."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from trader.application.research.baostock_daily import BaoStockShardContext
from trader.domain.research.baostock_daily import BaoStockDailySpec
from trader.infra.research.baostock_catalog import partition_name
from trader.infra.research.baostock_daily import BaoStockDailyArtifactConflictError, SQLiteBaoStockDailyShard


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


__all__ = ["checkpoint_paths", "migrate_legacy_archive"]
