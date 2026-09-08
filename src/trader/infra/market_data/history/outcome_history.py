"""Build explicit raw/qfq outcome bars at the market-data trust boundary."""

from __future__ import annotations

import math

from trader.domain.outcome.models import OutcomeBar, OutcomePrice, OutcomeTradingStatus
from trader.infra.market_data.history.history import DailyBar, PriceAdjustment

_MINIMUM_ONE_PRICE_LIMIT_DOWN_PCT = -4.5


def pair_outcome_history(
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
            OutcomeBar(
                trade_date=trade_date,
                qfq=_prices(qfq),
                raw=_prices(raw),
                trading_status=_trading_status(raw),
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


def _trading_status(raw: DailyBar) -> OutcomeTradingStatus:
    if not math.isfinite(raw.volume) or not math.isfinite(raw.pct_change):
        return OutcomeTradingStatus.UNKNOWN
    if raw.volume <= 0.0:
        return OutcomeTradingStatus.SUSPENDED
    one_price = len({raw.open_price, raw.high, raw.low, raw.close}) == 1
    if one_price and raw.pct_change <= _MINIMUM_ONE_PRICE_LIMIT_DOWN_PCT:
        return OutcomeTradingStatus.ONE_PRICE_LIMIT_DOWN
    return OutcomeTradingStatus.TRADABLE


__all__ = ["pair_outcome_history"]
