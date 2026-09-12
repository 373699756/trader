"""Read-only physical layout summary for the active monthly history archive."""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .reporting import emit_report

from trader.infra.research.history_control_repository import HistoryControlError, SQLiteHistoryControlRepository


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--page-sample-count", type=int, default=1)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if not 1 <= args.page_sample_count <= 100:
            raise ValueError("page sample count must be within 1..100")
        summary = inspect_history_archive(args.archive_root.resolve(), args.page_sample_count)
    except (HistoryControlError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        emit_report({"schema_version": "history_archive_performance", "status": "failed"})
        return 1
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
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0)) as connection:
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


def _sampled_positions(total: int, requested: int) -> frozenset[int]:
    if requested >= total:
        return frozenset(range(total))
    if requested == 1:
        return frozenset((total - 1,))
    return frozenset(round(index * (total - 1) / (requested - 1)) for index in range(requested))


if __name__ == "__main__":
    raise SystemExit(main())
