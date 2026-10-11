"""Durable resume state for unpublished monthly history sealing."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from trader.download.domain.history_control import HistorySnapshotPartition
from trader.download.domain.history_sync import HistorySyncProgress, HistorySyncProgressPort
from trader.download.infra.history_month_partition import SQLiteHistoryMonthPartitionRepository
from trader.download.infra.history_tencent_stage import HistorySealedMonthCheckpoint, HistoryTencentStage


def recover_sealed_prefix(
    root: Path,
    pending_paths: Mapping[tuple[int, int], Path],
    stage: HistoryTencentStage,
    progress: HistorySyncProgressPort | None = None,
) -> tuple[HistorySnapshotPartition, ...]:
    references: list[HistorySnapshotPartition] = []
    for (year, month), source in sorted(pending_paths.items()):
        destination = root / "partitions" / f"{year:04d}" / f"{month:02d}.sqlite3"
        checkpoint = stage.sealed_month(year, month)
        if checkpoint is not None and checkpoint.matches(destination):
            references.append(checkpoint.reference)
            continue
        if checkpoint is not None:
            stage.discard_sealed_month(year, month)
            break
        try:
            completed = source.is_file() and destination.is_file() and os.path.samefile(source, destination)
        except OSError:
            completed = False
        if not completed:
            break
        remove_sqlite_files(source.with_name(f".{month:02d}.seal.sqlite3"))
        reference = SQLiteHistoryMonthPartitionRepository(destination, year, month).recover_completed_seal()
        stage.save_sealed_month(year, month, HistorySealedMonthCheckpoint.capture(destination, reference))
        references.append(reference)
    if references and progress is not None:
        last_path = Path(references[-1].relative_path)
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
    return tuple(references)


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


__all__ = ["record_sealed_month", "recover_sealed_prefix", "remove_sqlite_files"]
