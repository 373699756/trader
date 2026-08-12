"""Injected read-only services used by Flask route groups."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from trader.application.decision_queries import UnifiedDecisionQueries
from trader.application.decision_stream import UnifiedDecisionEventStream
from trader.application.publisher import SnapshotPublisher
from trader.application.queries import RecommendationQueries
from trader.application.tomorrow_events import TomorrowDecisionEventStream
from trader.application.tomorrow_views import TomorrowDecisionQueries
from trader.domain.market.models import MarketQuote

StatusProvider = Callable[[], dict[str, object]]
DecisionQuoteProvider = Callable[[Sequence[str]], Mapping[str, MarketQuote]]


@dataclass(frozen=True)
class WebApiConfig:
    default_top_n: int = 12
    maximum_top_n: int = 12
    heartbeat_seconds: float = 15.0


@dataclass(frozen=True)
class TomorrowWebServices:
    queries: TomorrowDecisionQueries
    events: TomorrowDecisionEventStream
    cutover_status: Callable[[], Mapping[str, object]] | None = None


@dataclass(frozen=True)
class UnifiedWebServices:
    queries: UnifiedDecisionQueries
    events: UnifiedDecisionEventStream
    status_provider: StatusProvider
    config: WebApiConfig = WebApiConfig()
    legacy: WebServices | None = None


@dataclass(frozen=True)
class WebServices:
    status_provider: StatusProvider
    queries: RecommendationQueries | None = None
    publisher: SnapshotPublisher | None = None
    decision_queries: UnifiedDecisionQueries | None = None
    decision_events: UnifiedDecisionEventStream | None = None
    decision_quotes: DecisionQuoteProvider | None = None
    tomorrow_queries: TomorrowDecisionQueries | None = None
    tomorrow_events: TomorrowDecisionEventStream | None = None
    tomorrow_cutover_status: Callable[[], Mapping[str, object]] | None = None
    config: WebApiConfig = WebApiConfig()


__all__ = [
    "StatusProvider",
    "DecisionQuoteProvider",
    "TomorrowWebServices",
    "UnifiedWebServices",
    "WebApiConfig",
    "WebServices",
]
