"""Single writer for supplier pairs committed directly to their final monthly database."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockSecurity
from trader.download.domain.history_control import HistoryActiveSnapshot, HistorySnapshotPartition
from trader.download.domain.history_price_coverage import HistoryPriceCoverage
from trader.download.domain.history_price_qualification import qualify_history_pairs, qualify_price_overlap
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.infra.history_download_index import HistoryDownloadTask
from trader.download.infra.history_month_partition import HistoryMonthPrices
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity


@dataclass(frozen=True)
class HistoryPriceContext:
    full_window: bool
    previous: tuple[BaoStockDailyCell, ...] | None = None


@dataclass(frozen=True)
class HistoryPriceResult:
    task: HistoryDownloadTask
    revisions: tuple[HistoryRevision, ...]
    missing: tuple[date, ...]
    failure: str | None
    coverage: HistoryPriceCoverage | None = None


class HistoryDirectPrices:
    def __init__(
        self,
        root: Path,
        sequence: int,
        active: HistoryActiveSnapshot | None,
        activity: HistorySqliteActivity,
        *,
        fresh: bool = False,
    ) -> None:
        self._root = root
        self._sequence = sequence
        self._activity = activity
        self._fresh = fresh
        self._written_codes: set[str] = set()
        self._download_months = frozenset(
            (int(path.parent.name), int(path.stem))
            for path in (root / "partitions").glob("[0-9][0-9][0-9][0-9]/[0-9][0-9].sqlite3")
        )
        self._previous = (
            {
                (int(Path(item.relative_path).parts[1]), int(Path(item.relative_path).stem)): item
                for item in active.partitions
            }
            if active
            else {}
        )

    def _month(self, year: int, month: int) -> HistoryMonthPrices:
        return HistoryMonthPrices(
            self._root / "partitions" / f"{year:04d}" / f"{month:02d}.sqlite3",
            year,
            month,
            activity=self._activity,
        )

    def read(self, code: str, dates: tuple[date, ...]) -> tuple[BaoStockDailyCell, ...]:
        if self._fresh and code not in self._written_codes:
            return ()
        groups: dict[tuple[int, int], list[tuple[str, date]]] = defaultdict(list)
        for day in dates:
            groups[(day.year, day.month)].append((code, day))
        cells: list[BaoStockDailyCell] = []
        for (year, month), keys in groups.items():
            self._activity.tick()
            path = self._root / "partitions" / f"{year:04d}" / f"{month:02d}.sqlite3"
            if path.exists():
                cells.extend(
                    item.cell for item in self._month(year, month).read_price_keys(tuple(keys), self._sequence)
                )
        return tuple(sorted(cells, key=lambda cell: cell.trade_date))

    def read_for_download(self, code: str, dates: tuple[date, ...]) -> tuple[BaoStockDailyCell, ...]:
        # Each code is dispatched once. A fresh batch has no previous observations;
        # recovery reads only after all Tencent writes have been acknowledged.
        readable = tuple(day for day in dates if (day.year, day.month) in self._download_months)
        return () if self._fresh else self.read(code, readable)

    def cancel_pending(self) -> None:
        self._activity.cancel()

    def prepare(
        self,
        security: BaoStockSecurity,
        task: HistoryDownloadTask,
        window: PublishedHistoryWindow,
        recovery: tuple[BaoStockDailyCell, ...] = (),
        *,
        context: HistoryPriceContext,
    ) -> HistoryPriceResult:
        previous = context.previous
        if previous is None:
            previous = self.read(task.code, task.dates)
        cells = qualify_history_pairs(task.code, task.dates, window, recovery)
        cells = qualify_price_overlap(cells, previous, full_window=context.full_window)
        obtained = {cell.trade_date for cell in previous if cell.obtained}
        observed = {cell.trade_date for cell in previous}
        suspended = {cell.trade_date for cell in cells if cell.status == "supplier_marked_suspended"}
        paired = {cell.trade_date for cell in cells if cell.status == "complete"}
        raw = paired | {
            cell.trade_date
            for cell in window.cells
            if cell.unadjusted is not None and cell.unadjusted.trading_status == "trading"
        }
        qfq = paired | {
            cell.trade_date for cell in window.cells if cell.qfq is not None and cell.qfq.trading_status == "trading"
        }
        raw.difference_update(suspended)
        qfq.difference_update(suspended)
        return HistoryPriceResult(
            task,
            tuple(
                HistoryRevision(self._sequence, security.board, cell, None, None, None)
                for cell in cells
                if (cell.obtained and cell.trade_date not in obtained)
                or (not cell.obtained and cell.trade_date not in observed)
            ),
            tuple(cell.trade_date for cell in cells if not cell.obtained),
            None,
            HistoryPriceCoverage(len(task.dates), len(raw), len(qfq), len(paired), len(suspended)),
        )

    def commit(self, results: tuple[HistoryPriceResult, ...]) -> None:
        groups: dict[tuple[int, int], list[HistoryRevision]] = defaultdict(list)
        for result in results:
            for revision in result.revisions:
                groups[(revision.trade_date.year, revision.trade_date.month)].append(revision)
        for (year, month), revisions in sorted(groups.items()):
            self._activity.tick()
            target = self._month(year, month)
            if not (self._root / "partitions" / f"{year:04d}" / f"{month:02d}.sqlite3").exists():
                target.initialize()
            target.save_revision_batches(sorted(revisions, key=lambda item: (item.trade_date, item.code)))
        self._written_codes.update(result.task.code for result in results if result.revisions)

    def record_missing(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> None:
        """Publish explicit gaps once; a later successful batch appends a complete revision."""
        present = {cell.trade_date for cell in self.read(security.code, dates)}
        missing = tuple(day for day in dates if day not in present)
        task = HistoryDownloadTask(security.code, missing)
        self.commit(
            (
                HistoryPriceResult(
                    task,
                    tuple(
                        HistoryRevision(
                            self._sequence,
                            security.board,
                            BaoStockDailyCell(security.code, day, "unknown_missing", None, None),
                            None,
                            None,
                            None,
                        )
                        for day in missing
                    ),
                    missing,
                    None,
                ),
            )
        )

    def publishable_partitions(
        self,
        dates: tuple[date, ...],
        progress: Callable[[int, int, str], None],
    ) -> tuple[HistorySnapshotPartition, ...]:
        months = set(self._previous) | {(day.year, day.month) for day in dates}
        references = []
        for index, (year, month) in enumerate(sorted(months), 1):
            self._activity.tick()
            progress(index - 1, len(months), f"{year:04d}-{month:02d}")
            target = self._month(year, month)
            # Resume detects changes from persisted observation sequences, not an in-memory flag.
            if not (self._root / "partitions" / f"{year:04d}" / f"{month:02d}.sqlite3").exists():
                target.initialize()
            if (year, month) in self._previous and not target.has_observations_after(
                self._previous[(year, month)].sequence
            ):
                references.append(self._previous[(year, month)])
            else:
                references.append(target.seal(self._sequence))
            progress(index, len(months), f"{year:04d}-{month:02d}")
        return tuple(references)


__all__ = ["HistoryDirectPrices", "HistoryPriceContext", "HistoryPriceResult"]
