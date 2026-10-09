"""Serial, resumable refresh of the two bounded online daily windows."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from trader.download.domain.baostock_daily import BaoStockCodeDownload, BaoStockSecurity
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import PublishedHistoryWindow, project_history_cell
from trader.download.domain.qfq_window import (
    QfqUpdateResult,
    completed_daily_cutoff,
    merge_daily_tail,
    paired_window,
)


class QfqWindowPort(Protocol):
    def read_code(self, code: str) -> PublishedHistoryWindow: ...

    def replace_window(self, window: PublishedHistoryWindow, source_identity: str) -> tuple[tuple[str, ...], int]: ...


class QfqSupplierPort(Protocol):
    def load_qfq_context(self, as_of: date, sessions: int) -> HistorySupplierContext: ...

    def fetch_code(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload: ...


class QfqResumePort(Protocol):
    def completed(self, day: date, code: str) -> bool: ...

    def confirm(self, day: date, code: str) -> None: ...


@dataclass(frozen=True)
class UpdateQfqWindows:
    v2: QfqWindowPort
    v3: QfqWindowPort
    supplier: QfqSupplierPort
    resume: QfqResumePort
    cancel_requested: Callable[[], bool]
    report: Callable[[str], None]

    def seed(self, windows: Iterable[PublishedHistoryWindow], identity: str) -> QfqUpdateResult:
        changed: set[str] = set()
        rows = 0
        count = 0
        for window in windows:
            if self.cancel_requested():
                break
            for cache in (self.v2, self.v3):
                if not cache.read_code(window.code).cells:
                    files, delta = cache.replace_window(window, identity)
                    changed.update(files)
                    rows += delta
            count += 1
            if count % 64 == 0:
                self.report(f"qfq history extraction: codes={count} changed_files={len(changed)}")
        return QfqUpdateResult(None, count, changed_files=tuple(sorted(changed)), changed_rows=rows)

    def execute(self, observed_at: datetime) -> QfqUpdateResult:
        self.report("qfq context: loading BaoStock calendar and universe")
        context = self.supplier.load_qfq_context(completed_daily_cutoff(observed_at), 251)
        day = context.calendar.open_dates[-1]
        completed = 0
        pending = 0
        skipped = 0
        changed: set[str] = set()
        rows = 0
        for position, security in enumerate(context.universe, 1):
            if self.cancel_requested():
                pending += len(context.universe) - position + 1
                break
            dates = context.calendar.expected_dates(security)[-251:]
            if not dates:
                continue
            try:
                previous = self.v2.read_code(security.code)
                smaller = self.v3.read_code(security.code)
                if (
                    self.resume.completed(day, security.code)
                    and paired_window(previous, dates)
                    and paired_window(smaller, dates[-61:])
                ):
                    completed += 1
                    skipped += 1
                    continue
                window = self._download(security, dates, previous)
                source = f"baostock:{context.source_versions.content_hash}"
                for cache in (self.v2, self.v3):
                    files, delta = cache.replace_window(window, source)
                    changed.update(files)
                    rows += delta
                # Separate local checkpoint advances after both shard commits.
                self.resume.confirm(day, security.code)
                completed += 1
            except (RuntimeError, OSError, ValueError) as exc:
                pending += 1
                self.report(f"qfq code pending: position={position} reason={type(exc).__name__}")
            self.report(
                f"qfq progress: {position}/{len(context.universe)} completed={completed} "
                f"pending={pending} changed_files={len(changed)}"
            )
        return QfqUpdateResult(day, completed, pending, tuple(sorted(changed)), rows, skipped)

    def _download(
        self, security: BaoStockSecurity, dates: tuple[date, ...], previous: PublishedHistoryWindow
    ) -> PublishedHistoryWindow:
        last = previous.cells[-1].trade_date if previous.cells else None
        missing = tuple(day for day in dates if last is None or day > last)
        # A small tail plus three exact overlaps is enough in the common case.
        # Adjustment revisions rebuild one code's bounded window, not history.
        can_tail = bool(previous.cells) and len(missing) <= 5 and len(previous.cells) >= len(dates) - len(missing)
        requested = dates[-(len(missing) + 3) :] if can_tail else dates
        incoming = self._fetch(security, requested)
        merged = merge_daily_tail(previous, incoming, dates) if can_tail else incoming
        if can_tail and (merged is None or not paired_window(merged, dates)):
            merged = self._fetch(security, dates)
        if merged is None or not paired_window(merged, dates):
            raise RuntimeError("qfq paired window incomplete")
        return merged

    def _fetch(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        if self.cancel_requested():
            raise RuntimeError("qfq update cancelled")
        batch = self.supplier.fetch_code(security, dates).batch
        if batch.duplicate_rows or batch.future_rows or batch.out_of_window_rows:
            raise ValueError("qfq supplier response invalid")
        return PublishedHistoryWindow(security.code, tuple(project_history_cell(cell) for cell in batch.cells))
