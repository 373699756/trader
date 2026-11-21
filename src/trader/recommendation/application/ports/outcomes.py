"""Recommendation-owned ports for formal outcome settlement."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.outcomes.models import (
    BenchmarkReturn,
    D25CloseEvidence,
    D25RecommendationOutcome,
    OutcomeBar,
    OutcomeTarget,
    RecommendationOutcome,
    TomorrowCloseEvidence,
    TomorrowRecommendationOutcome,
)


class OutcomeEvidenceConflictError(RuntimeError):
    """Persisted immutable outcome evidence conflicts with its declared content."""


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
    def benchmark_return(self, trade_date: str) -> BenchmarkReturn | None: ...

    def pending_outcome_targets(self, *, limit: int) -> Sequence[OutcomeTarget]: ...

    def benchmark_returns_after(self, recommend_date: str, *, limit: int) -> Sequence[BenchmarkReturn]: ...


class OutcomeWriterPort(Protocol):
    def record_benchmark_return(self, benchmark: BenchmarkReturn, *, observed_at: datetime) -> None: ...

    def save_recommendation_outcomes(self, outcomes: Sequence[RecommendationOutcome]) -> None: ...


class TomorrowSettlementMarketData(Protocol):
    def read_tomorrow_close_evidence(
        self, targets: Sequence[OutcomeTarget], observed_at: datetime
    ) -> Sequence[TomorrowCloseEvidence]: ...


class TomorrowOutcomeEvidencePort(Protocol):
    def pending_tomorrow_targets(self, *, limit: int) -> Sequence[OutcomeTarget]: ...

    def save_tomorrow_outcomes(self, outcomes: Sequence[TomorrowRecommendationOutcome]) -> None: ...


class D25SettlementMarketData(Protocol):
    def read_d25_close_evidence(
        self, targets: Sequence[OutcomeTarget], observed_at: datetime,
    ) -> Sequence[D25CloseEvidence]: ...


class D25OutcomeEvidencePort(Protocol):
    def pending_d25_targets(self, *, limit: int) -> Sequence[OutcomeTarget]: ...

    def save_d25_outcomes(self, outcomes: Sequence[D25RecommendationOutcome]) -> None: ...


__all__ = ["OutcomeEvidenceConflictError", "OutcomeSettlementMarketData", "OutcomeTargetReaderPort", "OutcomeWriterPort"]
