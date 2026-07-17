"""Small ports owned by the application layer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Protocol

from trader.domain.models import DeepSeekReview, FeatureSnapshot, LiveOverlay, RecommendationSnapshot, Strategy


class MarketDataUnavailable(RuntimeError):
    """All full-market sources failed and no usable cached quote set exists."""


class Clock(Protocol):
    def now(self) -> datetime: ...


class MarketDataPort(Protocol):
    def fetch_market_features(self, observed_at: datetime) -> Sequence[FeatureSnapshot]: ...

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]: ...

    def health(self) -> Mapping[str, object]: ...


class TradingCalendarPort(Protocol):
    def is_trading_day(self, day: date) -> bool: ...


class DeepSeekReviewPort(Protocol):
    def review(
        self,
        strategy: Strategy,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
    ) -> Mapping[str, DeepSeekReview]: ...

    def preheat(
        self,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
    ) -> Mapping[str, DeepSeekReview]: ...

    def status(self) -> Mapping[str, object]: ...


class SnapshotRepositoryPort(Protocol):
    def initialize(self) -> None: ...

    def publish(self, snapshot: RecommendationSnapshot) -> None: ...

    def freeze(self, snapshot: RecommendationSnapshot) -> None: ...

    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None: ...

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None: ...

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]: ...

    def save_live_overlay(self, overlay: LiveOverlay) -> bool: ...

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None: ...

    def recover(self) -> Mapping[str, int]: ...


class EventAuditPort(Protocol):
    def append_event(self, event: Mapping[str, object]) -> None: ...

    def list_events(self, *, cursor: int, limit: int) -> Sequence[Mapping[str, object]]: ...

    def pending_priority_events(self) -> Sequence[Mapping[str, object]]: ...


__all__ = [
    "Clock",
    "DeepSeekReviewPort",
    "EventAuditPort",
    "MarketDataPort",
    "MarketDataUnavailable",
    "SnapshotRepositoryPort",
    "TradingCalendarPort",
]
