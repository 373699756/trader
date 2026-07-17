"""Immutable values shared by the recommendation domain."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType


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

    def age_seconds(self, now: datetime) -> float:
        return max(0.0, (now - self.source_time).total_seconds())


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def value(self, name: str, default: float = 50.0) -> float:
        raw = self.values.get(name)
        if raw is None:
            return default
        return float(raw)

    def optional_value(self, name: str) -> float | None:
        raw = self.values.get(name)
        return None if raw is None else float(raw)

    def missing_ratio(self, field_names: tuple[str, ...]) -> float:
        if not field_names:
            return 0.0
        missing = sum(1 for name in field_names if self.values.get(name) is None)
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimensions", MappingProxyType(dict(self.dimensions)))


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
    target_price: float | None = None


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
    risk_rules: Mapping[str, RiskRule]

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
        object.__setattr__(self, "risk_rules", MappingProxyType(dict(self.risk_rules)))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_features", tuple(self.market_features))
        object.__setattr__(self, "requested_codes", tuple(self.requested_codes))
        object.__setattr__(self, "candidate_features", tuple(self.candidate_features))
        object.__setattr__(self, "reviews", MappingProxyType(dict(self.reviews)))
        object.__setattr__(self, "target_prices", MappingProxyType(dict(self.target_prices)))
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
    "DeepSeekReview",
    "DimensionAssessment",
    "Evidence",
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
    "ReviewOutcome",
    "RiskFact",
    "RiskRule",
    "ScoreBreakdown",
    "Strategy",
]
