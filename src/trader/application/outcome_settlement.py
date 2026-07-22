"""Background-only settlement orchestration for frozen recommendation outcomes."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from trader.application.ports import MarketDataPort, OutcomeRepositoryPort
from trader.application.schedule import shanghai_now
from trader.domain.models import FeatureSnapshot, Strategy
from trader.domain.outcomes import BenchmarkReturn, evaluate_outcome


@dataclass(frozen=True)
class SettlementResult:
    target_count: int
    outcome_count: int
    completed_count: int
    benchmark_recorded: bool


class OutcomeSettlementService:
    def __init__(
        self,
        market_data: MarketDataPort,
        repository: OutcomeRepositoryPort,
        *,
        session_distance: Callable[[str, str], int | None],
        target_limit: int = 500,
    ) -> None:
        self._market_data = market_data
        self._repository = repository
        self._session_distance = session_distance
        self._target_limit = target_limit

    def settle(
        self,
        now: datetime,
        market_features: Sequence[FeatureSnapshot],
    ) -> SettlementResult:
        local = shanghai_now(now)
        benchmark = _equal_weight_benchmark(local.date().isoformat(), market_features)
        if benchmark is not None:
            self._repository.record_benchmark_return(benchmark, observed_at=now)
        else:
            return SettlementResult(0, 0, 0, False)
        targets = tuple(self._repository.pending_outcome_targets(limit=self._target_limit))
        if not targets:
            return SettlementResult(0, 0, 0, benchmark is not None)
        codes = tuple(dict.fromkeys(target.stock_code for target in targets))
        histories = self._market_data.read_outcome_bars(codes, now)
        outcomes = []
        current_date = local.date()
        for target in targets:
            elapsed = self._session_distance(target.recommend_date, current_date.isoformat())
            if elapsed is None:
                continue
            bars = tuple(
                bar
                for bar in histories.get(target.stock_code, ())
                if target.recommend_date <= bar.trade_date <= current_date.isoformat()
            )
            for horizon in _horizons(target.strategy):
                if elapsed < horizon:
                    continue
                loaded_benchmarks = tuple(
                    self._repository.benchmark_returns_after(target.recommend_date, limit=horizon)
                )
                benchmarks = self._aligned_benchmarks(target.recommend_date, loaded_benchmarks, horizon)
                expected_dates = tuple(item.trade_date for item in benchmarks)
                outcomes.append(
                    evaluate_outcome(
                        target,
                        bars,
                        horizon=horizon,
                        benchmark_returns=tuple(item.return_pct for item in benchmarks),
                        expected_sessions=horizon,
                        expected_trade_dates=expected_dates,
                        settled_at=now,
                    )
                )
        if outcomes:
            self._repository.save_recommendation_outcomes(outcomes)
        return SettlementResult(
            target_count=len(targets),
            outcome_count=len(outcomes),
            completed_count=sum(item.status == "complete" for item in outcomes),
            benchmark_recorded=benchmark is not None,
        )

    def _aligned_benchmarks(
        self,
        recommend_date: str,
        values: tuple[BenchmarkReturn, ...],
        horizon: int,
    ) -> tuple[BenchmarkReturn, ...]:
        if len(values) < horizon:
            return ()
        selected = values[:horizon]
        for index, item in enumerate(selected, start=1):
            if self._session_distance(recommend_date, item.trade_date) != index:
                return ()
        return selected


def _horizons(strategy: Strategy) -> tuple[int, ...]:
    return (2, 3, 5) if strategy is Strategy.D25 else (1,)


def _equal_weight_benchmark(
    trade_date: str,
    market_features: Sequence[FeatureSnapshot],
) -> BenchmarkReturn | None:
    if not market_features or any(
        shanghai_now(feature.quote.source_time).date().isoformat() != trade_date
        or shanghai_now(feature.quote.source_time).hour < 15
        for feature in market_features
    ):
        return None
    returns = tuple(
        feature.quote.pct_change
        for feature in market_features
        if feature.quote.pct_change is not None and math.isfinite(feature.quote.pct_change)
    )
    if not returns:
        return None
    return BenchmarkReturn(trade_date, sum(returns) / len(returns))


__all__ = ["OutcomeSettlementService", "SettlementResult"]
