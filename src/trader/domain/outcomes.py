"""Pure frozen-recommendation outcome calculations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from trader.domain.models import Strategy


@dataclass(frozen=True)
class OutcomeBar:
    trade_date: str
    open_price: float
    high: float
    low: float
    close: float
    pct_change: float


@dataclass(frozen=True)
class BenchmarkReturn:
    trade_date: str
    return_pct: float


@dataclass(frozen=True)
class OutcomeTarget:
    snapshot_id: str
    strategy: Strategy
    recommend_date: str
    stock_code: str
    anchor_price: float
    atr20_pct: float


@dataclass(frozen=True)
class RecommendationOutcome:
    snapshot_id: str
    strategy: Strategy
    recommend_date: str
    stock_code: str
    horizon: int
    status: Literal["complete", "benchmark_missing", "insufficient_data"]
    settled_at: datetime
    anchor_price: float
    atr20_pct: float
    minimum_low: float | None = None
    end_close: float | None = None
    gross_return_pct: float | None = None
    benchmark_return_pct: float | None = None
    net_excess_return_pct: float | None = None
    mae_pct: float | None = None
    mae_atr: float | None = None
    severe_drawdown: bool | None = None
    quality_reason: str = ""
    version: str = "outcome_v1_mae_atr_cost20bp"


def evaluate_outcome(
    target: OutcomeTarget,
    bars: tuple[OutcomeBar, ...],
    *,
    horizon: int,
    benchmark_returns: tuple[float, ...],
    settled_at: datetime,
    expected_sessions: int | None = None,
    expected_trade_dates: tuple[str, ...] = (),
    round_trip_cost_pct: float = 0.20,
) -> RecommendationOutcome:
    if horizon not in ({1} if target.strategy in {Strategy.TODAY, Strategy.TOMORROW} else {2, 3, 5}):
        raise ValueError("outcome horizon is incompatible with strategy")
    ordered_bars = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    reference = next((bar for bar in ordered_bars if bar.trade_date == target.recommend_date), None)
    ordered = tuple(bar for bar in ordered_bars if bar.trade_date > target.recommend_date)
    if expected_sessions is not None and expected_sessions > len(ordered):
        return _insufficient(target, horizon, settled_at, "missing_or_suspended_session")
    window = ordered[:horizon]
    if len(window) < horizon:
        return _insufficient(target, horizon, settled_at, "horizon_not_due")
    if expected_trade_dates and tuple(bar.trade_date for bar in window) != expected_trade_dates[:horizon]:
        return _insufficient(target, horizon, settled_at, "missing_or_suspended_session")
    if (
        reference is None
        or not _bars_are_valid((reference, *window))
        or target.anchor_price <= 0.0
        or target.atr20_pct <= 0.0
    ):
        return _insufficient(target, horizon, settled_at, "invalid_price_window")
    if _has_price_discontinuity(reference.close, window):
        return _insufficient(target, horizon, settled_at, "price_discontinuity")
    minimum_low = min(bar.low for bar in window)
    end_close = window[-1].close
    gross = (end_close / target.anchor_price - 1.0) * 100.0
    mae = (minimum_low / target.anchor_price - 1.0) * 100.0
    mae_atr = mae / target.atr20_pct
    threshold = -1.5 if target.strategy in {Strategy.TODAY, Strategy.TOMORROW} else -2.5
    benchmark = _compound_returns(benchmark_returns[:horizon]) if len(benchmark_returns) >= horizon else None
    net_excess = None if benchmark is None else gross - benchmark - round_trip_cost_pct
    return RecommendationOutcome(
        snapshot_id=target.snapshot_id,
        strategy=target.strategy,
        recommend_date=target.recommend_date,
        stock_code=target.stock_code,
        horizon=horizon,
        status="complete" if benchmark is not None else "benchmark_missing",
        settled_at=settled_at,
        anchor_price=target.anchor_price,
        atr20_pct=target.atr20_pct,
        minimum_low=minimum_low,
        end_close=end_close,
        gross_return_pct=gross,
        benchmark_return_pct=benchmark,
        net_excess_return_pct=net_excess,
        mae_pct=mae,
        mae_atr=mae_atr,
        severe_drawdown=mae_atr <= threshold,
        quality_reason="" if benchmark is not None else "benchmark_missing",
    )


def _insufficient(
    target: OutcomeTarget,
    horizon: int,
    settled_at: datetime,
    reason: str,
) -> RecommendationOutcome:
    return RecommendationOutcome(
        snapshot_id=target.snapshot_id,
        strategy=target.strategy,
        recommend_date=target.recommend_date,
        stock_code=target.stock_code,
        horizon=horizon,
        status="insufficient_data",
        settled_at=settled_at,
        anchor_price=target.anchor_price,
        atr20_pct=target.atr20_pct,
        quality_reason=reason,
    )


def _bars_are_valid(bars: tuple[OutcomeBar, ...]) -> bool:
    return all(
        all(math.isfinite(value) and value > 0.0 for value in (bar.open_price, bar.high, bar.low, bar.close))
        and math.isfinite(bar.pct_change)
        and bar.high >= max(bar.open_price, bar.close, bar.low)
        and bar.low <= min(bar.open_price, bar.close, bar.high)
        for bar in bars
    )


def _has_price_discontinuity(anchor_price: float, bars: tuple[OutcomeBar, ...]) -> bool:
    previous_close = anchor_price
    for current in bars:
        implied = (current.close / previous_close - 1.0) * 100.0
        if abs(implied - current.pct_change) > 0.5:
            return True
        previous_close = current.close
    return False


def _compound_returns(values: tuple[float, ...]) -> float | None:
    if not values or any(not math.isfinite(value) for value in values):
        return None
    total = 1.0
    for value in values:
        total *= 1.0 + value / 100.0
    return (total - 1.0) * 100.0


__all__ = [
    "BenchmarkReturn",
    "OutcomeBar",
    "OutcomeTarget",
    "RecommendationOutcome",
    "evaluate_outcome",
]
