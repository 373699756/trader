"""Resource container for one fully composed recommendation profile."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from trader.infra.cache import BoundedLruCache
from trader.infra.settings import LongWatchlist, RuntimeSettings, StrategySettings
from trader.infra.shutdown import ShutdownDeadline, ShutdownReport
from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.long_runtime import LongRuntime
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.runtime.resource_orchestration import (
    ApplicationResources,
    StartApplicationResources,
    StopApplicationResources,
)
from trader.recommendation.application.runtime.run_scope import RecommendationRunScope
from trader.recommendation.application.runtime.scheduler_runtime import SchedulerRuntime
from trader.recommendation.application.runtime.source_lanes import SourceLaneScheduler
from trader.recommendation.application.runtime.supervisor import RuntimeSupervisor
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.recommendation.infra.deepseek.cache import ReviewCache
from trader.recommendation.infra.market_data.market_feature_service import MarketFeatureService
from trader.recommendation.infra.market_data.company_eligibility import CompanyEligibility
from trader.recommendation.infra.market_data.published_input_preloader import PublishedInputPreloader
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords

ResearchTraceT = TypeVar("ResearchTraceT")
OutcomeEvidenceT = TypeVar("OutcomeEvidenceT")
WebServicesT = TypeVar("WebServicesT")
ApplicationT = TypeVar("ApplicationT")


@dataclass(frozen=True)
class RuntimeWorkerResources:
    """Worker and cache resources assembled by the composition root."""

    control_pool: BoundedExecutor
    data_pool: BoundedExecutor
    quote_pool: BoundedExecutor
    market_quote_pool: BoundedExecutor
    minute_pool: BoundedExecutor
    company_pool: BoundedExecutor
    source_lanes: SourceLaneScheduler
    market_cache: BoundedLruCache[object]


@dataclass(frozen=True)
class ApplicationSystem(Generic[ResearchTraceT, OutcomeEvidenceT, WebServicesT, ApplicationT]):
    settings: RuntimeSettings
    strategy: StrategySettings
    watchlist: LongWatchlist
    app: ApplicationT
    supervisor: RuntimeSupervisor
    scheduler: SchedulerRuntime
    records: SQLiteFreezeRecords
    market_cache: BoundedLruCache[object]
    minute_pool: BoundedExecutor
    source_lanes: SourceLaneScheduler
    data_pool: BoundedExecutor
    quote_pool: BoundedExecutor
    market_quote_pool: BoundedExecutor
    long_runtime: LongRuntime
    freeze_queries: UnifiedFreezeQueries
    freeze_events: UnifiedFreezeEventStream
    tomorrow_index: UnifiedFreezeIndex
    tomorrow_records: SQLiteFreezeRecords
    research_trace: ResearchTraceT
    outcome_evidence: OutcomeEvidenceT
    input_preloader: PublishedInputPreloader
    web_services: WebServicesT
    workers: RuntimeWorkerResources
    market_data: MarketFeatureService
    eligibility: CompanyEligibility
    budget: DeepSeekBudgetLedger
    review_cache: ReviewCache
    run_scope: RecommendationRunScope
    owns_shared_resources: bool = True

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
            self.minute_pool,
            (self.long_runtime, *((self.eligibility, self.input_preloader) if self.owns_shared_resources else ())),
            self.market_cache,
            self.owns_shared_resources,
        )

    def start(self) -> bool:
        try:
            return StartApplicationResources(
                self._application_resources(), timeout_seconds=self.settings.pipeline.shutdown_timeout_seconds
            )
        except BaseException:
            if self.owns_shared_resources:
                self.budget.close()
            raise

    def stop(self, *, deadline: ShutdownDeadline | None = None) -> ShutdownReport:
        shared = deadline or ShutdownDeadline.start(self.settings.pipeline.shutdown_timeout_seconds)
        try:
            return StopApplicationResources(self._application_resources(), deadline=shared)
        finally:
            if self.owns_shared_resources:
                self.budget.close()


__all__ = ["ApplicationSystem", "RuntimeWorkerResources"]
