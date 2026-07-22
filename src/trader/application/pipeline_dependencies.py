"""Explicit dependencies, configuration and owned worker resources for the pipeline."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from trader.application.cadence import CadencePolicy
from trader.application.ports.clock import TradingCalendarPort
from trader.application.ports.events import EventAuditPort
from trader.application.ports.market import MarketDataPorts
from trader.application.ports.outcomes import OutcomeSettlementPort
from trader.application.ports.reviews import DeepSeekReviewPort
from trader.application.ports.snapshots import PublishedSnapshotWritePort, SnapshotPorts
from trader.application.publisher import SnapshotPublisher
from trader.application.recommendations import RecommendationEngine
from trader.application.status import RuntimeState
from trader.application.workers import BoundedExecutor


@dataclass(frozen=True)
class PipelineDependencies:
    market: MarketDataPorts
    calendar: TradingCalendarPort
    snapshots: SnapshotPorts
    events: EventAuditPort
    publisher: SnapshotPublisher
    engine: RecommendationEngine
    state: RuntimeState
    now: Callable[[], datetime]
    published_snapshots: PublishedSnapshotWritePort | None = None
    reviews: DeepSeekReviewPort | None = None
    outcome_settlement: OutcomeSettlementPort | None = None


@dataclass(frozen=True)
class PipelineOptions:
    config_version: str
    candidate_pool_size: int
    event_queue_size: int
    priority_queue_size: int
    market_workers: int = 6
    normalization_workers: int = 2
    strategy_workers: int = 3
    deepseek_workers: int = 4
    market_data_manages_workers: bool = False
    cadence_policy: CadencePolicy | None = None
    long_codes: tuple[str, ...] = ()
    long_target_prices: Mapping[str, float | None] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "long_codes", tuple(self.long_codes))
        object.__setattr__(self, "long_target_prices", MappingProxyType(dict(self.long_target_prices)))


@dataclass(frozen=True)
class PipelineResources:
    data_pool: BoundedExecutor | None = None
    persistence_pool: BoundedExecutor | None = None
