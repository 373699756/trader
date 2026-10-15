"""Single writer for supplier pairs committed directly to their final monthly database."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from threading import get_ident

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockSecurity
from trader.download.domain.history_control import HistoryActiveSnapshot, HistorySnapshotPartition
from trader.download.domain.history_price_coverage import HistoryPriceCoverage
from trader.download.domain.history_price_qualification import QualifyHistoryPairs, QualifyPriceOverlap
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.infra.history_download_index import HistoryDownloadTask
from trader.download.infra.history_month_partition import HistoryMonthPrices, HistoryMonthWriteSession
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
    unusable_qfq_dates: tuple[date, ...] = ()


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
        self._write_sessions: dict[tuple[int, int], HistoryMonthWriteSession] = {}
        self._write_thread: int | None = None
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
        cells = QualifyHistoryPairs(task.code, task.dates, window, recovery)
        cells = QualifyPriceOverlap(cells, previous, full_window=context.full_window)
        prior = {cell.trade_date: cell for cell in previous}
        suspended = {
            cell.trade_date
            for cell in cells
            if cell.status in {"supplier_marked_suspended", "tencent_inferred_suspended"}
        }
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
                if prior.get(cell.trade_date) != cell
            ),
            tuple(cell.trade_date for cell in cells if not cell.obtained),
            None,
            HistoryPriceCoverage(
                len(task.dates),
                len(raw),
                len(qfq),
                len(paired),
                len(suspended),
                inferred_suspended=sum(cell.status == "tencent_inferred_suspended" for cell in cells),
            ),
        )

    def commit(self, results: tuple[HistoryPriceResult, ...]) -> None:
        for (year, month), revisions in self._revision_groups(results):
            self._activity.tick()
            target = self._month(year, month)
            if not (self._root / "partitions" / f"{year:04d}" / f"{month:02d}.sqlite3").exists():
                target.initialize()
            target.save_revision_batches(revisions)
        self._written_codes.update(result.task.code for result in results if result.revisions)

    def commit_pipeline(self, results: tuple[HistoryPriceResult, ...]) -> None:
        """Commit a pipeline batch while retaining bounded, thread-confined month connections."""
        thread = get_ident()
        if self._write_thread is None:
            self._write_thread = thread
        elif self._write_thread != thread:
            raise RuntimeError("history_month_writer_thread_conflict")
        for (year, month), revisions in self._revision_groups(results):
            self._activity.tick()
            key = year, month
            session = self._write_sessions.get(key)
            if session is None:
                if len(self._write_sessions) >= 128:
                    raise RuntimeError("history_month_writer_capacity_exceeded")
                target = self._month(year, month)
                if not (self._root / "partitions" / f"{year:04d}" / f"{month:02d}.sqlite3").exists():
                    target.initialize()
                session = target.open_write_session(
                    fresh=self._fresh, pending_sequence=self._sequence if not self._fresh else None
                )
                self._write_sessions[key] = session
            session.save_revision_batches(revisions)
        self._written_codes.update(result.task.code for result in results if result.revisions)

    def close_pipeline(self) -> None:
        """Close pipeline connections on their owning writer thread."""
        if self._write_sessions and self._write_thread != get_ident():
            raise RuntimeError("history_month_writer_thread_conflict")
        failure: BaseException | None = None
        try:
            for session in self._write_sessions.values():
                try:
                    session.close()
                except (OSError, RuntimeError) as exc:
                    if failure is None:
                        failure = exc
        finally:
            self._write_sessions.clear()
            self._write_thread = None
        if failure is not None:
            raise RuntimeError("history_month_writer_close_failed") from failure

    @staticmethod
    def _revision_groups(
        results: tuple[HistoryPriceResult, ...],
    ) -> tuple[tuple[tuple[int, int], tuple[HistoryRevision, ...]], ...]:
        groups: dict[tuple[int, int], list[HistoryRevision]] = defaultdict(list)
        for result in results:
            for revision in result.revisions:
                groups[(revision.trade_date.year, revision.trade_date.month)].append(revision)
        return tuple(
            (key, tuple(sorted(revisions, key=lambda item: (item.trade_date, item.code))))
            for key, revisions in sorted(groups.items())
        )

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
