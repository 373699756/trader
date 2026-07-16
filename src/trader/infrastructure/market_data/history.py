"""Daily history values and deterministic historical feature calculations."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class DailyBar:
    trade_date: str
    open_price: float
    close: float
    high: float
    low: float
    volume: float
    amount: float
    pct_change: float


def return_pct(bars: tuple[DailyBar, ...], days: int, current_price: float | None = None) -> float | None:
    if days < 1 or len(bars) < days + 1:
        return None
    end = current_price if current_price is not None else bars[-1].close
    start = bars[-days - 1].close
    if start <= 0 or end <= 0:
        return None
    return (end / start - 1.0) * 100.0


def moving_average(bars: tuple[DailyBar, ...], days: int) -> float | None:
    if days < 1 or len(bars) < days:
        return None
    closes = [bar.close for bar in bars[-days:] if bar.close > 0]
    return sum(closes) / days if len(closes) == days else None


def volatility_pct(bars: tuple[DailyBar, ...], days: int = 20) -> float | None:
    if len(bars) < days + 1:
        return None
    returns: list[float] = []
    for previous, current in zip(bars[-days - 1 : -1], bars[-days:], strict=True):
        if previous.close > 0 and current.close > 0:
            returns.append((current.close / previous.close - 1.0) * 100.0)
    return statistics.pstdev(returns) if len(returns) == days else None


def maximum_drawdown_pct(bars: tuple[DailyBar, ...], days: int = 20) -> float | None:
    if len(bars) < days:
        return None
    peak = -math.inf
    drawdown = 0.0
    for bar in bars[-days:]:
        if bar.close <= 0:
            continue
        peak = max(peak, bar.close)
        drawdown = min(drawdown, (bar.close / peak - 1.0) * 100.0)
    return drawdown if math.isfinite(peak) else None


def median_amount(bars: tuple[DailyBar, ...], days: int = 20) -> float | None:
    if len(bars) < days:
        return None
    values = [bar.amount for bar in bars[-days:] if bar.amount > 0]
    return statistics.median(values) if len(values) == days else None


def upward_consistency(bars: tuple[DailyBar, ...], days: int = 20) -> float | None:
    if len(bars) < days:
        return None
    values = [bar.pct_change for bar in bars[-days:] if math.isfinite(bar.pct_change)]
    return 100.0 * sum(value > 0 for value in values) / days if len(values) == days else None


__all__ = [
    "DailyBar",
    "maximum_drawdown_pct",
    "median_amount",
    "moving_average",
    "return_pct",
    "upward_consistency",
    "volatility_pct",
]
