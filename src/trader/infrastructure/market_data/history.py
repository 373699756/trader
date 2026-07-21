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
    turnover_rate: float | None = None


@dataclass(frozen=True)
class HistoryProfile:
    """Precomputed history metrics reused by feature extraction."""

    moving_average_5d: float | None
    moving_average_20d: float | None
    moving_average_60d: float | None
    volatility_20d: float | None
    max_drawdown_20d: float | None
    median_amount_20d: float | None
    median_turnover_20d: float | None
    upward_consistency_20d: float | None


def summarize_history_metrics(bars: tuple[DailyBar, ...]) -> HistoryProfile:
    """Compute all history metrics used by FeatureBuilder in one local pass."""

    ma5 = _moving_average_from_tail(bars, 5)
    ma20 = _moving_average_from_tail(bars, 20)
    ma60 = _moving_average_from_tail(bars, 60)

    window_20 = bars[-20:] if len(bars) >= 20 else bars[:]
    return_window_20 = bars[-21:] if len(bars) >= 21 else bars[:]
    if len(window_20) < 20:
        volatility = None
        max_drawdown = None
        median_amount = None
        median_turnover = None
        upward_consistency = None
    else:
        returns: list[float] = []
        close_peaks = []
        valid_amounts: list[float] = []
        valid_turnover: list[float] = []
        values = list(window_20)
        for previous, current in zip(return_window_20[:-1], return_window_20[1:], strict=True):
            if previous.close > 0 and current.close > 0:
                returns.append((current.close / previous.close - 1.0) * 100.0)
        for bar in values:
            if bar.close > 0:
                close_peaks.append(bar.close)
            if bar.amount > 0:
                valid_amounts.append(bar.amount)
            if bar.turnover_rate is not None and math.isfinite(bar.turnover_rate) and bar.turnover_rate > 0:
                valid_turnover.append(bar.turnover_rate)
        volatility = statistics.pstdev(returns) if len(returns) == 20 else None
        finite_changes = [bar.pct_change for bar in values if math.isfinite(bar.pct_change)]
        upward_consistency = (
            100.0 * sum(value > 0 for value in finite_changes) / 20 if len(finite_changes) == 20 else None
        )

        if close_peaks:
            peak = -math.inf
            drawdown = 0.0
            for close in close_peaks:
                peak = max(peak, close)
                drawdown = min(drawdown, (close / peak - 1.0) * 100.0)
            max_drawdown = drawdown if math.isfinite(peak) else None
        else:
            max_drawdown = None
        median_amount = statistics.median(valid_amounts) if len(valid_amounts) == 20 else None
        median_turnover = statistics.median(valid_turnover) if len(valid_turnover) == 20 else None

    return HistoryProfile(
        moving_average_5d=ma5,
        moving_average_20d=ma20,
        moving_average_60d=ma60,
        volatility_20d=volatility,
        max_drawdown_20d=max_drawdown,
        median_amount_20d=median_amount,
        median_turnover_20d=median_turnover,
        upward_consistency_20d=upward_consistency,
    )


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


def _moving_average_from_tail(bars: tuple[DailyBar, ...], days: int) -> float | None:
    if len(bars) < days:
        return None
    if days < 1:
        return None
    closes = tuple(bar.close for bar in bars[-days:])
    if any(bar <= 0 for bar in closes):
        return None
    return sum(closes) / float(days)


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
    "HistoryProfile",
    "median_amount",
    "moving_average",
    "summarize_history_metrics",
    "return_pct",
    "upward_consistency",
    "volatility_pct",
]
