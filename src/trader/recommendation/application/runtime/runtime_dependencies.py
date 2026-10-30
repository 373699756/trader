from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import (
    FreezeCommitted,
    FreezeObservation,
)
from trader.recommendation.application.pipeline.freeze_publish.freeze_observers import (
    FreezeObserverRuntime,
)
from trader.recommendation.application.pipeline.freeze_publish.publication_io import (
    PublicationIoTracker,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.ports.clock import Clock, TradingCalendarPort
from trader.recommendation.application.ports.long import LongRefreshPort
from trader.recommendation.application.ports.publisher import OverlayPublisher
from trader.recommendation.application.ports.runtime import (
    DataRefreshPort,
    DeepSeekUpgradePort,
    FreezeBuilderPort,
    FreezePort,
    ResearchRuntimeFactoryPort,
    SettlementPort,
)
from trader.recommendation.application.runtime.cadence import (
    CadencePlanner,
)
from trader.recommendation.application.runtime.latency import LatencyWaterfall
from trader.recommendation.application.runtime.run_scope import RecommendationRunScope
from trader.recommendation.application.runtime.recommendation_round import RecommendationRounds


@dataclass(frozen=True)
class RuntimeDependencies:
    control_pool: BoundedExecutor
    clock: Clock
    calendar: TradingCalendarPort
    cadence: CadencePlanner
    data: DataRefreshPort
    freezes: FreezeBuilderPort
    reviews: DeepSeekUpgradePort
    index: UnifiedFreezeIndex
    observer: FreezeObserverRuntime[FreezeObservation]
    sealing: FreezePort
    settlement: SettlementPort
    research_factory: ResearchRuntimeFactoryPort
    publish_freeze: Callable[[FreezeCommitted], object]
    publish_overlay: OverlayPublisher | None = None
    latency: LatencyWaterfall = field(default_factory=LatencyWaterfall)
    publication_io: PublicationIoTracker | None = None
    monotonic: Callable[[], float] = time.monotonic
    run_scope: RecommendationRunScope | None = None
    long_quotes: LongRefreshPort | None = None
    rounds: RecommendationRounds = field(default_factory=RecommendationRounds)
    round_profile: str = "default"
