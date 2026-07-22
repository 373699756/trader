"""Outcome read/write ports."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from trader.domain.market.models import FeatureSnapshot
from trader.domain.outcome.models import BenchmarkReturn, OutcomeTarget, RecommendationOutcome


class OutcomeTargetReaderPort(Protocol):
    def pending_outcome_targets(self, *, limit: int) -> Sequence[OutcomeTarget]: ...

    def benchmark_returns_after(self, recommend_date: str, *, limit: int) -> Sequence[BenchmarkReturn]: ...


class OutcomeWriterPort(Protocol):
    def record_benchmark_return(self, benchmark: BenchmarkReturn, *, observed_at: datetime) -> None: ...

    def save_recommendation_outcomes(self, outcomes: Sequence[RecommendationOutcome]) -> None: ...


class OutcomeSettlementResult(Protocol):
    @property
    def completed_count(self) -> int: ...

    @property
    def benchmark_recorded(self) -> bool: ...


class OutcomeSettlementPort(Protocol):
    def settle(self, now: datetime, market_features: Sequence[FeatureSnapshot]) -> OutcomeSettlementResult: ...
