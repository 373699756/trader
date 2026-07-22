"""Immutable recommendation policies, scores, selections, and snapshots."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from trader.domain.market.models import (
    Board,
    FeatureSnapshot,
    LiveQuote,
)
from trader.domain.review.models import DeepSeekReview, RiskFact, RiskRule

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
class LiveOverlay:
    snapshot_id: str
    strategy: Strategy
    trade_date: str
    version: str
    observed_at: datetime
    quotes: Mapping[str, LiveQuote]
    closing: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "quotes", MappingProxyType(dict(self.quotes)))
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("live overlay time must be timezone-aware")
        if any(code != quote.code for code, quote in self.quotes.items()):
            raise ValueError("live overlay quote keys must match quote codes")
        if any(quote.source_time > self.observed_at for quote in self.quotes.values()):
            raise ValueError("live overlay cannot contain future quotes")


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


@dataclass(frozen=True)
class BoardScoreBatch:
    board: Board
    strategy: Strategy
    merge_epoch: str
    policy_id: str
    status: Literal["success", "empty", "degraded", "failed"]
    recommendations: tuple[Recommendation, ...]
    degraded_reasons: tuple[str, ...] = ()
    policy_version: str = ""
    population_version: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"success", "empty", "degraded", "failed"}:
            raise ValueError("unsupported board score batch status")
        if self.board is Board.UNSUPPORTED or self.strategy is Strategy.LONG:
            raise ValueError("board score batches only support active short strategies")
        if not self.merge_epoch or not self.policy_id:
            raise ValueError("board score batch identity must not be empty")
        if any(
            item.strategy is not self.strategy
            or item.features.quote.board is not self.board
            or item.features.board_policy_id != self.policy_id
            for item in self.recommendations
        ):
            raise ValueError("board score batch recommendations must match its board policy")


@dataclass(frozen=True)
class FrozenReplayPolicy:
    strategy_version: str
    fusion_version: str
    local_weight: float
    deepseek_weight: float
    confidence_coverage_min: float
    minimum_known_dimensions: int
    local_risk_cap: float
    deepseek_risk_cap: float
    default_top_k: int
    maximum_top_k: int
    maximum_per_industry: int
    observation_margin: float
    thresholds: Mapping[str, float]
    candidate_weights: Mapping[str, float]
    dimension_weights: Mapping[str, Mapping[str, float]]
    local_strategy_weights: Mapping[str, Mapping[str, float]]
    risk_rules: Mapping[str, RiskRule]
    blacklist_codes: tuple[str, ...] = ()
    structured_risk_thresholds: Mapping[str, float] = field(default_factory=dict)
    maximum_board_fraction: float = 1.0
    competition_group_limits: Mapping[str, int] = field(default_factory=dict)
    candidate_min_score: float = 0.0
    minimum_board_reliability: float = 0.0
    board_policy_version: str = ""
    board_candidate_weights: Mapping[str, Mapping[str, Mapping[str, float]]] = field(default_factory=dict)
    board_local_strategy_weights: Mapping[str, Mapping[str, Mapping[str, float]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "thresholds", MappingProxyType(dict(self.thresholds)))
        object.__setattr__(self, "candidate_weights", MappingProxyType(dict(self.candidate_weights)))
        object.__setattr__(
            self,
            "dimension_weights",
            MappingProxyType(
                {name: MappingProxyType(dict(weights)) for name, weights in self.dimension_weights.items()}
            ),
        )
        object.__setattr__(
            self,
            "local_strategy_weights",
            MappingProxyType(
                {name: MappingProxyType(dict(weights)) for name, weights in self.local_strategy_weights.items()}
            ),
        )
        object.__setattr__(self, "risk_rules", MappingProxyType(dict(self.risk_rules)))
        object.__setattr__(self, "blacklist_codes", tuple(self.blacklist_codes))
        object.__setattr__(self, "structured_risk_thresholds", MappingProxyType(dict(self.structured_risk_thresholds)))
        object.__setattr__(self, "competition_group_limits", MappingProxyType(dict(self.competition_group_limits)))
        object.__setattr__(self, "board_candidate_weights", _freeze_nested_board_weights(self.board_candidate_weights))
        object.__setattr__(
            self,
            "board_local_strategy_weights",
            _freeze_nested_board_weights(self.board_local_strategy_weights),
        )


@dataclass(frozen=True)
class RecommendationReplayInput:
    schema_version: str
    algorithm_version: str
    policy: FrozenReplayPolicy
    evaluated_at: datetime
    market_features: tuple[FeatureSnapshot, ...]
    requested_codes: tuple[str, ...]
    candidate_features: tuple[FeatureSnapshot, ...]
    reviews: Mapping[str, DeepSeekReview]
    preselect_max_age_seconds: float
    score_max_age_seconds: float
    candidate_pool_size: int
    target_prices: Mapping[str, float | None] = field(default_factory=dict)
    board_batches: tuple[BoardScoreBatch, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_features", tuple(self.market_features))
        object.__setattr__(self, "requested_codes", tuple(self.requested_codes))
        object.__setattr__(self, "candidate_features", tuple(self.candidate_features))
        object.__setattr__(self, "reviews", MappingProxyType(dict(self.reviews)))
        object.__setattr__(self, "target_prices", MappingProxyType(dict(self.target_prices)))
        object.__setattr__(self, "board_batches", tuple(self.board_batches))
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("replay evaluation time must be timezone-aware")
        if self.candidate_pool_size < 0:
            raise ValueError("replay candidate pool size cannot be negative")
        if self.preselect_max_age_seconds < 0 or self.score_max_age_seconds < 0:
            raise ValueError("replay quote age thresholds cannot be negative")
        if len(set(self.requested_codes)) != len(self.requested_codes):
            raise ValueError("replay requested codes must be unique")
        market_codes = tuple(feature.quote.code for feature in self.market_features)
        candidate_codes = tuple(feature.quote.code for feature in self.candidate_features)
        if len(set(market_codes)) != len(market_codes):
            raise ValueError("replay market feature codes must be unique")
        if len(set(candidate_codes)) != len(candidate_codes):
            raise ValueError("replay candidate feature codes must be unique")
        for code, review in self.reviews.items():
            if code != review.code or code not in candidate_codes:
                raise ValueError("replay reviews must match candidate feature codes")


def _freeze_nested_board_weights(
    values: Mapping[str, Mapping[str, Mapping[str, float]]],
) -> Mapping[str, Mapping[str, Mapping[str, float]]]:
    return MappingProxyType(
        {
            strategy: MappingProxyType({board: MappingProxyType(dict(weights)) for board, weights in boards.items()})
            for strategy, boards in values.items()
        }
    )


@dataclass(frozen=True)
class RecommendationSnapshot:
    snapshot_id: str
    strategy: Strategy
    trade_date: str
    phase: str
    data_version: str
    strategy_version: str
    fusion_version: str
    fusion_mode: FusionMode
    published_at: datetime
    recommendations: tuple[Recommendation, ...]
    filtered_count: int
    filter_reasons: Mapping[str, int]
    config_version: str = ""
    filter_details: tuple[FilterAudit, ...] = ()
    stale: bool = False
    frozen: bool = False
    degraded_reasons: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    replay_input: RecommendationReplayInput | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "filter_reasons", MappingProxyType(dict(self.filter_reasons)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


__all__ = [
    "BoardScoreBatch",
    "BoardStrategyPolicy",
    "FilterAudit",
    "FrozenReplayPolicy",
    "FusionMode",
    "LiveOverlay",
    "Recommendation",
    "RecommendationAction",
    "RecommendationReplayInput",
    "RecommendationSnapshot",
    "ScoreBreakdown",
    "SelectionSkip",
    "Strategy",
]
