"""Pure frozen-recommendation outcome evaluation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from trader.domain.outcome.models import (
    BenchmarkConstituentReturn,
    BenchmarkReturn,
    OutcomeBar,
    OutcomeExitStatus,
    OutcomeTarget,
    OutcomeTradingStatus,
    RecommendationOutcome,
    outcome_horizons,
)
from trader.domain.recommendation.models import Strategy


@dataclass(frozen=True)
class OutcomeEvaluationRequest:
    target: OutcomeTarget
    bars: tuple[OutcomeBar, ...]
    horizon: int
    benchmark_returns: tuple[float, ...]
    settled_at: datetime
    expected_trade_dates: tuple[str, ...] = ()
    round_trip_cost_pct: float = 0.20

    def __post_init__(self) -> None:
        object.__setattr__(self, "bars", tuple(self.bars))
        object.__setattr__(self, "benchmark_returns", tuple(self.benchmark_returns))
        object.__setattr__(self, "expected_trade_dates", tuple(self.expected_trade_dates))
        if not math.isfinite(self.round_trip_cost_pct) or self.round_trip_cost_pct < 0.0:
            raise ValueError("outcome round-trip cost must be finite and non-negative")
        if tuple(sorted(set(self.expected_trade_dates))) != self.expected_trade_dates:
            raise ValueError("expected outcome trade dates must be sorted and unique")


@dataclass(frozen=True)
class _SettlementPoint:
    trade_date: str
    qfq_low: float
    qfq_close: float
    exit_status: OutcomeExitStatus


@dataclass(frozen=True)
class _SettlementWindow:
    points: tuple[_SettlementPoint, ...]
    failure_reason: str = ""


class CanonicalOutcomeEvaluator:
    """Pure owner of benchmark and immutable recommendation outcome calculations."""

    def equal_weight_benchmark(
        self,
        trade_date: str,
        constituents: tuple[BenchmarkConstituentReturn, ...],
    ) -> BenchmarkReturn | None:
        codes = tuple(item.stock_code for item in constituents)
        if (
            not constituents
            or len(set(codes)) != len(codes)
            or any(item.trade_date != trade_date or not math.isfinite(item.return_pct) for item in constituents)
        ):
            return None
        return BenchmarkReturn(trade_date, sum(item.return_pct for item in constituents) / len(constituents))

    def evaluate(self, request: OutcomeEvaluationRequest) -> RecommendationOutcome:
        target = request.target
        bars = request.bars
        horizon = request.horizon
        benchmark_returns = request.benchmark_returns
        settled_at = request.settled_at
        expected_trade_dates = request.expected_trade_dates
        round_trip_cost_pct = request.round_trip_cost_pct
        if horizon not in outcome_horizons(target.strategy):
            raise ValueError("outcome horizon is incompatible with strategy")
        ordered_bars = tuple(sorted(bars, key=lambda bar: bar.trade_date))
        if len({bar.trade_date for bar in ordered_bars}) != len(ordered_bars):
            return _insufficient(target, horizon, settled_at, "duplicate_trade_date")
        reference = next((bar for bar in ordered_bars if bar.trade_date == target.recommend_date), None)
        ordered = tuple(bar for bar in ordered_bars if bar.trade_date > target.recommend_date)
        if reference is None or reference.trading_status is OutcomeTradingStatus.UNKNOWN:
            return _insufficient(target, horizon, settled_at, "invalid_reference_bar")
        anchor_qfq_price = _anchor_qfq_price(target.anchor_raw_price, reference)
        if anchor_qfq_price is None or not math.isfinite(target.atr20_pct) or target.atr20_pct <= 0.0:
            return _insufficient(target, horizon, settled_at, "invalid_price_window")
        settlement = _settlement_window(reference, ordered, expected_trade_dates, horizon)
        if settlement is None:
            return _insufficient(target, horizon, settled_at, "horizon_not_due")
        if settlement.failure_reason:
            return _insufficient(target, horizon, settled_at, settlement.failure_reason)
        window = settlement.points
        minimum_low = min(point.qfq_low for point in window)
        end_close = window[-1].qfq_close
        gross = (end_close / anchor_qfq_price - 1.0) * 100.0
        mae = (minimum_low / anchor_qfq_price - 1.0) * 100.0
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
            anchor_raw_price=target.anchor_raw_price,
            anchor_qfq_price=anchor_qfq_price,
            atr20_pct=target.atr20_pct,
            minimum_qfq_low=minimum_low,
            end_qfq_close=end_close,
            exit_status=window[-1].exit_status,
            untradable_dates=tuple(
                point.trade_date for point in window if point.exit_status is not OutcomeExitStatus.TRADABLE
            ),
            gross_return_pct=gross,
            benchmark_return_pct=benchmark,
            net_excess_return_pct=net_excess,
            mae_pct=mae,
            mae_atr=mae_atr,
            severe_drawdown=mae_atr <= threshold,
            quality_reason="" if benchmark is not None else "benchmark_missing",
        )

    def d25_aggregate(self, outcomes: tuple[RecommendationOutcome, ...]) -> float | None:
        if len(outcomes) != len(outcome_horizons(Strategy.D25)):
            return None
        ordered = tuple(sorted(outcomes, key=lambda item: item.horizon))
        first = ordered[0]
        if tuple(item.horizon for item in ordered) != outcome_horizons(Strategy.D25) or any(
            item.strategy is not Strategy.D25
            or item.snapshot_id != first.snapshot_id
            or item.recommend_date != first.recommend_date
            or item.stock_code != first.stock_code
            or item.status != "complete"
            or item.net_excess_return_pct is None
            or not math.isfinite(item.net_excess_return_pct)
            for item in ordered
        ):
            return None
        return sum(item.net_excess_return_pct for item in ordered if item.net_excess_return_pct is not None) / len(
            ordered
        )


def evaluate_outcome(request: OutcomeEvaluationRequest) -> RecommendationOutcome:
    return CanonicalOutcomeEvaluator().evaluate(request)


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
        anchor_raw_price=target.anchor_raw_price,
        anchor_qfq_price=None,
        atr20_pct=target.atr20_pct,
        quality_reason=reason,
    )


def _anchor_qfq_price(anchor_raw_price: float, reference: OutcomeBar) -> float | None:
    if not math.isfinite(anchor_raw_price) or anchor_raw_price <= 0.0:
        return None
    factor = reference.qfq.close / reference.raw.close
    converted = anchor_raw_price * factor
    return converted if math.isfinite(converted) and converted > 0.0 else None


def _settlement_window(
    reference: OutcomeBar,
    ordered: tuple[OutcomeBar, ...],
    expected_trade_dates: tuple[str, ...],
    horizon: int,
) -> _SettlementWindow | None:
    if expected_trade_dates:
        if len(expected_trade_dates) < horizon:
            return None
        by_date = {bar.trade_date: bar for bar in ordered}
        dates = expected_trade_dates[:horizon]
    else:
        if len(ordered) < horizon:
            return None
        dates = tuple(bar.trade_date for bar in ordered[:horizon])
        by_date = {bar.trade_date: bar for bar in ordered}
    previous_close = reference.qfq.close
    points: list[_SettlementPoint] = []
    for trade_date in dates:
        bar = by_date.get(trade_date)
        if bar is None:
            points.append(
                _SettlementPoint(
                    trade_date,
                    previous_close,
                    previous_close,
                    OutcomeExitStatus.MISSING_CARRIED_FORWARD,
                )
            )
            continue
        exit_status = _exit_status(bar.trading_status)
        if exit_status is None:
            return _SettlementWindow((), "tradability_unknown")
        points.append(_SettlementPoint(trade_date, bar.qfq.low, bar.qfq.close, exit_status))
        previous_close = bar.qfq.close
    return _SettlementWindow(tuple(points))


def _exit_status(status: OutcomeTradingStatus) -> OutcomeExitStatus | None:
    if status is OutcomeTradingStatus.TRADABLE:
        return OutcomeExitStatus.TRADABLE
    if status is OutcomeTradingStatus.SUSPENDED:
        return OutcomeExitStatus.SUSPENDED
    if status is OutcomeTradingStatus.ONE_PRICE_LIMIT_DOWN:
        return OutcomeExitStatus.ONE_PRICE_LIMIT_DOWN
    return None


def _compound_returns(values: tuple[float, ...]) -> float | None:
    if not values or any(not math.isfinite(value) for value in values):
        return None
    total = 1.0
    for value in values:
        total *= 1.0 + value / 100.0
    return (total - 1.0) * 100.0


__all__ = ["CanonicalOutcomeEvaluator", "OutcomeEvaluationRequest", "evaluate_outcome"]
