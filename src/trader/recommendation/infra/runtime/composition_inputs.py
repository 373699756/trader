"""Immutable dependency bundles consumed by the sole composition root."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from trader.infra.settings import LongWatchlist, RuntimeSettings, StrategySettings
from trader.recommendation.application.long_runtime import LongRuntime
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
from trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator import ScoredFreezeCoordinator
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import FreezeObservation
from trader.recommendation.application.pipeline.freeze_publish.freeze_observers import (
    AsyncFreezeObserver,
    FreezeEventConsumer,
)
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.runtime.cadence import CadencePolicy
from trader.recommendation.application.runtime.latency import LatencyWaterfall
from trader.recommendation.application.runtime.run_scope import RecommendationRunScope
from trader.recommendation.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.recommendation.infra.deepseek.cache import ReviewCache
from trader.recommendation.infra.market_data.market_feature_service import MarketFeatureService
from trader.recommendation.infra.market_data.company_eligibility import CompanyEligibility
from trader.recommendation.infra.market_data.published_input_preloader import PublishedInputPreloader
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords
from trader.recommendation.infra.persistence.freeze_trace_index import SQLiteFreezeTraceIndex
from trader.recommendation.infra.persistence.outcome_evidence_index import SQLiteOutcomeEvidenceIndex
from trader.recommendation.infra.persistence.publication_sequence import PublicationSequence
from trader.recommendation.infra.runtime.application_system import RuntimeWorkerResources


@dataclass(frozen=True)
class RuntimeBuildContext:
    settings: RuntimeSettings
    strategy: StrategySettings
    watchlist: LongWatchlist
    effective_config_version: str
    now: Callable[[], datetime]
    latency: LatencyWaterfall
    cadence_policy: CadencePolicy
    workers: RuntimeWorkerResources
    owns_shared_runtime: bool


@dataclass(frozen=True)
class RuntimePersistenceContext:
    sequence: PublicationSequence
    records: SQLiteFreezeRecords
    budget: DeepSeekBudgetLedger
    outcomes: SQLiteOutcomeEvidenceIndex


@dataclass(frozen=True)
class SharedRecommendationInputs:
    workers: RuntimeWorkerResources
    market: MarketFeatureService
    budget: DeepSeekBudgetLedger
    review_cache: ReviewCache
    input_preloader: PublishedInputPreloader
    run_scope: RecommendationRunScope
    eligibility: CompanyEligibility


@dataclass(frozen=True)
class RuntimePublicationContext:
    tomorrow_records: SQLiteFreezeRecords
    tomorrow_index: UnifiedFreezeIndex
    freeze_drafts: UnifiedFreezeDraftIndex
    research_trace: SQLiteFreezeTraceIndex
    long_runtime: LongRuntime
    freeze_queries: UnifiedFreezeQueries
    freeze_events: UnifiedFreezeEventStream
    tomorrow_freezer: ScoredFreezeCoordinator
    d25_freezer: ScoredFreezeCoordinator
    observer: AsyncFreezeObserver[FreezeObservation]


@dataclass(frozen=True)
class RuntimePublicationDependencies:
    next_sequence: Callable[[], int]
    records: SQLiteFreezeRecords
    market_data: MarketFeatureService
    outcomes: SQLiteOutcomeEvidenceIndex
    additional_observers: tuple[FreezeEventConsumer[FreezeObservation], ...] = ()


__all__ = [
    "RuntimeBuildContext",
    "RuntimePersistenceContext",
    "RuntimePublicationContext",
    "RuntimePublicationDependencies",
    "SharedRecommendationInputs",
]
