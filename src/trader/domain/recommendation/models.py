"""Immutable V2 recommendation policies, scores, and selections."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING

from trader.domain.market.models import Board, FeatureSnapshot
from trader.domain.review.models import DeepSeekReview, RiskFact

if TYPE_CHECKING:
    from trader.domain.recommendation.downside import DownsideAssessment


class Strategy(str, Enum):
    TODAY = "today"
    TOMORROW = "tomorrow"
    D25 = "d25"
    LONG = "long"


class RecommendationAction(str, Enum):
    EXECUTABLE = "executable"
    OBSERVE = "observe"
    UNAVAILABLE = "unavailable"


class FusionMode(str, Enum):
    HYBRID = "hybrid"
    LOCAL_DEGRADED = "local_degraded"


@dataclass(frozen=True)
class FilterAudit:
    stock_code: str
    filter_code: str
    threshold: str
    actual: str | float | bool | None
    source: str
    observed_at: datetime

    @property
    def code(self) -> str:
        return self.filter_code


@dataclass(frozen=True)
class ScoreBreakdown:
    components: Mapping[str, float]
    base_score: float
    local_risk_penalty: float
    local_score: float
    deepseek_score: float | None
    confidence_coverage: float
    deepseek_risk_penalty: float
    final_score: float
    fusion_mode: FusionMode
    fusion_applied: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))


@dataclass(frozen=True)
class Recommendation:
    strategy: Strategy
    features: FeatureSnapshot
    score: ScoreBreakdown
    local_risk_facts: tuple[RiskFact, ...]
    deepseek_risk_facts: tuple[RiskFact, ...]
    review: DeepSeekReview | None
    action: RecommendationAction
    action_reason: str
    veto: bool
    rank: int = 0
    board_rank: int = 0
    target_price: float | None = None
    selection_skip_reason: str = ""
    competition_group_limit: int | None = None
    downside: DownsideAssessment | None = None


@dataclass(frozen=True)
class SelectionSkip:
    stock_code: str
    board: Board
    competition_group_id: str
    board_rank: int
    global_rank: int
    reason: str
    limit: int | None
    policy_version: str
    observed_at: datetime


@dataclass(frozen=True)
class BoardStrategyPolicy:
    policy_id: str
    version: str
    board: Board
    strategy: Strategy
    candidate_weights: Mapping[str, float]
    local_weights: Mapping[str, float]
    candidate_min_score: float = 50.0
    minimum_reliability: float = 0.85

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_weights", MappingProxyType(dict(self.candidate_weights)))
        object.__setattr__(self, "local_weights", MappingProxyType(dict(self.local_weights)))
        if not self.policy_id or not self.version:
            raise ValueError("board strategy policy identity must not be empty")
        if self.board is Board.UNSUPPORTED or self.strategy is Strategy.LONG:
            raise ValueError("board strategy policies only support the three active short strategies")
        for name, weights in (("candidate", self.candidate_weights), ("local", self.local_weights)):
            if not weights or any(not math.isfinite(value) or value < 0.0 for value in weights.values()):
                raise ValueError(f"{name} weights must contain finite non-negative values")
            if abs(sum(weights.values()) - 1.0) > 1e-9:
                raise ValueError(f"{name} weights must sum to 1.0")
        if not math.isfinite(self.candidate_min_score) or not 0.0 <= self.candidate_min_score <= 100.0:
            raise ValueError("candidate minimum score must be in [0, 100]")
        if not math.isfinite(self.minimum_reliability) or not 0.0 <= self.minimum_reliability <= 1.0:
            raise ValueError("minimum reliability must be in [0, 1]")


__all__ = [
    "BoardStrategyPolicy",
    "FilterAudit",
    "FusionMode",
    "Recommendation",
    "RecommendationAction",
    "ScoreBreakdown",
    "SelectionSkip",
    "Strategy",
]
