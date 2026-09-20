"""Immutable coherent market snapshot shared across recommendation boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from trader.recommendation.domain.market.epochs import CandidateQuoteEpoch, DailyFeaturePack, MarketEpoch, ResearchEpoch

_SHANGHAI_TIMEZONE = "Asia/Shanghai"
_FAILURE_REASON = re.compile(r"^[a-z0-9_]{1,64}$")


class DataPlaneChannel(str, Enum):
    DAILY_FEATURES = "daily_features"
    MARKET = "market"
    CANDIDATE_QUOTES = "candidate_quotes"
    RESEARCH = "research"


@dataclass(frozen=True)
class DataPlaneFailure:
    reason: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if _FAILURE_REASON.fullmatch(self.reason) is None:
            raise ValueError("data-plane failure reason must be a structured code")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("data-plane failure time must be timezone-aware")
        if getattr(self.observed_at.tzinfo, "key", None) != _SHANGHAI_TIMEZONE:
            raise ValueError("data-plane failure time must use Asia/Shanghai")


@dataclass(frozen=True)
class DataPlaneCoverageSummary:
    potential_executable_count: int = 0
    security_master_covered_count: int = 0
    candidate_count: int = 0
    candidate_core_history_covered_count: int = 0

    def __post_init__(self) -> None:
        counts = (
            self.potential_executable_count,
            self.security_master_covered_count,
            self.candidate_count,
            self.candidate_core_history_covered_count,
        )
        if any(count < 0 for count in counts):
            raise ValueError("data-plane coverage counts cannot be negative")
        if self.security_master_covered_count > self.potential_executable_count:
            raise ValueError("security-master coverage cannot exceed the executable population")
        if self.candidate_core_history_covered_count > self.candidate_count:
            raise ValueError("candidate history coverage cannot exceed the candidate population")

    @property
    def security_master_ratio(self) -> float | None:
        if self.potential_executable_count == 0:
            return None
        return self.security_master_covered_count / self.potential_executable_count

    @property
    def candidate_core_history_ratio(self) -> float | None:
        if self.candidate_count == 0:
            return None
        return self.candidate_core_history_covered_count / self.candidate_count


@dataclass(frozen=True)
class MarketDataPlaneSnapshot:
    daily_features: DailyFeaturePack | None
    market: MarketEpoch | None
    candidate_quotes: CandidateQuoteEpoch | None
    research: ResearchEpoch | None
    failures: Mapping[DataPlaneChannel, DataPlaneFailure] = field(default_factory=lambda: MappingProxyType({}))
    coverage: DataPlaneCoverageSummary = field(default_factory=DataPlaneCoverageSummary)

    def __post_init__(self) -> None:
        if self.market is not None:
            if self.daily_features is None:
                raise ValueError("market data-plane snapshot requires daily features")
            if self.market.daily_feature_pack_version != self.daily_features.version:
                raise ValueError("market data-plane snapshot has mismatched daily features")
        if self.candidate_quotes is not None:
            if self.market is None:
                raise ValueError("candidate data-plane snapshot requires a market epoch")
            if self.candidate_quotes.market_epoch_version != self.market.version:
                raise ValueError("candidate data-plane snapshot has mismatched market epoch")
        if self.daily_features is not None and self.research is not None:
            if self.daily_features.trade_date != self.research.trade_date:
                raise ValueError("research and daily-feature epochs must use the same trade date")
            if self.daily_features.config_version != self.research.config_version:
                raise ValueError("research and daily-feature epochs must use the same config version")
        object.__setattr__(self, "failures", MappingProxyType(dict(self.failures)))


__all__ = [
    "DataPlaneChannel",
    "DataPlaneCoverageSummary",
    "DataPlaneFailure",
    "MarketDataPlaneSnapshot",
]
