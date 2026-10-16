"""Unique composition root for the current application."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

import requests
from flask import Flask

from trader.download.application.download_history import DownloadHistoryUseCase
from trader.download.application.progress_message import ProgressLevel, ProgressMessage, ProgressNumber
from trader.download.application.qfq_download_progress import QfqMessage
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.application.update_qfq import UpdateQfqWindows
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import (
    HistoryStEligibilitySummary,
    HistorySyncConfiguration,
    HistorySyncProgress,
    HistorySyncProgressPort,
)
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier
from trader.download.infra.exchange_security_universe import (
    LoadCurrentAShareUniverse,
    SelectStEligibleUniverse,
)
from trader.download.infra.history_industry_cache import HistoryIndustryQueryCache
from trader.download.infra.history_reference_files import HistoryStEvidencePath
from trader.download.infra.history_st_source import HistoryStNameSource
from trader.download.infra.history_supplier_router import HistorySupplierRouter
from trader.download.infra.history_sync_gateway import HistorySyncGateway
from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
from trader.download.infra.qfq_checkpoint import QfqCheckpoint
from trader.download.infra.qfq_maintenance import QfqDailyMaintenance
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.qfq_update_runner import QfqUpdateRunner
from trader.download.infra.tencent_qfq_supplier import (
    TencentQfqDependencies,
    TencentQfqSupplier,
    TencentSessionPool,
)
from trader.download.infra.terminal_progress import TerminalDownloadProgress
from trader.http_api.route_services import ProfiledWebServices, UnifiedWebServices, WebApiConfig
from trader.infra.atomic_files.json import RuntimeJsonWriter
from trader.infra.cache import BoundedLruCache
from trader.infra.clock.shanghai import ShanghaiClock
from trader.infra.clock.utc import UtcNow as _utc_now
from trader.infra.initialization import InitializeIfAvailable as _initialize_if_available
from trader.infra.market_data.providers.akshare_http_contracts import AkshareGetFunction
from trader.infra.market_data.providers.baostock_industry import BaoStockIndustryClient
from trader.infra.market_data.providers.exchange_security_master import (
    ExchangeSecurityMasterClient,
    FetchSseListings,
    FetchSzseListings,
)
from trader.infra.market_data.providers.tushare import TushareClient
from trader.infra.market_data.providers.tushare_records import BuildTushareSdk
from trader.infra.serialization.canonical import ContentHash
from trader.infra.settings import (
    LoadLongWatchlist,
    LoadRuntimeSettings,
    LoadStrategySettings,
    LongWatchlist,
    RuntimeSettings,
    StrategySettings,
)
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.long_runtime import LongRuntime, LongRuntimeDependencies
from trader.recommendation.application.pipeline.candidate_pool.candidate_pool_service import CandidateFilteringService
from trader.recommendation.application.pipeline.data_source.source_router import (
    InputRefreshDependencies,
    MarketInputCoordinator,
)
from trader.recommendation.application.pipeline.downside_action.downside_protection import RiskControlService
from trader.recommendation.application.pipeline.final_selection.grouped_ranking import RankingSelectionService
from trader.recommendation.application.pipeline.freeze_publish.decision_events import DecisionObservation
from trader.recommendation.application.pipeline.freeze_publish.decision_observers import (
    AsyncDecisionObserver,
    DecisionEventConsumer,
)
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedDecisionDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedDecisionEventStream
from trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator import (
    DecisionRuntimeIdentity,
    ScoredFreezeCoordinator,
)
from trader.recommendation.application.pipeline.freeze_publish.publication_io import PublicationIoTracker
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import (
    DecisionQuerySources,
    UnifiedDecisionQueries,
)
from trader.recommendation.application.pipeline.freeze_publish.runtime_adapters import DeepSeekAdapter, FreezeAdapter
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.application.pipeline.local_score.base_scoring import LocalScoringService
from trader.recommendation.application.pipeline.local_score.local_decision_builder import (
    DecisionBuildDependencies,
    LocalDecisionBuilder,
)
from trader.recommendation.application.pipeline.local_score.model_capability import PublishedModelScoringService
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.model_scoring import (
    ProductionModelScoringService,
    SharedModelFeatureCache,
)
from trader.recommendation.application.pipeline.local_score.unavailable_model import UnavailableModelCapability
from trader.recommendation.application.pipeline.score_merge.score_fusion import ScoreFusionService
from trader.recommendation.application.pipeline.static_market.static_pipeline import StaticMarketPipeline
from trader.recommendation.application.runtime.cadence import CadencePlanner, CadencePolicy, PipelineTask
from trader.recommendation.application.runtime.latency import LatencyWaterfall
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies
from trader.recommendation.application.runtime.scheduler_runtime import SchedulerRuntime
from trader.recommendation.application.runtime.source_lanes import SourceLaneScheduler
from trader.recommendation.application.runtime.supervisor import (
    RuntimeSupervisor,
    RuntimeSupervisorConfig,
    SchedulerIntervalSeconds,
)
from trader.recommendation.domain.publication.decision_identity import DecisionOverlay, ScoredDecision
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.recommendation.infra.deepseek.cache import ReviewCache
from trader.recommendation.infra.deepseek.factory import CreateDeepseekClient
from trader.recommendation.infra.deepseek.health_gate import DeepSeekHealthPolicy
from trader.recommendation.infra.deepseek.reviewer import DeepSeekReviewer
from trader.recommendation.infra.market_data.candidate_quote_cache import QuoteCache, QuoteCacheDependencies
from trader.recommendation.infra.market_data.gateway import MarketDataGateway
from trader.recommendation.infra.market_data.intraday_loader import IntradayLoader
from trader.recommendation.infra.market_data.local_industry_source import LocalIndustrySource
from trader.recommendation.infra.market_data.market_data_health import MarketDataHealth, MarketDataHealthDependencies
from trader.recommendation.infra.market_data.market_feature_service import (
    MarketFeatureDependencies,
    MarketFeatureService,
)
from trader.recommendation.infra.market_data.market_task_runner import MarketTaskRunner
from trader.recommendation.infra.market_data.providers.akshare import AkshareResearchClient
from trader.recommendation.infra.market_data.providers.eastmoney import EastmoneyClient
from trader.recommendation.infra.market_data.providers.sina import SinaClient
from trader.recommendation.infra.market_data.providers.tencent import TencentClient, TencentFullMarketSource
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.published_history_observer import PublishedHistoryObserver
from trader.recommendation.infra.market_data.research_observation_loader import ResearchLoader
from trader.recommendation.infra.market_data.trading_calendar import ChinaTradingCalendar
from trader.recommendation.infra.market_data.tushare_reference_loader import ReferenceLoader
from trader.recommendation.infra.normalization.features import FeatureBuilder
from trader.recommendation.infra.persistence.data_plane import SQLiteDataPlane
from trader.recommendation.infra.persistence.data_plane_initialization import _initialize_reference_data_plane
from trader.recommendation.infra.persistence.decision_records import SQLiteDecisionRecords
from trader.recommendation.infra.persistence.issuer_eligibility import SQLiteIssuerEligibilityIndex
from trader.recommendation.infra.persistence.publication_sequence import PublicationSequence
from trader.recommendation.infra.policy_settings import (
    _long_group_definitions,
    _long_item_definitions,
    _recommendation_policy,
)
from trader.recommendation.infra.runtime.application_system import ApplicationSystem, RuntimeWorkerResources
from trader.recommendation.infra.runtime.initialization import (
    FixedCacheTtl as _fixed_cache_ttl,
)
from trader.recommendation.infra.runtime.profiled_application import ProfiledApplicationSystem
from trader.recommendation.infra.runtime.startup_readiness import WarnMissingLocalData
from trader.recommendation.infra.scoring.cost_settings import LoadExecutionCostPolicy
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile, ScoringArtifactsMissingError
from trader.recommendation.infra.status_projection import RuntimeStatus as _runtime_status
from trader.training.application.outcome_settlement import OutcomeSettlementAdapter, OutcomeSettlementService
from trader.training.application.research_audit import TryBuildCommittedResearchAudit
from trader.training.application.research_runtime import ResearchRuntime
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.research.outcome_evidence_repository import SQLiteOutcomeEvidenceRepository
from trader.training.infra.research.research_trace_index import ResearchTraceLimits, SQLiteResearchTraceIndex
from trader.training.infra.training_settings import LoadProfileTrainingSettings
from trader.web import CreateApp

if TYPE_CHECKING:
    from trader.training.entrypoints.research_evidence import ResearchEvidenceCommand, ResearchEvidenceResult


def ExecuteHistoryDownload(
    configuration: HistorySyncConfiguration,
    *,
    progress: HistorySyncProgressPort | None = None,
    supplier_progress: HistorySyncProgressPort | None = None,
    clock: Callable[[], datetime] | None = None,
    cancel_requested: Callable[[], bool] = lambda: False,
) -> HistoryMaintenanceStatus:
    """Compose both manual and scheduled history routes in the unique root."""
    history_pool = BoundedExecutor(
        worker_count=configuration.history_workers,
        queue_capacity=0,
        thread_name_prefix="history-download",
    )
    history_pool.start()
    history_writer = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="history-writer")
    http_resources = ExitStack()
    try:
        history_writer.start()
        load_current_securities = partial(
            LoadCurrentAShareUniverse,
            partial(FetchSseListings, get=requests.get),
            partial(FetchSzseListings, get=requests.get),
            15.0,
        )
        with BaoStockHistorySupplier(
            configuration,
            progress=supplier_progress or progress,
            cancel_requested=cancel_requested,
        ) as baseline:
            http_pool = TencentSessionPool(
                tuple(
                    http_resources.enter_context(requests.Session()) for _index in range(configuration.history_workers)
                )
            )
            prices = TencentQfqSupplier(
                TencentQfqDependencies(
                    http_pool.borrow,
                    lambda _as_of: load_current_securities(),
                    cancel_requested,
                )
            )
            return DownloadHistoryUseCase(
                HistorySyncGateway(worker_pool=history_pool, writer_pool=history_writer)
            ).execute(
                configuration,
                HistorySupplierRouter(
                    baseline,
                    prices,
                    load_current_securities,
                    HistoryStNameSource(
                        HistoryStEvidencePath(configuration.reference_root),
                        http_pool.borrow,
                        history_pool,
                        cancel_requested,
                        report=lambda done, total, summary: (
                            progress.publish(
                                HistorySyncProgress(
                                    "history_st_reference",
                                    "completed",
                                    done,
                                    total,
                                    st_summary=summary,
                                )
                            )
                            if progress is not None
                            else None
                        ),
                        batch_size=configuration.history_workers,
                    ),
                    industry_cache=HistoryIndustryQueryCache(
                        HistoryStEvidencePath(configuration.reference_root).with_name("industry_queries.sqlite3")
                    ),
                ),
                clock=clock,
                progress=progress,
                cancel_requested=cancel_requested,
            )
    finally:
        deadline = ShutdownDeadline.start(30.0)
        writer_stopped = history_writer.stop(wait=True, cancel_futures=True, deadline=deadline)
        stopped = history_pool.stop(wait=True, cancel_futures=True, deadline=deadline)
        http_resources.close()
        if not stopped.completed or not writer_stopped.completed:
            raise RuntimeError("history worker shutdown exceeded 30 seconds")


def ExecuteQfqDownload(
    project_root: Path,
    *,
    reference_root: Path,
    cancel_requested: Callable[[], bool] = lambda: False,
    report: Callable[[ProgressMessage], None] | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> QfqUpdateResult:
    """Compose the zero-argument CLI/background use case without building the server."""
    if report is None:
        report = TerminalDownloadProgress()
    root = project_root / "data" / "qfq"
    history_root = project_root / "data/history"
    v2 = SQLiteQfqWindowCache(root, "v2")
    v3 = SQLiteQfqWindowCache(root, "v3")
    history = ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(history_root))
    started = time.monotonic()
    qfq_pool = BoundedExecutor(worker_count=8, queue_capacity=0, thread_name_prefix="qfq-download")
    qfq_pool.start()
    try:
        with ExitStack() as http_resources:
            http_pool = TencentSessionPool(
                tuple(http_resources.enter_context(requests.Session()) for _index in range(8))
            )
            st_source = HistoryStNameSource(
                HistoryStEvidencePath(reference_root),
                http_pool.borrow,
                qfq_pool,
                cancel_requested,
                report=lambda done, total, summary: _report_qfq_st_progress(report, started, done, total, summary),
                batch_size=8,
            )

            def load_qfq_universe(as_of: date) -> tuple[BaoStockSecurity, ...]:
                official = LoadCurrentAShareUniverse(
                    partial(FetchSseListings, get=requests.get),
                    partial(FetchSzseListings, get=requests.get),
                    15.0,
                )
                return SelectStEligibleUniverse(official, st_source.fetch(official, as_of))

            tencent = TencentQfqSupplier(
                TencentQfqDependencies(
                    http_pool.borrow,
                    load_qfq_universe,
                    cancel_requested,
                )
            )
            updater = UpdateQfqWindows(
                v2,
                v3,
                tencent,
                QfqCheckpoint(root / ".checkpoint.json"),
                cancel_requested,
                report,
                workers=8,
                worker_pool=qfq_pool,
            )
            return QfqUpdateRunner(
                history,
                updater,
                v2,
                v3,
                root / ".maintenance.lock",
                ShanghaiClock(now).now,
            ).execute()
    finally:
        stopped = qfq_pool.stop(wait=True, cancel_futures=True, deadline=ShutdownDeadline.start(30.0))
        if not stopped.completed:
            report(
                QfqMessage(
                    time.monotonic() - started, "关闭失败", "worker_deadline_exceeded", level=ProgressLevel.ERROR
                )
            )


def _report_qfq_st_progress(
    report: Callable[[ProgressMessage], None],
    started: float,
    completed: int,
    total: int,
    summary: HistoryStEligibilitySummary | None,
) -> None:
    if summary is None and completed % 256 != 0:
        return
    details = f"{ProgressNumber(completed)}/{ProgressNumber(total)}"
    if summary is not None:
        details += f" | 合格 {ProgressNumber(summary.clear)} | 曾ST排除 {ProgressNumber(summary.ever_st)} | 未知排除 {ProgressNumber(summary.unknown)}"
    report(QfqMessage(time.monotonic() - started, "ST资格", details))


def ExecuteResearchEvidence(command: ResearchEvidenceCommand) -> ResearchEvidenceResult:
    """Lazily assemble one offline research use case; never build the server runtime."""
    from trader.training.entrypoints.research_evidence import (
        CapabilityCommand,
        HoldoutCommand,
        IndustryAuditCommand,
        QualificationCommand,
    )
    from trader.training.infra.research.command_evidence import (
        CapabilityEvidencePublisher,
        HistoricalIndustryEvidenceReader,
        TerminalConclusionPublisher,
        TerminalHoldoutParentsReader,
    )

    if isinstance(command, IndustryAuditCommand):
        from trader.training.application.historical_industry_audit import AuditHistoricalIndustry

        return AuditHistoricalIndustry(HistoricalIndustryEvidenceReader(command.history_root)).execute(
            required_sample_codes=command.required_sample_codes, tushare_access_points=command.tushare_access_points
        )
    if isinstance(command, HoldoutCommand):
        from trader.training.application.terminal_holdout_execution import ExecuteTerminalHoldout

        return ExecuteTerminalHoldout(
            TerminalHoldoutParentsReader(command.parent_artifact_dir), TerminalConclusionPublisher(command.output_dir)
        ).execute()

    import requests

    from trader.training.infra.research.capability_http import BoundedCapabilitySession
    from trader.training.infra.research.h1_point_in_time_capability import FreeSourceH1CapabilityProbe

    with requests.Session() as session:
        session.trust_env = False
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        transport = BoundedCapabilitySession(session, timeout_seconds=command.timeout_seconds, monotonic=time.monotonic)
        probe = FreeSourceH1CapabilityProbe(transport, timeout_seconds=command.timeout_seconds)
        if isinstance(command, CapabilityCommand):
            from trader.training.application.capability_completion import CompleteCapabilityEvidence
            from trader.training.infra.research.h1_point_in_time_snapshot import SQLiteH1PointInTimeSnapshot

            return CompleteCapabilityEvidence(
                probe,
                SQLiteH1PointInTimeSnapshot(command.runtime_dir),
                CapabilityEvidencePublisher(command.artifact_dir),
            ).execute(code=command.code, historical_anchor_date=command.anchor_date)
        if isinstance(command, QualificationCommand):
            from functools import partial

            from trader.download.infra.history_snapshot_inspection import InspectHistorySnapshot
            from trader.training.application.point_in_time_data_qualification import QualifyPointInTimeData

            return QualifyPointInTimeData(
                partial(InspectHistorySnapshot, command.history_root, verify_partitions=True),
                HistoricalIndustryEvidenceReader(command.history_root),
                probe,
            ).execute(code=command.code, historical_anchor_date=command.anchor_date)
    raise TypeError("unsupported research evidence command")


@dataclass(frozen=True)
class _BuildContext:
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
class _PersistenceContext:
    sequence: PublicationSequence
    records: SQLiteDecisionRecords
    data_plane: SQLiteDataPlane
    budget: DeepSeekBudgetLedger
    outcomes: SQLiteOutcomeEvidenceRepository


@dataclass(frozen=True)
class _PublicationContext:
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
class _PublicationDependencies:
    sequence: PublicationSequence
    records: SQLiteDecisionRecords
    market_data: MarketFeatureService
    outcomes: SQLiteOutcomeEvidenceRepository
    additional_observers: tuple[DecisionEventConsumer[DecisionObservation], ...] = ()


def BuildSystem(
    config_path: str | Path,
    *,
    scoring_profile: ScoringProfileId | None = None,
    runtime_scope: ScoringProfileId | None = None,
    budget_path: Path | None = None,
    owns_shared_runtime: bool = True,
) -> ApplicationSystem[
    SQLiteResearchTraceIndex,
    SQLiteOutcomeEvidenceRepository,
    QfqDailyMaintenance,
    UnifiedWebServices,
    Flask,
]:
    settings = LoadRuntimeSettings(config_path)
    if runtime_scope is not None:
        settings = replace(
            settings,
            runtime_dir=settings.runtime_dir / "profiles" / runtime_scope,
            freeze_dir=settings.freeze_dir / runtime_scope,
        )
    strategy = LoadStrategySettings(
        settings.strategy_config_path,
        scoring_profile=scoring_profile,
    )
    watchlist = LoadLongWatchlist(settings.long_watchlist_path)
    cost_policy = LoadExecutionCostPolicy(Path(config_path).expanduser().resolve().parent)
    effective_config_version = (
        f"{settings.config_version}+{strategy.strategy_version}+cost_{ContentHash(cost_policy)[:16]}"
    )
    now = _utc_now
    latency = LatencyWaterfall()
    cadence_policy = CadencePolicy.from_seconds(settings.pipeline.cadence_seconds)
    cadence_planner = CadencePlanner(cadence_policy, started_at=ShanghaiClock(now).now())
    workers = _build_worker_context(settings, latency)
    context = _BuildContext(
        settings,
        strategy,
        watchlist,
        effective_config_version,
        now,
        latency,
        cadence_policy,
        workers,
        budget_path or settings.runtime_dir / "deepseek-budget.sqlite3",
        owns_shared_runtime,
    )
    calendar = ChinaTradingCalendar(settings.runtime_dir / "calendar.json")
    persistence = _build_persistence(context)
    profile_id = strategy.scoring_profile
    try:
        loaded_profile = LoadScoringProfile(profile_id, training_root=settings.project_root / "data" / "train")
    except ScoringArtifactsMissingError:
        loaded_profile = None
    WarnMissingLocalData(settings.project_root, profile_id, model_missing=loaded_profile is None)
    profile_contract = V2_TRAINING_PROFILE if profile_id == "v2" else V3_TRAINING_PROFILE
    training_settings = LoadProfileTrainingSettings(settings.project_root, profile_id)
    training_settings.require_profile(profile_contract)
    market_data = _build_market_data(
        context,
        persistence.data_plane,
        calendar,
        history_lookback_sessions=training_settings.history_sessions,
        model_momentum_horizons=training_settings.momentum_horizons,
    )
    reviewer = _build_reviewer(context, persistence.budget)
    policy = _recommendation_policy(context.strategy)
    shared_model_features = SharedModelFeatureCache()
    model_scoring = PublishedModelScoringService(
        ModelScoringRouter(
            profile_id,
            {
                strategy: ProductionModelScoringService(
                    loaded_profile, strategy, cost_policy=cost_policy, shared_features=shared_model_features
                )
                if loaded_profile is not None
                else UnavailableModelCapability(profile_id, training_settings.history_sessions)
                for strategy in (Strategy.TOMORROW, Strategy.D25)
            },
        )
    )
    candidate_filtering = CandidateFilteringService(
        policy,
        model_scoring,
        settings.market_data.candidate_pool_size,
    )
    local_scoring = LocalScoringService(
        model_scoring,
        RankingSelectionService(),
        RiskControlService(),
    )
    publication_io = PublicationIoTracker(now=ShanghaiClock(now).now, monotonic=time.monotonic)
    publication = _build_publication(
        context,
        calendar,
        _PublicationDependencies(persistence.sequence, persistence.records, market_data, persistence.outcomes),
        publication_io=publication_io,
    )
    native_data = MarketInputCoordinator(
        market_data,
        candidate_pool_size=settings.market_data.candidate_pool_size,
        dependencies=InputRefreshDependencies(
            publication.long_runtime,
            policy,
            ShanghaiClock(now).now,
            model_scoring,
            candidate_filtering,
            monotonic=time.monotonic,
        ),
    )
    decision_builder = LocalDecisionBuilder(
        native_data,
        config_version=effective_config_version,
        candidate_pool_size=settings.market_data.candidate_pool_size,
        dependencies=DecisionBuildDependencies(
            policy,
            publication.decision_drafts,
            ShanghaiClock(now).now,
            model_scoring,
            local_scoring,
            TryBuildCommittedResearchAudit,
            monotonic=time.monotonic,
            next_sequence=lambda: persistence.sequence.allocate(
                width=2,
                minimum=max(
                    (
                        current.sequence + 1
                        for strategy in (Strategy.TOMORROW, Strategy.D25)
                        if (current := publication.tomorrow_index.snapshot(strategy).current) is not None
                    ),
                    default=1,
                ),
            ),
        ),
    )
    deepseek = DeepSeekAdapter(reviewer, policy, decision_builder, ScoreFusionService(), now=ShanghaiClock(now).now)

    def publish_overlay_event(overlay: DecisionOverlay) -> object:
        current = publication.tomorrow_index.snapshot(overlay.strategy).current
        if not isinstance(current, ScoredDecision) or current.version != overlay.parent_version:
            raise ValueError("overlay event parent decision is unavailable")
        return publication.decision_events.publish_overlay(
            overlay,
            parent_version=current.version,
        )

    scheduler = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=context.workers.control_pool,
            clock=ShanghaiClock(context.now),
            calendar=calendar,
            cadence=cadence_planner,
            data=native_data,
            decisions=decision_builder,
            reviews=deepseek,
            index=publication.tomorrow_index,
            observer=publication.observer,
            freezes=FreezeAdapter(
                publication.tomorrow_freezer,
                publication.d25_freezer,
            ),
            settlement=OutcomeSettlementAdapter(
                market_data,
                OutcomeSettlementService(
                    market_data,
                    persistence.outcomes,
                    persistence.outcomes,
                    session_distance=calendar.session_distance,
                ),
            ),
            research_factory=ResearchRuntime,
            publish_decision=publication.decision_events.publish_committed,
            publish_overlay=publish_overlay_event,
            latency=latency,
            publication_io=publication_io,
        ),
        config_version=effective_config_version,
        shutdown_timeout_seconds=settings.pipeline.shutdown_timeout_seconds,
    )

    history_observer = PublishedHistoryObserver(market_data.history)
    qfq_maintenance = QfqDailyMaintenance(
        lambda cancel: ExecuteQfqDownload(
            settings.project_root, reference_root=settings.reference_dir, cancel_requested=cancel, now=now
        ),
        now=ShanghaiClock(now).now,
        enabled=owns_shared_runtime,
    )
    supervisor = RuntimeSupervisor(
        scheduler,
        RuntimeSupervisorConfig(
            now=now,
            initializers=(
                persistence.sequence.initialize,
                publication.tomorrow_records.initialize,
                lambda: _initialize_if_available(publication.research_trace),
                lambda: _initialize_if_available(persistence.outcomes),
                lambda: _initialize_reference_data_plane(market_data, persistence.data_plane, now()),
                persistence.budget.initialize,
                lambda: publication.tomorrow_freezer.restore(now().date()),
                lambda: publication.d25_freezer.restore(now().date()),
                lambda: persistence.budget.recover_incomplete(now()),
            ),
            interval_seconds=SchedulerIntervalSeconds,
            shutdown_timeout_seconds=settings.pipeline.shutdown_timeout_seconds,
            record_error=lambda _error: None,
        ),
    )
    web_services = UnifiedWebServices(
        publication.decision_queries,
        publication.decision_events,
        lambda: _runtime_status(
            scheduler,
            reviewer,
            market_data.health,
            model_scoring.status(),
        ),
        WebApiConfig(
            heartbeat_seconds=settings.pipeline.publish_heartbeat_seconds,
            snapshot_retention_seconds=settings.api.web_snapshot_retention_seconds,
        ),
        profile_id,
    )
    app = CreateApp(services=web_services)
    return ApplicationSystem(
        settings=settings,
        strategy=strategy,
        watchlist=watchlist,
        app=app,
        supervisor=supervisor,
        scheduler=scheduler,
        records=persistence.records,
        market_cache=workers.market_cache,
        research_pool=workers.research_pool,
        source_lanes=workers.source_lanes,
        data_pool=workers.data_pool,
        quote_pool=workers.quote_pool,
        market_quote_pool=workers.market_quote_pool,
        long_runtime=publication.long_runtime,
        decision_queries=publication.decision_queries,
        decision_events=publication.decision_events,
        tomorrow_index=publication.tomorrow_index,
        tomorrow_records=publication.tomorrow_records,
        research_trace=publication.research_trace,
        outcome_evidence=persistence.outcomes,
        history_observer=history_observer,
        qfq_maintenance=qfq_maintenance,
        web_services=web_services,
    )


def BuildProfiledSystem(config_path: str | Path) -> ProfiledApplicationSystem[Flask]:
    """Build both production profiles with one read-only delivery surface."""

    settings = LoadRuntimeSettings(config_path)
    shared_budget = settings.runtime_dir / "shared" / "deepseek-budget.sqlite3"
    systems = (
        BuildSystem(
            config_path,
            scoring_profile="v2",
            runtime_scope="v2",
            budget_path=shared_budget,
            owns_shared_runtime=True,
        ),
        BuildSystem(
            config_path,
            scoring_profile="v3",
            runtime_scope="v3",
            budget_path=shared_budget,
            owns_shared_runtime=False,
        ),
    )
    default_profile = LoadStrategySettings(settings.strategy_config_path).scoring_profile
    profiles: dict[str, UnifiedWebServices] = {system.profile_id: system.web_services for system in systems}
    app = CreateApp(
        services=ProfiledWebServices(
            profiles=profiles,
            default_profile=default_profile,
            long_profile="v2",
            config=WebApiConfig(
                heartbeat_seconds=settings.pipeline.publish_heartbeat_seconds,
                snapshot_retention_seconds=settings.api.web_snapshot_retention_seconds,
            ),
        )
    )
    return ProfiledApplicationSystem(settings, app, systems)


def _build_worker_context(settings: RuntimeSettings, latency: LatencyWaterfall) -> RuntimeWorkerResources:
    urgent_worker_count = 1 if settings.pipeline.market_workers > 1 else 0
    control_pool = BoundedExecutor(
        worker_count=2,
        urgent_worker_count=1,
        queue_capacity=4,
        thread_name_prefix="trader-control",
    )
    data_pool = BoundedExecutor(
        worker_count=settings.pipeline.market_workers + urgent_worker_count,
        urgent_worker_count=urgent_worker_count,
        queue_capacity=5,
        thread_name_prefix="source-data",
    )
    source_lanes = SourceLaneScheduler(data_pool, latency=latency)
    quote_pool = BoundedExecutor(
        worker_count=4,
        queue_capacity=4,
        thread_name_prefix="candidate-quotes",
    )
    market_quote_pool = BoundedExecutor(
        worker_count=3,
        queue_capacity=3,
        thread_name_prefix="market-quotes",
    )
    research_pool = BoundedExecutor(
        worker_count=settings.pipeline.market_workers,
        queue_capacity=settings.market_data.candidate_pool_size,
        thread_name_prefix="research-data",
    )
    persistence_pool = BoundedExecutor(
        worker_count=1,
        queue_capacity=max(1, settings.pipeline.event_queue_size),
        thread_name_prefix="trader-persistence",
    )
    json_writer = RuntimeJsonWriter(persistence_pool)
    market_cache: BoundedLruCache[object] = BoundedLruCache(
        settings.market_data.cache_policy,
        cadence_seconds=settings.pipeline.cadence_seconds,
        wall_clock=_utc_now,
    )
    return RuntimeWorkerResources(
        control_pool=control_pool,
        data_pool=data_pool,
        quote_pool=quote_pool,
        market_quote_pool=market_quote_pool,
        research_pool=research_pool,
        persistence_pool=persistence_pool,
        source_lanes=source_lanes,
        json_writer=json_writer,
        market_cache=market_cache,
    )


def _build_market_data(
    context: _BuildContext,
    data_plane: SQLiteDataPlane,
    calendar: ChinaTradingCalendar,
    *,
    history_lookback_sessions: int = 61,
    model_momentum_horizons: tuple[int, ...] = (20, 40, 60),
) -> MarketFeatureService:
    settings = context.settings
    strategy = context.strategy
    workers = context.workers
    now = context.now
    data_pool = workers.data_pool
    source_lanes = workers.source_lanes
    market_cache = workers.market_cache
    intraday_client = EastmoneyClient(
        session_factory=requests.Session,
        timeout_seconds=settings.market_data.candidate_timeout_seconds,
        worker_pool=workers.quote_pool,
        cancel_requested=lambda: source_lanes.is_stopped("eastmoney"),
        wall_clock=now,
    )
    sina = SinaClient(
        worker_pool=workers.quote_pool,
        session_factory=requests.Session,
        timeout_seconds=settings.market_data.sina_timeout_seconds,
        cancel_requested=lambda: source_lanes.is_stopped("sina"),
        wall_clock=now,
    )
    tencent = TencentClient(
        session_factory=requests.Session,
        timeout_seconds=settings.market_data.candidate_timeout_seconds,
        cancel_requested=lambda: source_lanes.is_stopped("tencent"),
        wall_clock=now,
        worker_pool=workers.quote_pool,
    )

    def official_quote_codes() -> tuple[str, ...]:
        snapshot = references.static_reference().reference
        if snapshot is None:
            return ()
        codes = eligibility.filter_codes(tuple(issuer.code for issuer in snapshot.records), now())
        historical_st = history_cache.historical_st_eligibility()
        if historical_st.status == "ready":
            return tuple(code for code in codes if historical_st.eligible(code))
        return codes

    tencent_market_client = TencentClient(
        session_factory=requests.Session,
        timeout_seconds=settings.market_data.candidate_timeout_seconds,
        cancel_requested=lambda: source_lanes.is_stopped("tencent_market"),
        wall_clock=now,
        worker_pool=workers.market_quote_pool,
    )
    tencent_market = TencentFullMarketSource(tencent_market_client, official_quote_codes)
    gateway = MarketDataGateway(
        tencent_market,
        sina,
        tencent,
        minimum_market_rows=settings.market_data.minimum_market_rows,
        circuit_breaker_failures=settings.market_data.circuit_breaker_failures,
        circuit_breaker_seconds=settings.market_data.circuit_breaker_seconds,
        full_market_primary_timeout_seconds=settings.market_data.full_market_primary_timeout_seconds,
        full_market_fetchers={
            "tencent_market": lambda deadline, cancel_event: tencent_market.fetch_market(
                deadline=deadline,
                cancel_event=cancel_event,
            ),
            "sina": lambda deadline, cancel_event: sina.fetch_market(
                deadline=deadline,
                cancel_event=cancel_event,
            ),
        },
        recovery_probes={
            "tencent_market": tencent_market.probe_market,
            "sina": sina.probe_market,
        },
        worker_pool=data_pool,
        source_lanes=source_lanes,
        cache=market_cache,
        source_contracts=settings.market_data.source_contracts,
        config_version=settings.config_version,
        schema_version="market_snapshot",
        wall_clock=now,
        latency=context.latency,
        listing_open_dates=calendar.open_dates,
    )
    evidence_cache_dir = settings.runtime_dir / "evidence_cache"
    feature_builder = FeatureBuilder(
        strategy.news_signal,
        strategy.tomorrow_tail_signal,
        strategy.market_regime,
        strategy.long_research,
        strategy.feature_component_weights,
        model_momentum_horizons=model_momentum_horizons,
    )
    research_client = AkshareResearchClient(
        get=cast(AkshareGetFunction, requests.get),
        timeout_seconds=settings.market_data.research_timeout_seconds,
        long_research_policy=strategy.long_research,
        evidence_cache_dir=evidence_cache_dir,
        json_writer=workers.json_writer,
        cancel_requested=lambda: not workers.research_pool.is_running(),
    )
    tushare_client = TushareClient(
        sdk_factory=partial(BuildTushareSdk, session_factory=requests.Session),
        token=settings.market_data.tushare.token if settings.market_data.tushare.enabled else "",
        points=settings.market_data.tushare.points,
        timeout_seconds=settings.market_data.tushare.timeout_seconds,
        circuit_breaker_failures=settings.market_data.circuit_breaker_failures,
        circuit_breaker_seconds=settings.market_data.circuit_breaker_seconds,
        cancel_requested=lambda: source_lanes.is_stopped("tushare"),
        wall_clock=now,
    )
    runner = MarketTaskRunner(
        worker_pool=data_pool,
        source_lanes=source_lanes,
        cache=market_cache,
        source_contracts=settings.market_data.source_contracts,
        config_version=settings.config_version,
        schema_version="market_snapshot",
        wall_clock=now,
    )
    research_runner = MarketTaskRunner(
        worker_pool=workers.research_pool,
        source_lanes=None,
        cache=market_cache,
        source_contracts=settings.market_data.source_contracts,
        config_version=settings.config_version,
        schema_version="market_snapshot",
        wall_clock=now,
    )
    history_root = settings.project_root / "data" / "history"
    published_history = ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(history_root))
    history_cache = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(
            SQLiteQfqWindowCache(settings.project_root / "data/qfq", "v2" if history_lookback_sessions > 61 else "v3")
        ),
        lookback_sessions=history_lookback_sessions,
        outcome_history=published_history,
        open_dates=calendar.open_dates,
    )
    industry_source = LocalIndustrySource(
        settings.reference_dir / "industry_queries.sqlite3",
        BaoStockIndustryClient(),
    )
    references = ReferenceLoader(
        gateway,
        runner,
        tushare_client,
        security_master_client=ExchangeSecurityMasterClient(
            sse_fetcher=partial(FetchSseListings, get=requests.get),
            szse_fetcher=partial(FetchSzseListings, get=requests.get),
            timeout_seconds=max(15.0, settings.market_data.eastmoney_timeout_seconds),
            wall_clock=now,
        ),
        model_industry_client=industry_source,
        model_industry_cache_reader=industry_source.load_cached,
        security_master_refresh_ttl_seconds=_fixed_cache_ttl(settings, "security_master_calendar"),
        security_master_retry_seconds=(
            settings.market_data.cache_policy.datasets["security_master_calendar"].negative_ttl_seconds
        ),
        data_plane=data_plane,
        monotonic=time.monotonic,
    )
    references.load_model_industry_cache(now())
    gateway.set_security_reference_persistence_sink(references.schedule_security_master_persistence)
    reference_root = settings.reference_dir
    eligibility = SQLiteIssuerEligibilityIndex(reference_root, read_only=not context.owns_shared_runtime)
    if context.owns_shared_runtime:
        try:
            eligibility.record_manual_blacklist(
                strategy.hard_filters.blacklist_codes,
                now(),
                context.effective_config_version,
            )
        except RuntimeError:
            # The typed index retains any facts it could verify; persistence degradation stays observable.
            pass
    research = ResearchLoader(
        research_client,
        research_runner,
        data_plane=data_plane,
        workers=settings.pipeline.market_workers,
        ttl_seconds=_fixed_cache_ttl(settings, "research_success"),
        circuit_breaker_failures=settings.market_data.circuit_breaker_failures,
        circuit_breaker_seconds=settings.market_data.circuit_breaker_seconds,
        capacity=settings.market_data.cache_policy.datasets["research_success"].capacity,
        cache_dir=evidence_cache_dir,
        json_writer=workers.json_writer,
        monotonic=time.monotonic,
    )
    intraday_loader = IntradayLoader(
        intraday_client,
        runner,
        workers=settings.pipeline.market_workers,
        ttl_seconds=_fixed_cache_ttl(settings, "intraday_minutes"),
        batch_timeout_seconds=settings.market_data.candidate_timeout_seconds,
        capacity=settings.market_data.cache_policy.datasets["intraday_minutes"].capacity,
        monotonic=time.monotonic,
    )
    quote_cache = QuoteCache(
        QuoteCacheDependencies(gateway, feature_builder, history_cache, references),
        market_ttl_seconds=min(context.cadence_policy.intervals[PipelineTask.FULL_MARKET].values()),
        candidate_capacity=settings.market_data.cache_policy.datasets["candidate_quotes"].capacity,
        monotonic=time.monotonic,
    )
    market_health = MarketDataHealth(
        MarketDataHealthDependencies(
            quote_cache,
            history_cache,
            research,
            intraday_loader,
            references,
            eligibility,
        ),
        wall_clock=now,
    )
    market_data = MarketFeatureService(
        MarketFeatureDependencies(
            quote_cache,
            history_cache,
            research,
            intraday_loader,
            references,
            runner,
            market_health,
            eligibility,
            time.monotonic,
            StaticMarketPipeline(
                eligibility,
                monotonic=time.monotonic,
                historical_st_eligibility=history_cache.historical_st_eligibility,
            ),
        )
    )
    return market_data


def _build_persistence(context: _BuildContext) -> _PersistenceContext:
    settings = context.settings
    runtime_database_lock = threading.Lock()
    records = SQLiteDecisionRecords(settings.freeze_dir)
    data_plane = SQLiteDataPlane(settings.runtime_dir)
    outcomes = SQLiteOutcomeEvidenceRepository(settings.runtime_dir, records)
    budget = DeepSeekBudgetLedger(
        context.budget_path,
        daily_hard_limit=settings.deepseek.daily_hard_limit,
        strategy_limits=settings.deepseek.strategy_limits,
        stage_targets=settings.deepseek.stage_targets,
        stage_limits=settings.deepseek.stage_limits,
        challenger_limits=settings.deepseek.challenger_limits,
        challenger_daily_limit=settings.deepseek.challenger_daily_limit,
        health_policy=DeepSeekHealthPolicy(
            consecutive_failure_limit=settings.deepseek.adaptive.consecutive_failure_limit,
            rolling_window=settings.deepseek.adaptive.rolling_window,
            minimum_application_ratio=settings.deepseek.adaptive.minimum_application_ratio,
            healthy_application_ratio=settings.deepseek.adaptive.healthy_application_ratio,
            healthy_batch_count=settings.deepseek.adaptive.healthy_batch_count,
            cooldown_seconds=settings.deepseek.adaptive.cooldown_seconds,
        ),
        write_lock=runtime_database_lock,
    )
    return _PersistenceContext(
        PublicationSequence(settings.runtime_dir / "publication-sequence.sqlite3"),
        records,
        data_plane,
        budget,
        outcomes,
    )


def _build_reviewer(context: _BuildContext, budget: DeepSeekBudgetLedger) -> DeepSeekReviewer:
    settings = context.settings
    strategy = context.strategy
    return DeepSeekReviewer(
        settings.deepseek,
        budget,
        CreateDeepseekClient(),
        ReviewCache(
            maximum_entries=2000,
            ttl_seconds=600,
            shared_cache=context.workers.market_cache,
            config_version=context.effective_config_version,
            seen_capacity=6000,
        ),
        dimension_weights={Strategy(name): weights for name, weights in strategy.dimension_weights.items()},
        strategy_version=strategy.strategy_version,
        confidence_coverage_min=strategy.fusion.confidence_coverage_min,
        minimum_known_dimensions=strategy.fusion.minimum_known_dimensions,
        now=context.now,
    )


def _build_publication(
    context: _BuildContext,
    calendar: ChinaTradingCalendar,
    dependencies: _PublicationDependencies,
    *,
    publication_io: PublicationIoTracker,
) -> _PublicationContext:
    settings = context.settings
    records = dependencies.records
    market_data = dependencies.market_data
    tomorrow_decisions = UnifiedDecisionIndex()
    decision_drafts = UnifiedDecisionDraftIndex()
    decision_events = UnifiedDecisionEventStream(
        history_size=settings.api.sse_history_size,
        client_queue_size=settings.api.sse_client_queue_size,
        subscriber_limit=settings.api.sse_max_clients,
    )
    clock = ShanghaiClock(context.now)
    query_sources = DecisionQuerySources(market_data, dependencies.outcomes, market_data.references)
    decision_queries = UnifiedDecisionQueries(tomorrow_decisions, decision_drafts, records, clock, query_sources)
    research_trace = SQLiteResearchTraceIndex(
        settings.runtime_dir,
        limits=ResearchTraceLimits(events_per_trade_date=max(2048, settings.pipeline.event_queue_size * 4)),
    )

    observer = AsyncDecisionObserver[DecisionObservation](
        (research_trace.record, *dependencies.additional_observers),
        capacity=max(1, min(16, settings.pipeline.event_queue_size)),
        thread_name="trader-decision-observer",
        prepare=DecisionObservation.materialize,
    )
    tomorrow_freezer = ScoredFreezeCoordinator(
        tomorrow_decisions,
        records,
        clock,
        runtime_identity=DecisionRuntimeIdentity(
            context.effective_config_version,
            context.strategy.strategy_version,
            context.strategy.fusion.version,
        ),
        strategy=Strategy.TOMORROW,
        publication_io=publication_io,
    )
    d25_freezer = ScoredFreezeCoordinator(
        tomorrow_decisions,
        records,
        clock,
        runtime_identity=DecisionRuntimeIdentity(
            context.effective_config_version,
            context.strategy.strategy_version,
            context.strategy.fusion.version,
        ),
        strategy=Strategy.D25,
        publication_io=publication_io,
    )
    long_runtime = LongRuntime(
        LongRuntimeDependencies(
            market_data,
            tomorrow_decisions,
            context.now,
            decision_events.publish_projection,
            context.owns_shared_runtime,
            next_sequence=dependencies.sequence.allocate,
        ),
        config_version=context.effective_config_version,
        watchlist_version=context.watchlist.watchlist_version,
        items=_long_item_definitions(context.watchlist),
        groups=_long_group_definitions(context.watchlist),
    )
    return _PublicationContext(
        records,
        tomorrow_decisions,
        decision_drafts,
        research_trace,
        long_runtime,
        decision_queries,
        decision_events,
        tomorrow_freezer,
        d25_freezer,
        observer,
    )


__all__ = ["ApplicationSystem", "ProfiledApplicationSystem", "BuildProfiledSystem", "BuildSystem"]
