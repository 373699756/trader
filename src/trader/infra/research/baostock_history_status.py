"""Read BaoStock checkpoint progress before an immutable manifest exists."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from trader.application.research.baostock_history_runtime import BaoStockRuntimeStatus
from trader.domain.research.baostock_daily import BaoStockDailySpec
from trader.infra.research.baostock_daily import BaoStockDailyArtifactConflictError, SQLiteBaoStockDailyShard


def inspect_baostock_checkpoints(root: Path, *, sessions: int) -> BaoStockRuntimeStatus:
    try:
        spec = BaoStockDailySpec(sessions=sessions)
        paths = tuple(sorted((root / "shards").glob("*.sqlite3")))
        if not paths:
            return BaoStockRuntimeStatus(sessions=sessions)
        shards = tuple(SQLiteBaoStockDailyShard(path) for path in paths)
        context = shards[0].context(spec)
        if context is None:
            return BaoStockRuntimeStatus(sessions=sessions)
        if any(not shard.context_matches(spec, context) for shard in shards[1:]):
            raise BaoStockDailyArtifactConflictError("BaoStock resume shard contexts do not match")
        expected = {item.code: len(context.calendar.expected_dates(item)) for item in context.universe}
        checkpoints = tuple(shard.checkpoint(spec, expected_records_by_code=expected) for shard in shards)
        completed = frozenset(code for item in checkpoints for code in item.completed_codes)
        ready = frozenset(code for item in checkpoints for code in item.ready_codes)
        failed = frozenset(code for item in checkpoints for code, _reason in item.failures)
        return BaoStockRuntimeStatus(
            state="completed_with_failures" if completed or failed else "not_started",
            sessions=sessions,
            shard_count=len(checkpoints),
            universe_count=len(context.universe),
            completed_codes=len(completed),
            training_ready_codes=len(ready),
            failed_codes=len(failed),
            failure_reasons=("incomplete_codes",) if completed or failed else (),
        )
    except (BaoStockDailyArtifactConflictError, OSError, ValueError, sqlite3.DatabaseError):
        return BaoStockRuntimeStatus(
            state="failed",
            sessions=sessions,
            failure_reasons=("checkpoint_invalid",),
        )


__all__ = ["inspect_baostock_checkpoints"]
