"""Immutable values shared by the recommendation domain."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Literal


class Strategy(str, Enum):
    TODAY = "today"
    TOMORROW = "tomorrow"
    D25 = "d25"
    LONG = "long"


class Board(str, Enum):
    MAIN = "main"
    CHINEXT = "chinext"
    STAR = "star"
    UNSUPPORTED = "unsupported"


class RecommendationAction(str, Enum):
    EXECUTABLE = "executable"
    OBSERVE = "observe"
    UNAVAILABLE = "unavailable"


class FusionMode(str, Enum):
    HYBRID = "hybrid"
    LOCAL_DEGRADED = "local_degraded"


class ReviewOutcome(str, Enum):
    APPLIED = "applied"
    ABSTAIN = "abstain"
    REJECTED = "rejected"
    LATE = "late"


@dataclass(frozen=True)
class MarketQuote:
    code: str
    name: str
    price: float | None
    previous_close: float | None
    open_price: float | None
    high: float | None
    low: float | None
    pct_change: float | None
    change_5m: float | None
    speed: float | None
    volume_ratio: float | None
    turnover_rate: float | None
    amount: float | None
    amplitude: float | None
    market_cap: float | None
    industry: str
    source: str
    source_time: datetime
    received_time: datetime
    data_version: str
    is_st: bool = False
    is_suspended: bool = False
    is_one_price_limit: bool = False
    is_blacklisted: bool = False
    has_major_regulatory_risk: bool = False
    cross_source_deviation_pct: float | None = None
    cross_source_verified: bool = True
    board: Board = Board.UNSUPPORTED
    board_source: str = ""
    board_reliability: str = "unknown"
    exchange: str = ""
    listing_date: date | None = None
    listing_age_sessions: int | None = None
    is_relisted_first_session: bool | None = None
    is_delisting_period_first_session: bool | None = None
    has_price_limit: bool | None = None
    exchange_limit_pct: float | None = None
    strategy_hot_cap_pct: float | None = None
    rule_version: str = ""
    rule_effective_date: date | None = None
    execution_restrictions: tuple[str, ...] = ()

    def age_seconds(self, now: datetime) -> float:
        return max(0.0, (now - self.source_time).total_seconds())


@dataclass(frozen=True)
class CanonicalMarketSnapshot:
    observed_at: datetime
    merge_epoch: str
    quotes: tuple[MarketQuote, ...]
    field_sources: Mapping[str, Mapping[str, str]]
    source_versions: Mapping[str, str]
    conflicts: tuple[str, ...]
    missing_reasons: Mapping[str, str]
    degraded_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("canonical snapshot observed_at must be timezone-aware")
        if not self.merge_epoch:
            raise ValueError("canonical snapshot merge_epoch must not be empty")
        if tuple(sorted(quote.code for quote in self.quotes)) != tuple(quote.code for quote in self.quotes):
            raise ValueError("canonical snapshot quotes must be sorted by code")
        nested = {str(code): MappingProxyType(dict(sources)) for code, sources in self.field_sources.items()}
        object.__setattr__(self, "field_sources", MappingProxyType(nested))
        object.__setattr__(self, "source_versions", MappingProxyType(dict(self.source_versions)))
        object.__setattr__(self, "missing_reasons", MappingProxyType(dict(self.missing_reasons)))


@dataclass(frozen=True)
class LiveQuote:
    code: str
    price: float | None
    pct_change: float | None
    source: str
    source_time: datetime
    received_time: datetime
    data_version: str

    def __post_init__(self) -> None:
        for value in (self.source_time, self.received_time):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("live quote times must be timezone-aware")

    def age_seconds(self, now: datetime) -> float:
        return max(0.0, (now - self.source_time).total_seconds())


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
class Evidence:
    evidence_id: str
    evidence_type: str
    title: str
    source: str
    published_at: datetime
    received_at: datetime | None = None
    data_version: str = ""


@dataclass(frozen=True)
class RiskFact:
    risk_fact_id: str
    risk_code: str
    severity: str
    penalty: float
    source: str
    observed_at: datetime
    confidence: float = 1.0
    evidence_ids: tuple[str, ...] = ()
    group: str = ""
    veto: bool = False
    threshold: str = ""
    actual: str | float | bool | None = None
    assessment: str = ""


@dataclass(frozen=True)
class RiskRule:
    risk_code: str
    severity: str
    penalty: float
    minimum_confidence: float
    group: str
    evidence_ttl_hours: int = 876_000
    veto: bool = False
    allowed_evidence_types: tuple[str, ...] = ()
    strategies: tuple[str, ...] = ()
    trigger_factor: str = ""
    trigger_operator: str = ""
    trigger_thresholds: tuple[float, ...] = ()
    combination_mode: str = "exclusive"
    risk_fact_id_fields: tuple[str, ...] = ()
    local_trigger_enabled: bool = True


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
class CrossSectionStats:
    lower_bound: float | None
    upper_bound: float | None
    sample_size: int
    missing_count: int
    lower_quantile: float
    upper_quantile: float
    population_data_version: str


@dataclass(frozen=True)
class BoardPopulation:
    trade_date: str
    phase: str
    board: Board
    data_version: str
    schema_version: str
    population_version: str
    sample_size: int
    missing_count: int
    liquidity_p50: float | None
    liquidity_p80: float | None
    fallback_trade_date: str | None = None
    fallback_age_sessions: int | None = None
    status: Literal["current", "fallback", "stale", "insufficient"] = "current"

    def __post_init__(self) -> None:
        if not all((self.trade_date, self.phase, self.data_version, self.schema_version, self.population_version)):
            raise ValueError("board population identity must not be empty")
        if self.board is Board.UNSUPPORTED:
            raise ValueError("board population requires a supported board")
        if self.sample_size < 0 or self.missing_count < 0:
            raise ValueError("board population counts cannot be negative")
        for value in (self.liquidity_p50, self.liquidity_p80):
            if value is not None and not math.isfinite(value):
                raise ValueError("board liquidity quantiles must be finite when present")
        if self.fallback_age_sessions is not None and self.fallback_age_sessions < 0:
            raise ValueError("board population fallback age cannot be negative")
        if (self.fallback_trade_date is None) != (self.fallback_age_sessions is None):
            raise ValueError("board population fallback date and age must be recorded together")
        if self.status == "current" and self.fallback_trade_date is not None:
            raise ValueError("current board population cannot declare a fallback")


@dataclass(frozen=True)
class FeatureSnapshot:
    quote: MarketQuote
    values: Mapping[str, float | None]
    observed_at: datetime
    history_days: int = 0
    market_regime: str = "neutral"
    missing_fields: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    external_risk_facts: tuple[RiskFact, ...] = ()
    normalization: Mapping[str, CrossSectionStats] = field(default_factory=dict)
    missing_reasons: Mapping[str, str] = field(default_factory=dict)
    board_data_reliability: float = 1.0
    board_supported_weight: float = 1.0
    board_policy_id: str = ""
    board_policy_version: str = ""
    board_population: BoardPopulation | None = None
    merge_epoch: str = ""
    competition_group_id: str = ""
    competition_group_source: str = ""
    competition_group_version: str = ""
    liquidity_bucket: str = ""
    parameter_status: str = "current"
    selection_skip_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "normalization", MappingProxyType(dict(self.normalization)))
        object.__setattr__(self, "missing_reasons", MappingProxyType(dict(self.missing_reasons)))
        if not math.isfinite(self.board_data_reliability) or not 0.0 <= self.board_data_reliability <= 1.0:
            raise ValueError("board data reliability must be in [0, 1]")
        if not math.isfinite(self.board_supported_weight) or not 0.0 <= self.board_supported_weight <= 1.0:
            raise ValueError("board supported weight must be in [0, 1]")

    def value(self, name: str, default: float = 50.0) -> float:
        raw = self.values.get(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return default
        return value if math.isfinite(value) else default

    def optional_value(self, name: str) -> float | None:
        raw = self.values.get(name)
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) else None

    def missing_ratio(self, field_names: tuple[str, ...]) -> float:
        if not field_names:
            return 0.0
        missing = sum(1 for name in field_names if self.optional_value(name) is None)
        return missing / len(field_names)


@dataclass(frozen=True)
class DimensionAssessment:
    name: str
    score: float
    confidence: float
    assessment: str
    flags: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    is_unknown: bool = False


@dataclass(frozen=True)
class DeepSeekReview:
    code: str
    outcome: ReviewOutcome
    dimensions: Mapping[str, DimensionAssessment]
    risk_facts: tuple[RiskFact, ...]
    completed_at: datetime
    error: str = ""
    rating: str = "neutral"
    review_stage: str = "primary"
    challenger_status: str = "not_run"
    requested_model: str | None = None
    actual_model: str | None = None
    thinking_mode: str | None = None
    raw_confidence: float | None = None
    calibrated_confidence: float | None = None
    evidence_manifest_hash: str | None = None
    calibration_version: str | None = None
    model_role: str | None = None
    reasoning_effort: str | None = None
    system_fingerprint: str | None = None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    challenger_requested_model: str | None = None
    challenger_actual_model: str | None = None
    challenger_thinking_mode: str | None = None
    challenger_reasoning_effort: str | None = None
    challenger_system_fingerprint: str | None = None
    challenger_prompt_cache_hit_tokens: int | None = None
    challenger_prompt_cache_miss_tokens: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimensions", MappingProxyType(dict(self.dimensions)))


@dataclass(frozen=True)
class ReviewCandidateContext:
    local_score: float
    local_rank: int
    action_threshold: float | None
    in_protection_set: bool
    has_new_high_risk: bool = False
    near_action_threshold: bool = False
    near_global_boundary: bool = False
    direction_conflict: bool = False
    evidence_conflict: bool = False
    was_reviewed: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.local_score):
            raise ValueError("review context local score must be finite")
        if self.local_rank < 1:
            raise ValueError("review context local rank must be positive")
        if self.action_threshold is not None and not math.isfinite(self.action_threshold):
            raise ValueError("review context action threshold must be finite")


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
        object.__setattr__(
            self,
            "structured_risk_thresholds",
            MappingProxyType(dict(self.structured_risk_thresholds)),
        )
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
            strategy: MappingProxyType(
                {board: MappingProxyType(dict(weights)) for board, weights in boards.items()}
            )
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
    "Board",
    "BoardPopulation",
    "BoardScoreBatch",
    "BoardStrategyPolicy",
    "CanonicalMarketSnapshot",
    "CrossSectionStats",
    "DeepSeekReview",
    "DimensionAssessment",
    "Evidence",
    "FilterAudit",
    "FeatureSnapshot",
    "FrozenReplayPolicy",
    "FusionMode",
    "LiveOverlay",
    "LiveQuote",
    "MarketQuote",
    "Recommendation",
    "RecommendationAction",
    "RecommendationReplayInput",
    "RecommendationSnapshot",
    "SelectionSkip",
    "ReviewCandidateContext",
    "ReviewOutcome",
    "RiskFact",
    "RiskRule",
    "ScoreBreakdown",
    "Strategy",
]
