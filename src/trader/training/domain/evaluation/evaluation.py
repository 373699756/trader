"""Pure frozen-recommendation outcome evaluation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from trader.recommendation.domain.publication.models import Strategy
from trader.training.domain.evaluation.models import (
    BenchmarkConstituentReturn,
    BenchmarkReturn,
    EntryDayPriceWindow,
    OutcomeBar,
    OutcomeExitStatus,
    OutcomeTarget,
    OutcomeTradingStatus,
    RecommendationOutcome,
    outcome_horizons,
)


@dataclass(frozen=True)
class OutcomeEvaluationRequest:
    target: OutcomeTarget
    bars: tuple[OutcomeBar, ...]
    horizon: int
    benchmark_returns: tuple[float, ...]
    settled_at: datetime
    expected_trade_dates: tuple[str, ...] = ()
    round_trip_cost_pct: float = 0.20
    entry_day_window: EntryDayPriceWindow | None = None

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


@dataclass(frozen=True)
class _BarWindow:
    reference: OutcomeBar
    future: tuple[OutcomeBar, ...]


@dataclass(frozen=True)
class _PreparedOutcome:
    anchor_qfq_price: float
    entry_low: float
    settlement: tuple[_SettlementPoint, ...]


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
        if request.horizon not in outcome_horizons(request.target.strategy):
            raise ValueError("outcome horizon is incompatible with strategy")
        prepared = _prepare_outcome(request)
        if isinstance(prepared, str):
            return _insufficient(request.target, request.horizon, request.settled_at, prepared)
        return _complete_outcome(request, prepared)

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


def _complete_outcome(request: OutcomeEvaluationRequest, prepared: _PreparedOutcome) -> RecommendationOutcome:
    target = request.target
    window = prepared.settlement
    minimum_low = min(
        prepared.anchor_qfq_price,
        prepared.entry_low,
        *(point.qfq_low for point in window),
    )
    end_close = window[-1].qfq_close
    gross = (end_close / prepared.anchor_qfq_price - 1.0) * 100.0
    mae = (minimum_low / prepared.anchor_qfq_price - 1.0) * 100.0
    mae_atr = mae / target.atr20_pct
    threshold = -1.5 if target.strategy is Strategy.TOMORROW else -2.5
    benchmark = (
        _compound_returns(request.benchmark_returns[: request.horizon])
        if len(request.benchmark_returns) >= request.horizon
        else None
    )
    net_excess = None if benchmark is None else gross - benchmark - request.round_trip_cost_pct
    return RecommendationOutcome(
        snapshot_id=target.snapshot_id,
        strategy=target.strategy,
        recommend_date=target.recommend_date,
        stock_code=target.stock_code,
        horizon=request.horizon,
        status="complete" if benchmark is not None else "benchmark_missing",
        settled_at=request.settled_at,
        anchor_raw_price=target.anchor_raw_price,
        anchor_qfq_price=prepared.anchor_qfq_price,
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


def _prepare_outcome(request: OutcomeEvaluationRequest) -> _PreparedOutcome | str:
    bar_window = _bar_window(request)
    if isinstance(bar_window, str):
        return bar_window
    anchor_qfq_price = _anchor_qfq_price(request.target.anchor_raw_price, bar_window.reference)
    if anchor_qfq_price is None or not math.isfinite(request.target.atr20_pct) or request.target.atr20_pct <= 0.0:
        return "invalid_price_window"
    settlement = _evaluated_settlement(request, bar_window)
    if isinstance(settlement, str):
        return settlement
    entry_low = _entry_day_low(request, bar_window.reference)
    if entry_low is None:
        return "entry_day_price_window_missing"
    return _PreparedOutcome(anchor_qfq_price, entry_low, settlement)


def _bar_window(request: OutcomeEvaluationRequest) -> _BarWindow | str:
    ordered = tuple(sorted(request.bars, key=lambda bar: bar.trade_date))
    if len({bar.trade_date for bar in ordered}) != len(ordered):
        return "duplicate_trade_date"
    reference = next((bar for bar in ordered if bar.trade_date == request.target.recommend_date), None)
    if reference is None or reference.trading_status is OutcomeTradingStatus.UNKNOWN:
        return "invalid_reference_bar"
    future = tuple(bar for bar in ordered if bar.trade_date > request.target.recommend_date)
    return _BarWindow(reference, future)


def _evaluated_settlement(request: OutcomeEvaluationRequest, bars: _BarWindow) -> tuple[_SettlementPoint, ...] | str:
    settlement = _settlement_window(
        bars.reference,
        bars.future,
        request.expected_trade_dates,
        request.horizon,
    )
    if settlement is None:
        return "horizon_not_due"
    if settlement.failure_reason:
        return settlement.failure_reason
    return settlement.points


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


def _entry_day_low(request: OutcomeEvaluationRequest, reference: OutcomeBar) -> float | None:
    evidence = request.entry_day_window
    if evidence is not None:
        return _verified_entry_day_low(request.target, evidence, reference)
    return _closing_entry_day_low(request.target, reference)


def _verified_entry_day_low(
    target: OutcomeTarget,
    evidence: EntryDayPriceWindow,
    reference: OutcomeBar,
) -> float | None:
    evidence_date = evidence.entry_at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if evidence_date != target.recommend_date or target.entry_at is None or evidence.entry_at != target.entry_at:
        return None
    if evidence.low_raw_price > target.anchor_raw_price:
        return None
    return _anchor_qfq_price(evidence.low_raw_price, reference)


def _closing_entry_day_low(target: OutcomeTarget, reference: OutcomeBar) -> float | None:
    if target.entry_at is None:
        return None
    entry = target.entry_at.astimezone(ZoneInfo("Asia/Shanghai"))
    if (entry.hour, entry.minute, entry.second, entry.microsecond) != (15, 0, 0, 0):
        return None
    return _anchor_qfq_price(target.anchor_raw_price, reference)


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
