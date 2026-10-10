from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.pipeline.freeze_publish.decision_events import (
    DecisionCommitted,
    DecisionObservation,
)
from trader.recommendation.application.pipeline.freeze_publish.decision_observers import (
    DecisionObserverRuntime,
)
from trader.recommendation.application.pipeline.freeze_publish.publication_io import (
    PublicationIoTracker,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.application.ports.clock import Clock, TradingCalendarPort
from trader.recommendation.application.ports.publisher import OverlayPublisher
from trader.recommendation.application.ports.runtime import (
    DataRefreshPort,
    DecisionBuilderPort,
    DeepSeekUpgradePort,
    FreezePort,
    ResearchRuntimeFactoryPort,
    SettlementPort,
)
from trader.recommendation.application.runtime.cadence import (
    CadencePlanner,
)
from trader.recommendation.application.runtime.latency import LatencyWaterfall


@dataclass(frozen=True)
class RuntimeDependencies:
    control_pool: BoundedExecutor
    clock: Clock
    calendar: TradingCalendarPort
    cadence: CadencePlanner
    data: DataRefreshPort
    decisions: DecisionBuilderPort
    reviews: DeepSeekUpgradePort
    index: UnifiedDecisionIndex
    observer: DecisionObserverRuntime[DecisionObservation]
    freezes: FreezePort
    settlement: SettlementPort
    research_factory: ResearchRuntimeFactoryPort
    publish_decision: Callable[[DecisionCommitted], object]
    publish_overlay: OverlayPublisher
    latency: LatencyWaterfall = field(default_factory=LatencyWaterfall)
    publication_io: PublicationIoTracker | None = None
