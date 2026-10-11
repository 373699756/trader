"""Lazy copy-on-write monthly price batches; published partitions stay immutable."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from functools import partial
from itertools import chain
from pathlib import Path
from typing import Literal

from trader.download.domain.history_control import HistoryActiveSnapshot, HistorySnapshotPartition
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.history_sync import HistorySyncProgress, HistorySyncProgressPort
from trader.download.infra.history_archive_reader import route_history_months
from trader.download.infra.history_month_partition import SQLiteHistoryMonthPartitionRepository
from trader.download.infra.history_seal_resume import remove_sqlite_files
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.download.infra.history_tencent_stage import HistoryTencentStage


@dataclass(frozen=True)
class PendingHistoryPartitions:
    root: Path
    paths: dict[tuple[int, int], Path]
    active_by_month: dict[tuple[int, int], HistorySnapshotPartition]
    first_date: date


def partition_month(reference: HistorySnapshotPartition) -> tuple[int, int]:
    path = Path(reference.relative_path)
    return int(path.parts[1]), int(path.stem)


def backup_database(source: Path, destination: Path, *, activity: HistorySqliteActivity | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as input_connection,
        closing(sqlite3.connect(destination)) as output_connection,
    ):
        if activity is None:
            input_connection.backup(output_connection)
        else:
            input_connection.backup(output_connection, pages=256, progress=lambda *_: activity.tick())


def require_download_capacity(
    root: Path, active: HistoryActiveSnapshot | None, requests: Iterable[tuple[date, ...]], minimum_free_bytes: int
) -> int:
    """Conservative planning estimate, followed by per-month free-space guards."""
    rows = 0
    months: set[tuple[int, int]] = set()
    for dates in requests:
        rows += len(dates)
        months.update((day.year, day.month) for day in dates)
    copies = sum(
        (root / reference.relative_path).stat().st_size * 2
        for reference in (() if active is None else active.partitions)
        if partition_month(reference) in months
    )
    required = minimum_free_bytes + rows * 2048 + copies
    if minimum_free_bytes and shutil.disk_usage(root).free < required:
        raise RuntimeError("disk_space_insufficient")
    return required


def prepare_pending(
    root: Path,
    dates: tuple[date, ...],
    active: HistoryActiveSnapshot | None,
    resume: bool,
    activity: HistorySqliteActivity | None = None,
) -> PendingHistoryPartitions:
    active_by_month = {partition_month(item): item for item in active.partitions} if active is not None else {}
    paths: dict[tuple[int, int], Path] = {}
    for year, month in route_history_months(dates[0], dates[-1]):
        path = root / "partitions" / f"{year:04d}" / f".{month:02d}.pending.sqlite3"
        paths[(year, month)] = path
        if not resume:
            remove_sqlite_files(path)
    pending = PendingHistoryPartitions(root, paths, active_by_month, dates[0])
    if resume:
        required = paths.keys() - active_by_month.keys()
        if any(not paths[month].is_file() for month in required):
            raise RuntimeError("sync_checkpoint_incomplete")
    if active is None:
        for month_key in paths:
            if activity is not None:
                activity.tick()
            _ensure_pending(pending, month_key)
    else:
        first = (dates[0].year, dates[0].month)
        previous = active_by_month.get(first)
        if previous is not None and SQLiteHistoryMonthPartitionRepository(
            root / previous.relative_path, *first
        ).contains_before(dates[0]):
            _ensure_pending(pending, first)
        for month_key in paths.keys() - active_by_month.keys():
            _ensure_pending(pending, month_key)
    return pending


def _ensure_pending(pending: PendingHistoryPartitions, month: tuple[int, int]) -> Path:
    year, calendar_month = month
    path = pending.paths[month]
    if path.is_file():
        return path
    reference = pending.active_by_month.get(month)
    if reference is not None:
        required = (pending.root / reference.relative_path).stat().st_size * 2 + 64 * 1024**2
        if shutil.disk_usage(pending.root).free < required:
            raise RuntimeError("disk_space_insufficient")
        backup_database(pending.root / reference.relative_path, path)
    repository = SQLiteHistoryMonthPartitionRepository(path, year, calendar_month)
    repository.initialize()
    if month == (pending.first_date.year, pending.first_date.month):
        repository.prune_before(pending.first_date)
    return path


def write_qualified_month(
    pending: PendingHistoryPartitions,
    stage: HistoryTencentStage,
    month: tuple[int, int],
    progress: HistorySyncProgressPort | None = None,
    cancel_requested: Callable[[], bool] = lambda: False,
) -> None:
    year, calendar_month = month
    item = f"{year:04d}-{calendar_month:02d}"
    index = tuple(sorted(pending.paths)).index(month)
    _publish_partition_progress(progress, "started", index, len(pending.paths), item)
    activity = HistorySqliteActivity(
        partial(_publish_partition_progress, progress, "waiting", index, len(pending.paths), item), cancel_requested
    )
    with closing(stage.iter_qualified_month(*month)) as revisions:
        checked = _checked_revisions(revisions, activity)
        previous = pending.active_by_month.get(month)
        candidate = pending.paths[month]
        readable = candidate if candidate.is_file() else pending.root / previous.relative_path if previous else None
        if readable is not None:
            batches = SQLiteHistoryMonthPartitionRepository(readable, *month).iter_changed_batches(checked)
            with closing(batches):
                _write_month_changes(pending, month, (value for batch in batches for value in batch), activity)
        else:
            _write_month_changes(pending, month, checked, activity)
    _publish_partition_progress(progress, "completed", index + 1, len(pending.paths), item)


def _checked_revisions(
    revisions: Iterable[HistoryRevision], activity: HistorySqliteActivity
) -> Iterator[HistoryRevision]:
    for index, value in enumerate(revisions):
        if index % 512 == 0:
            activity.tick()
        yield value


def _write_month_changes(
    pending: PendingHistoryPartitions,
    month: tuple[int, int],
    revisions: Iterable[HistoryRevision],
    activity: HistorySqliteActivity,
) -> None:
    iterator = iter(revisions)
    first = next(iterator, None)
    if first is None:
        return
    path = _ensure_pending(pending, month)
    SQLiteHistoryMonthPartitionRepository(path, *month, activity=activity).save_revision_batches(
        chain((first,), iterator)
    )


def _publish_partition_progress(
    progress: HistorySyncProgressPort | None,
    state: Literal["started", "waiting", "completed"],
    completed: int,
    total: int,
    item: str,
) -> None:
    if progress is None:
        return
    try:
        progress.publish(HistorySyncProgress("persisting_partitions", state, completed, total, item))
    except OSError:
        pass
