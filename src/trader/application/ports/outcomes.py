"""Outcome read/write ports for immutable V2 research evidence."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from trader.domain.outcome.models import BenchmarkReturn, OutcomeTarget, RecommendationOutcome


class OutcomeTargetReaderPort(Protocol):
    def pending_outcome_targets(self, *, limit: int) -> Sequence[OutcomeTarget]: ...

    def benchmark_returns_after(self, recommend_date: str, *, limit: int) -> Sequence[BenchmarkReturn]: ...


class OutcomeWriterPort(Protocol):
    def record_benchmark_return(self, benchmark: BenchmarkReturn, *, observed_at: datetime) -> None: ...

    def save_recommendation_outcomes(self, outcomes: Sequence[RecommendationOutcome]) -> None: ...


__all__ = ["OutcomeTargetReaderPort", "OutcomeWriterPort"]
