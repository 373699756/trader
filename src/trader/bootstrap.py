"""Unique composition root for the v2 application."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from flask import Flask

from trader.application.decisions.decision_core import UnifiedDecisionIndex
from trader.application.decisions.decision_drafts import UnifiedDecisionDraftIndex
from trader.application.decisions.decision_observers import AsyncDecisionObserver, DecisionEventConsumer
from trader.application.decisions.decision_queries import UnifiedDecisionQueries
from trader.application.decisions.decision_stream import UnifiedDecisionEventStream
from trader.application.decisions.v2_decision_adapters import V2DeepSeekAdapter, V2FreezeAdapter
from trader.application.long_v2_runtime import LongV2Runtime, LongV2RuntimeDependencies
from trader.application.market_data.v2_input_runtime import V2DecisionBuildDependencies, V2MarketDataAdapter
from trader.application.outcomes.outcome_settlement import OutcomeSettlementService, V2OutcomeSettlementAdapter
from trader.application.ports.tomorrow_model import TomorrowScoringProfile
from trader.application.recommendation.scored_v2_freezing import (
    ScoredV2FreezeCoordinator,
    V2DecisionRuntimeIdentity,
)
from trader.application.recommendation.today_v2_freezing import TodayV2FreezeCoordinator
from trader.application.recommendation.tomorrow_model_scoring import TomorrowProductionModelScoringService
from trader.application.research.v2_research_runtime import V2ResearchRuntime
from trader.application.runtime.cadence import CadencePlanner, CadencePolicy, PipelineTask
from trader.application.runtime.latency import LatencyWaterfall
from trader.application.runtime.resource_orchestration import (
    ApplicationResources,
    start_application_resources,
    stop_application_resources,
)
from trader.application.runtime.runtime import RuntimeSupervisor, RuntimeSupervisorConfig, scheduler_interval_seconds
from trader.application.runtime.shutdown import ShutdownDeadline, ShutdownReport
from trader.application.runtime.source_lanes import SourceLaneRegistry
from trader.application.runtime.v2_runtime import V2RuntimeDependencies, V2SchedulerRuntime
from trader.application.runtime.workers import BoundedExecutor
from trader.bootstrap_clock import utc_now as _utc_now
from trader.bootstrap_data_plane import _initialize_reference_data_plane
from trader.bootstrap_policy import _long_group_definitions, _long_item_definitions, _recommendation_policy
from trader.bootstrap_status import runtime_status as _runtime_status
from trader.domain.recommendation.decision_identity import DecisionOverlay, ScoredDecision
from trader.domain.recommendation.models import Strategy
from trader.infra.cache import BoundedLruCache
from trader.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.infra.deepseek.cache import ReviewCache
from trader.infra.deepseek.factory import create_deepseek_client
from trader.infra.deepseek.health_gate import DeepSeekHealthPolicy
from trader.infra.deepseek.reviewer import DeepSeekReviewer
from trader.infra.market_data.history.history_seed import (
    FallbackHistoryClient,
)
from trader.infra.market_data.history.service_history import HistoryCache
from trader.infra.market_data.history.service_history_warmup import HistoryWarmup, build_history_warmup_policy
from trader.infra.market_data.normalization.features import FeatureBuilder
from trader.infra.market_data.providers.akshare import AkshareResearchClient
from trader.infra.market_data.providers.eastmoney import EastmoneyClient
from trader.infra.market_data.providers.exchange_security_master import ExchangeSecurityMasterClient
from trader.infra.market_data.providers.sina import SinaClient
from trader.infra.market_data.providers.tencent import TencentClient
from trader.infra.market_data.providers.tushare import TushareClient
from trader.infra.market_data.references.calendar import ChinaTradingCalendar
from trader.infra.market_data.service.facade import MarketFeatureDependencies, MarketFeatureService
from trader.infra.market_data.service.gateway import MarketDataGateway
from trader.infra.market_data.service.service_candidates import QuoteCache, QuoteCacheDependencies
from trader.infra.market_data.service.service_execution import MarketTaskRunner
from trader.infra.market_data.service.service_health import MarketDataHealth, MarketDataHealthDependencies
from trader.infra.market_data.service.service_intraday import IntradayLoader
from trader.infra.market_data.service.service_research import ResearchLoader
from trader.infra.market_data.service.service_tushare import ReferenceLoader
from trader.infra.persistence.data_plane import DataPlaneRepository
from trader.infra.persistence.decision_records import SQLiteDecisionRecordRepository
from trader.infra.persistence.issuer_eligibility import SQLiteIssuerEligibilityRegistry
from trader.infra.persistence.outcomes import SQLiteOutcomeEvidenceRepository
from trader.infra.persistence.research_trace import ResearchTraceLimits, SQLiteV2ResearchTraceStore
from trader.infra.persistence.runtime_json import RuntimeJsonWriter
from trader.infra.runtime_support import RuntimeWorkerResources, ShanghaiClock
from trader.infra.settings import (
    LongWatchlist,
    RuntimeSettings,
    StrategySettings,
    load_long_watchlist,
    load_runtime_settings,
    load_strategy_settings,
)
from trader.infra.tomorrow_production_model import load_packaged_tomorrow_production_model
from trader.web import create_app
from trader.web.api.route_services import UnifiedWebServices, WebApiConfig

if TYPE_CHECKING:
    from trader.application.research.historical_backtest import HistoricalBarBacktestService
    from trader.application.research.historical_screening import HistoricalDownloadService
    from trader.application.research.score_r6 import ScoreR6HistoricalScreeningService
    from trader.application.research.score_r6_daily import ScoreR6DailyScreeningService
    from trader.application.research.score_r6_stability import ScoreR6StabilityScreeningService
    from trader.application.research.tomorrow_historical_p2_screening import TomorrowHistoricalP2ScreeningService
    from trader.application.research.tomorrow_historical_validation import HistoricalRiskValidationService
    from trader.application.research.tomorrow_profile_holdout import TomorrowProfileHoldoutService
    from trader.infra.research.history_archive import SQLiteHistoricalArchive


@dataclass(frozen=True)
class ApplicationSystem:
    settings: RuntimeSettings
    strategy: StrategySettings
    watchlist: LongWatchlist
    app: Flask
    supervisor: RuntimeSupervisor
    scheduler: V2SchedulerRuntime
    repository: SQLiteDecisionRecordRepository
    market_cache: BoundedLruCache[object]
    history_pool: BoundedExecutor
    research_pool: BoundedExecutor
    source_lanes: SourceLaneRegistry
    data_pool: BoundedExecutor
    long_v2_runtime: LongV2Runtime
    decision_queries: UnifiedDecisionQueries
    decision_events: UnifiedDecisionEventStream
    tomorrow_index: UnifiedDecisionIndex
    tomorrow_records: SQLiteDecisionRecordRepository
    research_trace: SQLiteV2ResearchTraceStore
    outcome_evidence: SQLiteOutcomeEvidenceRepository

    def _application_resources(self) -> ApplicationResources:
        return ApplicationResources(
            self.supervisor,
            self.source_lanes,
            self.data_pool,
            self.history_pool,
            self.research_pool,
            (self.long_v2_runtime,),
            self.market_cache,
        )

    def start(self) -> bool:
        return start_application_resources(
            self._application_resources(),
            timeout_seconds=self.settings.pipeline.shutdown_timeout_seconds,
        )

    def stop(self, *, deadline: ShutdownDeadline | None = None) -> ShutdownReport:
        shared_deadline = deadline or ShutdownDeadline.start(self.settings.pipeline.shutdown_timeout_seconds)
        return stop_application_resources(
            self._application_resources(),
            deadline=shared_deadline,
        )


@dataclass(frozen=True)
class HistoricalResearchServices:
    download: HistoricalDownloadService
    backtest: HistoricalBarBacktestService
    score_r6: ScoreR6HistoricalScreeningService
    score_r6_daily: ScoreR6DailyScreeningService
    score_r6_stability: ScoreR6StabilityScreeningService
    tomorrow_historical_p2: TomorrowHistoricalP2ScreeningService
    tomorrow_profile_holdout: TomorrowProfileHoldoutService
    tomorrow_historical_risk: HistoricalRiskValidationService
    archive: SQLiteHistoricalArchive


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


@dataclass(frozen=True)
class _PersistenceContext:
    repository: SQLiteDecisionRecordRepository
    data_plane: DataPlaneRepository
    budget: DeepSeekBudgetLedger
    outcomes: SQLiteOutcomeEvidenceRepository


@dataclass(frozen=True)
class _PublicationContext:
    tomorrow_repository: SQLiteDecisionRecordRepository
    tomorrow_index: UnifiedDecisionIndex
    decision_drafts: UnifiedDecisionDraftIndex
    research_trace: SQLiteV2ResearchTraceStore
    long_runtime: LongV2Runtime
    decision_queries: UnifiedDecisionQueries
    decision_events: UnifiedDecisionEventStream
    today_freezer: TodayV2FreezeCoordinator
    tomorrow_freezer: ScoredV2FreezeCoordinator
    d25_freezer: ScoredV2FreezeCoordinator
    observer: AsyncDecisionObserver


@dataclass(frozen=True)
class _PublicationDependencies:
    repository: SQLiteDecisionRecordRepository
    market_data: MarketFeatureService
    additional_observers: tuple[DecisionEventConsumer, ...] = ()


@dataclass(frozen=True)
class _V2Adapters:
    market_data: MarketFeatureService
    calendar: ChinaTradingCalendar
    reviewer: DeepSeekReviewer


def build_system(
    config_path: str | Path,
    *,
    tomorrow_scoring_profile: TomorrowScoringProfile | None = None,
) -> ApplicationSystem:
    settings = load_runtime_settings(config_path)
    strategy = load_strategy_settings(
        settings.strategy_config_path,
        tomorrow_scoring_profile=tomorrow_scoring_profile,
    )
    watchlist = load_long_watchlist(settings.long_watchlist_path)
    effective_config_version = f"{settings.config_version}+{strategy.strategy_version}"
    now = _utc_now
    latency = LatencyWaterfall()
    cadence_policy = CadencePolicy.from_seconds(settings.pipeline.cadence_seconds)
    cadence_planner = CadencePlanner(cadence_policy, started_at=ShanghaiClock(now).now())
    workers = _build_worker_context(settings, latency)
    context = _BuildContext(
        settings, strategy, watchlist, effective_config_version, now, latency, cadence_policy, workers
    )
    calendar = ChinaTradingCalendar(settings.runtime_dir / "calendar.json")
    persistence = _build_persistence(context)
    market_data = _build_market_data(context, persistence.data_plane, calendar)
    reviewer = _build_reviewer(context, persistence.budget)
    policy = _recommendation_policy(context.strategy)
    v1_model = TomorrowProductionModelScoringService(load_packaged_tomorrow_production_model("v1"))
    v2_model = TomorrowProductionModelScoringService(load_packaged_tomorrow_production_model("v2"))
    tomorrow_model = v1_model if strategy.tomorrow_scoring_profile == "v1" else v2_model
    publication = _build_publication(
        context,
        calendar,
        _PublicationDependencies(
            persistence.repository,
            market_data,
        ),
    )
    native_data = V2MarketDataAdapter(
        market_data,
        config_version=effective_config_version,
        candidate_pool_size=settings.market_data.candidate_pool_size,
        decision_build=V2DecisionBuildDependencies(
            publication.long_runtime,
            policy,
            publication.decision_drafts,
            tomorrow_model,
        ),
    )
    deepseek = V2DeepSeekAdapter(reviewer, policy, native_data)

    def publish_overlay_event(overlay: DecisionOverlay) -> object:
        current = publication.tomorrow_index.snapshot(overlay.strategy).current
        if not isinstance(current, ScoredDecision) or current.version != overlay.parent_version:
            raise ValueError("overlay event parent decision is unavailable")
        return publication.decision_events.publish_overlay(
            overlay,
            parent_content_hash=current.content_hash,
        )

    scheduler = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=ShanghaiClock(context.now),
            calendar=calendar,
            cadence=cadence_planner,
            data=native_data,
            decisions=native_data,
            reviews=deepseek,
            index=publication.tomorrow_index,
            observer=publication.observer,
            freezes=V2FreezeAdapter(
                publication.today_freezer,
                publication.tomorrow_freezer,
                publication.d25_freezer,
            ),
            settlement=V2OutcomeSettlementAdapter(
                market_data,
                OutcomeSettlementService(
                    market_data,
                    persistence.outcomes,
                    persistence.outcomes,
                    session_distance=calendar.session_distance,
                ),
            ),
            research_factory=lambda on_result: V2ResearchRuntime(
                market_data,
                cadence=context.cadence_policy,
                now=context.now,
                on_result=on_result,
            ),
            publish_decision=publication.decision_events.publish_committed,
            publish_overlay=publish_overlay_event,
            latency=latency,
        ),
        config_version=effective_config_version,
        shutdown_timeout_seconds=settings.pipeline.shutdown_timeout_seconds,
    )
    supervisor = RuntimeSupervisor(
        scheduler,
        RuntimeSupervisorConfig(
            now=now,
            initializers=(
                publication.tomorrow_repository.initialize,
                lambda: _initialize_research_trace(publication.research_trace),
                lambda: _initialize_outcome_evidence(persistence.outcomes),
                lambda: _initialize_reference_data_plane(market_data, persistence.data_plane, now()),
                persistence.budget.initialize,
                publication.today_freezer.initialize,
                lambda: publication.tomorrow_freezer.restore(now().date()),
                lambda: publication.d25_freezer.restore(now().date()),
                lambda: persistence.budget.recover_incomplete(now()),
            ),
            interval_seconds=scheduler_interval_seconds,
            shutdown_timeout_seconds=settings.pipeline.shutdown_timeout_seconds,
            record_error=lambda _error: None,
        ),
    )
    app = create_app(
        services=UnifiedWebServices(
            publication.decision_queries,
            publication.decision_events,
            lambda: _runtime_status(
                scheduler,
                reviewer,
                market_data.health,
                tomorrow_model.status(),
            ),
            WebApiConfig(
                heartbeat_seconds=settings.pipeline.publish_heartbeat_seconds,
                snapshot_retention_seconds=settings.api.web_snapshot_retention_seconds,
            ),
        )
    )
    return ApplicationSystem(
        settings=settings,
        strategy=strategy,
        watchlist=watchlist,
        app=app,
        supervisor=supervisor,
        scheduler=scheduler,
        repository=persistence.repository,
        market_cache=workers.market_cache,
        history_pool=workers.history_pool,
        research_pool=workers.research_pool,
        source_lanes=workers.source_lanes,
        data_pool=workers.data_pool,
        long_v2_runtime=publication.long_runtime,
        decision_queries=publication.decision_queries,
        decision_events=publication.decision_events,
        tomorrow_index=publication.tomorrow_index,
        tomorrow_records=publication.tomorrow_repository,
        research_trace=publication.research_trace,
        outcome_evidence=persistence.outcomes,
    )


def build_historical_research_services(
    config_path: str | Path,
    *,
    workers: int = 5,
) -> HistoricalResearchServices:
    """Compose explicit offline research without starting production resources."""

    from trader.application.research.historical_backtest import HistoricalBarBacktestService
    from trader.application.research.historical_screening import HistoricalDownloadService
    from trader.application.research.score_r6 import ScoreR6HistoricalScreeningService
    from trader.application.research.score_r6_daily import ScoreR6DailyScreeningService
    from trader.application.research.score_r6_stability import ScoreR6StabilityScreeningService
    from trader.application.research.tomorrow_historical_p2_screening import TomorrowHistoricalP2ScreeningService
    from trader.application.research.tomorrow_historical_validation import HistoricalRiskValidationService
    from trader.application.research.tomorrow_profile_holdout import TomorrowProfileHoldoutService
    from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
    from trader.infra.research.history_archive import SQLiteHistoricalArchive
    from trader.infra.research.history_sources import HistoricalPriceProviderAdapter, SinaHistoricalUniverseProvider
    from trader.infra.research.score_r6_daily_artifacts import ScoreR6DailyArtifactStore
    from trader.infra.research.tomorrow_historical_p2_model import TomorrowHistoricalP2EnsembleTrainer

    settings = load_runtime_settings(config_path)
    fixed_source_time = datetime(
        SCORE_H0_V1_SPEC.source_cutoff.year,
        SCORE_H0_V1_SPEC.source_cutoff.month,
        SCORE_H0_V1_SPEC.source_cutoff.day,
        15,
        0,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    ).astimezone(timezone.utc)
    history = FallbackHistoryClient(
        TencentClient(
            timeout_seconds=settings.market_data.history_timeout_seconds,
            wall_clock=lambda: fixed_source_time,
        ),
        EastmoneyClient(
            timeout_seconds=settings.market_data.history_timeout_seconds,
            workers=workers,
            wall_clock=lambda: fixed_source_time,
        ),
    )
    archive = SQLiteHistoricalArchive(settings.runtime_dir)
    download = HistoricalDownloadService(
        SinaHistoricalUniverseProvider(
            SinaClient(
                timeout_seconds=settings.market_data.sina_timeout_seconds,
                workers=workers,
                wall_clock=_utc_now,
            )
        ),
        HistoricalPriceProviderAdapter(history),
        archive,
        workers=workers,
    )
    return HistoricalResearchServices(
        download,
        HistoricalBarBacktestService(archive),
        ScoreR6HistoricalScreeningService(archive),
        ScoreR6DailyScreeningService(archive),
        ScoreR6StabilityScreeningService(
            archive,
            ScoreR6DailyArtifactStore(settings.runtime_dir / "score-r6-daily"),
        ),
        TomorrowHistoricalP2ScreeningService(archive, TomorrowHistoricalP2EnsembleTrainer()),
        TomorrowProfileHoldoutService(
            archive,
            load_packaged_tomorrow_production_model("v1"),
            load_packaged_tomorrow_production_model("v2"),
        ),
        HistoricalRiskValidationService(
            archive,
            load_packaged_tomorrow_production_model("v2"),
        ),
        archive,
    )


def _build_worker_context(settings: RuntimeSettings, latency: LatencyWaterfall) -> RuntimeWorkerResources:
    urgent_worker_count = 1 if settings.pipeline.market_workers > 1 else 0
    data_pool = BoundedExecutor(
        worker_count=settings.pipeline.market_workers + urgent_worker_count,
        urgent_worker_count=urgent_worker_count,
        queue_capacity=5,
        thread_name_prefix="source-data",
    )
    source_lanes = SourceLaneRegistry(data_pool, latency=latency)
    history_pool = BoundedExecutor(
        worker_count=settings.pipeline.market_workers,
        queue_capacity=settings.market_data.candidate_pool_size,
        thread_name_prefix="history-data",
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
        data_pool,
        history_pool,
        research_pool,
        persistence_pool,
        source_lanes,
        json_writer,
        market_cache,
    )


def _build_market_data(
    context: _BuildContext,
    data_plane: DataPlaneRepository,
    calendar: ChinaTradingCalendar,
) -> MarketFeatureService:
    settings = context.settings
    strategy = context.strategy
    workers = context.workers
    now = context.now
    data_pool = workers.data_pool
    source_lanes = workers.source_lanes
    market_cache = workers.market_cache
    history_warmup_policy = build_history_warmup_policy(
        worker_count=settings.pipeline.market_workers,
        source_timeout_seconds=settings.market_data.history_timeout_seconds,
        maximum_batch_size=30,
        maximum_batch_timeout_seconds=20.0,
    )
    eastmoney = EastmoneyClient(
        timeout_seconds=settings.market_data.eastmoney_timeout_seconds,
        workers=settings.pipeline.market_workers,
        worker_pool=data_pool,
        cancel_requested=lambda: source_lanes.is_stopped("eastmoney"),
        wall_clock=now,
    )
    remote_history = EastmoneyClient(
        timeout_seconds=history_warmup_policy.source_attempt_timeout_seconds,
        workers=settings.pipeline.market_workers,
        worker_pool=data_pool,
        cancel_requested=lambda: source_lanes.is_stopped("history"),
        wall_clock=now,
    )
    history_client = FallbackHistoryClient(
        TencentClient(
            timeout_seconds=history_warmup_policy.source_attempt_timeout_seconds,
            cancel_requested=lambda: source_lanes.is_stopped("history"),
            wall_clock=now,
        ),
        remote_history,
    )
    intraday_client = EastmoneyClient(
        timeout_seconds=settings.market_data.candidate_timeout_seconds,
        workers=settings.pipeline.market_workers,
        worker_pool=data_pool,
        cancel_requested=lambda: source_lanes.is_stopped("eastmoney"),
        wall_clock=now,
    )
    gateway = MarketDataGateway(
        eastmoney,
        SinaClient(
            timeout_seconds=settings.market_data.sina_timeout_seconds,
            cancel_requested=lambda: source_lanes.is_stopped("sina"),
            wall_clock=now,
        ),
        TencentClient(
            timeout_seconds=settings.market_data.candidate_timeout_seconds,
            cancel_requested=lambda: source_lanes.is_stopped("tencent"),
            wall_clock=now,
            worker_pool=data_pool,
        ),
        minimum_market_rows=settings.market_data.minimum_market_rows,
        circuit_breaker_failures=settings.market_data.circuit_breaker_failures,
        circuit_breaker_seconds=settings.market_data.circuit_breaker_seconds,
        full_market_hedge_delay_seconds=settings.market_data.full_market_hedge_delay_seconds,
        worker_pool=data_pool,
        source_lanes=source_lanes,
        cache=market_cache,
        source_contract_versions=settings.market_data.source_contract_versions,
        config_version=settings.config_version,
        schema_version="market_snapshot_v15",
        wall_clock=now,
        latency=context.latency,
        listing_open_dates=calendar.open_dates,
    )
    evidence_cache_dir = settings.runtime_dir / "evidence_cache"
    feature_builder = FeatureBuilder(
        strategy.today_news_signal,
        strategy.tomorrow_tail_signal,
        strategy.market_regime,
        strategy.long_research,
    )
    research_client = AkshareResearchClient(
        timeout_seconds=settings.market_data.research_timeout_seconds,
        long_research_policy=strategy.long_research,
        evidence_cache_dir=evidence_cache_dir,
        json_writer=workers.json_writer,
        cancel_requested=lambda: not workers.research_pool.is_running(),
    )
    tushare_client = TushareClient(
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
        source_contract_versions=settings.market_data.source_contract_versions,
        config_version=settings.config_version,
        schema_version="market_snapshot_v15",
        wall_clock=now,
    )
    research_runner = MarketTaskRunner(
        worker_pool=workers.research_pool,
        source_lanes=None,
        cache=market_cache,
        source_contract_versions=settings.market_data.source_contract_versions,
        config_version=settings.config_version,
        schema_version="market_snapshot_v15",
        wall_clock=now,
    )
    history_cache = HistoryCache(
        history_client,
        runner,
        history_worker_pool=workers.history_pool,
        workers=settings.pipeline.market_workers,
        ttl_seconds=_fixed_cache_ttl(settings, "daily_history"),
        capacity=settings.market_data.cache_policy.datasets["daily_history"].capacity,
        history_data_plane=data_plane,
        monotonic=time.monotonic,
    )
    references = ReferenceLoader(
        gateway,
        history_cache,
        runner,
        tushare_client,
        security_master_client=ExchangeSecurityMasterClient(
            timeout_seconds=max(15.0, settings.market_data.eastmoney_timeout_seconds),
            wall_clock=now,
        ),
        security_master_refresh_ttl_seconds=_fixed_cache_ttl(settings, "security_master_calendar"),
        security_master_retry_seconds=(
            settings.market_data.cache_policy.datasets["security_master_calendar"].negative_ttl_seconds
        ),
        data_plane=data_plane,
        monotonic=time.monotonic,
    )
    gateway.set_security_reference_persistence_sink(references.schedule_security_master_persistence)
    eligibility = SQLiteIssuerEligibilityRegistry(settings.runtime_dir / "issuer-eligibility.sqlite3")
    try:
        eligibility.record_manual_blacklist(
            strategy.hard_filters.blacklist_codes,
            now(),
            context.effective_config_version,
        )
    except RuntimeError:
        # The typed registry retains any facts it could verify; persistence degradation stays observable.
        pass
    warmup = HistoryWarmup(
        history_cache,
        references,
        runner,
        eligibility_filter=eligibility.filter_codes,
        batch_size=history_warmup_policy.batch_size,
        batch_timeout_seconds=history_warmup_policy.batch_timeout_seconds,
        monotonic=time.monotonic,
    )
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
        candidate_capacity=settings.market_data.cache_policy.datasets["intraday_minutes"].capacity,
        monotonic=time.monotonic,
    )
    market_health = MarketDataHealth(
        MarketDataHealthDependencies(
            quote_cache,
            history_cache,
            warmup,
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
            warmup,
            research,
            intraday_loader,
            references,
            runner,
            market_health,
            eligibility,
        ),
        history_preload_limit=settings.market_data.candidate_pool_size * 3,
    )
    return market_data


def _build_persistence(context: _BuildContext) -> _PersistenceContext:
    settings = context.settings
    runtime_database_lock = threading.Lock()
    repository = SQLiteDecisionRecordRepository(settings.runtime_dir)
    data_plane = DataPlaneRepository(settings.runtime_dir)
    outcomes = SQLiteOutcomeEvidenceRepository(settings.runtime_dir, repository, data_plane)
    budget = DeepSeekBudgetLedger(
        settings.runtime_dir / "deepseek-budget.sqlite3",
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
    return _PersistenceContext(repository, data_plane, budget, outcomes)


def _build_reviewer(context: _BuildContext, budget: DeepSeekBudgetLedger) -> DeepSeekReviewer:
    settings = context.settings
    strategy = context.strategy
    return DeepSeekReviewer(
        settings.deepseek,
        budget,
        create_deepseek_client(),
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
) -> _PublicationContext:
    settings = context.settings
    repository = dependencies.repository
    market_data = dependencies.market_data
    tomorrow_decisions = UnifiedDecisionIndex()
    decision_drafts = UnifiedDecisionDraftIndex()
    decision_events = UnifiedDecisionEventStream(
        history_size=settings.api.sse_history_size,
        client_queue_size=settings.api.sse_client_queue_size,
        subscriber_limit=settings.api.sse_max_clients,
    )
    clock = ShanghaiClock(context.now)
    decision_queries = UnifiedDecisionQueries(tomorrow_decisions, decision_drafts, repository, clock)
    research_trace = SQLiteV2ResearchTraceStore(
        settings.runtime_dir,
        limits=ResearchTraceLimits(events_per_trade_date=max(2048, settings.pipeline.event_queue_size * 4)),
    )

    observer = AsyncDecisionObserver(
        (research_trace.record, *dependencies.additional_observers),
        capacity=max(1, min(16, settings.pipeline.event_queue_size)),
        thread_name="trader-v2-decision-observer",
    )
    tomorrow_freezer = ScoredV2FreezeCoordinator(
        tomorrow_decisions,
        repository,
        clock,
        runtime_identity=V2DecisionRuntimeIdentity(
            context.effective_config_version,
            context.strategy.strategy_version,
            context.strategy.fusion.version,
        ),
        strategy=Strategy.TOMORROW,
    )
    d25_freezer = ScoredV2FreezeCoordinator(
        tomorrow_decisions,
        repository,
        clock,
        runtime_identity=V2DecisionRuntimeIdentity(
            context.effective_config_version,
            context.strategy.strategy_version,
            context.strategy.fusion.version,
        ),
        strategy=Strategy.D25,
    )
    today_freezer = TodayV2FreezeCoordinator(
        tomorrow_decisions,
        repository,
        clock,
        runtime_identity=V2DecisionRuntimeIdentity(
            context.effective_config_version,
            context.strategy.strategy_version,
            context.strategy.fusion.version,
        ),
    )
    long_runtime = LongV2Runtime(
        LongV2RuntimeDependencies(
            market_data,
            tomorrow_decisions,
            context.now,
            decision_events.publish_projection,
        ),
        config_version=context.effective_config_version,
        watchlist_version=context.watchlist.watchlist_version,
        items=_long_item_definitions(context.watchlist),
        groups=_long_group_definitions(context.watchlist),
    )
    return _PublicationContext(
        repository,
        tomorrow_decisions,
        decision_drafts,
        research_trace,
        long_runtime,
        decision_queries,
        decision_events,
        today_freezer,
        tomorrow_freezer,
        d25_freezer,
        observer,
    )


def _initialize_research_trace(trace: SQLiteV2ResearchTraceStore) -> None:
    try:
        trace.initialize()
    except (OSError, sqlite3.Error):
        return


def _initialize_outcome_evidence(evidence: SQLiteOutcomeEvidenceRepository) -> None:
    try:
        evidence.initialize()
    except (OSError, sqlite3.Error):
        return


def _fixed_cache_ttl(settings: RuntimeSettings, dataset: str) -> float:
    value = settings.market_data.cache_policy.datasets[dataset].refresh_ttl_seconds
    if value is None:
        raise ValueError(f"cache dataset {dataset} does not define a fixed TTL")
    return value


__all__ = ["ApplicationSystem", "HistoricalResearchServices", "build_historical_research_services", "build_system"]
