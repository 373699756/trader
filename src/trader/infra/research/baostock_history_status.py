"""Read BaoStock checkpoint progress before an immutable manifest exists."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from trader.application.research.baostock_history_runtime import BaoStockRuntimeStatus
from trader.domain.research.baostock_daily import (
    BaoStockDailyManifest,
    BaoStockDailySpec,
    BaoStockTrainingDatasetManifest,
)
from trader.domain.research.historical_effective_facts import HistoricalEffectiveFactsAudit
from trader.infra.research.baostock_daily import SQLiteBaoStockDailyShard
from trader.infra.research.baostock_errors import BaoStockDailyArtifactConflictError


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
        checkpointed = completed | failed
        universe_codes = frozenset(item.code for item in context.universe)
        reason = "history_manifest_unavailable" if checkpointed == universe_codes else "incomplete_codes"
        return BaoStockRuntimeStatus(
            state="completed_with_failures" if completed or failed else "not_started",
            sessions=sessions,
            shard_count=len(checkpoints),
            universe_count=len(context.universe),
            completed_codes=len(completed),
            training_ready_codes=len(ready),
            failed_codes=len(failed),
            failure_reasons=(reason,) if completed or failed else (),
        )
    except (BaoStockDailyArtifactConflictError, OSError, ValueError, sqlite3.DatabaseError):
        return BaoStockRuntimeStatus(
            state="failed",
            sessions=sessions,
            failure_reasons=("checkpoint_invalid",),
        )


def manifest_runtime_status(
    root: Path,
    spec: BaoStockDailySpec,
    manifest: BaoStockDailyManifest,
    facts: HistoricalEffectiveFactsAudit,
    dataset: BaoStockTrainingDatasetManifest,
) -> BaoStockRuntimeStatus:
    completed = frozenset(code for partition in manifest.partitions for code in partition.codes)
    training_ready = frozenset(
        code
        for partition in manifest.partitions
        for code in SQLiteBaoStockDailyShard(root / partition.relative_path).training_ready_codes(spec)
    )
    if not training_ready <= completed:
        raise BaoStockDailyArtifactConflictError("BaoStock training-ready code is outside the daily manifest")
    audit = manifest.audit
    return BaoStockRuntimeStatus(
        state="completed" if audit.status == "coverage_ready" else "completed_with_failures",
        sessions=spec.sessions,
        shard_count=len(tuple((root / "shards").glob("*.sqlite3"))),
        universe_count=audit.universe_count,
        completed_codes=len(completed),
        training_ready_codes=len(training_ready),
        failed_codes=audit.universe_count - len(completed),
        manifest_hash=manifest.content_hash,
        coverage_status=audit.status,
        historical_effective_facts_status=facts.status,
        historical_effective_facts_hash=facts.content_hash,
        training_dataset_status=dataset.status,
        training_dataset_hash=dataset.content_hash,
        failure_reasons=audit.failure_reasons,
    )


__all__ = ["inspect_baostock_checkpoints", "manifest_runtime_status"]
