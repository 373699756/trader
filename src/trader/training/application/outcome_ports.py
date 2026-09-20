"""Typed protocol for formal recommendation outcome settlement."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.training.domain.evaluation.models import (
    BenchmarkReturn,
    OutcomeBar,
    OutcomeTarget,
    RecommendationOutcome,
)


class OutcomeSettlementMarketData(Protocol):
    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
    ) -> Sequence[FeatureSnapshot]: ...

    def read_outcome_bars(
        self, codes: Sequence[str], observed_at: datetime
    ) -> Mapping[str, tuple[OutcomeBar, ...]]: ...


class OutcomeTargetReaderPort(Protocol):
    def pending_outcome_targets(self, *, limit: int) -> Sequence[OutcomeTarget]: ...

    def benchmark_returns_after(self, recommend_date: str, *, limit: int) -> Sequence[BenchmarkReturn]: ...


class OutcomeWriterPort(Protocol):
    def record_benchmark_return(self, benchmark: BenchmarkReturn, *, observed_at: datetime) -> None: ...

    def save_recommendation_outcomes(self, outcomes: Sequence[RecommendationOutcome]) -> None: ...


__all__ = ["OutcomeSettlementMarketData", "OutcomeTargetReaderPort", "OutcomeWriterPort"]
