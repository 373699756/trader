"""Read only requested overlap keys in month-major order for one bounded stock batch."""

from collections import defaultdict
from datetime import date
from pathlib import Path

from trader.download.domain.baostock_daily import BaoStockDailyCell
from trader.download.domain.history_control import HistoryActiveSnapshot
from trader.download.infra.history_month_partition import SQLiteHistoryMonthPartitionRepository
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity


def read_overlap_prices(
    root: Path,
    active: HistoryActiveSnapshot | None,
    requests: tuple[tuple[str, tuple[date, ...]], ...],
    *,
    activity: HistorySqliteActivity | None = None,
) -> dict[str, tuple[BaoStockDailyCell, ...]]:
    if active is None:
        return {}
    months: dict[tuple[int, int], list[tuple[str, date]]] = defaultdict(list)
    for code, dates in requests:
        for day in dates:
            if day <= active.data_cutoff:
                months[(day.year, day.month)].append((code, day))
    results: dict[str, list[BaoStockDailyCell]] = defaultdict(list)
    references = {item.relative_path: item for item in active.partitions}
    for (year, month), keys in sorted(months.items()):
        relative = f"partitions/{year:04d}/{month:02d}.sqlite3"
        if relative not in references:
            raise RuntimeError("history_tail_qfq_overlap_missing")
        rows = SQLiteHistoryMonthPartitionRepository(root / relative, year, month, activity=activity).read_price_keys(
            tuple(keys), active.sequence
        )
        for row in rows:
            results[row.code].append(row.cell)
    return {code: tuple(values) for code, values in results.items()}


__all__ = ["read_overlap_prices"]
