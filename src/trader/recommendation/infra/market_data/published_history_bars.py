"""Project download-owned daily facts to recommendation feature and outcome bars."""

from typing import cast

from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide, PublishedHistoryWindow
from trader.infra.market_data.history.history import DailyBar, HistorySessionKind, PriceAdjustment
from trader.recommendation.infra.market_data.outcome_history import BuildOutcomeBar
from trader.recommendation.domain.outcomes.models import OutcomeBar, OutcomePrice

_DailyValues = tuple[float, float, float, float, float, float]


def QfqBars(window: PublishedHistoryWindow) -> tuple[DailyBar, ...]:
    return tuple(
        bar for cell in window.cells if (bar := ProjectPublishedDailyBar(cell, PriceAdjustment.QFQ)) is not None
    )


def OutcomeBars(cells: tuple[PublishedHistoryCell, ...]) -> tuple[OutcomeBar, ...]:
    raw_by_date: dict[str, tuple[PublishedHistorySide, _DailyValues]] = {}
    qfq_by_date: dict[str, tuple[PublishedHistorySide, _DailyValues]] = {}
    for cell in cells:
        for side, projected in ((cell.unadjusted, raw_by_date), (cell.qfq, qfq_by_date)):
            values = _daily_values(side, cell.unadjusted)
            if side is None or values is None:
                continue
            day = side.trade_date.isoformat()
            if day in projected:
                raise ValueError("outcome history contains duplicate trade dates")
            projected[day] = side, values
    result = []
    for day in sorted(qfq_by_date.keys() & raw_by_date.keys()):
        raw, raw_values = raw_by_date[day]
        _, qfq_values = qfq_by_date[day]
        raw_open, raw_close, raw_high, raw_low, volume, _ = raw_values
        qfq_open, qfq_close, qfq_high, qfq_low, _, _ = qfq_values
        result.append(
            BuildOutcomeBar(
                trade_date=day,
                qfq=OutcomePrice(qfq_open, qfq_high, qfq_low, qfq_close),
                raw=OutcomePrice(raw_open, raw_high, raw_low, raw_close),
                volume=volume,
                pct_change=raw.pct_change,
                source="baostock",
            )
        )
    return tuple(result)


def _daily_values(side: PublishedHistorySide | None, raw: PublishedHistorySide | None) -> _DailyValues | None:
    if side is None or raw is None:
        return None
    values = (side.open_price, side.close_price, side.high_price, side.low_price, raw.volume, side.amount)
    if any(value is None for value in values):
        return None
    return cast(_DailyValues, values)


def ProjectPublishedDailyBar(cell: PublishedHistoryCell, adjustment: PriceAdjustment) -> DailyBar | None:
    side = cell.qfq if adjustment is PriceAdjustment.QFQ else cell.unadjusted
    raw = cell.unadjusted
    values = _daily_values(side, raw)
    if side is None or raw is None or values is None:
        return None
    open_price, close_price, high_price, low_price, volume, amount = values
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
        session_kind=(
            HistorySessionKind.SUSPENDED
            if cell.status == "supplier_marked_suspended"
            else HistorySessionKind.TRADING
            if cell.status == "complete"
            else HistorySessionKind.ESTIMATED
        ),
    )
