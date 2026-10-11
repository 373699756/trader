"""Read active monthly partitions for unresolved raw/qfq price pairs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Literal

from trader.download.domain.history_control import HistoryActiveSnapshot
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.infra.history_month_partition import SQLiteHistoryMonthPartitionRepository

GapScanProgress = Callable[[Literal["started", "completed"], int, int, str], None]


def scan_active_price_gaps(
    root: Path,
    active: HistoryActiveSnapshot | None,
    context: HistorySupplierContext,
    cancel_requested: Callable[[], bool],
    progress: GapScanProgress,
) -> dict[str, tuple[date, ...]]:
    """Scan each active partition while exposing its otherwise long read boundary."""

    if active is None:
        return {}
    gaps: dict[str, list[date]] = defaultdict(list)
    codes = frozenset(item.code for item in context.universe)
    dates = frozenset(context.calendar.open_dates)
    total = len(active.partitions)
    for index, reference in enumerate(active.partitions, start=1):
        if cancel_requested():
            raise KeyboardInterrupt
        relative_path = PurePosixPath(reference.relative_path)
        year, month = int(relative_path.parts[1]), int(relative_path.stem)
        current_item = f"{year:04d}-{month:02d}"
        progress("started", index - 1, total, current_item)
        partition = SQLiteHistoryMonthPartitionRepository(root / reference.relative_path, year, month)
        for code, day in partition.missing_price_dates(active.sequence):
            if code in codes and day in dates:
                gaps[code].append(day)
        progress("completed", index, total, current_item)
    return {code: tuple(sorted(days)) for code, days in gaps.items()}


__all__ = ["scan_active_price_gaps"]
