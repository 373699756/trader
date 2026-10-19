"""Immutable dependency bundles consumed by the sole composition root."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trader.infra.settings import LongWatchlist, RuntimeSettings, StrategySettings
from trader.recommendation.application.long_runtime import LongRuntime
from trader.recommendation.application.pipeline.freeze_publish.decision_events import DecisionObservation
from trader.recommendation.application.pipeline.freeze_publish.decision_observers import (
    AsyncDecisionObserver,
    DecisionEventConsumer,
)
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedDecisionDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedDecisionEventStream
from trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator import ScoredFreezeCoordinator
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedDecisionQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.application.runtime.cadence import CadencePolicy
from trader.recommendation.application.runtime.latency import LatencyWaterfall
from trader.recommendation.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.recommendation.infra.market_data.market_feature_service import MarketFeatureService
from trader.recommendation.infra.persistence.data_plane import SQLiteDataPlane
from trader.recommendation.infra.persistence.decision_records import SQLiteDecisionRecords
from trader.recommendation.infra.persistence.publication_sequence import PublicationSequence
from trader.recommendation.infra.runtime.application_system import RuntimeWorkerResources
from trader.training.infra.research.outcome_evidence_index import SQLiteOutcomeEvidenceIndex
from trader.training.infra.research.research_trace_index import SQLiteResearchTraceIndex


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
    budget_path: Path
    owns_shared_runtime: bool


@dataclass(frozen=True)
class RuntimePersistenceContext:
    sequence: PublicationSequence
    records: SQLiteDecisionRecords
    data_plane: SQLiteDataPlane
    budget: DeepSeekBudgetLedger
    outcomes: SQLiteOutcomeEvidenceIndex


@dataclass(frozen=True)
class RuntimePublicationContext:
    tomorrow_records: SQLiteDecisionRecords
    tomorrow_index: UnifiedDecisionIndex
    decision_drafts: UnifiedDecisionDraftIndex
    research_trace: SQLiteResearchTraceIndex
    long_runtime: LongRuntime
    decision_queries: UnifiedDecisionQueries
    decision_events: UnifiedDecisionEventStream
    tomorrow_freezer: ScoredFreezeCoordinator
    d25_freezer: ScoredFreezeCoordinator
    observer: AsyncDecisionObserver[DecisionObservation]


@dataclass(frozen=True)
class RuntimePublicationDependencies:
    sequence: PublicationSequence
    records: SQLiteDecisionRecords
    market_data: MarketFeatureService
    outcomes: SQLiteOutcomeEvidenceIndex
    additional_observers: tuple[DecisionEventConsumer[DecisionObservation], ...] = ()


__all__ = [
    "RuntimeBuildContext",
    "RuntimePersistenceContext",
    "RuntimePublicationContext",
    "RuntimePublicationDependencies",
]
