"""Background-only settlement of immutable formal scored decisions."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from trader.application.outcomes.ports import OutcomeSettlementMarketData
from trader.application.ports.outcomes import OutcomeTargetReaderPort, OutcomeWriterPort
from trader.application.ports.scheduler import SettlementPort
from trader.application.runtime.schedule import shanghai_now
from trader.domain.market.models import FeatureSnapshot
from trader.domain.outcome.evaluation import CanonicalOutcomeEvaluator, OutcomeEvaluationRequest
from trader.domain.outcome.models import BenchmarkConstituentReturn, BenchmarkReturn, outcome_horizons


@dataclass(frozen=True)
class SettlementResult:
    target_count: int
    outcome_count: int
    completed_count: int
    benchmark_recorded: bool


class OutcomeSettlementService:
    def __init__(
        self,
        market_data: OutcomeSettlementMarketData,
        targets: OutcomeTargetReaderPort,
        writer: OutcomeWriterPort,
        *,
        session_distance: Callable[[str, str], int | None],
        target_limit: int = 500,
    ) -> None:
        self._market_data = market_data
        self._targets = targets
        self._writer = writer
        self._session_distance = session_distance
        self._target_limit = target_limit
        self._evaluator = CanonicalOutcomeEvaluator()

    def settle(self, now: datetime, market_features: Sequence[FeatureSnapshot]) -> SettlementResult:
        local = shanghai_now(now)
        benchmark = self._equal_weight_benchmark(local.date().isoformat(), market_features)
        if benchmark is not None:
            self._writer.record_benchmark_return(benchmark, observed_at=now)
        targets = tuple(self._targets.pending_outcome_targets(limit=self._target_limit))
        if not targets:
            return SettlementResult(0, 0, 0, benchmark is not None)
        codes = tuple(dict.fromkeys(target.stock_code for target in targets))
        histories = self._market_data.read_outcome_bars(codes, now)
        outcomes = []
        current_date = local.date().isoformat()
        for target in targets:
            elapsed = self._session_distance(target.recommend_date, current_date)
            if elapsed is None:
                continue
            bars = tuple(
                bar
                for bar in histories.get(target.stock_code, ())
                if target.recommend_date <= bar.trade_date <= current_date
            )
            for horizon in target.pending_horizons or outcome_horizons(target.strategy):
                if elapsed < horizon:
                    continue
                loaded = tuple(self._targets.benchmark_returns_after(target.recommend_date, limit=horizon))
                benchmarks = self._aligned_benchmarks(target.recommend_date, loaded, horizon)
                expected_dates = tuple(item.trade_date for item in benchmarks)
                outcomes.append(
                    self._evaluator.evaluate(
                        OutcomeEvaluationRequest(
                            target=target,
                            bars=bars,
                            horizon=horizon,
                            benchmark_returns=tuple(item.return_pct for item in benchmarks),
                            expected_trade_dates=expected_dates,
                            settled_at=now,
                        )
                    )
                )
        if outcomes:
            self._writer.save_recommendation_outcomes(outcomes)
        return SettlementResult(
            len(targets),
            len(outcomes),
            sum(item.status == "complete" for item in outcomes),
            benchmark is not None,
        )

    def _equal_weight_benchmark(
        self,
        trade_date: str,
        market_features: Sequence[FeatureSnapshot],
    ) -> BenchmarkReturn | None:
        if any(
            shanghai_now(feature.quote.source_time).date().isoformat() != trade_date
            or shanghai_now(feature.quote.source_time).hour < 15
            for feature in market_features
        ):
            return None
        constituents = tuple(
            BenchmarkConstituentReturn(feature.quote.code, trade_date, value)
            for feature in market_features
            if (value := feature.quote.pct_change) is not None and math.isfinite(value)
        )
        if len(constituents) != len(market_features):
            return None
        return self._evaluator.equal_weight_benchmark(trade_date, constituents)

    def _aligned_benchmarks(
        self,
        recommend_date: str,
        values: tuple[BenchmarkReturn, ...],
        horizon: int,
    ) -> tuple[BenchmarkReturn, ...]:
        if len(values) < horizon:
            return ()
        selected = values[:horizon]
        if any(
            self._session_distance(recommend_date, item.trade_date) != index for index, item in enumerate(selected, 1)
        ):
            return ()
        return selected


class OutcomeSettlementAdapter(SettlementPort):
    """Run outcome settlement only from the scheduler's after-close control lane."""

    def __init__(
        self,
        market_data: OutcomeSettlementMarketData,
        service: OutcomeSettlementService,
    ) -> None:
        self._market_data = market_data
        self._service = service

    def settle(self, at: datetime) -> None:
        features = self._market_data.fetch_market_features(at, force=True)
        self._service.settle(at, features)


__all__ = [
    "OutcomeSettlementMarketData",
    "OutcomeSettlementService",
    "SettlementResult",
    "OutcomeSettlementAdapter",
]
