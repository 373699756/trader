"""Minimal market, quote, research and history ports."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Protocol

from trader.application.ports.types import JsonObject, JsonValue
from trader.domain.market.epochs import CandidateQuoteEpoch, DailyFeaturePack, MarketEpoch, ResearchEpoch
from trader.domain.market.models import FeatureSnapshot, LiveQuote
from trader.domain.outcome.models import OutcomeBar

_SHANGHAI_TIMEZONE = "Asia/Shanghai"
_FAILURE_REASON = re.compile(r"^[a-z0-9_]{1,64}$")


class MarketDataUnavailableError(RuntimeError):
    """No usable current or cached market data is available."""


class MarketDataDeadlineExceededError(MarketDataUnavailableError):
    """A deadline-bound market-data operation exhausted its budget."""


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
class MarketDataPlaneSnapshot:
    daily_features: DailyFeaturePack | None
    market: MarketEpoch | None
    candidate_quotes: CandidateQuoteEpoch | None
    research: ResearchEpoch | None
    failures: Mapping[DataPlaneChannel, DataPlaneFailure] = field(default_factory=lambda: MappingProxyType({}))

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


class RealtimeDataPlaneReaderPort(Protocol):
    def snapshot(self) -> MarketDataPlaneSnapshot: ...


class MarketDataNoDataError(RuntimeError):
    """A valid provider response contained no usable data."""


class MarketDataFailedError(RuntimeError):
    """A provider transport or protocol operation failed."""

    def __init__(self, vendor: str, error: str) -> None:
        super().__init__(f"{vendor}: {error}")
        self.vendor = vendor
        self.error = error


@dataclass(frozen=True)
class MarketSnapshotMetadata:
    merge_epoch: str = ""
    source_versions: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    field_sources: Mapping[str, Mapping[str, str]] = field(default_factory=lambda: MappingProxyType({}))
    conflicts: tuple[str, ...] = ()
    missing_reasons: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    degraded_reasons: tuple[str, ...] = ()
    observed_at: datetime | None = None
    reference_versions: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_versions", MappingProxyType(dict(self.source_versions)))
        object.__setattr__(
            self,
            "field_sources",
            MappingProxyType({code: MappingProxyType(dict(fields)) for code, fields in self.field_sources.items()}),
        )
        object.__setattr__(self, "missing_reasons", MappingProxyType(dict(self.missing_reasons)))
        object.__setattr__(self, "reference_versions", MappingProxyType(dict(self.reference_versions)))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "merge_epoch": self.merge_epoch,
            "source_versions": self.source_versions,
            "field_sources": self.field_sources,
            "market_conflicts": self.conflicts,
            "market_missing_reasons": self.missing_reasons,
            "market_degraded_reasons": self.degraded_reasons,
            "market_observed_at": self.observed_at.isoformat() if self.observed_at is not None else "",
            "tushare_reference_versions": self.reference_versions,
        }


class FullMarketReaderPort(Protocol):
    def fetch_market_features(
        self, observed_at: datetime, *, force: bool = False, deadline: datetime | None = None
    ) -> Sequence[FeatureSnapshot]: ...


class CandidateFeatureReaderPort(Protocol):
    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]: ...

    def read_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]: ...


class QuoteReaderPort(Protocol):
    def refresh_candidate_quotes(
        self, codes: Sequence[str], observed_at: datetime, *, force: bool = False, deadline: datetime | None = None
    ) -> Sequence[FeatureSnapshot]: ...

    def current_quotes(self, codes: Sequence[str]) -> Mapping[str, LiveQuote]: ...


class ResearchReaderPort(Protocol):
    def refresh_industry_heat(self, observed_at: datetime) -> Sequence[FeatureSnapshot]: ...

    def refresh_market_news(
        self, codes: Sequence[str], observed_at: datetime, *, deadline: datetime | None = None
    ) -> None: ...

    def refresh_stock_risk(
        self, codes: Sequence[str], observed_at: datetime, *, deadline: datetime | None = None
    ) -> None: ...


class ReferenceDataPort(Protocol):
    def refresh_reference_data(self, codes: Sequence[str], observed_at: datetime, *, force: bool = False) -> None: ...

    def schedule_reference_data(self, codes: Sequence[str], observed_at: datetime, *, force: bool = False) -> None: ...

    def refresh_intraday_tail(self, codes: Sequence[str], observed_at: datetime) -> None: ...


class MarketMetadataPort(Protocol):
    def health(self) -> JsonObject: ...

    def snapshot_metadata(self, codes: Sequence[str] | None = None) -> MarketSnapshotMetadata: ...


class OutcomePriceReaderPort(Protocol):
    def read_outcome_bars(
        self, codes: Sequence[str], observed_at: datetime
    ) -> Mapping[str, tuple[OutcomeBar, ...]]: ...


@dataclass(frozen=True)
class MarketDataPorts:
    full_market: FullMarketReaderPort
    candidates: CandidateFeatureReaderPort
    quotes: QuoteReaderPort
    research: ResearchReaderPort
    references: ReferenceDataPort
    metadata: MarketMetadataPort
    outcomes: OutcomePriceReaderPort
