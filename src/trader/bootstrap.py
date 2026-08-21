"""Unique composition root for the v2 application."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask

from trader.application.cadence import CadencePolicy, PipelineTask
from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_observers import AsyncDecisionObserver
from trader.application.decision_queries import UnifiedDecisionQueries
from trader.application.decision_stream import UnifiedDecisionEventStream
from trader.application.latency import LatencyWaterfall
from trader.application.long_v2_runtime import LongV2Runtime, LongV2RuntimeDependencies
from trader.application.outcome_settlement import OutcomeSettlementService, V2OutcomeSettlementAdapter
from trader.application.research.historical_backtest import HistoricalBarBacktestService
from trader.application.research.historical_screening import HistoricalDownloadService
from trader.application.research.score_r6 import ScoreR6HistoricalScreeningService
from trader.application.research_audit import V2DecisionObservation
from trader.application.runtime import RuntimeSupervisor, RuntimeSupervisorConfig, scheduler_interval_seconds
from trader.application.shutdown import ShutdownDeadline, ShutdownReport
from trader.application.source_lanes import SourceLaneRegistry
from trader.application.system_lifecycle import (
    SystemLifecycleResources,
    start_application_resources,
    stop_application_resources,
)
from trader.application.today_v2_freezing import TodayV2FreezeCoordinator
from trader.application.tomorrow_v2_freezing import (
    TomorrowV2FreezeCoordinator,
    V2DecisionRuntimeIdentity,
)
from trader.application.v2_input_runtime import (
    V2DeepSeekAdapter,
    V2FreezeAdapter,
    V2MarketDataAdapter,
)
from trader.application.v2_research_runtime import V2ResearchRuntime
from trader.application.v2_runtime import V2RuntimeDependencies, V2RuntimeIssue, V2SchedulerRuntime
from trader.application.workers import BoundedExecutor
from trader.bootstrap_clock import utc_now as _utc_now
from trader.bootstrap_data_plane import _initialize_reference_data_plane
from trader.bootstrap_policy import _long_group_definitions, _long_item_definitions, _recommendation_policy
from trader.domain.recommendation.models import Strategy
from trader.infra.cache import BoundedLruCache
from trader.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.infra.deepseek.cache import ReviewCache
from trader.infra.deepseek.factory import create_deepseek_client
from trader.infra.deepseek.health_gate import DeepSeekHealthPolicy
from trader.infra.deepseek.reviewer import DeepSeekReviewer
from trader.infra.market_data.akshare import AkshareResearchClient
from trader.infra.market_data.calendar import ChinaTradingCalendar
from trader.infra.market_data.eastmoney import EastmoneyClient
from trader.infra.market_data.features import FeatureBuilder
from trader.infra.market_data.gateway import MarketDataGateway
from trader.infra.market_data.history_seed import (
    FallbackHistoryClient,
)
from trader.infra.market_data.service import MarketFeatureDependencies, MarketFeatureService
from trader.infra.market_data.service_candidates import QuoteCache, QuoteCacheDependencies
from trader.infra.market_data.service_execution import MarketTaskRunner
from trader.infra.market_data.service_health import MarketDataHealth, MarketDataHealthDependencies
from trader.infra.market_data.service_history import HistoryCache
from trader.infra.market_data.service_history_warmup import HistoryWarmup
from trader.infra.market_data.service_intraday import IntradayLoader
from trader.infra.market_data.service_research import ResearchLoader
from trader.infra.market_data.service_tushare import ReferenceLoader
from trader.infra.market_data.sina import SinaClient
from trader.infra.market_data.tencent import TencentClient
from trader.infra.market_data.tushare import TushareClient
from trader.infra.persistence.data_plane import DataPlaneRepository
from trader.infra.persistence.decision_records import SQLiteDecisionRecordRepository
from trader.infra.persistence.outcomes import SQLiteOutcomeEvidenceRepository
from trader.infra.persistence.research_trace import ResearchTraceLimits, SQLiteV2ResearchTraceStore
from trader.infra.persistence.runtime_json import RuntimeJsonWriter
from trader.infra.research.history_archive import SQLiteHistoricalArchive
from trader.infra.research.history_sources import HistoricalPriceProviderAdapter, SinaHistoricalUniverseProvider
from trader.infra.runtime_support import RuntimeWorkerResources, ShanghaiClock
from trader.infra.settings import (
    LongWatchlist,
    RuntimeSettings,
    StrategySettings,
    load_long_watchlist,
    load_runtime_settings,
    load_strategy_settings,
)
from trader.web import create_app
from trader.web.route_services import UnifiedWebServices, WebApiConfig


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
    long_v2_runtime: LongV2Runtime | None = None
    decision_queries: UnifiedDecisionQueries | None = None
    decision_events: UnifiedDecisionEventStream | None = None
    tomorrow_index: UnifiedDecisionIndex | None = None
    tomorrow_records: SQLiteDecisionRecordRepository | None = None
    research_trace: SQLiteV2ResearchTraceStore | None = None
    outcome_evidence: SQLiteOutcomeEvidenceRepository | None = None

    def _lifecycle_resources(self) -> SystemLifecycleResources:
        return SystemLifecycleResources(
            self.supervisor,
            self.source_lanes,
            self.data_pool,
            self.history_pool,
            self.research_pool,
            tuple(runtime for runtime in (self.long_v2_runtime,) if runtime is not None),
            self.market_cache,
        )

    def start(self) -> bool:
        return start_application_resources(
            self._lifecycle_resources(),
            timeout_seconds=self.settings.pipeline.shutdown_timeout_seconds,
        )

    def stop(self, *, deadline: ShutdownDeadline | None = None) -> ShutdownReport:
        shared_deadline = deadline or ShutdownDeadline.start(self.settings.pipeline.shutdown_timeout_seconds)
        return stop_application_resources(
            self._lifecycle_resources(),
            deadline=shared_deadline,
        )


@dataclass(frozen=True)
class HistoricalResearchServices:
    download: HistoricalDownloadService
    backtest: HistoricalBarBacktestService
    score_r6: ScoreR6HistoricalScreeningService
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
    research_trace: SQLiteV2ResearchTraceStore
    long_runtime: LongV2Runtime
    decision_queries: UnifiedDecisionQueries
    decision_events: UnifiedDecisionEventStream
    today_freezer: TodayV2FreezeCoordinator
    tomorrow_freezer: TomorrowV2FreezeCoordinator
    d25_freezer: TomorrowV2FreezeCoordinator
    observer: AsyncDecisionObserver


@dataclass(frozen=True)
class _V2Adapters:
    market_data: MarketFeatureService
    calendar: ChinaTradingCalendar
    reviewer: DeepSeekReviewer


def build_system(config_path: str | Path) -> ApplicationSystem:
    settings = load_runtime_settings(config_path)
    strategy = load_strategy_settings(settings.strategy_config_path)
    watchlist = load_long_watchlist(settings.long_watchlist_path)
    effective_config_version = f"{settings.config_version}+{strategy.strategy_version}"
    now = _utc_now
    latency = LatencyWaterfall()
    cadence_policy = CadencePolicy.from_seconds(settings.pipeline.cadence_seconds)
    workers = _build_worker_context(settings, latency)
    context = _BuildContext(
        settings, strategy, watchlist, effective_config_version, now, latency, cadence_policy, workers
    )
    calendar = ChinaTradingCalendar(settings.runtime_dir / "calendar.json")
    persistence = _build_persistence(context)
    market_data = _build_market_data(context, persistence.data_plane, calendar)
    reviewer = _build_reviewer(context, persistence.budget)
    publication = _build_publication(context, calendar, persistence.repository, reviewer, market_data)
    policy = _recommendation_policy(context.strategy)
    native_data = V2MarketDataAdapter(
        market_data,
        config_version=effective_config_version,
        candidate_pool_size=settings.market_data.candidate_pool_size,
        long_runtime=publication.long_runtime,
        policy=policy,
    )
    deepseek = V2DeepSeekAdapter(reviewer, policy, native_data)
    scheduler = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=ShanghaiClock(context.now),
            calendar=calendar,
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
                lambda: _initialize_reference_data_plane(market_data, persistence.data_plane),
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
            lambda: _runtime_status(scheduler, reviewer, persistence.budget),
            WebApiConfig(heartbeat_seconds=settings.pipeline.publish_heartbeat_seconds),
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

    from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC

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
    eastmoney = EastmoneyClient(
        timeout_seconds=settings.market_data.eastmoney_timeout_seconds,
        workers=settings.pipeline.market_workers,
        worker_pool=data_pool,
        cancel_requested=lambda: source_lanes.is_stopped("eastmoney"),
        wall_clock=now,
    )
    remote_history = EastmoneyClient(
        timeout_seconds=settings.market_data.history_timeout_seconds,
        workers=settings.pipeline.market_workers,
        worker_pool=data_pool,
        cancel_requested=lambda: source_lanes.is_stopped("history"),
        wall_clock=now,
    )
    history_client = FallbackHistoryClient(
        TencentClient(
            timeout_seconds=settings.market_data.history_timeout_seconds,
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
        strategy.d25_signal,
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
        data_plane=data_plane,
        monotonic=time.monotonic,
    )
    history_warmup_batch_size = 30
    history_warmup_batch_timeout = min(
        20.0,
        settings.market_data.history_timeout_seconds
        * (
            4 * ((history_warmup_batch_size + settings.pipeline.market_workers - 1) // settings.pipeline.market_workers)
            + 1
        ),
    )
    warmup = HistoryWarmup(
        history_cache,
        references,
        runner,
        batch_size=history_warmup_batch_size,
        batch_timeout_seconds=history_warmup_batch_timeout,
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
    repository: SQLiteDecisionRecordRepository,
    reviewer: DeepSeekReviewer,
    market_data: MarketFeatureService,
) -> _PublicationContext:
    settings = context.settings
    tomorrow_decisions = UnifiedDecisionIndex()
    decision_events = UnifiedDecisionEventStream(
        history_size=settings.api.sse_history_size,
        client_queue_size=settings.api.sse_client_queue_size,
        subscriber_limit=settings.api.sse_max_clients,
    )
    clock = ShanghaiClock(context.now)
    decision_queries = UnifiedDecisionQueries(tomorrow_decisions, repository, clock)
    research_trace = SQLiteV2ResearchTraceStore(
        settings.runtime_dir,
        limits=ResearchTraceLimits(events_per_trade_date=max(2048, settings.pipeline.event_queue_size * 4)),
    )

    def publish_decision_event(observation: V2DecisionObservation) -> None:
        decision_events.publish_committed(observation.event)

    observer = AsyncDecisionObserver(
        (publish_decision_event, research_trace.record),
        capacity=max(1, min(16, settings.pipeline.event_queue_size)),
        thread_name="trader-v2-decision-observer",
    )
    tomorrow_freezer = TomorrowV2FreezeCoordinator(
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
    d25_freezer = TomorrowV2FreezeCoordinator(
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
        research_trace,
        long_runtime,
        decision_queries,
        decision_events,
        today_freezer,
        tomorrow_freezer,
        d25_freezer,
        observer,
    )


def _runtime_status(
    scheduler: V2SchedulerRuntime,
    reviewer: DeepSeekReviewer,
    budget: DeepSeekBudgetLedger,
) -> dict[str, object]:
    status = scheduler.status()
    strategy_errors = dict(status.strategy_error_codes)
    recent_errors = [_runtime_issue_payload(issue) for issue in status.recent_errors]
    active_issues = [issue for issue in status.recent_errors if issue.recovery_status == "active"]
    observer = asdict(status.observer)
    observer_error = status.observer.last_error_code
    degraded_reasons = [
        f"{issue.strategy.value}:{issue.code}" if issue.strategy is not None else issue.code for issue in active_issues
    ]
    if observer_error:
        degraded_reasons.append(f"observer:{observer_error}")
    issue_count = len(active_issues) + int(bool(observer_error))
    health_level = (
        "error"
        if not status.running or any(issue.severity == "error" for issue in active_issues)
        else "degraded"
        if issue_count
        else "normal"
    )
    return {
        "status": "running" if status.running else "stopped",
        "runtime_started": status.running,
        "runtime_version": status.config_version,
        "phase": status.phase.value,
        "deepseek_budget": budget.summary(_utc_now().date().isoformat()),
        "deepseek": reviewer.status(),
        "company_research": asdict(status.company_research),
        "degraded_reasons": degraded_reasons,
        "health": {"level": health_level, "issue_count": issue_count},
        "recent_errors": recent_errors,
        "last_error": status.last_error_code or (f"observer:{observer_error}" if observer_error else None),
        "observer": observer,
        "scheduler": {
            "config_version": status.config_version,
            "lanes": [asdict(lane) for lane in status.lanes],
            "strategy_errors": strategy_errors,
            "last_error_code": status.last_error_code,
            "refresh_failure_count": status.refresh_failure_count,
            "decision_failure_count": status.decision_failure_count,
            "review_failure_count": status.review_failure_count,
            "local_publish_count": status.local_publish_count,
            "hybrid_publish_count": status.hybrid_publish_count,
            "settlement_completed_count": status.settlement_completed_count,
            "settlement_failure_count": status.settlement_failure_count,
        },
    }


def _runtime_issue_payload(issue: V2RuntimeIssue) -> dict[str, object]:
    return {
        "code": issue.code,
        "severity": issue.severity,
        "strategy": issue.strategy.value if issue.strategy is not None else None,
        "stage": issue.stage,
        "occurred_at": issue.occurred_at.isoformat(),
        "last_occurred_at": issue.last_occurred_at.isoformat(),
        "count": issue.count,
        "recovery_status": issue.recovery_status,
        "resolved_at": issue.resolved_at.isoformat() if issue.resolved_at is not None else None,
    }


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
