"""Build recommendation outcome bars from paired raw and qfq history."""

from __future__ import annotations

import math

from trader.recommendation.domain.market.history_metrics import DailyBar, PriceAdjustment
from trader.recommendation.domain.outcomes.models import OutcomeBar, OutcomePrice, OutcomeTradingStatus

_MINIMUM_ONE_PRICE_LIMIT_DOWN_PCT = -4.5


def BuildOutcomeBar(
    *,
    trade_date: str,
    qfq: OutcomePrice,
    raw: OutcomePrice,
    volume: float,
    pct_change: float | None,
    source: str,
) -> OutcomeBar:
    """Preserve unknown raw changes when a one-price session cannot be classified."""
    return OutcomeBar(
        trade_date=trade_date,
        qfq=qfq,
        raw=raw,
        trading_status=_trading_status(raw, volume, pct_change),
        source=source,
    )


def PairOutcomeHistory(
    qfq_bars: tuple[DailyBar, ...],
    raw_bars: tuple[DailyBar, ...],
) -> tuple[OutcomeBar, ...]:
    if any(bar.adjustment is not PriceAdjustment.QFQ for bar in qfq_bars):
        raise ValueError("outcome qfq history contains a non-qfq bar")
    if any(bar.adjustment is not PriceAdjustment.RAW for bar in raw_bars):
        raise ValueError("outcome raw history contains an adjusted bar")
    qfq_by_date = _unique_by_date(qfq_bars)
    raw_by_date = _unique_by_date(raw_bars)
    paired: list[OutcomeBar] = []
    for trade_date in sorted(qfq_by_date.keys() & raw_by_date.keys()):
        qfq = qfq_by_date[trade_date]
        raw = raw_by_date[trade_date]
        if qfq.source != raw.source:
            raise ValueError("outcome raw/qfq bars must come from the same source")
        paired.append(
            BuildOutcomeBar(
                trade_date=trade_date,
                qfq=_prices(qfq),
                raw=_prices(raw),
                volume=raw.volume,
                pct_change=raw.pct_change,
                source=raw.source,
            )
        )
    return tuple(paired)


def _unique_by_date(bars: tuple[DailyBar, ...]) -> dict[str, DailyBar]:
    result: dict[str, DailyBar] = {}
    for bar in bars:
        if bar.trade_date in result:
            raise ValueError("outcome history contains duplicate trade dates")
        result[bar.trade_date] = bar
    return result


def _prices(bar: DailyBar) -> OutcomePrice:
    return OutcomePrice(bar.open_price, bar.high, bar.low, bar.close)


def _trading_status(raw: OutcomePrice, volume: float, pct_change: float | None) -> OutcomeTradingStatus:
    if not math.isfinite(volume) or volume < 0.0:
        return OutcomeTradingStatus.UNKNOWN
    if pct_change is not None and not math.isfinite(pct_change):
        return OutcomeTradingStatus.UNKNOWN
    if volume == 0.0:
        return OutcomeTradingStatus.SUSPENDED
    one_price = len({raw.open_price, raw.high, raw.low, raw.close}) == 1
    if one_price:
        if pct_change is None:
            return OutcomeTradingStatus.UNKNOWN
        if pct_change <= _MINIMUM_ONE_PRICE_LIMIT_DOWN_PCT:
            return OutcomeTradingStatus.ONE_PRICE_LIMIT_DOWN
    return OutcomeTradingStatus.TRADABLE


__all__ = [
    "BuildOutcomeBar",
    "PairOutcomeHistory",
]
