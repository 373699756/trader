"""Structured-review ports."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from trader.application.ports.types import JsonObject
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import Strategy
from trader.domain.review.models import DeepSeekReview, ReviewCandidateContext


class DeepSeekReviewPort(Protocol):
    def review(
        self,
        strategy: Strategy,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
        contexts: Mapping[str, ReviewCandidateContext] | None = None,
    ) -> Mapping[str, DeepSeekReview]: ...

    def preheat(
        self, candidates: Sequence[FeatureSnapshot], *, phase: str, deadline: datetime
    ) -> Mapping[str, DeepSeekReview]: ...

    def status(self) -> JsonObject: ...
