"""Bounded paired daily windows and immutable update results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistoryWindow

QFQ_WINDOWS = (("v2", 251), ("v3", 61))


class QfqWindowIncompleteError(RuntimeError):
    """A requested calendar window contains an unpaired supplier gap."""

    def __init__(self, raw_missing: tuple[date, ...], qfq_missing: tuple[date, ...]) -> None:
        self.raw_missing = raw_missing
        self.qfq_missing = qfq_missing
        super().__init__(f"qfq_incomplete_raw_{len(raw_missing)}_qfq_{len(qfq_missing)}")


@dataclass(frozen=True, slots=True)
class QfqUpdateResult:
    target_date: date | None
    completed_codes: int = 0
    pending_codes: int = 0
    changed_files: tuple[str, ...] = ()
    changed_rows: int = 0
    skipped_codes: int = 0
    failure_reason: str | None = None


def completed_daily_cutoff(observed_at: datetime) -> date:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("qfq update requires an aware business clock")
    return observed_at.date() if observed_at.time() >= time(15, 10) else observed_at.date() - timedelta(days=1)


def paired_window(window: PublishedHistoryWindow, dates: tuple[date, ...]) -> bool:
    return tuple(cell.trade_date for cell in window.cells) == dates and all(
        cell.unadjusted is not None and cell.qfq is not None for cell in window.cells
    )


def merge_daily_tail(
    previous: PublishedHistoryWindow, incoming: PublishedHistoryWindow, dates: tuple[date, ...]
) -> PublishedHistoryWindow | None:
    """Exact overlap establishes one adjustment basis; conflicts require a full fetch."""
    existing = {cell.trade_date: cell for cell in previous.cells}
    overlap = tuple(cell for cell in incoming.cells if cell.trade_date in existing)
    if not overlap or any(existing[cell.trade_date] != cell for cell in overlap):
        return None
    combined: dict[date, PublishedHistoryCell] = existing | {cell.trade_date: cell for cell in incoming.cells}
    return PublishedHistoryWindow(previous.code, tuple(combined[day] for day in dates if day in combined))
