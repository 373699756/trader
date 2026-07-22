"""Pure extreme market-structure risk derivation for feature building."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from trader.domain.market.models import MarketQuote


def extreme_structure_risks(
    quote: MarketQuote,
    values: Mapping[str, float | None],
    observed_at: datetime,
    *,
    valid_minute_count: int | None = None,
) -> dict[str, float | None]:
    return_5d = _finite(values.get("return_5d"))
    return_10d = _finite(values.get("return_10d"))
    ma20_deviation = _finite(values.get("ma20_deviation_pct"))
    overheat_inputs = (return_5d, return_10d, ma20_deviation)
    short_term_overheat = (
        None
        if all(value is None for value in overheat_inputs)
        else float(
            (return_5d is not None and return_5d >= 12.0)
            or (return_10d is not None and return_10d >= 20.0)
            or (ma20_deviation is not None and ma20_deviation >= 15.0)
        )
    )
    close_location = _finite(values.get("close_location"))
    high_drawdown = None
    if (
        quote.high is not None
        and quote.price is not None
        and math.isfinite(quote.high)
        and math.isfinite(quote.price)
        and quote.high > 0.0
        and quote.price > 0.0
    ):
        high_drawdown = (quote.high - quote.price) / quote.high * 100.0
    minutes = valid_minute_count if valid_minute_count is not None else _completed_trading_minutes(observed_at)
    intraday_reversal = (
        float(high_drawdown >= 3.0 and close_location <= 35.0)
        if minutes >= 30 and high_drawdown is not None and close_location is not None
        else None
    )
    volume_ratio = _nonnegative_finite(quote.volume_ratio)
    amount_median = _finite(values.get("amount_median_20d"))
    amount_ratio = (
        quote.amount / amount_median
        if (
            quote.amount is not None
            and math.isfinite(quote.amount)
            and quote.amount >= 0.0
            and amount_median is not None
            and amount_median > 0.0
        )
        else None
    )
    liquidity_contraction = (
        None
        if volume_ratio is None and amount_ratio is None
        else float(
            (volume_ratio is not None and volume_ratio <= 0.6) or (amount_ratio is not None and amount_ratio <= 0.6)
        )
    )
    slope = _finite(values.get("ma_slope"))
    trend_breakdown = (
        float(ma20_deviation < 0.0 and slope < 50.0 and return_5d < 0.0)
        if ma20_deviation is not None and slope is not None and return_5d is not None
        else None
    )
    price_volume_divergence = (
        float((return_5d > 0.0 and amount_ratio < 0.8) or (return_5d < 0.0 and amount_ratio > 1.2))
        if return_5d is not None and amount_ratio is not None
        else None
    )
    return {
        "price_volume_divergence": price_volume_divergence,
        "short_term_overheat": short_term_overheat,
        "intraday_reversal": intraday_reversal,
        "liquidity_contraction": liquidity_contraction,
        "trend_breakdown": trend_breakdown,
    }


def _completed_trading_minutes(observed_at: datetime) -> int:
    local = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
    minute = local.hour * 60 + local.minute
    morning = max(0, min(minute, 11 * 60 + 30) - (9 * 60 + 30))
    afternoon = max(0, min(minute, 15 * 60) - (13 * 60))
    return morning + afternoon


def _finite(value: float | None) -> float | None:
    return float(value) if value is not None and math.isfinite(value) else None


def _nonnegative_finite(value: float | None) -> float | None:
    parsed = _finite(value)
    return parsed if parsed is not None and parsed >= 0.0 else None


__all__ = ["extreme_structure_risks"]
