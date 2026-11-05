"""Single writer for supplier pairs committed directly to their final monthly database."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from functools import partial
from pathlib import Path
from threading import get_ident
from typing import Literal

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockSecurity
from trader.download.domain.history_control import HistorySnapshot, HistorySnapshotPartition
from trader.download.domain.history_price_coverage import HistoryPriceCoverage
from trader.download.domain.history_price_qualification import QualifyHistoryPairs, QualifyPriceOverlap
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.domain.price_review import PriceReviewBudgetExhausted, PriceReviewRequestBudget
from trader.download.infra.history_download_index import HistoryDownloadTask
from trader.download.infra.history_prices import HistoryPricePartition, HistoryPricePartitionError, HistoryPriceWriteSession
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.infra.sqlite_publication import PublishSqliteFile


@dataclass(frozen=True)
class HistoryPriceContext:
    full_window: bool
    previous: tuple[BaoStockDailyCell, ...] | None = None


@dataclass(frozen=True)
class HistoryDownloadPreviousPrices:
    code: str
    cells: tuple[BaoStockDailyCell, ...]


@dataclass(frozen=True)
class HistoryPriceResult:
    task: HistoryDownloadTask
    revisions: tuple[HistoryRevision, ...]
    missing: tuple[date, ...]
    failure: str | None
    coverage: HistoryPriceCoverage | None = None
    unusable_qfq_dates: tuple[date, ...] = ()
    replacement: PublishedHistoryWindow | None = None
    inferred_suspended_dates: tuple[date, ...] = ()


class HistoryDirectPrices:
    def __init__(
        self,
        root: Path,
        sequence: int,
        current_snapshot: HistorySnapshot | None,
        activity: HistorySqliteActivity,
        *,
        fresh: bool = False, sqlite_policy: SqliteIoPolicy,
    ) -> None:
        self._sqlite_policy = sqlite_policy
        self._root = root
        self._sequence = sequence
        self._activity = activity
        self._fresh = fresh
        self._written_codes: set[str] = set()
        self._write_sessions: dict[int, HistoryPriceWriteSession] = {}
        self._write_thread: int | None = None
        self._download_months = frozenset(
            (int(path.stem[7:]), month)
            for path in root.glob("prices_[0-9][0-9][0-9][0-9].sqlite3")
            for month in range(1, 13)
        )
        self._previous = (
            {(item.calendar_year, item.calendar_month): item for item in current_snapshot.partitions}
            if current_snapshot
            else {}
        )

    def _month(self, year: int, month: int) -> HistoryPricePartition:
        return HistoryPricePartition(
            self._root / f"prices_{year:04d}.sqlite3",
            year,
            month,
            activity=self._activity, sqlite_policy=self._sqlite_policy,
        )

    def read(self, code: str, dates: tuple[date, ...]) -> tuple[BaoStockDailyCell, ...]:
        if self._fresh and code not in self._written_codes:
            return ()
        groups: dict[int, list[tuple[str, date]]] = defaultdict(list)
        for day in dates:
            groups[day.year].append((code, day))
        cells: list[BaoStockDailyCell] = []
        for year, keys in groups.items():
            self._activity.tick()
            path = self._root / f"prices_{year:04d}.sqlite3"
            if path.exists():
                cells.extend(
                    item.cell
                    for item in self._month(year, keys[0][1].month).read_price_keys(tuple(keys), self._sequence)
                )
        return tuple(sorted(cells, key=lambda cell: cell.trade_date))

    def read_for_download(self, code: str, dates: tuple[date, ...]) -> tuple[BaoStockDailyCell, ...]:
        # A retry reads its first durable observation, including explicit missing cells.
        if code in self._written_codes:
            return self.read(code, dates)
        # A fresh batch has no previous observations; recovery reads only after
        # all Tencent writes have been acknowledged.
        readable = tuple(day for day in dates if (day.year, day.month) in self._download_months)
        return () if self._fresh else self.read(code, readable)

    def read_for_download_batch(
        self, tasks: tuple[HistoryDownloadTask, ...],
    ) -> tuple[HistoryDownloadPreviousPrices, ...]:
        """Read a bounded dispatch wave at this batch's frozen revision sequence.

        Connections remain on the dispatch thread and close before immutable
        cells are handed to workers. Fresh and retry visibility match single reads.
        """
        if len(tasks) > 64 or sum(len(task.dates) for task in tasks) > 32768:
            raise ValueError("history_download_read_capacity_exceeded")
        if len({task.code for task in tasks}) != len(tasks):
            raise ValueError("history_download_read_duplicate_code")
        groups: dict[int, list[tuple[str, date]]] = defaultdict(list)
        cells: dict[str, list[BaoStockDailyCell]] = {task.code: [] for task in tasks}
        for task in tasks:
            written = task.code in self._written_codes
            if self._fresh and not written:
                continue
            for day in task.dates:
                if written or (day.year, day.month) in self._download_months:
                    groups[day.year].append((task.code, day))
        for year, keys in sorted(groups.items()):
            self._activity.tick()
            if (self._root / f"prices_{year:04d}.sqlite3").exists():
                for revision in self._month(year, keys[0][1].month).read_price_keys(tuple(keys), self._sequence):
                    cells[revision.code].append(revision.cell)
        return tuple(
            HistoryDownloadPreviousPrices(task.code, tuple(sorted(cells[task.code], key=lambda cell: cell.trade_date)))
            for task in tasks
        )

    @contextmanager
    def _review_activity(self, request_budget: PriceReviewRequestBudget) -> Iterator[HistorySqliteActivity]:
        expired: PriceReviewBudgetExhausted | None = None

        def cancelled() -> bool:
            nonlocal expired
            self._activity.tick()
            try:
                request_budget.check_deadline()
            except PriceReviewBudgetExhausted as exc:
                expired = exc
                return True
            return False

        activity = HistorySqliteActivity(self._activity.tick, cancelled)
        try:
            yield activity
        except KeyboardInterrupt:
            if expired is not None:
                raise expired from None
            raise
        request_budget.check_deadline()

    def read_for_review(
        self,
        code: str,
        dates: tuple[date, ...],
        request_budget: PriceReviewRequestBudget,
    ) -> tuple[BaoStockDailyCell, ...]:
        """Bound exact-key discovery by the same deadline as its extra requests."""
        review_policy = replace(
            self._sqlite_policy,
            connection_timeout_seconds=min(self._sqlite_policy.connection_timeout_seconds, 0.001),
        )
        groups: dict[int, list[tuple[str, date]]] = defaultdict(list)
        for day in dates:
            groups[day.year].append((code, day))
        cells: list[BaoStockDailyCell] = []
        with self._review_activity(request_budget) as activity:
            for year, keys in sorted(groups.items()):
                activity.tick()
                path = self._root / f"prices_{year:04d}.sqlite3"
                if path.exists():
                    target = HistoryPricePartition(path, year, keys[0][1].month, activity=activity, sqlite_policy=review_policy)
                    cells.extend(item.cell for item in target.read_price_keys(tuple(keys), self._sequence, retry_locks=False))
        return tuple(sorted(cells, key=lambda cell: cell.trade_date))

    def commit_for_review(
        self, results: tuple[HistoryPriceResult, ...], request_budget: PriceReviewRequestBudget,
    ) -> None:
        """Bound extra writes while letting an entered atomic commit finish safely."""
        policy = replace(
            self._sqlite_policy,
            connection_timeout_seconds=min(self._sqlite_policy.connection_timeout_seconds, 0.001),
        )
        with self._review_activity(request_budget) as activity:
            for year, revisions in self._revision_groups(results):
                activity.tick()
                path = self._root / f"prices_{year:04d}.sqlite3"
                if not path.is_file():
                    raise HistoryPricePartitionError("history_review_partition_missing")
                target = HistoryPricePartition(path, year, 1, activity=activity, sqlite_policy=policy)
                session = target.open_write_session(pending_sequence=self._sequence)
                try:
                    session.save_revision_batches(revisions)
                finally:
                    session.close()
            self._written_codes.update(result.task.code for result in results if result.revisions)

    def prepare_retry_pass(self) -> None:
        """Allow an unpublished first-pass observation to gain verified facts."""
        if self._write_sessions or self._write_thread is not None:
            raise RuntimeError("history_month_writer_still_open")
        self._fresh = False

    def cancel_pending(self) -> None:
        self._activity.cancel()

    def replace_window(self, security: BaoStockSecurity, window: PublishedHistoryWindow, source: Literal["tencent", "baostock"]) -> None:
        from trader.download.infra.history_price_repair import HistoryPriceRepair

        HistoryPriceRepair(self._root, sqlite_policy=self._sqlite_policy).replace(security, tuple(cell.trade_date for cell in window.cells), window, self, self._sequence, source=source)

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
            inferred_suspended_dates=tuple(cell.trade_date for cell in cells if cell.status == "tencent_inferred_suspended"),
        )

    def commit(self, results: tuple[HistoryPriceResult, ...]) -> None:
        for year, revisions in self._revision_groups(results):
            self._activity.tick()
            target = self._month(year, 1)
            if not (self._root / f"prices_{year:04d}.sqlite3").exists():
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
        for year, revisions in self._revision_groups(results):
            self._activity.tick()
            key = year
            session = self._write_sessions.get(key)
            if session is None:
                if len(self._write_sessions) >= 32:
                    raise RuntimeError("history_month_writer_capacity_exceeded")
                target = self._month(year, 1)
                if not (self._root / f"prices_{year:04d}.sqlite3").exists():
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
    ) -> tuple[tuple[int, tuple[HistoryRevision, ...]], ...]:
        groups: dict[int, list[HistoryRevision]] = defaultdict(list)
        for result in results:
            for revision in result.revisions:
                groups[revision.trade_date.year].append(revision)
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
        progress: Callable[[Literal["started", "waiting", "completed"], int, int, str], None],
        *,
        exact_month_ranges: bool = False,
        fingerprint_before_statistics: bool = False,
    ) -> tuple[HistorySnapshotPartition, ...]:
        months = tuple(sorted(set(self._previous) | {(day.year, day.month) for day in dates}))
        references: dict[tuple[int, int], HistorySnapshotPartition] = {}
        for year, month in months:
            self._activity.tick()
            target = self._month(year, month)
            if not (self._root / f"prices_{year:04d}.sqlite3").exists():
                target.initialize()
            if (year, month) in self._previous and not target.has_observations_after(
                self._previous[(year, month)].sequence
            ):
                references[(year, month)] = self._previous[(year, month)]

        years = tuple(sorted(
            {year for year, month in months if (year, month) not in references}
            | {year for year, _ in months if not (self._root / f"prices_{year:04d}.sqlite3.sha256").is_file()}
        ))
        for completed, year in enumerate(years):
            label = f"prices_{year:04d}.sqlite3"
            progress("started", completed, len(years), label)
            selected = tuple(month for candidate_year, month in months if candidate_year == year and (year, month) not in references)
            with self._activity.reporting(
                partial(progress, "waiting", completed, len(years), label)
            ):
                if selected:
                    sealed = self._month(year, selected[0]).seal_months(
                        selected,
                        self._sequence,
                        exact_month_ranges=exact_month_ranges,
                        fingerprint_before_statistics=fingerprint_before_statistics,
                    )
                    references.update(((year, month), value) for month, value in zip(selected, sealed, strict=True))
                else:
                    PublishSqliteFile(
                        self._root / label,
                        2,
                        sqlite_policy=self._sqlite_policy,
                        check=self._activity.tick,
                    )
            progress("completed", completed + 1, len(years), label)
        return tuple(references[month] for month in months)

__all__ = [
    "HistoryDirectPrices",
    "HistoryDownloadPreviousPrices",
    "HistoryPriceContext",
    "HistoryPriceResult",
]
