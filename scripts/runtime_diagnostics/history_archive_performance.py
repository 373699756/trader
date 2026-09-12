"""Read-only physical layout summary for the active monthly history archive."""

from __future__ import annotations

import argparse
import calendar
import os
import resource
import sqlite3
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from itertools import islice
from pathlib import Path

from trader.domain.research.history_control import HistoryActiveSnapshot, HistorySnapshotPartition
from trader.domain.research.history_monthly import HistoryMonthlyRevision
from trader.infra.research.history_control_repository import HistoryControlError, SQLiteHistoryControlRepository
from trader.infra.research.history_month_partition import SQLiteHistoryMonthPartitionRepository

from .reporting import emit_report


@dataclass(frozen=True)
class _PhysicalSummary:
    partition_count: int
    total_bytes: int
    page_count: int
    free_page_count: int
    overflow_bytes: int
    index_bytes: int
    page_sizes: tuple[int, ...]
    page_stat_partition_count: int


@dataclass(frozen=True)
class _QueryMeasurement:
    workload: str
    row_count: int
    cold_median_ms: float
    warm_median_ms: float
    temporary_btree: bool
    query_plan: tuple[str, ...]


@dataclass(frozen=True)
class _RevisionWriteMeasurement:
    requested_rows: int
    statement_count: int
    transaction_count: int
    elapsed_ms: float
    rows_per_second: float
    database_growth_bytes: int


@dataclass(frozen=True)
class _ArchivePerformanceReport:
    physical: _PhysicalSummary
    queries: tuple[_QueryMeasurement, ...]
    revision_write: _RevisionWriteMeasurement
    peak_rss_bytes: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--page-sample-count", type=int, default=1)
    parser.add_argument("--query-rounds", type=int, default=3)
    parser.add_argument("--revision-write-sample-count", type=int, default=512)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if not 1 <= args.page_sample_count <= 100 or not 1 <= args.query_rounds <= 9:
            raise ValueError("history archive sample counts are invalid")
        if not 1 <= args.revision_write_sample_count <= 5_000:
            raise ValueError("revision write sample count must be within 1..5000")
        report = inspect_history_archive_performance(
            args.archive_root.resolve(),
            args.page_sample_count,
            args.query_rounds,
            args.revision_write_sample_count,
        )
    except (HistoryControlError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        emit_report({"schema_version": "history_archive_performance", "status": "failed"})
        return 1
    summary = report.physical
    compact = summary.page_sizes == (8192,)
    payload = {
        "schema_version": "history_archive_performance",
        "status": "passed" if compact else "degraded",
        "summary": {
            "partition_count": summary.partition_count,
            "total_bytes": summary.total_bytes,
            "page_count": summary.page_count,
            "free_page_count": summary.free_page_count,
            "sampled_overflow_bytes": summary.overflow_bytes,
            "sampled_index_bytes": summary.index_bytes,
            "page_stat_partition_count": summary.page_stat_partition_count,
            "page_sizes": list(summary.page_sizes),
            "peak_rss_bytes": report.peak_rss_bytes,
            "queries": [
                {
                    "workload": item.workload,
                    "row_count": item.row_count,
                    "cold_median_ms": item.cold_median_ms,
                    "warm_median_ms": item.warm_median_ms,
                    "temporary_btree": item.temporary_btree,
                    "query_plan": list(item.query_plan),
                }
                for item in report.queries
            ],
            "revision_write": {
                "requested_rows": report.revision_write.requested_rows,
                "statement_count": report.revision_write.statement_count,
                "transaction_count": report.revision_write.transaction_count,
                "elapsed_ms": report.revision_write.elapsed_ms,
                "rows_per_second": report.revision_write.rows_per_second,
                "database_growth_bytes": report.revision_write.database_growth_bytes,
            },
        },
    }
    emit_report(payload)
    return 0


def inspect_history_archive(root: Path, page_sample_count: int = 1) -> _PhysicalSummary:
    state = SQLiteHistoryControlRepository(root / "control.sqlite3").load_state()
    active = state.active_snapshot
    if active is None:
        raise RuntimeError("history archive has no active snapshot")
    total_bytes = 0
    page_count = 0
    free_pages = 0
    overflow_bytes = 0
    index_bytes = 0
    page_sizes: set[int] = set()
    sampled = _sampled_positions(len(active.partitions), page_sample_count)
    for position, reference in enumerate(active.partitions):
        path = root / reference.relative_path
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("history archive partition is unavailable")
        total_bytes += path.stat().st_size
        with closing(
            sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True, timeout=5.0)
        ) as connection:
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_sizes.add(page_size)
            page_count += int(connection.execute("PRAGMA page_count").fetchone()[0])
            free_pages += int(connection.execute("PRAGMA freelist_count").fetchone()[0])
            if position in sampled:
                overflow_bytes += int(
                    connection.execute(
                        "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE pagetype='overflow'"
                    ).fetchone()[0]
                )
                index_bytes += int(
                    connection.execute(
                        "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat "
                        "WHERE name IN ('history_month_code_date_idx', 'history_month_date_board_code_idx', "
                        "'history_month_observation_code_date_idx')"
                    ).fetchone()[0]
                )
    return _PhysicalSummary(
        len(active.partitions),
        total_bytes,
        page_count,
        free_pages,
        overflow_bytes,
        index_bytes,
        tuple(sorted(page_sizes)),
        len(sampled),
    )


def inspect_history_archive_performance(
    root: Path,
    page_sample_count: int = 1,
    query_rounds: int = 3,
    revision_write_sample_count: int = 512,
) -> _ArchivePerformanceReport:
    control = SQLiteHistoryControlRepository(root / "control.sqlite3").load_state()
    active = control.active_snapshot
    if active is None:
        raise RuntimeError("history archive has no active snapshot")
    calendar_identity = next((item for item in control.calendars if item.content_hash == active.calendar_hash), None)
    if calendar_identity is None:
        raise RuntimeError("history archive calendar is unavailable")
    physical = inspect_history_archive(root, page_sample_count)
    queries, sample = _measure_queries(
        root,
        active,
        calendar_identity.open_dates,
        query_rounds,
        revision_write_sample_count,
    )
    revision_write = _measure_revision_write(sample)
    return _ArchivePerformanceReport(physical, queries, revision_write, _peak_rss_bytes())


def _measure_queries(
    root: Path,
    active: HistoryActiveSnapshot,
    open_dates: tuple[date, ...],
    rounds: int,
    revision_sample_count: int,
) -> tuple[tuple[_QueryMeasurement, ...], tuple[HistoryMonthlyRevision, ...]]:
    reference = active.partitions[-1]
    path, repository = _partition_repository(root, reference)
    year, month = _partition_identity(reference)
    month_start = date(year, month, 1)
    month_end = min(active.data_cutoff, date(year, month, calendar.monthrange(year, month)[1]))
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)) as connection:
        seed = connection.execute(
            "SELECT trade_date, code, board FROM daily_records ORDER BY trade_date DESC, code LIMIT 1"
        ).fetchone()
    if seed is None:
        raise RuntimeError("history archive query sample is empty")
    sample_day, code, board = date.fromisoformat(str(seed[0])), str(seed[1]), str(seed[2])
    code_dates = tuple(day for day in open_dates if day <= sample_day)[-61:]
    if len(code_dates) != 61:
        raise RuntimeError("history archive code query window is unavailable")
    code_paths = tuple(
        root / item.relative_path
        for item in active.partitions
        if code_dates[0] <= _partition_end(item) and _partition_start(item) <= code_dates[-1]
    )
    trace: list[str] = []
    traced = SQLiteHistoryMonthPartitionRepository(
        path,
        year,
        month,
        statement_trace=trace.append,
        immutable_read=True,
    )
    workloads: tuple[tuple[str, Callable[[], int]], ...] = (
        (
            "latest_month",
            lambda: sum(1 for _ in repository.iter_range(month_start, month_end, snapshot_sequence=active.sequence)),
        ),
        (
            "single_code_window",
            lambda: _count_code_window(root, active, code_dates[0], code_dates[-1], code),
        ),
        (
            "single_day_board",
            lambda: len(repository.read_day(sample_day, snapshot_sequence=active.sequence, board=board)),
        ),
    )
    traced_workloads: tuple[Callable[[], int], ...] = (
        lambda: sum(1 for _ in traced.iter_range(month_start, month_end, snapshot_sequence=active.sequence)),
        lambda: sum(1 for _ in traced.iter_range(month_start, month_end, snapshot_sequence=active.sequence, code=code)),
        lambda: len(traced.read_day(sample_day, snapshot_sequence=active.sequence, board=board)),
    )
    measurements: list[_QueryMeasurement] = []
    for (name, workload), traced_workload in zip(workloads, traced_workloads, strict=True):
        trace.clear()
        traced_workload()
        statement = next(value for value in trace if value.lstrip().upper().startswith("WITH LATEST AS"))
        query_plan = _query_plan(path, statement)
        workload_paths = code_paths if name == "single_code_window" else (path,)
        cold = _timings(workload_paths, workload, rounds, cold=True)
        warm = _timings(workload_paths, workload, rounds, cold=False)
        row_count = workload()
        measurements.append(
            _QueryMeasurement(
                name,
                row_count,
                round(statistics.median(cold), 3),
                round(statistics.median(warm), 3),
                any("TEMP B-TREE" in item.upper() for item in query_plan),
                query_plan,
            )
        )
    sample = tuple(
        islice(
            repository.iter_range(month_start, month_end, snapshot_sequence=active.sequence),
            revision_sample_count,
        )
    )
    return tuple(measurements), sample


def _timings(
    paths: tuple[Path, ...],
    workload: Callable[[], int],
    rounds: int,
    *,
    cold: bool,
) -> tuple[float, ...]:
    values: list[float] = []
    for _ in range(rounds):
        if cold:
            for path in paths:
                _drop_file_cache(path)
        started = time.perf_counter()
        workload()
        values.append((time.perf_counter() - started) * 1_000.0)
    return tuple(values)


def _query_plan(path: Path, statement: str) -> tuple[str, ...]:
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)) as connection:
        rows = connection.execute(f"EXPLAIN QUERY PLAN {statement}")
        return tuple(str(row[3]) for row in rows)


def _measure_revision_write(sample: tuple[HistoryMonthlyRevision, ...]) -> _RevisionWriteMeasurement:
    if not sample:
        raise RuntimeError("history archive revision write sample is empty")
    first = sample[0]
    year = first.trade_date.year
    month = first.trade_date.month
    statements: list[str] = []
    with tempfile.TemporaryDirectory(prefix="trader-history-write-probe-") as directory:
        path = Path(directory) / "partitions" / f"{year:04d}" / f"{month:02d}.sqlite3"
        repository = SQLiteHistoryMonthPartitionRepository(path, year, month)
        repository.initialize()
        before = _database_bytes(path)
        traced = SQLiteHistoryMonthPartitionRepository(path, year, month, statement_trace=statements.append)
        started = time.perf_counter()
        traced.save_revisions(sample)
        elapsed = time.perf_counter() - started
        growth = _database_bytes(path) - before
    statement_count = sum(not value.lstrip().upper().startswith("PRAGMA") for value in statements)
    transaction_count = sum(value.lstrip().upper().startswith("BEGIN IMMEDIATE") for value in statements)
    return _RevisionWriteMeasurement(
        len(sample),
        statement_count,
        transaction_count,
        round(elapsed * 1_000.0, 3),
        round(len(sample) / elapsed, 3),
        growth,
    )


def _partition_repository(
    root: Path,
    reference: HistorySnapshotPartition,
) -> tuple[Path, SQLiteHistoryMonthPartitionRepository]:
    year, month = _partition_identity(reference)
    path = root / reference.relative_path
    return path, SQLiteHistoryMonthPartitionRepository(path, year, month, immutable_read=True)


def _partition_identity(reference: HistorySnapshotPartition) -> tuple[int, int]:
    parts = Path(reference.relative_path).parts
    return int(parts[1]), int(Path(parts[2]).stem)


def _partition_start(reference: HistorySnapshotPartition) -> date:
    year, month = _partition_identity(reference)
    return date(year, month, 1)


def _partition_end(reference: HistorySnapshotPartition) -> date:
    year, month = _partition_identity(reference)
    return date(year, month, calendar.monthrange(year, month)[1])


def _count_code_window(
    root: Path,
    active: HistoryActiveSnapshot,
    start: date,
    end: date,
    code: str,
) -> int:
    total = 0
    for reference in active.partitions:
        if end < _partition_start(reference) or _partition_end(reference) < start:
            continue
        _path, repository = _partition_repository(root, reference)
        total += sum(
            1
            for _ in repository.iter_range(
                max(start, _partition_start(reference)),
                min(end, _partition_end(reference)),
                snapshot_sequence=active.sequence,
                code=code,
            )
        )
    return total


def _database_bytes(path: Path) -> int:
    return sum(candidate.stat().st_size for candidate in (path, Path(f"{path}-wal")) if candidate.exists())


def _drop_file_cache(path: Path) -> None:
    if not hasattr(os, "posix_fadvise") or not hasattr(os, "POSIX_FADV_DONTNEED"):
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(descriptor)


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _sampled_positions(total: int, requested: int) -> frozenset[int]:
    if requested >= total:
        return frozenset(range(total))
    if requested == 1:
        return frozenset((total - 1,))
    return frozenset(round(index * (total - 1) / (requested - 1)) for index in range(requested))


if __name__ == "__main__":
    raise SystemExit(main())
