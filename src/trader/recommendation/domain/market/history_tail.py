"""Pure trading-session and adjustment checks for a weekly history base."""

from dataclasses import dataclass
from datetime import date
from enum import Enum

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide


class HistoryQuality(str, Enum):
    FULL_HISTORY_READY = "full_history_ready"
    TAIL_PENDING = "tail_pending"
    RAW_ONLY = "raw_only"
    QFQ_ONLY = "qfq_only"
    ADJUSTMENT_CONFLICT = "adjustment_conflict"
    HISTORY_STALE = "history_stale"
    HISTORY_UNAVAILABLE = "history_unavailable"


@dataclass(frozen=True, slots=True)
class HistoryTailPlan:
    expected_date: date
    missing_dates: tuple[date, ...]
    quality: HistoryQuality


def plan_history_tail(open_dates: tuple[date, ...], latest: date, observed_on: date) -> HistoryTailPlan:
    # A live quote is not a completed daily bar. Same-day publication, when
    # already present in the archive, is accepted separately by its caller.
    completed = tuple(day for day in open_dates if day < observed_on)
    if not completed or open_dates != tuple(sorted(set(open_dates))) or open_dates[-1] < observed_on:
        raise ValueError("history_calendar_unverifiable")
    expected = completed[-1]
    missing = tuple(day for day in completed if day > latest)
    if latest > observed_on or latest not in open_dates:
        quality = HistoryQuality.HISTORY_UNAVAILABLE
    elif not missing:
        quality = HistoryQuality.FULL_HISTORY_READY
    elif len(missing) <= 5:
        quality = HistoryQuality.TAIL_PENDING
    else:
        quality = HistoryQuality.HISTORY_STALE
    return HistoryTailPlan(expected, missing, quality)


def validate_tail_overlap(
    original: tuple[BaoStockDailyCell, ...],
    recovered: tuple[BaoStockDailyCell, ...],
) -> HistoryQuality:
    """Require exact same-source raw/qfq facts; never infer an adjustment factor."""
    if any(cell.qfq is None and cell.unadjusted is None for cell in recovered):
        return HistoryQuality.HISTORY_UNAVAILABLE
    if any(cell.qfq is None for cell in recovered):
        return HistoryQuality.RAW_ONLY
    if any(cell.unadjusted is None for cell in recovered):
        return HistoryQuality.QFQ_ONLY
    if any(not _valid_side(side) for cell in recovered for side in (cell.unadjusted, cell.qfq)):
        return HistoryQuality.TAIL_PENDING
    returned = {cell.trade_date: cell for cell in recovered}
    if any(returned.get(cell.trade_date) != cell for cell in original):
        return HistoryQuality.ADJUSTMENT_CONFLICT
    return HistoryQuality.FULL_HISTORY_READY


def _valid_side(side: BaoStockDailySide | None) -> bool:
    if side is None:
        return False
    prices = (side.open_price, side.close_price, side.high_price, side.low_price)
    if any(value is None or value <= 0 for value in prices):
        return False
    assert side.high_price is not None and side.low_price is not None
    assert side.open_price is not None and side.close_price is not None
    return (
        side.low_price
        <= min(side.open_price, side.close_price)
        <= max(side.open_price, side.close_price)
        <= side.high_price
    )
