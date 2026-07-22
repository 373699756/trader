"""Small ports owned by the application layer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Protocol

from trader.domain.market.models import (
    FeatureSnapshot,
    LiveQuote,
)
from trader.domain.outcome.models import (
    BenchmarkReturn,
    OutcomeBar,
    OutcomeTarget,
    RecommendationOutcome,
)
from trader.domain.recommendation.models import (
    LiveOverlay,
    RecommendationSnapshot,
    Strategy,
)
from trader.domain.review.models import (
    DeepSeekReview,
    ReviewCandidateContext,
)


class MarketDataUnavailable(RuntimeError):
    """All full-market sources failed and no usable cached quote set exists."""


class MarketDataDeadlineExceeded(MarketDataUnavailable):
    """A deadline-bound market-data operation exhausted its time budget."""


class MarketDataNoData(RuntimeError):
    """Data source returned a valid response but contained no usable data.

    This is semantically different from a transport failure — the source is
    reachable but the requested data does not exist or is empty.
    """


class MarketDataFailed(RuntimeError):
    """Data source transport or protocol failure (timeout, TLS, HTTP 5xx).

    Carries the vendor name and original error for observability.
    """

    def __init__(self, vendor: str, error: str) -> None:
        super().__init__(f"{vendor}: {error}")
        self.vendor = vendor
        self.error = error


class Clock(Protocol):
    def now(self) -> datetime: ...


class MarketDataPort(Protocol):
    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]: ...

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]: ...

    def refresh_candidate_quotes(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]: ...

    def refresh_industry_heat(self, observed_at: datetime) -> Sequence[FeatureSnapshot]: ...

    def refresh_market_news(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
    ) -> None: ...

    def refresh_stock_risk(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
    ) -> None: ...

    def refresh_reference_data(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
    ) -> None: ...

    def schedule_reference_data(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
    ) -> None: ...

    def refresh_intraday_tail(self, codes: Sequence[str], observed_at: datetime) -> None: ...

    def read_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]: ...

    def health(self) -> Mapping[str, object]: ...

    def snapshot_metadata(self, codes: Sequence[str] | None = None) -> Mapping[str, object]: ...

    def read_outcome_bars(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> Mapping[str, tuple[OutcomeBar, ...]]: ...


class TradingCalendarPort(Protocol):
    def is_trading_day(self, day: date) -> bool: ...

    def session_distance(self, start: str, end: str) -> int | None: ...


class OutcomeRepositoryPort(Protocol):
    def pending_outcome_targets(self, *, limit: int) -> Sequence[OutcomeTarget]: ...

    def record_benchmark_return(self, benchmark: BenchmarkReturn, *, observed_at: datetime) -> None: ...

    def benchmark_returns_after(self, recommend_date: str, *, limit: int) -> Sequence[BenchmarkReturn]: ...

    def save_recommendation_outcomes(self, outcomes: Sequence[RecommendationOutcome]) -> None: ...


class OutcomeSettlementPort(Protocol):
    def settle(
        self,
        now: datetime,
        market_features: Sequence[FeatureSnapshot],
    ) -> OutcomeSettlementResult: ...


class OutcomeSettlementResult(Protocol):
    @property
    def completed_count(self) -> int: ...

    @property
    def benchmark_recorded(self) -> bool: ...


class CurrentQuoteReaderPort(Protocol):
    def current_quotes(self, codes: Sequence[str]) -> Mapping[str, LiveQuote]: ...


class DeepSeekReviewPort(Protocol):
    def review(
        self,
        strategy: Strategy,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
        contexts: Mapping[str, ReviewCandidateContext] | None = None,
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

    def record_data_source_health(self, health: Mapping[str, object], *, updated_at: datetime) -> None: ...

    def observability_status(self) -> Mapping[str, object]: ...


class CurrentSnapshotReaderPort(Protocol):
    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None: ...

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None: ...


class EventReaderPort(Protocol):
    def list_events(self, *, cursor: int, limit: int) -> Sequence[Mapping[str, object]]: ...


class EventAuditPort(EventReaderPort, Protocol):
    def reserve_event(self, event: Mapping[str, object]) -> bool: ...

    def compare_and_set_event(
        self,
        event_id: str,
        *,
        expected_status: str,
        status: str,
        retry_count: int,
        error: str = "",
    ) -> bool: ...

    def pending_priority_events(self) -> Sequence[Mapping[str, object]]: ...


__all__ = [
    "Clock",
    "CurrentSnapshotReaderPort",
    "CurrentQuoteReaderPort",
    "DeepSeekReviewPort",
    "EventAuditPort",
    "EventReaderPort",
    "MarketDataFailed",
    "MarketDataNoData",
    "MarketDataPort",
    "MarketDataUnavailable",
    "OutcomeRepositoryPort",
    "OutcomeSettlementPort",
    "OutcomeSettlementResult",
    "SnapshotRepositoryPort",
    "TradingCalendarPort",
]
