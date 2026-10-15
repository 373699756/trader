"""Resource container for one fully composed recommendation profile."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from trader.infra.atomic_files.json import RuntimeJsonWriter
from trader.infra.cache import BoundedLruCache
from trader.infra.settings import LongWatchlist, RuntimeSettings, StrategySettings
from trader.infra.shutdown import ShutdownDeadline, ShutdownReport
from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.long_runtime import LongRuntime
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedDecisionEventStream
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedDecisionQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.application.runtime.resource_orchestration import (
    ApplicationResources,
    AuxiliaryRuntimeResource,
    start_application_resources,
    stop_application_resources,
)
from trader.recommendation.application.runtime.scheduler_runtime import SchedulerRuntime
from trader.recommendation.application.runtime.source_lanes import SourceLaneScheduler
from trader.recommendation.application.runtime.supervisor import RuntimeSupervisor
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.infra.market_data.published_history_observer import PublishedHistoryObserver
from trader.recommendation.infra.persistence.decision_records import SQLiteDecisionRecords

ResearchTraceT = TypeVar("ResearchTraceT")
OutcomeEvidenceT = TypeVar("OutcomeEvidenceT")
MaintenanceT = TypeVar("MaintenanceT", bound=AuxiliaryRuntimeResource)
WebServicesT = TypeVar("WebServicesT")
ApplicationT = TypeVar("ApplicationT")


@dataclass(frozen=True)
class RuntimeWorkerResources:
    """Worker and cache resources assembled by the composition root."""

    control_pool: BoundedExecutor
    data_pool: BoundedExecutor
    quote_pool: BoundedExecutor
    market_quote_pool: BoundedExecutor
    research_pool: BoundedExecutor
    company_research_pool: BoundedExecutor
    persistence_pool: BoundedExecutor
    source_lanes: SourceLaneScheduler
    json_writer: RuntimeJsonWriter
    market_cache: BoundedLruCache[object]


@dataclass(frozen=True)
class ApplicationSystem(Generic[ResearchTraceT, OutcomeEvidenceT, MaintenanceT, WebServicesT, ApplicationT]):
    settings: RuntimeSettings
    strategy: StrategySettings
    watchlist: LongWatchlist
    app: ApplicationT
    supervisor: RuntimeSupervisor
    scheduler: SchedulerRuntime
    records: SQLiteDecisionRecords
    market_cache: BoundedLruCache[object]
    research_pool: BoundedExecutor
    source_lanes: SourceLaneScheduler
    data_pool: BoundedExecutor
    quote_pool: BoundedExecutor
    market_quote_pool: BoundedExecutor
    long_runtime: LongRuntime
    decision_queries: UnifiedDecisionQueries
    decision_events: UnifiedDecisionEventStream
    tomorrow_index: UnifiedDecisionIndex
    tomorrow_records: SQLiteDecisionRecords
    research_trace: ResearchTraceT
    outcome_evidence: OutcomeEvidenceT
    history_observer: PublishedHistoryObserver
    qfq_maintenance: MaintenanceT
    web_services: WebServicesT

    @property
    def profile_id(self) -> ScoringProfileId:
        return self.strategy.scoring_profile

    def _application_resources(self) -> ApplicationResources:
        return ApplicationResources(
            self.supervisor,
            self.source_lanes,
            self.data_pool,
            self.quote_pool,
            self.market_quote_pool,
            self.research_pool,
            (self.long_runtime, self.history_observer, self.qfq_maintenance),
            self.market_cache,
        )

    def start(self) -> bool:
        return start_application_resources(
            self._application_resources(), timeout_seconds=self.settings.pipeline.shutdown_timeout_seconds
        )

    def stop(self, *, deadline: ShutdownDeadline | None = None) -> ShutdownReport:
        shared = deadline or ShutdownDeadline.start(self.settings.pipeline.shutdown_timeout_seconds)
        return stop_application_resources(self._application_resources(), deadline=shared)


__all__ = ["ApplicationSystem", "RuntimeWorkerResources"]
