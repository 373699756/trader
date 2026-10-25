"""Compact historical arithmetic inputs derived from the published history owner."""

from __future__ import annotations

import math
from dataclasses import dataclass

from trader.infra.market_data.history.history import DailyBar, HistoryContext, SummarizeHistoryMetrics


@dataclass(frozen=True)
class FactorHistory:
    ma5: float | None
    ma10: float | None
    ma20: float | None
    ma60: float | None
    ma20_slope_pct: float | None
    atr20_pct: float | None
    average_volume_5d: float | None
    prior_high_20d: float | None
    breakout_high: float | None
    volatility_20d: float | None
    max_drawdown_20d: float | None
    median_amount_20d: float | None
    average_amount_20d: float | None
    amihud_20d: float | None
    median_turnover_20d: float | None
    upward_consistency_20d: float | None
    return_anchors: tuple[tuple[int, float | None], ...]
    require_finite_price: bool

    def returns(self, price: float | None) -> dict[int, float | None]:
        if price is None or price <= 0 or (self.require_finite_price and not math.isfinite(price)):
            return dict.fromkeys(days for days, _ in self.return_anchors)
        return {
            days: (price / anchor - 1.0) * 100.0 if anchor is not None and anchor > 0 else None
            for days, anchor in self.return_anchors
        }


def BuildFactorHistory(
    bars: tuple[DailyBar, ...], context: HistoryContext | None, observation_date: str
) -> FactorHistory:
    """Project only consumed scalars; published contexts avoid historical scans."""
    history = context.profile if context is not None else SummarizeHistoryMetrics(bars)
    completed = tuple(bar for bar in bars if bar.trade_date < observation_date)
    if context is not None and context.latest_trade_date >= observation_date and context.previous_profile is not None:
        setup = context.previous_profile
    else:
        setup = SummarizeHistoryMetrics(completed) if len(completed) != len(bars) else history
    return FactorHistory(
        ma5=setup.moving_average_5d,
        ma10=setup.moving_average_10d,
        ma20=setup.moving_average_20d,
        ma60=history.moving_average_60d,
        ma20_slope_pct=setup.ma20_slope_pct,
        atr20_pct=setup.atr20_pct,
        average_volume_5d=setup.average_volume_5d,
        prior_high_20d=setup.high_20d,
        breakout_high=max(bar.high for bar in bars[-20:]) if len(bars) >= 20 else None,
        volatility_20d=history.volatility_20d,
        max_drawdown_20d=history.max_drawdown_20d,
        median_amount_20d=history.median_amount_20d,
        average_amount_20d=history.average_amount_20d,
        amihud_20d=history.amihud_20d,
        median_turnover_20d=history.median_turnover_20d,
        upward_consistency_20d=history.upward_consistency_20d,
        return_anchors=tuple(
            (
                days,
                context.anchor_price(days, observation_date)
                if context is not None
                else CompletedHistoryClose(bars, days, observation_date),
            )
            for days in (3, 5, 10, 20, 60)
        ),
        require_finite_price=context is None,
    )


def CompletedHistoryClose(bars: tuple[DailyBar, ...], days: int, observation_date: str) -> float | None:
    if days < 1:
        return None
    completed = tuple(bar for bar in bars if bar.trade_date < observation_date)
    if len(completed) < days:
        return None
    value = completed[-days].close
    return value if math.isfinite(value) and value > 0.0 else None


__all__ = ["BuildFactorHistory", "CompletedHistoryClose", "FactorHistory"]
