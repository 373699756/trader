"""Minimal market, quote, research and history ports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Protocol

from trader.recommendation.application.ports.json_values import JsonObject
from trader.recommendation.domain.market.data_plane import MarketDataPlaneSnapshot as _MarketDataPlaneSnapshot
from trader.recommendation.domain.market.models import FeatureSnapshot, LiveQuote
from trader.recommendation.domain.market.refresh import ResearchRefreshResult as _ResearchRefreshResult


class MarketDataUnavailableError(RuntimeError):
    """No usable current or cached market data is available."""


class MarketDataDeadlineExceededError(MarketDataUnavailableError):
    """A deadline-bound market-data operation exhausted its budget."""


class DataPlaneReadPort(Protocol):
    def snapshot(self) -> _MarketDataPlaneSnapshot: ...


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
    market_epoch: str = ""
    reference_epoch: str = ""
    source_versions: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    field_sources: Mapping[str, Mapping[str, str]] = field(default_factory=lambda: MappingProxyType({}))
    conflicts: tuple[str, ...] = ()
    missing_reasons: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    degraded_reasons: tuple[str, ...] = ()
    observed_at: datetime | None = None
    reference_versions: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    source_ages_seconds: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    failure_categories: tuple[str, ...] = ()
    status: str = "missing"

    def __post_init__(self) -> None:
        if self.status != "missing" and not self.merge_epoch:
            raise ValueError("market snapshot merge_epoch must not be empty")
        if self.status != "missing" and not self.market_epoch:
            object.__setattr__(self, "market_epoch", self.merge_epoch)
        if self.status != "missing" and not self.reference_epoch:
            object.__setattr__(self, "reference_epoch", "reference:unknown")
        object.__setattr__(self, "source_versions", MappingProxyType(dict(self.source_versions)))
        object.__setattr__(
            self,
            "field_sources",
            MappingProxyType({code: MappingProxyType(dict(fields)) for code, fields in self.field_sources.items()}),
        )
        object.__setattr__(self, "missing_reasons", MappingProxyType(dict(self.missing_reasons)))
        object.__setattr__(self, "reference_versions", MappingProxyType(dict(self.reference_versions)))
        object.__setattr__(self, "source_ages_seconds", MappingProxyType(dict(self.source_ages_seconds)))
        object.__setattr__(self, "failure_categories", tuple(sorted(set(self.failure_categories))))


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

    def refresh_topk_quotes(
        self, codes: Sequence[str], observed_at: datetime, *, force: bool = False, deadline: datetime | None = None
    ) -> Sequence[FeatureSnapshot]: ...

    def refresh_long_quotes(
        self, codes: Sequence[str], observed_at: datetime, *, force: bool = False, deadline: datetime | None = None
    ) -> Sequence[FeatureSnapshot]: ...

    def current_quotes(self, codes: Sequence[str]) -> Mapping[str, LiveQuote]: ...


class ResearchReaderPort(Protocol):
    def refresh_industry_heat(self, observed_at: datetime) -> Sequence[FeatureSnapshot]: ...

    def refresh_market_news(
        self, codes: Sequence[str], observed_at: datetime, *, deadline: datetime | None = None
    ) -> _ResearchRefreshResult: ...

    def refresh_stock_risk(
        self, codes: Sequence[str], observed_at: datetime, *, deadline: datetime | None = None
    ) -> _ResearchRefreshResult: ...


class ReferenceDataPort(Protocol):
    def refresh_reference_data(self, codes: Sequence[str], observed_at: datetime, *, force: bool = False) -> None: ...

    def schedule_reference_data(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        security_master_codes: Sequence[str] | None = None,
    ) -> None: ...

    def refresh_intraday_tail(self, codes: Sequence[str], observed_at: datetime) -> None: ...


class MarketMetadataPort(Protocol):
    def health(self) -> JsonObject: ...

    def snapshot_metadata(self, codes: Sequence[str] | None = None) -> MarketSnapshotMetadata: ...
