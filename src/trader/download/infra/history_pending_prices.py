"""Lazy copy-on-write monthly price batches; published partitions stay immutable."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.download.domain.history_control import HistoryActiveSnapshot, HistorySnapshotPartition
from trader.download.domain.history_revision import HistoryRevision
from trader.download.infra.history_archive_reader import route_history_months
from trader.download.infra.history_month_partition import SQLiteHistoryMonthPartitionRepository
from trader.download.infra.history_seal_resume import remove_sqlite_files


@dataclass(frozen=True)
class PendingHistoryPartitions:
    root: Path
    paths: dict[tuple[int, int], Path]
    active_by_month: dict[tuple[int, int], HistorySnapshotPartition]
    first_date: date


def partition_month(reference: HistorySnapshotPartition) -> tuple[int, int]:
    path = Path(reference.relative_path)
    return int(path.parts[1]), int(path.stem)


def backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as input_connection,
        closing(sqlite3.connect(destination)) as output_connection,
    ):
        input_connection.backup(output_connection)


def prepare_pending(
    root: Path,
    dates: tuple[date, ...],
    active: HistoryActiveSnapshot | None,
    resume: bool,
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
        backup_database(pending.root / reference.relative_path, path)
    repository = SQLiteHistoryMonthPartitionRepository(path, year, calendar_month)
    repository.initialize()
    if month == (pending.first_date.year, pending.first_date.month):
        repository.prune_before(pending.first_date)
    return path


def write_revision_batch(pending: PendingHistoryPartitions, revisions: tuple[HistoryRevision, ...]) -> None:
    grouped: dict[tuple[int, int], list[HistoryRevision]] = defaultdict(list)
    for value in revisions:
        grouped[(value.trade_date.year, value.trade_date.month)].append(value)
    for (year, month), values in sorted(grouped.items()):
        previous = pending.active_by_month.get((year, month))
        candidate = pending.paths[(year, month)]
        readable = candidate if candidate.is_file() else pending.root / previous.relative_path if previous else None
        if readable is not None:
            values = list(SQLiteHistoryMonthPartitionRepository(readable, year, month).changed_revisions(tuple(values)))
        if not values:
            continue
        path = _ensure_pending(pending, (year, month))
        repository = SQLiteHistoryMonthPartitionRepository(path, year, month)
        repository.save_revisions(sorted(values, key=lambda item: (item.trade_date, item.code, item.revision_id)))
