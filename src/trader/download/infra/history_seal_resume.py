"""Durable resume state for unpublished monthly history sealing."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from trader.download.domain.history_control import HistorySnapshotPartition
from trader.download.domain.history_sync import HistorySyncProgress, HistorySyncProgressPort
from trader.download.infra.history_month_partition import (
    HistoryMonthPartitionError,
    SQLiteHistoryMonthPartitionRepository,
)
from trader.download.infra.history_tencent_stage import HistorySealedMonthCheckpoint, HistoryTencentStage


def recover_sealed_months(
    root: Path,
    pending_paths: Mapping[tuple[int, int], Path],
    stage: HistoryTencentStage,
    progress: HistorySyncProgressPort | None = None,
) -> dict[tuple[int, int], HistorySnapshotPartition]:
    references: dict[tuple[int, int], HistorySnapshotPartition] = {}
    for (year, month), source in sorted(pending_paths.items()):
        destination = root / "partitions" / f"{year:04d}" / f"{month:02d}.sqlite3"
        checkpoint = stage.sealed_month(year, month)
        if checkpoint is not None and checkpoint.matches(destination):
            references[(year, month)] = checkpoint.reference
            continue
        if checkpoint is not None:
            # Publication rollback may restore the old destination while the
            # already sealed bytes survive in pending. Retain their proof.
            continue
        try:
            completed = source.is_file() and destination.is_file() and os.path.samefile(source, destination)
        except OSError:
            completed = False
        if not completed:
            continue
        remove_sqlite_files(source.with_name(f".{month:02d}.seal.sqlite3"))
        reference = SQLiteHistoryMonthPartitionRepository(destination, year, month).recover_completed_seal()
        stage.save_sealed_month(year, month, HistorySealedMonthCheckpoint.capture(destination, reference))
        references[(year, month)] = reference
    if references and progress is not None:
        last_path = Path(references[max(references)].relative_path)
        try:
            progress.publish(
                HistorySyncProgress(
                    "sealing_partitions",
                    "completed",
                    len(references),
                    len(pending_paths),
                    f"{last_path.parent.name}-{last_path.stem}",
                )
            )
        except OSError:
            pass
    return references


def promote_completed_candidate(
    stage: HistoryTencentStage, month: tuple[int, int], candidate: Path, destination: Path
) -> HistorySnapshotPartition | None:
    """Reuse a full seal only after proving the candidate byte identity."""
    checkpoint = stage.sealed_month(*month)
    if checkpoint is None:
        return None
    try:
        SQLiteHistoryMonthPartitionRepository.verify_published_bytes(candidate, checkpoint.reference)
    except HistoryMonthPartitionError:
        return None
    os.replace(candidate, destination)
    descriptor = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return checkpoint.reference


def record_sealed_month(
    stage: HistoryTencentStage,
    month: tuple[int, int],
    destination: Path,
    candidate: Path,
    reference: HistorySnapshotPartition,
) -> None:
    year, calendar_month = month
    remove_sqlite_files(candidate)
    stage.save_sealed_month(year, calendar_month, HistorySealedMonthCheckpoint.capture(destination, reference))


def remove_sqlite_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)


__all__ = ["promote_completed_candidate", "record_sealed_month", "recover_sealed_months", "remove_sqlite_files"]
