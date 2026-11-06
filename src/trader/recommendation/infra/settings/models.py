"""Immutable recommendation strategy and watchlist settings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from trader.recommendation.domain.market.news import NewsSignalPolicy
from trader.recommendation.domain.market.research import (
    FeatureComponentWeightPolicy,
    LongResearchPolicy,
    MarketRegimePolicy,
)
from trader.recommendation.domain.market.tail import TailSignalPolicy
from trader.recommendation.domain.scoring.company import CompanyScoringPolicy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId


@dataclass(frozen=True)
class FusionSettings:
    version: str
    confidence_coverage_min: float
    minimum_known_dimensions: int
    local_risk_cap: float
    score_decimals: int
    rounding: str


@dataclass(frozen=True)
class SelectionSettings:
    default_top_k: int
    maximum_top_k: int
    observation_margin: float
    thresholds: Mapping[str, float]
    maximum_board_fraction: float
    competition_group_limits: Mapping[str, int]
    minimum_board_reliability: float
    review_candidate_limit: int


@dataclass(frozen=True)
class HardFilterSettings:
    blacklist_codes: tuple[str, ...]
    structured_risk_thresholds: Mapping[str, float]


@dataclass(frozen=True)
class RiskRuleSettings:
    risk_code: str
    severity: str
    penalty: float
    minimum_confidence: float
    evidence_ttl_hours: int
    group: str
    veto: bool
    allowed_evidence_types: tuple[str, ...]
    strategies: tuple[str, ...]
    trigger_factor: str
    trigger_operator: str
    trigger_thresholds: tuple[float, ...]
    combination_mode: str
    risk_fact_id_fields: tuple[str, ...]
    local_trigger_enabled: bool


@dataclass(frozen=True)
class FactorDefinition:
    factor_id: str
    strategies: tuple[str, ...]
    raw_inputs: tuple[str, ...]
    formula: str
    unit: str
    direction: str
    observation_time: str
    adjustment: str
    lookback_window: int
    minimum_samples: int
    winsor_enabled: bool
    winsor_lower_quantile: float
    winsor_upper_quantile: float
    normalization: str
    missing_policy: str
    output_range: tuple[float, float]


@dataclass(frozen=True)
class StrategySettings:
    strategy_version: str
    scoring_profile: ScoringProfileId
    fusion: FusionSettings
    selection: SelectionSettings
    hard_filters: HardFilterSettings
    news_signal: NewsSignalPolicy
    tomorrow_tail_signal: TailSignalPolicy
    market_regime: MarketRegimePolicy
    long_research: LongResearchPolicy
    dimension_weights: Mapping[str, Mapping[str, float]]
    board_policy_version: str
    board_local_strategy_weights: Mapping[str, Mapping[str, Mapping[str, float]]]
    local_component_weights: Mapping[str, Mapping[str, Mapping[str, float]]]
    feature_component_weights: FeatureComponentWeightPolicy
    risk_rules: tuple[RiskRuleSettings, ...]
    factor_contract: Mapping[str, object]
    factor_index: Mapping[str, FactorDefinition]
    company_scoring: CompanyScoringPolicy


@dataclass(frozen=True)
class LongWatchItem:
    code: str
    name: str
    industry: str
    target_price: float | None
    selected_on: date
    selection_price: float


@dataclass(frozen=True)
class LongWatchGroupSection:
    source_section: str
    codes: tuple[str, ...]


@dataclass(frozen=True)
class LongWatchGroup:
    name: str
    category: str
    codes: tuple[str, ...]
    source: str
    source_section: str
    sections: tuple[LongWatchGroupSection, ...]


@dataclass(frozen=True)
class LongWatchlist:
    watchlist_version: str
    items: tuple[LongWatchItem, ...]
    groups: tuple[LongWatchGroup, ...] = ()
