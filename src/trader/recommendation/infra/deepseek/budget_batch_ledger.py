"""Typed batch inputs; persistence belongs solely to DeepSeekBudgetLedger."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from trader.recommendation.domain.evidence.review import DeepSeekReview
from trader.recommendation.domain.publication.models import Strategy


@dataclass(frozen=True)
class BudgetBatchRequest:
    strategy: Strategy
    phase: str
    bucket: str
    model: str
    requested_at: datetime
    deadline: datetime
    candidate_codes: Sequence[str]
    cache_hit_count: int = 0
    profile: str = "v2"


@dataclass(frozen=True)
class BudgetBatchCompletion:
    batch_id: str
    status: str
    completed_at: datetime
    reviews: Mapping[str, DeepSeekReview]
    physical_attempts: int
    error: str = ""


__all__ = ["BudgetBatchRequest", "BudgetBatchCompletion"]
