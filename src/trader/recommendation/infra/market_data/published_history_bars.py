"""Project download-owned daily facts to recommendation feature and outcome bars."""

from typing import cast

from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistoryWindow
from trader.infra.market_data.history.history import DailyBar, PriceAdjustment
from trader.recommendation.infra.market_data.outcome_history import PairOutcomeHistory
from trader.training.domain.evaluation.models import OutcomeBar


def QfqBars(window: PublishedHistoryWindow) -> tuple[DailyBar, ...]:
    return tuple(
        bar for cell in window.cells if (bar := ProjectPublishedDailyBar(cell, PriceAdjustment.QFQ)) is not None
    )


def OutcomeBars(cells: tuple[PublishedHistoryCell, ...]) -> tuple[OutcomeBar, ...]:
    qfq = tuple(bar for cell in cells if (bar := ProjectPublishedDailyBar(cell, PriceAdjustment.QFQ)) is not None)
    raw = tuple(bar for cell in cells if (bar := ProjectPublishedDailyBar(cell, PriceAdjustment.RAW)) is not None)
    return PairOutcomeHistory(qfq, raw)


def ProjectPublishedDailyBar(cell: PublishedHistoryCell, adjustment: PriceAdjustment) -> DailyBar | None:
    side = cell.qfq if adjustment is PriceAdjustment.QFQ else cell.unadjusted
    raw = cell.unadjusted
    if side is None or raw is None:
        return None
    values = (side.open_price, side.close_price, side.high_price, side.low_price, raw.volume, side.amount)
    if any(value is None for value in values):
        return None
    open_price, close_price, high_price, low_price, volume, amount = cast(tuple[float, ...], values)
    return DailyBar(
        trade_date=side.trade_date.isoformat(),
        open_price=open_price,
        close=close_price,
        high=high_price,
        low=low_price,
        volume=volume,
        amount=amount,
        pct_change=float(raw.pct_change or 0.0),
        turnover_rate=raw.turnover,
        adjustment=adjustment,
        source="baostock",
    )
