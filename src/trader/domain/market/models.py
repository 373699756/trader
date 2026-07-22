"""Immutable point-in-time market values."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from trader.domain.review.models import RiskFact


class Board(str, Enum):
    MAIN = "main"
    CHINEXT = "chinext"
    STAR = "star"
    UNSUPPORTED = "unsupported"


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
class Evidence:
    evidence_id: str
    evidence_type: str
    title: str
    source: str
    published_at: datetime
    received_at: datetime | None = None
    data_version: str = ""


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


__all__ = [
    "Board",
    "BoardPopulation",
    "CanonicalMarketSnapshot",
    "CrossSectionStats",
    "Evidence",
    "FeatureSnapshot",
    "LiveQuote",
    "MarketQuote",
]
