"""Explicit dependencies, configuration and owned worker resources for the pipeline."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from trader.application.cadence import CadencePolicy
from trader.application.latency import LatencyWaterfall
from trader.application.long_groups import LongGroupDefinition, LongWatchItemDefinition
from trader.application.ports.clock import TradingCalendarPort
from trader.application.ports.events import EventAuditPort
from trader.application.ports.market import MarketDataPorts
from trader.application.ports.outcomes import OutcomeSettlementPort
from trader.application.ports.reviews import DeepSeekReviewPort
from trader.application.ports.snapshots import PublishedSnapshotWritePort, SnapshotPorts
from trader.application.ports.tomorrow import TomorrowNativeInputPort, TomorrowV2ControlPort
from trader.application.publisher import SnapshotPublisher
from trader.application.recommendations import RecommendationEngine
from trader.application.status import RuntimeState
from trader.application.trading_session import TradingSessionTracker
from trader.application.workers import BoundedExecutor
from trader.domain.recommendation.models import Strategy


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
    latency: LatencyWaterfall | None = None
    tomorrow_native_inputs: TomorrowNativeInputPort | None = None
    tomorrow_v2_control: TomorrowV2ControlPort | None = None
    trading_session: TradingSessionTracker | None = None


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
    decision_execution_mode: str = "serialized"
    market_data_manages_workers: bool = False
    cadence_policy: CadencePolicy | None = None
    long_codes: tuple[str, ...] = ()
    long_items: tuple[LongWatchItemDefinition, ...] = ()
    long_target_prices: Mapping[str, float | None] = field(default_factory=lambda: MappingProxyType({}))
    long_groups: tuple[LongGroupDefinition, ...] = ()
    v2_owned_strategies: tuple[Strategy, ...] = ()

    def __post_init__(self) -> None:
        if self.decision_execution_mode not in {"serialized", "versioned_dag"}:
            raise ValueError("decision_execution_mode must be serialized or versioned_dag")
        object.__setattr__(self, "long_codes", tuple(self.long_codes))
        object.__setattr__(self, "long_items", tuple(self.long_items))
        object.__setattr__(self, "long_target_prices", MappingProxyType(dict(self.long_target_prices)))
        object.__setattr__(self, "long_groups", tuple(self.long_groups))
        owned = tuple(self.v2_owned_strategies)
        if any(strategy is Strategy.LONG for strategy in owned) or len(set(owned)) != len(owned):
            raise ValueError("V2-owned strategies must be unique scored strategies")
        object.__setattr__(self, "v2_owned_strategies", owned)


@dataclass(frozen=True)
class PipelineResources:
    data_pool: BoundedExecutor | None = None
    persistence_pool: BoundedExecutor | None = None
