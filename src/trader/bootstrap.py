"""Unique composition root for the current application."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack, suppress
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from flask import Flask

from trader.download.application.company_evidence import CompanyEvidenceDependencies, SyncCompanyEvidence
from trader.download.application.company_exclusions import PublishCompanyExclusions
from trader.download.application.download_history import DownloadHistoryUseCase
from trader.download.application.prepare_reference import PrepareReference, ReferencePreparationDependencies
from trader.download.application.progress_message import ProgressLevel, ProgressMessage
from trader.download.application.qfq_download_progress import QfqMessage
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.application.sync_data import SyncData, SyncResult, SyncTarget
from trader.download.application.update_qfq import UpdateQfqWindows
from trader.download.domain import company_evidence
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import HistorySyncConfiguration, HistorySyncProgressPort
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.download.entrypoints import company_evidence_progress
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra import company_document_files, company_facts_reader
from trader.download.infra.akshare_calendar import FetchAkshareCalendar
from trader.download.infra.baostock_industry import BaoStockIndustryClient, FetchBaoStockIndustries
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.download.infra.current_industry_publication import CurrentIndustryCache, CurrentIndustryPublication
from trader.download.infra.download_settings import LoadCompanySyncConfiguration, LoadHistorySyncConfiguration
from trader.download.infra.exchange_security_master import FetchSseListings, FetchSzseListings
from trader.download.infra.exchange_security_universe import LoadCurrentAShareUniverse
from trader.download.infra.history_industry_cache import HistoryIndustryQueryCache
from trader.download.infra.history_st_source import HistoryStNameSource
from trader.download.infra.history_supplier_router import HistorySupplierRouter
from trader.download.infra.history_sync_gateway import HistorySyncGateway
from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.qfq_update_runner import QfqUpdateRunner
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.download.infra.tencent_qfq_supplier import TencentQfqDependencies, TencentQfqSupplier, TencentSessionPool
from trader.download.infra.terminal_progress import TerminalDownloadProgress
from trader.http_api.route_services import ProfiledWebServices, UnifiedWebServices, WebApiConfig
from trader.infra.atomic_files.json import RuntimeJsonWriter
from trader.infra.cache import BoundedLruCache
from trader.infra.cache_contracts import CanonicalJsonBytes
from trader.infra.clock.shanghai import ShanghaiClock
from trader.infra.clock.utc import UtcNow as _utc_now
from trader.infra.http_contracts import HttpAttempt, HttpParameter, HttpRequest, HttpResponse, HttpTransport
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.initialization import InitializeIfAvailable as _initialize_if_available
from trader.infra.isolated_call import RunIsolatedCall
from trader.infra.process_lock import ProcessLock
from trader.infra.progress_heartbeat import ProgressHeartbeat
from trader.infra.serialization.canonical import ContentHash
from trader.infra.settings import LoadLongWatchlist, LoadRuntimeSettings, LoadStrategySettings, RuntimeSettings
from trader.infra.settings.data_paths import DataPaths
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.storage_probe import StorageProbe
from trader.infra.storage_runtime import ConfigureStorage
from trader.infra.storage_types import StorageOverride
from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.long_runtime import LongRuntime, LongRuntimeDependencies
from trader.recommendation.application.outcome_settlement import OutcomeSettlementAdapter, OutcomeSettlementService
from trader.recommendation.application.pipeline.candidate_pool.candidate_pool_service import CandidateFilteringService
from trader.recommendation.application.pipeline.data_source import source_router
from trader.recommendation.application.pipeline.downside_action.downside_protection import RiskControlService
from trader.recommendation.application.pipeline.final_selection.grouped_ranking import RankingSelectionService
from trader.recommendation.application.pipeline.freeze_publish import freeze_coordinator, read_only_queries
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import FreezeObservation
from trader.recommendation.application.pipeline.freeze_publish.freeze_observers import AsyncFreezeObserver
from trader.recommendation.application.pipeline.freeze_publish.publication_io import PublicationIoTracker
from trader.recommendation.application.pipeline.freeze_publish.recent_news_codes import RecentPublishedNewsCodes
from trader.recommendation.application.pipeline.freeze_publish.runtime_adapters import DeepSeekAdapter, FreezeAdapter
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.pipeline.local_score import local_freeze_builder
from trader.recommendation.application.pipeline.local_score.base_scoring import LocalScoringService
from trader.recommendation.application.pipeline.score_merge.score_fusion import ScoreFusionService
from trader.recommendation.application.pipeline.static_market.static_market_cache import StaticMarketCache
from trader.recommendation.application.pipeline.static_market.static_pipeline import StaticMarketPipeline
from trader.recommendation.application.ports.eligibility import IssuerEligibilityPort
from trader.recommendation.application.runtime import supervisor as runtime_supervisor
from trader.recommendation.application.runtime.cadence import CadencePlanner, CadencePolicy, PipelineTask
from trader.recommendation.application.runtime.latency import LatencyWaterfall
from trader.recommendation.application.runtime.run_scope import RecommendationRunScope
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies
from trader.recommendation.application.runtime.scheduler_runtime import SchedulerRuntime
from trader.recommendation.application.runtime.source_lanes import SourceLaneScheduler
from trader.recommendation.domain.market.eligibility import SelectQuotePopulation
from trader.recommendation.domain.publication.freeze_identity import FreezeOverlay, ScoredFreeze
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection
from trader.recommendation.infra import policy_settings
from trader.recommendation.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.recommendation.infra.deepseek.budget_file import BudgetStateFile
from trader.recommendation.infra.deepseek.cache import BuildSharedReviewCacheIdentity, ReviewCache
from trader.recommendation.infra.deepseek.client import DeepSeekHttpClient
from trader.recommendation.infra.deepseek.health_gate import DeepSeekHealthPolicy
from trader.recommendation.infra.deepseek.reviewer import DeepSeekReviewer
from trader.recommendation.infra.market_data import market_feature_service
from trader.recommendation.infra.market_data.candidate_quote_cache import QuoteCache, QuoteCacheDependencies
from trader.recommendation.infra.market_data.company_eligibility import CompanyEligibility
from trader.recommendation.infra.market_data.gateway import MarketDataGateway
from trader.recommendation.infra.market_data.intraday_loader import IntradayLoader
from trader.recommendation.infra.market_data.market_data_health import MarketDataHealth, MarketDataHealthDependencies
from trader.recommendation.infra.market_data.market_task_runner import MarketTaskRunner
from trader.recommendation.infra.market_data.providers.eastmoney import EastmoneyClient
from trader.recommendation.infra.market_data.providers.sina import SinaClient
from trader.recommendation.infra.market_data.providers.tencent import TencentClient, TencentFullMarketSource
from trader.recommendation.infra.market_data.published_company_research import PublishedCompanyResearch
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.published_input_preloader import PublishedInputPreloader
from trader.recommendation.infra.market_data.published_references import PublishedReferences
from trader.recommendation.infra.market_data.reference_initialization import InitializeMarketReferences
from trader.recommendation.infra.market_data.shared_history_projection import SharedHistoryProjection
from trader.recommendation.infra.market_data.trading_calendar import ChinaTradingCalendar
from trader.recommendation.infra.normalization.features import FeatureBuilder
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords
from trader.recommendation.infra.persistence.freeze_trace_index import ResearchTraceLimits, SQLiteFreezeTraceIndex
from trader.recommendation.infra.persistence.outcome_evidence_index import SQLiteOutcomeEvidenceIndex
from trader.recommendation.infra.persistence.publication_sequence import PublicationSequence
from trader.recommendation.infra.runtime import composition_inputs
from trader.recommendation.infra.runtime.application_system import ApplicationSystem, RuntimeWorkerResources
from trader.recommendation.infra.runtime.initialization import FixedCacheTtl as _fixed_cache_ttl
from trader.recommendation.infra.runtime.profiled_application import ProfiledApplicationSystem
from trader.recommendation.infra.runtime.startup_readiness import WarnMissingLocalData
from trader.recommendation.infra.scoring import profile_factory
from trader.recommendation.infra.scoring.cost_settings import LoadExecutionCostPolicy
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile, ScoringModelsUnavailableError
from trader.recommendation.infra.status_projection import RuntimeStatus as _runtime_status
from trader.training.application.research_audit import TryBuildCommittedResearchAudit
from trader.training.application.research_runtime import ResearchRuntime
from trader.training.domain.training_reminder import TrainingReminderStatus
from trader.training.domain.v1.learned_rank import V1TrainingSettings
from trader.training.infra.history.history_training_due import PublishedTrainingState
from trader.training.infra.history.training_reminder_status import ReadTrainingReminderStatus
from trader.training.infra.training_settings import LoadRuntimeTrainingSettings, ProfileTrainingSettings
from trader.web import CreateApp

if TYPE_CHECKING:
    from trader.training.entrypoints.research_evidence import ResearchEvidenceCommand, ResearchEvidenceResult


def OpenExternalHttp(
    *,
    observe: Callable[[HttpAttempt], None] | None = None,
    inspect_response: Callable[[HttpRequest, HttpResponse], None] | None = None,
) -> HttpTransport:
    return RequestsHttpTransport(observe=observe, inspect_response=inspect_response)


def FetchExternalHttp(
    url: str,
    *,
    timeout: float,
    params: Mapping[str, HttpParameter] | None = None,
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    with OpenExternalHttp() as transport:
        return transport.get(url, timeout=timeout, params=params, headers=headers, max_response_bytes=32 * 1024 * 1024)


def _post_external_http(url: str, *, headers: Mapping[str, str], json: object, timeout: float) -> HttpResponse:
    with OpenExternalHttp() as transport:
        return transport.post(url, headers=headers, json=json, timeout=timeout)


def BuildIndustrySource(*, cancel_requested: Callable[[], bool] | None = None) -> BaoStockIndustryClient:
    execute = partial(RunIsolatedCall, cancel_requested=cancel_requested)
    return BaoStockIndustryClient(fetch_rows=partial(FetchBaoStockIndustries, execute=execute))


def BuildPerformanceExecutor(workers: int) -> BoundedExecutor:
    return BoundedExecutor(worker_count=workers, queue_capacity=workers, thread_name_prefix="performance-models")


def PrepareRuntimeStorage(settings: RuntimeSettings, *, choose: bool = False, report: bool = True) -> RuntimeSettings:
    return ConfigureStorage(settings, choose=choose, report=report)


def ExecuteSync(
    config_path: Path, target: SyncTarget, *, cancel_requested: Callable[[], bool] = lambda: False
) -> SyncResult:
    settings = LoadRuntimeSettings(config_path)
    paths = settings.data_paths
    clock = ShanghaiClock(_utc_now)
    progress = company_evidence_progress.CompanyEvidenceProgress()
    history_progress = StderrHistorySyncProgress()
    configuration = LoadHistorySyncConfiguration(settings.project_root, settings.data_paths)
    company_configuration = LoadCompanySyncConfiguration(settings.project_root)
    return SyncData(
        prepare=lambda: _prepare_reference(paths.reference, clock.now(), progress, cancel_requested=cancel_requested),
        history=lambda reference: ExecuteHistoryDownload(
            configuration,
            reference=reference,
            progress=history_progress,
            cancel_requested=cancel_requested,
            storage_overrides=settings.storage_overrides,
        ),
        market=lambda reference: ExecuteQfqDownload(paths, reference=reference, cancel_requested=cancel_requested),
        company=lambda reference: _sync_company_evidence(
            settings.data_paths,
            progress,
            company_evidence.CompanyEvidenceRequest.from_reference(
                reference,
                clock.now(),
                company_configuration,
                LoadStrategySettings(settings.strategy_config_path).company_scoring.events,
                news_codes=RecentPublishedNewsCodes(
                    tuple(
                        SQLiteFreezeRecords(settings.data_paths.recommendation(profile) / "freeze.sqlite3")
                        for profile in ("v2", "v3")
                    ),
                    clock.now(),
                ),
            ),
            cancel_requested=cancel_requested,
        ),
        cancelled=cancel_requested,
    ).execute(target)


def CreateCompanyEligibilityReader(path: Path) -> CompanyEligibility:
    return CompanyEligibility(PublishCompanyExclusions(SQLiteCompanyFactsIndex(path)), read_only=True)


def _sync_company_evidence(
    paths: DataPaths,
    progress: Callable[[company_evidence.EvidenceSyncProgress], None] | None,
    request: company_evidence.CompanyEvidenceRequest,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> company_evidence.CompanyEvidenceSyncResult:
    with ExitStack() as resources:
        resources.enter_context(ProcessLock(paths.company_collection.parent / "evidence.lock", label="sync company"))
        heartbeat = resources.enter_context(ProgressHeartbeat(progress))
        configuration = request.configuration
        source_factory = partial(
            CninfoMarketDisclosureSource,
            monotonic=time.monotonic,
            sleeper=time.sleep,
            timeout_seconds=configuration.source_timeout_seconds,
            document_parse_timeout_seconds=configuration.document_parse_timeout_seconds,
            document_limits=configuration.document_limits,
            request_interval_seconds=configuration.request_interval_seconds,
            cancel_requested=cancel_requested,
        )
        documents = tuple(
            source_factory(resources.enter_context(OpenExternalHttp())) for _ in range(configuration.document_workers)
        )
        structured = EastmoneyCompanySource(
            resources.enter_context(OpenExternalHttp()),
            monotonic=time.monotonic,
            timeout_seconds=configuration.source_timeout_seconds,
            cancel_requested=cancel_requested,
        )
        workers = BoundedExecutor(
            worker_count=configuration.document_workers, queue_capacity=2, thread_name_prefix="company-document"
        )
        workers.start()
        resources.callback(workers.stop, wait=True, cancel_futures=True)
        dependencies = CompanyEvidenceDependencies(
            SQLiteCompanyEvidenceIndex(
                paths.company_collection,
                facts_path=paths.company,
                documents=company_document_files.CompanyDocumentFiles(
                    paths.company_documents, max_text_characters=configuration.document_limits.max_text_characters
                ),
                cancel_requested=cancel_requested,
                event_policy=request.event_policy,
                listings=request.listings,
                stock_progress=partial(company_evidence_progress.ReportCompanyStock, heartbeat, request.stock_names),
            ),
            documents[0],
            structured,
            time.monotonic,
            heartbeat,
            cancel_requested,
            request.stock_names,
            document_sources=documents,
            document_executor=workers,
        )
        return SyncCompanyEvidence(dependencies, request)


def _prepare_reference(
    reference_root: Path,
    observed_at: datetime,
    progress: Callable[[company_evidence.EvidenceSyncProgress], None] | None,
    *,
    cancel_requested: Callable[[], bool],
) -> PreparedReference:
    if cancel_requested():
        raise RuntimeError("reference_preparation_cancelled")
    universe = LoadCurrentAShareUniverse(
        partial(FetchSseListings, get=FetchExternalHttp), partial(FetchSzseListings, get=FetchExternalHttp), 15.0
    )
    workers = BoundedExecutor(worker_count=8, queue_capacity=0, thread_name_prefix="risk-st-check")
    workers.start()
    resources = ExitStack()
    try:
        sessions = TencentSessionPool(tuple(resources.enter_context(OpenExternalHttp()) for _ in range(8)))
        publication = SQLiteReferenceIndex(reference_root / "company_st.sqlite3")
        source = HistoryStNameSource(
            publication,
            sessions.borrow,
            workers,
            lambda: cancel_requested() or not workers.is_running(),
            report=partial(company_evidence_progress.ReportStPreparation, progress),
        )
        calendar = partial(
            FetchAkshareCalendar, 30.0, execute=partial(RunIsolatedCall, cancel_requested=cancel_requested)
        )
        industry = CurrentIndustryPublication(
            reference_root / "industry.sqlite3", BuildIndustrySource(cancel_requested=cancel_requested)
        )
        dependencies = ReferencePreparationDependencies(
            source.fetch,
            partial(publication.calendar, observed_at.date(), fetch=calendar, progress=progress),
            industry.fetch,
            publication.publish,
            cancel_requested,
        )
        return PrepareReference(universe, observed_at, dependencies)
    finally:
        stopped = workers.stop(wait=True, cancel_futures=True, deadline=ShutdownDeadline.start(30.0))
        resources.close()
        if not stopped.completed:
            raise RuntimeError("history ST worker shutdown exceeded 30 seconds")


def ExecuteDownloadTrain(
    config_path: Path, *, scoring_profiles: tuple[ScoringProfileId, ...] = SCORING_PROFILE_IDS
) -> int:
    """One history maintenance, one executor sized to the selected profiles."""
    from trader.download.entrypoints.commands import RunDownload
    from trader.training.entrypoints.commands import ConfigureTrainingResources

    scoring_profiles = ParseProfileSelection(",".join(scoring_profiles))
    runtime = LoadRuntimeSettings(config_path)
    ConfigureTrainingResources()
    result = RunDownload(runtime.project_root, config_path, quiet_success=True, allow_price_gaps=True)
    return result or ExecuteSelectedTraining(runtime, scoring_profiles)


def ExecuteSelectedTraining(
    runtime: RuntimeSettings,
    scoring_profiles: tuple[ScoringProfileId, ...],
    *,
    history_root: Path | None = None,
    training_root: Path | None = None,
) -> int:
    """Compose offline training resources, also used by isolated diagnostics."""
    from trader.training.application.selected_training import TrainingLocations
    from trader.training.entrypoints.selected_training import RunSelectedTraining
    from trader.training.infra.memory_admission import FittingMemoryCapacity, TrainingMemoryBudget

    if history_root is not None or training_root is not None:
        ConfigureStorage(runtime, additional_paths=tuple(path for path in (history_root, training_root) if path))
    paths = TrainingLocations(
        history_root or runtime.data_paths.history,
        training_root / "models" if training_root else runtime.data_paths.models,
        training_root / "samples" if training_root else runtime.data_paths.samples,
    )
    return RunSelectedTraining(
        runtime,
        scoring_profiles,
        executor=BoundedExecutor(
            worker_count=len(scoring_profiles), queue_capacity=0, thread_name_prefix="profile-training"
        ),
        memory_admission=TrainingMemoryBudget(FittingMemoryCapacity()),
        paths=paths,
    )


def ExecuteTrainingReminderStatus(settings: RuntimeSettings) -> TrainingReminderStatus:
    return ReadTrainingReminderStatus(settings.data_paths.history, ShanghaiClock(_utc_now).now())


def ExecuteHistoryAutomationRemoval() -> tuple[Path, ...]:
    import os
    import sys

    from trader.download.infra import history_automation_uninstall as automation

    platform: automation.HistoryAutomationPlatform = (
        "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"
    )
    plan = automation.PlanHistoryAutomationRemoval(platform, Path.home(), os.getuid() if os.name != "nt" else 0)
    return automation.RemoveHistoryAutomation(plan, run=automation.RunHistoryRemovalCommand)


def ExecuteHistoryDownload(
    configuration: HistorySyncConfiguration,
    *,
    reference: PreparedReference | None = None,
    progress: HistorySyncProgressPort | None = None,
    clock: Callable[[], datetime] | None = None,
    cancel_requested: Callable[[], bool] = lambda: False,
    storage_overrides: tuple[StorageOverride, ...] = (),
) -> HistoryMaintenanceStatus:
    storage_kind = StorageProbe(storage_overrides).inspect(configuration.history_root).kind
    if reference is None:
        reference = _prepare_reference(
            configuration.reference_root,
            ShanghaiClock(clock or _utc_now).now(),
            None,
            cancel_requested=cancel_requested,
        )
    history_pool = BoundedExecutor(
        worker_count=configuration.history_workers, queue_capacity=0, thread_name_prefix="history-download"
    )
    history_pool.start()
    history_writer = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="history-writer")
    http_resources = ExitStack()
    try:
        history_writer.start()
        with BaoStockHistorySupplier(configuration, progress=progress, cancel_requested=cancel_requested) as baseline:
            http_pool = TencentSessionPool(
                tuple(
                    http_resources.enter_context(OpenExternalHttp()) for _index in range(configuration.history_workers)
                )
            )
            prices = TencentQfqSupplier(
                TencentQfqDependencies(
                    http_pool.borrow, lambda _as_of: reference.eligible_universe, cancel_requested, reference.calendar
                )
            )
            return DownloadHistoryUseCase(HistorySyncGateway(history_pool, history_writer, storage_kind)).execute(
                configuration,
                HistorySupplierRouter(
                    baseline,
                    prices,
                    reference,
                    industry_cache=HistoryIndustryQueryCache(configuration.reference_root / "industry.sqlite3"),
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
    paths: DataPaths,
    *,
    reference: PreparedReference | None = None,
    cancel_requested: Callable[[], bool] = lambda: False,
    report: Callable[[ProgressMessage], None] | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> QfqUpdateResult:
    """Compose explicit short-window maintenance without building the server."""
    if report is None:
        report = TerminalDownloadProgress()
    if reference is None:
        reference = _prepare_reference(
            paths.reference, ShanghaiClock(now).now(), None, cancel_requested=cancel_requested
        )
    history_root = paths.history
    v2 = SQLiteQfqWindowCache(paths.daily_window, "v2")
    v3 = SQLiteQfqWindowCache(paths.daily_window, "v3")
    history = ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(history_root))
    started = time.monotonic()
    qfq_pool = BoundedExecutor(worker_count=8, queue_capacity=0, thread_name_prefix="qfq-download")
    qfq_pool.start()
    qfq_writer = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="qfq-writer")
    with ExitStack() as http_resources:
        try:
            qfq_writer.start()
            http_pool = TencentSessionPool(
                tuple(http_resources.enter_context(OpenExternalHttp()) for _index in range(8))
            )
            tencent = TencentQfqSupplier(
                TencentQfqDependencies(
                    http_pool.borrow, lambda _as_of: reference.eligible_universe, cancel_requested, reference.calendar
                )
            )
            updater = UpdateQfqWindows(
                v2,
                v3,
                tencent,
                cancel_requested,
                report,
                workers=8,
                worker_pool=qfq_pool,
                writer_pool=qfq_writer,
            )
            return QfqUpdateRunner(
                history, updater, v2, v3, paths.daily_window.with_suffix(".lock"), ShanghaiClock(now).now
            ).execute()
        finally:
            deadline = ShutdownDeadline.start(30.0)
            writer_stopped = qfq_writer.stop(wait=True, cancel_futures=True, deadline=deadline)
            stopped = qfq_pool.stop(wait=True, cancel_futures=True, deadline=deadline)
            if not stopped.completed or not writer_stopped.completed:
                report(
                    QfqMessage(
                        time.monotonic() - started, "关闭失败", "worker_deadline_exceeded", level=ProgressLevel.ERROR
                    )
                )


def ExecuteResearchEvidence(command: ResearchEvidenceCommand) -> ResearchEvidenceResult:
    """Lazily assemble one offline research use case; never build the server runtime."""
    from trader.training.entrypoints import research_evidence
    from trader.training.infra.research.command_evidence import (
        CapabilityEvidencePublisher,
        HistoricalIndustryEvidenceReader,
        TerminalConclusionPublisher,
        TerminalHoldoutParentsReader,
    )

    if isinstance(command, research_evidence.SelectionCommand):
        from trader.training.application.selection_execution import ExecuteSelectionResearch
        from trader.training.infra.research.selection_evidence import SelectionEvidenceReader

        return ExecuteSelectionResearch(SelectionEvidenceReader(command.evidence_dir))
    if isinstance(command, research_evidence.IndustryAuditCommand):
        from trader.training.application.historical_industry_audit import AuditHistoricalIndustry

        return AuditHistoricalIndustry(HistoricalIndustryEvidenceReader(command.history_root)).execute(
            required_sample_codes=command.required_sample_codes, tushare_access_points=command.tushare_access_points
        )
    if isinstance(command, research_evidence.HoldoutCommand):
        from trader.training.application.terminal_holdout_execution import ExecuteTerminalHoldout

        return ExecuteTerminalHoldout(
            TerminalHoldoutParentsReader(command.parent_artifact_dir), TerminalConclusionPublisher(command.output_dir)
        ).execute()

    from trader.download.infra.history_source_capability import FreeHistorySourceCapability
    from trader.training.infra.research.capability_http import BoundedCapabilitySession
    from trader.training.infra.research.h1_point_in_time_capability import FreeSourceH1CapabilityProbe

    with OpenExternalHttp() as session:
        transport = BoundedCapabilitySession(session, timeout_seconds=command.timeout_seconds, monotonic=time.monotonic)
        probe = FreeSourceH1CapabilityProbe(
            FreeHistorySourceCapability(transport), timeout_seconds=command.timeout_seconds
        )
        if isinstance(command, research_evidence.CapabilityCommand):
            from trader.training.application.capability_completion import CompleteCapabilityEvidence
            from trader.training.infra.research.h1_point_in_time_snapshot import SQLiteH1PointInTimeSnapshot

            return CompleteCapabilityEvidence(
                probe,
                SQLiteH1PointInTimeSnapshot(command.runtime_dir),
                CapabilityEvidencePublisher(command.artifact_dir),
            ).execute(code=command.code, historical_anchor_date=command.anchor_date)
        if isinstance(command, research_evidence.QualificationCommand):
            from trader.download.infra.history_snapshot_inspection import InspectHistorySnapshot
            from trader.training.application.point_in_time_data_qualification import QualifyPointInTimeData

            return QualifyPointInTimeData(
                partial(InspectHistorySnapshot, command.history_root, verify_partitions=True),
                HistoricalIndustryEvidenceReader(command.history_root),
                probe,
            ).execute(code=command.code, historical_anchor_date=command.anchor_date)
    raise TypeError("unsupported research evidence command")


def BuildSystem(  # noqa: PLR0915 - the sole composition root explicitly wires runtime resource owners
    config_path: str | Path,
    *,
    scoring_profile: ScoringProfileId | None = None,
    runtime_scope: ScoringProfileId | None = None,
    owns_shared_runtime: bool = True,
    shared_inputs: composition_inputs.SharedRecommendationInputs | None = None,
) -> ApplicationSystem[SQLiteFreezeTraceIndex, SQLiteOutcomeEvidenceIndex, UnifiedWebServices, Flask]:
    settings = LoadRuntimeSettings(config_path)
    budget_path = settings.runtime_dir / "deepseek/state.json"
    if runtime_scope is not None:
        settings = replace(settings, runtime_dir=settings.runtime_dir / "profiles" / runtime_scope)
    if owns_shared_runtime:
        PrepareRuntimeStorage(settings)
    strategy = LoadStrategySettings(settings.strategy_config_path, scoring_profile=scoring_profile)
    paths = settings.data_paths
    watchlist = LoadLongWatchlist(settings.long_watchlist_path)
    cost_policy = LoadExecutionCostPolicy(Path(config_path).expanduser().resolve().parent)
    cost_version = ContentHash(cost_policy)[:16]
    effective_config_version = f"{settings.config_version}+{strategy.strategy_version}+cost_{cost_version}"
    now = _utc_now
    latency = LatencyWaterfall()
    cadence_policy = CadencePolicy.from_seconds(settings.pipeline.cadence_seconds)
    cadence_planner = CadencePlanner(cadence_policy, started_at=ShanghaiClock(now).now())
    workers = _build_worker_context(settings, latency, shared_inputs.workers if shared_inputs else None)
    context = composition_inputs.RuntimeBuildContext(
        settings,
        strategy,
        watchlist,
        effective_config_version,
        now,
        latency,
        cadence_policy,
        workers,
        owns_shared_runtime,
    )
    reference_publication = SQLiteReferenceIndex(paths.reference_database)
    references = (
        shared_inputs.market.references
        if shared_inputs
        else PublishedReferences(
            reference_publication.read,
            reference_publication.identity,
            CurrentIndustryCache(paths.reference / "industry.sqlite3").load,
            max_age_seconds=_fixed_cache_ttl(settings, "security_master_calendar"),
            reserve_bytes=partial(
                workers.market_cache.reserve_resident, "security_master_calendar", "startup_reference"
            ),
        )
    )
    calendar = ChinaTradingCalendar(references.reference, now=now)
    run_scope = (
        shared_inputs.run_scope
        if shared_inputs
        else RecommendationRunScope(
            True, references.initialize, calendar.open_dates, ShanghaiClock(now).now, lambda: uuid4().hex
        )
    )
    persistence = _build_persistence(context, shared_inputs.budget if shared_inputs else None, budget_path=budget_path)
    profile_id = strategy.scoring_profile
    model_failure = "production_model_missing"
    try:
        loaded_profile = LoadScoringProfile(profile_id, training_root=paths.models)
    except ScoringModelsUnavailableError as exc:
        loaded_profile = None
        model_failure = f"production_model_{exc.reason}"
    WarnMissingLocalData(paths, profile_id, model_failure=model_failure if loaded_profile is None else None)
    training_settings = LoadRuntimeTrainingSettings(settings.project_root, profile_id)
    market_data = _build_market_data(
        context,
        calendar,
        references,
        training_settings=training_settings,
        shared_market=shared_inputs.market if shared_inputs else None,
    )
    review_cache = (
        shared_inputs.review_cache
        if shared_inputs
        else ReviewCache(
            maximum_entries=2000,
            ttl_seconds=600,
            shared_cache=workers.market_cache,
            config_version=BuildSharedReviewCacheIdentity(settings.deepseek),
            seen_capacity=6000,
        )
    )
    review_client = DeepSeekHttpClient(post=_post_external_http)
    reviewer = DeepSeekReviewer.from_context(context, persistence.budget, review_client, review_cache)
    policy = policy_settings._recommendation_policy(context.strategy)
    model_publications = PublishedTrainingState(
        paths.history,
        paths.models,
        loaded_profile.publications if loaded_profile else (),
        owned_profile=profile_id,
        wall_clock=ShanghaiClock(now).now,
    )
    model_scoring = profile_factory.BuildProfileScoring(
        profile_id,
        loaded_profile,
        history_sessions=training_settings.history_sessions,
        model_failure=model_failure,
        cost_policy=cost_policy,
        maintenance=model_publications.status if profile_id != "v1" else None,
    )
    candidate_filtering = CandidateFilteringService(policy, model_scoring)
    local_scoring = LocalScoringService(model_scoring, RankingSelectionService(), RiskControlService())
    publication_io = PublicationIoTracker(now=ShanghaiClock(now).now, monotonic=time.monotonic)
    next_long_sequence = partial(run_scope.next_sequence, persistence.sequence.allocate)
    publication = _build_publication(
        context,
        composition_inputs.RuntimePublicationDependencies(
            next_long_sequence, persistence.records, market_data, persistence.outcomes
        ),
        publication_io=publication_io,
        production_authority=bool((profile_status := model_scoring.status()) and profile_status.production_authority),
    )
    native_data = source_router.MarketInputCoordinator(
        market_data,
        dependencies=source_router.InputRefreshDependencies(
            publication.long_runtime,
            policy,
            ShanghaiClock(now).now,
            model_scoring,
            candidate_filtering,
            monotonic=time.monotonic,
        ),
    )
    freeze_builder = local_freeze_builder.LocalFreezeBuilder(
        native_data,
        config_version=effective_config_version,
        dependencies=local_freeze_builder.FreezeBuildDependencies(
            policy,
            publication.freeze_drafts,
            ShanghaiClock(now).now,
            model_scoring,
            local_scoring,
            TryBuildCommittedResearchAudit,
            monotonic=time.monotonic,
            next_sequence=lambda: run_scope.next_sequence(
                lambda: persistence.sequence.allocate(width=2, minimum=publication.tomorrow_index.next_scored_sequence)
            ),
        ),
    )
    deepseek = DeepSeekAdapter(reviewer, policy, freeze_builder, ScoreFusionService(), now=ShanghaiClock(now).now)

    def publish_overlay_event(overlay: FreezeOverlay) -> object:
        current = publication.tomorrow_index.snapshot(overlay.strategy).current
        if not isinstance(current, ScoredFreeze) or current.version != overlay.parent_version:
            raise ValueError("overlay event parent freeze is unavailable")
        return publication.freeze_events.publish_overlay(overlay, parent_version=current.version)

    scheduler = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=context.workers.control_pool,
            clock=ShanghaiClock(context.now),
            calendar=calendar,
            cadence=cadence_planner,
            data=native_data,
            freezes=freeze_builder,
            reviews=deepseek,
            index=publication.tomorrow_index,
            observer=publication.observer,
            sealing=FreezeAdapter(publication.tomorrow_freezer, publication.d25_freezer),
            settlement=OutcomeSettlementAdapter(
                market_data,
                OutcomeSettlementService(
                    market_data, persistence.outcomes, persistence.outcomes, session_distance=calendar.session_distance
                ),
            ),
            research_factory=ResearchRuntime,
            publish_freeze=publication.freeze_events.publish_committed,
            publish_overlay=publish_overlay_event,
            latency=latency,
            publication_io=publication_io,
            run_scope=run_scope,
            long_quotes=publication.long_runtime,
        ),
        config_version=effective_config_version,
        shutdown_timeout_seconds=settings.pipeline.shutdown_timeout_seconds,
    )

    def initialize_published_inputs(cancelled: Callable[[], bool]) -> None:
        market_data.initialize_published_inputs(
            cancelled,
            model_version=ContentHash(loaded_profile.publications) if loaded_profile else model_failure,
            config_version=effective_config_version,
        )
        if not cancelled() and profile_id != "v1":
            model_publications.initialize()

    input_preloader = (
        shared_inputs.input_preloader
        if shared_inputs
        else PublishedInputPreloader(market_data.history, initialize=initialize_published_inputs)
    )
    if shared_inputs:
        input_preloader.register(market_data.history, initialize_published_inputs)
    supervisor = runtime_supervisor.RuntimeSupervisor(
        scheduler,
        runtime_supervisor.RuntimeSupervisorConfig(
            now=now,
            initializers=(
                run_scope.initialize,
                *(
                    partial(run_scope.run_live, initialize)
                    for initialize in (
                        persistence.sequence.initialize,
                        publication.tomorrow_records.initialize,
                        lambda: _initialize_if_available(publication.research_trace),
                        lambda: _initialize_if_available(persistence.outcomes),
                        lambda: InitializeMarketReferences(market_data),
                        lambda: publication.tomorrow_freezer.restore(now().date()),
                        lambda: publication.d25_freezer.restore(now().date()),
                    )
                ),
                *((lambda: persistence.budget.initialize(now()),) if shared_inputs is None else ()),
            ),
            interval_seconds=runtime_supervisor.SchedulerIntervalSeconds,
            shutdown_timeout_seconds=settings.pipeline.shutdown_timeout_seconds,
        ),
    )
    web_services = UnifiedWebServices(
        publication.freeze_queries,
        publication.freeze_events,
        lambda: _runtime_status(scheduler, reviewer, market_data.health, model_scoring.status()),
        WebApiConfig(settings.pipeline.publish_heartbeat_seconds, settings.api.web_snapshot_retention_seconds),
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
        minute_pool=workers.minute_pool,
        source_lanes=workers.source_lanes,
        data_pool=workers.data_pool,
        quote_pool=workers.quote_pool,
        market_quote_pool=workers.market_quote_pool,
        long_runtime=publication.long_runtime,
        freeze_queries=publication.freeze_queries,
        freeze_events=publication.freeze_events,
        tomorrow_index=publication.tomorrow_index,
        tomorrow_records=publication.tomorrow_records,
        research_trace=publication.research_trace,
        outcome_evidence=persistence.outcomes,
        input_preloader=input_preloader,
        web_services=web_services,
        workers=workers,
        market_data=market_data,
        budget=persistence.budget,
        review_cache=review_cache,
        run_scope=run_scope,
        owns_shared_resources=shared_inputs is None,
    )


def BuildProfiledSystem(
    config_path: str | Path, *, profiles: tuple[ScoringProfileId, ...] = SCORING_PROFILE_IDS
) -> ProfiledApplicationSystem[Flask]:
    """Build the selected profiles with one shared resource owner and read-only surface."""
    profiles = ParseProfileSelection(",".join(profiles))
    settings = LoadRuntimeSettings(config_path)
    default_profile = "v2" if "v2" in profiles else profiles[0]
    primary = BuildSystem(config_path, scoring_profile=default_profile, runtime_scope=default_profile)
    shared = composition_inputs.SharedRecommendationInputs(
        primary.workers,
        primary.market_data,
        primary.budget,
        primary.review_cache,
        primary.input_preloader,
        primary.run_scope,
    )
    build_secondary = partial(BuildSystem, config_path, owns_shared_runtime=False, shared_inputs=shared)
    systems = [primary]
    for profile in profiles:
        if profile != default_profile:
            systems.append(build_secondary(scoring_profile=profile, runtime_scope=profile))
    web_profiles = {system.profile_id: system.web_services for system in systems}
    app = CreateApp(
        services=ProfiledWebServices(
            profiles={profile: web_profiles[profile] for profile in profiles},
            default_profile=default_profile,
            long_profile=default_profile,
            config=primary.web_services.config,
        )
    )
    return ProfiledApplicationSystem(settings, app, tuple(systems))


def _build_worker_context(
    settings: RuntimeSettings, latency: LatencyWaterfall, shared: RuntimeWorkerResources | None = None
) -> RuntimeWorkerResources:
    urgent_worker_count = 1 if settings.pipeline.market_workers > 1 else 0
    control_pool = BoundedExecutor(
        worker_count=2, urgent_worker_count=1, queue_capacity=4, thread_name_prefix="trader-control"
    )
    persistence_pool = BoundedExecutor(
        worker_count=1,
        queue_capacity=max(1, settings.pipeline.event_queue_size),
        thread_name_prefix="trader-persistence",
    )
    json_writer = RuntimeJsonWriter(persistence_pool)
    if shared is not None:
        return replace(shared, control_pool=control_pool, persistence_pool=persistence_pool, json_writer=json_writer)
    data_pool = BoundedExecutor(
        worker_count=settings.pipeline.market_workers + urgent_worker_count,
        urgent_worker_count=urgent_worker_count,
        queue_capacity=5,
        thread_name_prefix="source-data",
    )
    source_lanes = SourceLaneScheduler(data_pool, latency=latency)
    quote_pool = BoundedExecutor(worker_count=4, queue_capacity=4, thread_name_prefix="candidate-quotes")
    market_quote_pool = BoundedExecutor(worker_count=3, queue_capacity=3, thread_name_prefix="market-quotes")
    minute_pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="intraday-minutes")
    market_cache: BoundedLruCache[object] = BoundedLruCache(
        settings.market_data.cache_policy, cadence_seconds=settings.pipeline.cadence_seconds, wall_clock=_utc_now
    )
    return RuntimeWorkerResources(
        control_pool=control_pool,
        data_pool=data_pool,
        quote_pool=quote_pool,
        market_quote_pool=market_quote_pool,
        minute_pool=minute_pool,
        persistence_pool=persistence_pool,
        source_lanes=source_lanes,
        json_writer=json_writer,
        market_cache=market_cache,
    )


def _build_market_data(
    context: composition_inputs.RuntimeBuildContext,
    calendar: ChinaTradingCalendar,
    references: PublishedReferences,
    *,
    training_settings: ProfileTrainingSettings | V1TrainingSettings,
    shared_market: market_feature_service.MarketFeatureService | None = None,
) -> market_feature_service.MarketFeatureService:
    settings, strategy = context.settings, context.strategy
    paths = settings.data_paths
    workers = context.workers
    now = context.now
    data_pool = workers.data_pool
    source_lanes = workers.source_lanes
    market_cache = workers.market_cache
    feature_builder = FeatureBuilder.from_settings(strategy, training_settings.momentum_horizons, market_cache)
    published_history = ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(paths.history))
    projection_owner = (
        shared_market.history.projection_owner
        if shared_market
        else SharedHistoryProjection(
            published_history,
            monotonic=time.monotonic,
            reserve_bytes=partial(market_cache.reserve_resident, "security_master_calendar", "qfq_shared_bars"),
        )
    )
    history_cache = PublishedHistoryCache(
        published_history,
        lookback_sessions=training_settings.history_sessions,
        monotonic=time.monotonic,
        outcome_history=published_history,
        open_dates=calendar.open_dates,
        reserve_bytes=partial(
            market_cache.reserve_resident, "security_master_calendar", f"qfq_{strategy.scoring_profile}"
        ),
        require_input_binding=True,
        learned_rank_inputs=strategy.scoring_profile == "v1",
        sequence_inputs=strategy.scoring_profile == "v5",
        projection_owner=projection_owner,
    )
    if shared_market is None:
        intraday_client = EastmoneyClient(
            session_factory=OpenExternalHttp,
            timeout_seconds=settings.market_data.candidate_timeout_seconds,
            worker_pool=workers.minute_pool,
            cancel_requested=lambda: source_lanes.is_stopped("eastmoney_intraday"),
            wall_clock=now,
            monotonic=time.monotonic,
        )
        sina = SinaClient(
            worker_pool=workers.quote_pool,
            session_factory=OpenExternalHttp,
            timeout_seconds=settings.market_data.sina_timeout_seconds,
            cancel_requested=lambda: source_lanes.is_stopped("sina"),
            wall_clock=now,
        )
        tencent = TencentClient(
            session_factory=OpenExternalHttp,
            timeout_seconds=settings.market_data.candidate_timeout_seconds,
            cancel_requested=lambda: source_lanes.is_stopped("tencent"),
            wall_clock=now,
            worker_pool=workers.quote_pool,
        )

        def official_quote_codes() -> tuple[str, ...]:
            return SelectQuotePopulation(
                references.static_reference().reference,
                eligibility.exclusions(now()),
                references.historical_st_eligibility(),
            )

        tencent_market_client = TencentClient(
            session_factory=OpenExternalHttp,
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
                    deadline=deadline, cancel_event=cancel_event
                ),
                "sina": lambda deadline, cancel_event: sina.fetch_market(deadline=deadline, cancel_event=cancel_event),
            },
            recovery_probes={"tencent_market": tencent_market.probe_market, "sina": sina.probe_market},
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
        runner = MarketTaskRunner(
            worker_pool=data_pool,
            source_lanes=source_lanes,
            cache=market_cache,
            source_contracts=settings.market_data.source_contracts,
            config_version=settings.config_version,
            schema_version="market_snapshot",
            wall_clock=now,
        )
        eligibility_index = CompanyEligibility(
            PublishCompanyExclusions(
                SQLiteCompanyFactsIndex(paths.company, event_policy=strategy.company_scoring.events)
            ),
            read_only=not context.owns_shared_runtime,
        )
        if context.owns_shared_runtime:
            with suppress(RuntimeError):
                eligibility_index.record_manual_blacklist(
                    strategy.hard_filters.blacklist_codes, now(), context.effective_config_version
                )
        eligibility: IssuerEligibilityPort = eligibility_index
        research = PublishedCompanyResearch(
            company_facts_reader.SQLiteCompanyEvidenceReader(
                paths.company, raw_path=paths.company_collection, event_policy=strategy.company_scoring.events
            ),
            wall_clock=now,
            open_dates=calendar.open_dates,
            monotonic=time.monotonic,
            reserve_bytes=partial(market_cache.reserve_resident, "security_master_calendar", "company"),
            source_versions=references.versions,
            config_version=settings.config_version,
            company_policy=strategy.company_scoring,
        )
        intraday_loader = IntradayLoader(
            intraday_client,
            runner,
            workers=5,
            minute_pool=workers.minute_pool,
            ttl_seconds=_fixed_cache_ttl(settings, "intraday_minutes"),
            batch_timeout_seconds=settings.market_data.candidate_timeout_seconds,
            capacity=settings.market_data.cache_policy.datasets["intraday_minutes"].capacity,
            monotonic=time.monotonic,
        )
    else:
        gateway = shared_market.quotes.gateway
        runner = shared_market.runner
        eligibility = shared_market.eligibility
        research = shared_market.research
        intraday_loader = shared_market.intraday
    quote_cache = QuoteCache(
        QuoteCacheDependencies(gateway, feature_builder, history_cache, references),
        market_ttl_seconds=min(context.cadence_policy.intervals[PipelineTask.FULL_MARKET].values()),
        candidate_capacity=settings.market_data.cache_policy.datasets["candidate_quotes"].capacity,
        monotonic=time.monotonic,
    )
    market_health = MarketDataHealth(
        MarketDataHealthDependencies(quote_cache, history_cache, research, intraday_loader, references, eligibility),
        wall_clock=now,
    )
    return market_feature_service.MarketFeatureService(
        market_feature_service.MarketFeatureDependencies(
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
                cache=StaticMarketCache(
                    reserve=lambda snapshots: market_cache.reserve_resident(
                        "security_master_calendar", "static_eligibility", len(CanonicalJsonBytes(snapshots))
                    )
                )
                if shared_market is None
                else shared_market.static_cache,
                monotonic=time.monotonic,
                historical_st_eligibility=references.historical_st_eligibility,
                blacklist_codes=frozenset(strategy.hard_filters.blacklist_codes),
            ),
        )
    )


def _build_persistence(
    context: composition_inputs.RuntimeBuildContext,
    shared_budget: DeepSeekBudgetLedger | None = None,
    *,
    budget_path: Path,
) -> composition_inputs.RuntimePersistenceContext:
    settings = context.settings
    profile_root = settings.data_paths.recommendation(context.strategy.scoring_profile)
    records = SQLiteFreezeRecords(profile_root / "freeze.sqlite3")
    outcomes = SQLiteOutcomeEvidenceIndex(settings.data_paths.research(context.strategy.scoring_profile), records)
    budget = shared_budget or DeepSeekBudgetLedger(
        BudgetStateFile(budget_path, ProcessLock(settings.data_paths.root / ".deepseek.lock", label="DeepSeek")),
        daily_hard_limit=settings.deepseek.daily_hard_limit,
        strategy_limits=settings.deepseek.strategy_limits,
        stage_targets=settings.deepseek.stage_targets,
        stage_limits=settings.deepseek.stage_limits,
        health_policy=DeepSeekHealthPolicy.from_settings(settings.deepseek.adaptive),
        enabled=settings.deepseek.enabled,
    )
    sequence = PublicationSequence(profile_root / "publication_state.sqlite3")
    return composition_inputs.RuntimePersistenceContext(sequence, records, budget, outcomes)


def _build_publication(
    context: composition_inputs.RuntimeBuildContext,
    dependencies: composition_inputs.RuntimePublicationDependencies,
    *,
    publication_io: PublicationIoTracker,
    production_authority: bool,
) -> composition_inputs.RuntimePublicationContext:
    settings, records, market_data = context.settings, dependencies.records, dependencies.market_data
    tomorrow_freezes = UnifiedFreezeIndex()
    freeze_drafts = UnifiedFreezeDraftIndex()
    freeze_events = UnifiedFreezeEventStream(
        history_size=settings.api.sse_history_size,
        client_queue_size=settings.api.sse_client_queue_size,
        subscriber_limit=settings.api.sse_max_clients,
    )
    clock = ShanghaiClock(context.now)
    query_sources = read_only_queries.FreezeQuerySources(
        market_data, dependencies.outcomes, market_data.references, market_data.research
    )
    freeze_queries = read_only_queries.UnifiedFreezeQueries(
        tomorrow_freezes, freeze_drafts, records, clock, query_sources
    )
    research_trace = SQLiteFreezeTraceIndex(
        settings.data_paths.research(context.strategy.scoring_profile),
        limits=ResearchTraceLimits(events_per_trade_date=max(2048, settings.pipeline.event_queue_size * 4)),
    )
    observer = AsyncFreezeObserver[FreezeObservation](
        (research_trace.record, *dependencies.additional_observers),
        capacity=max(1, min(16, settings.pipeline.event_queue_size)),
        thread_name="trader-freeze-observer",
        prepare=FreezeObservation.materialize,
    )
    freezer = partial(
        freeze_coordinator.ScoredFreezeCoordinator,
        tomorrow_freezes,
        records,
        clock,
        runtime_identity=freeze_coordinator.FreezeRuntimeIdentity(
            context.effective_config_version, context.strategy.strategy_version, context.strategy.fusion.version
        ),
        publication_io=publication_io,
        production_authority=production_authority,
    )
    return composition_inputs.RuntimePublicationContext(
        records,
        tomorrow_freezes,
        freeze_drafts,
        research_trace,
        LongRuntime(
            LongRuntimeDependencies(
                market_data,
                tomorrow_freezes,
                context.now,
                freeze_events.publish_projection,
                context.owns_shared_runtime,
                next_sequence=dependencies.next_sequence,
            ),
            config_version=context.effective_config_version,
            watchlist_version=context.watchlist.watchlist_version,
            items=policy_settings._long_item_definitions(context.watchlist),
            groups=policy_settings._long_group_definitions(context.watchlist),
        ),
        freeze_queries,
        freeze_events,
        freezer(strategy=Strategy.TOMORROW),
        freezer(strategy=Strategy.D25),
        observer,
    )
