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
    stale: bool = False
    frozen: bool = False
    degraded_reasons: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "filter_reasons", MappingProxyType(dict(self.filter_reasons)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


__all__ = [
    "Board",
    "DeepSeekReview",
    "DimensionAssessment",
    "Evidence",
    "FeatureSnapshot",
    "FusionMode",
    "MarketQuote",
    "Recommendation",
    "RecommendationAction",
    "RecommendationSnapshot",
    "ReviewOutcome",
    "RiskFact",
    "RiskRule",
    "ScoreBreakdown",
    "Strategy",
]
