"""Unique composition root for the v2 application."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from flask import Flask

from trader.application.board_scoring import BoardScoringCoordinator
from trader.application.board_scoring_cache import BoardScoringCache
from trader.application.cadence import CadencePolicy, PipelineTask
from trader.application.current_decisions import CurrentDecisionIndex
from trader.application.events import InMemoryEventLedger
from trader.application.latency import LatencyWaterfall
from trader.application.long_groups import LongGroupDefinition, LongGroupSectionDefinition, LongWatchItemDefinition
from trader.application.outcome_settlement import OutcomeSettlementService
from trader.application.pipeline import RecommendationPipeline
from trader.application.pipeline_dependencies import PipelineDependencies, PipelineOptions, PipelineResources
from trader.application.policy import RecommendationPolicy, SelectionPolicy
from trader.application.ports.market import MarketDataPorts
from trader.application.ports.snapshots import SnapshotPorts
from trader.application.published_snapshots import PublishedSnapshotIndex
from trader.application.publisher import SnapshotPublisher
from trader.application.queries import CloseFallbackReplay, RecommendationQueries
from trader.application.recommendations import RecommendationEngine
from trader.application.runtime import RuntimeSupervisor, RuntimeSupervisorConfig, scheduler_interval_seconds
from trader.application.shutdown import ShutdownDeadline, ShutdownReport
from trader.application.source_lanes import SourceLaneRegistry
from trader.application.status import RuntimeState
from trader.application.system_lifecycle import (
    SystemLifecycleResources,
    start_application_resources,
    stop_application_resources,
)
from trader.application.tomorrow_events import TomorrowDecisionEventStream
from trader.application.tomorrow_freezing import DecisionRuntimeIdentity, TomorrowFreezeCoordinator
from trader.application.tomorrow_shadow import TomorrowCutoverGate
from trader.application.tomorrow_shadow_runtime import (
    ShadowObservingSnapshotIndex,
    TomorrowShadowDependencies,
    TomorrowShadowRuntime,
    TomorrowShadowWorker,
)
from trader.application.tomorrow_views import (
    TomorrowDecisionQueries,
    TomorrowQuoteOverlayIndex,
    TomorrowRuntimeTelemetry,
)
from trader.application.trading_session import TradingSessionTracker
from trader.application.workers import BoundedExecutor
from trader.bootstrap_clock import utc_now as _utc_now
from trader.bootstrap_data_plane import _initialize_reference_data_plane, initialize_tomorrow_evidence
from trader.domain.market.models import Board
from trader.domain.recommendation.filters import HardFilterPolicy
from trader.domain.recommendation.fusion import FusionPolicy
from trader.domain.recommendation.models import Strategy
from trader.domain.review.models import RiskRule
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
from trader.infra.persistence.runtime_json import RuntimeJsonWriter
from trader.infra.persistence.tomorrow_decision_freezes import TomorrowDecisionFreezeRepository
from trader.infra.persistence.tomorrow_shadow_evidence import TomorrowShadowEvidenceRepository
from trader.infra.persistence.writer import SnapshotRepository
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
from trader.web.route_services import TomorrowWebServices
from trader.web.routes import WebApiConfig


@dataclass(frozen=True)
class ApplicationSystem:
    settings: RuntimeSettings
    strategy: StrategySettings
    watchlist: LongWatchlist
    app: Flask
    supervisor: RuntimeSupervisor
    pipeline: RecommendationPipeline
    repository: SnapshotRepository
    publisher: SnapshotPublisher
    published_snapshots: PublishedSnapshotIndex
    state: RuntimeState
    market_cache: BoundedLruCache[object]
    history_pool: BoundedExecutor
    research_pool: BoundedExecutor
    source_lanes: SourceLaneRegistry
    tomorrow_shadow_worker: TomorrowShadowWorker | None = None
    tomorrow_shadow_runtime: TomorrowShadowRuntime | None = None

    def _lifecycle_resources(self) -> SystemLifecycleResources:
        return SystemLifecycleResources(
            self.supervisor,
            self.source_lanes,
            self.history_pool,
            self.research_pool,
            self.tomorrow_shadow_worker,
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
    repository: SnapshotRepository
    data_plane: DataPlaneRepository
    budget: DeepSeekBudgetLedger


@dataclass(frozen=True)
class _PublicationContext:
    state: RuntimeState
    publisher: SnapshotPublisher
    published_snapshots: PublishedSnapshotIndex
    pipeline_snapshots: ShadowObservingSnapshotIndex
    recommendation_engine: RecommendationEngine
    tomorrow_repository: TomorrowDecisionFreezeRepository
    tomorrow_evidence: TomorrowShadowEvidenceRepository
    tomorrow_gate: TomorrowCutoverGate
    tomorrow_runtime: TomorrowShadowRuntime
    tomorrow_worker: TomorrowShadowWorker
    tomorrow_queries: TomorrowDecisionQueries
    tomorrow_events: TomorrowDecisionEventStream


@dataclass(frozen=True)
class _PipelineAdapters:
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
    market_data = _build_market_data(context, persistence.data_plane)
    reviewer = _build_reviewer(context, persistence.budget)
    publication = _build_publication(context, calendar, persistence.repository)
    trading_session = TradingSessionTracker(now())
    adapters = _PipelineAdapters(market_data, calendar, reviewer)
    pipeline = _build_pipeline(context, adapters, persistence, publication, trading_session)
    queries = RecommendationQueries(
        publication.published_snapshots,
        now=now,
        current_quote_reader=market_data,
        close_fallback_replay=CloseFallbackReplay(persistence.repository, publication.recommendation_engine),
        session_status=pipeline.session_status,
    )
    supervisor = RuntimeSupervisor(
        pipeline,
        RuntimeSupervisorConfig(
            now=now,
            initializers=(
                publication.tomorrow_repository.initialize,
                lambda: initialize_tomorrow_evidence(publication.tomorrow_evidence, publication.tomorrow_gate),
                lambda: _initialize_reference_data_plane(market_data, persistence.data_plane),
                pipeline.initialize,
                publication.published_snapshots.initialize,
                persistence.budget.initialize,
                lambda: persistence.budget.recover_incomplete(now()),
            ),
            interval_seconds=scheduler_interval_seconds,
            shutdown_timeout_seconds=settings.pipeline.shutdown_timeout_seconds,
            record_error=publication.state.record_error,
        ),
    )
    app = create_app(
        status_provider=pipeline.status,
        queries=queries,
        publisher=publication.publisher,
        tomorrow=TomorrowWebServices(
            publication.tomorrow_queries,
            publication.tomorrow_events,
            publication.tomorrow_runtime.status,
        ),
        api_config=WebApiConfig(
            default_top_n=settings.api.default_top_n,
            maximum_top_n=settings.api.maximum_top_n,
            heartbeat_seconds=settings.pipeline.publish_heartbeat_seconds,
        ),
    )
    return ApplicationSystem(
        settings,
        strategy,
        watchlist,
        app,
        supervisor,
        pipeline,
        persistence.repository,
        publication.publisher,
        publication.published_snapshots,
        publication.state,
        workers.market_cache,
        workers.history_pool,
        workers.research_pool,
        workers.source_lanes,
        publication.tomorrow_worker,
        publication.tomorrow_runtime,
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


def _build_market_data(context: _BuildContext, data_plane: DataPlaneRepository) -> MarketFeatureService:
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
    history_warmup_batch_timeout = settings.market_data.history_timeout_seconds * (
        4 * ((history_warmup_batch_size + settings.pipeline.market_workers - 1) // settings.pipeline.market_workers) + 1
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
    repository = SnapshotRepository(
        settings.runtime_dir,
        config_version=context.effective_config_version,
        write_lock=runtime_database_lock,
    )
    data_plane = DataPlaneRepository(settings.runtime_dir)
    budget = DeepSeekBudgetLedger(
        settings.runtime_dir / "runtime.sqlite3",
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
    return _PersistenceContext(repository, data_plane, budget)


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
    repository: SnapshotRepository,
) -> _PublicationContext:
    settings = context.settings
    state = RuntimeState()
    publisher = SnapshotPublisher(
        history_size=settings.api.sse_history_size,
        client_queue_size=settings.api.sse_client_queue_size,
        maximum_subscribers=settings.api.sse_max_clients,
    )
    published_snapshots = PublishedSnapshotIndex(repository)
    recommendation_engine = _build_recommendation_engine(context, calendar)
    tomorrow_repository = TomorrowDecisionFreezeRepository(settings.runtime_dir)
    tomorrow_evidence = TomorrowShadowEvidenceRepository(settings.runtime_dir)
    tomorrow_decisions = CurrentDecisionIndex()
    tomorrow_quotes = TomorrowQuoteOverlayIndex(tomorrow_decisions)
    tomorrow_events = TomorrowDecisionEventStream(
        history_size=settings.api.sse_history_size,
        client_queue_size=settings.api.sse_client_queue_size,
        subscriber_limit=settings.api.sse_max_clients,
    )
    clock = ShanghaiClock(context.now)
    tomorrow_runtime_holder: list[TomorrowShadowRuntime] = []
    tomorrow_queries = TomorrowDecisionQueries(
        tomorrow_decisions,
        tomorrow_repository,
        clock,
        quotes=tomorrow_quotes,
        telemetry=lambda: (
            tomorrow_runtime_holder[0].telemetry() if tomorrow_runtime_holder else TomorrowRuntimeTelemetry()
        ),
    )
    tomorrow_gate = TomorrowCutoverGate(evidence=tomorrow_evidence)
    tomorrow_freezer = TomorrowFreezeCoordinator(
        tomorrow_decisions,
        tomorrow_repository,
        clock,
        runtime_identity=DecisionRuntimeIdentity(
            context.effective_config_version,
            context.strategy.strategy_version,
            context.strategy.fusion.version,
        ),
    )
    tomorrow_runtime = TomorrowShadowRuntime(
        _recommendation_policy(context.strategy),
        TomorrowShadowDependencies(
            tomorrow_decisions,
            tomorrow_quotes,
            tomorrow_events,
            tomorrow_queries,
            tomorrow_freezer,
            tomorrow_gate,
            clock,
        ),
    )
    tomorrow_runtime_holder.append(tomorrow_runtime)
    tomorrow_worker = TomorrowShadowWorker(tomorrow_runtime)
    pipeline_snapshots = ShadowObservingSnapshotIndex(
        published_snapshots,
        tomorrow_worker,
        tomorrow_runtime,
    )
    return _PublicationContext(
        state,
        publisher,
        published_snapshots,
        pipeline_snapshots,
        recommendation_engine,
        tomorrow_repository,
        tomorrow_evidence,
        tomorrow_gate,
        tomorrow_runtime,
        tomorrow_worker,
        tomorrow_queries,
        tomorrow_events,
    )


def _initialize_tomorrow_evidence(publication: _PublicationContext) -> None:
    initialize_tomorrow_evidence(publication.tomorrow_evidence, publication.tomorrow_gate)


def _build_recommendation_engine(context: _BuildContext, calendar: ChinaTradingCalendar) -> RecommendationEngine:
    return RecommendationEngine(
        _recommendation_policy(context.strategy),
        board_scoring=BoardScoringCoordinator(
            BoardScoringCache(
                context.workers.market_cache,
                config_version=context.effective_config_version,
                session_distance=calendar.session_distance,
            )
        ),
    )


def _build_pipeline(
    context: _BuildContext,
    adapters: _PipelineAdapters,
    persistence: _PersistenceContext,
    publication: _PublicationContext,
    trading_session: TradingSessionTracker,
) -> RecommendationPipeline:
    settings = context.settings
    return RecommendationPipeline(
        PipelineDependencies(
            market=MarketDataPorts(
                full_market=adapters.market_data,
                candidates=adapters.market_data,
                quotes=adapters.market_data,
                research=adapters.market_data,
                references=adapters.market_data,
                metadata=adapters.market_data,
                outcomes=adapters.market_data,
            ),
            calendar=adapters.calendar,
            reviews=adapters.reviewer,
            snapshots=SnapshotPorts(reader=persistence.repository, writer=persistence.repository),
            events=InMemoryEventLedger(terminal_capacity=max(1024, settings.pipeline.event_queue_size * 4)),
            publisher=publication.publisher,
            engine=publication.recommendation_engine,
            state=publication.state,
            published_snapshots=publication.pipeline_snapshots,
            now=context.now,
            outcome_settlement=OutcomeSettlementService(
                adapters.market_data,
                persistence.repository,
                persistence.repository,
                session_distance=adapters.calendar.session_distance,
            ),
            latency=context.latency,
            tomorrow_native_inputs=publication.tomorrow_worker,
            trading_session=trading_session,
        ),
        PipelineOptions(
            config_version=context.effective_config_version,
            candidate_pool_size=settings.market_data.candidate_pool_size,
            event_queue_size=settings.pipeline.event_queue_size,
            priority_queue_size=settings.pipeline.priority_queue_size,
            market_workers=settings.pipeline.market_workers,
            normalization_workers=settings.pipeline.normalization_workers,
            strategy_workers=settings.pipeline.strategy_workers,
            deepseek_workers=settings.pipeline.deepseek_workers,
            decision_execution_mode=settings.pipeline.decision_execution_mode,
            market_data_manages_workers=True,
            cadence_policy=context.cadence_policy,
            long_codes=tuple(item.code for item in context.watchlist.items),
            long_items=tuple(
                LongWatchItemDefinition(item.code, item.name, item.industry) for item in context.watchlist.items
            ),
            long_target_prices={item.code: item.target_price for item in context.watchlist.items},
            long_groups=_long_groups(context.watchlist),
        ),
        PipelineResources(data_pool=context.workers.data_pool, persistence_pool=context.workers.persistence_pool),
    )


def _long_groups(watchlist: LongWatchlist) -> tuple[LongGroupDefinition, ...]:
    return tuple(
        LongGroupDefinition(
            name=group.name,
            category=group.category,
            codes=group.codes,
            source=group.source,
            source_section=group.source_section,
            sections=tuple(
                LongGroupSectionDefinition(section.source_section, section.codes) for section in group.sections
            ),
        )
        for group in watchlist.groups
    )


def _recommendation_policy(settings: StrategySettings) -> RecommendationPolicy:
    return RecommendationPolicy(
        strategy_version=settings.strategy_version,
        fusion_version=settings.fusion.version,
        fusion=FusionPolicy(
            local_weight=settings.fusion.local_weight,
            deepseek_weight=settings.fusion.deepseek_weight,
            confidence_coverage_min=settings.fusion.confidence_coverage_min,
            minimum_known_dimensions=settings.fusion.minimum_known_dimensions,
            local_risk_cap=settings.fusion.local_risk_cap,
            deepseek_risk_cap=settings.fusion.deepseek_risk_cap,
        ),
        selection=SelectionPolicy(
            default_top_k=settings.selection.default_top_k,
            maximum_top_k=settings.selection.maximum_top_k,
            maximum_per_industry=settings.selection.maximum_per_industry,
            observation_margin=settings.selection.observation_margin,
            thresholds=settings.selection.thresholds,
            maximum_board_fraction=settings.selection.maximum_board_fraction,
            competition_group_limits={
                Board(name): limit for name, limit in settings.selection.competition_group_limits.items()
            },
            candidate_min_score=settings.selection.candidate_min_score,
            minimum_board_reliability=settings.selection.minimum_board_reliability,
            review_candidate_limit=settings.selection.review_candidate_limit,
        ),
        candidate_weights=settings.candidate_weights,
        dimension_weights={Strategy(name): weights for name, weights in settings.dimension_weights.items()},
        local_strategy_weights={Strategy(name): weights for name, weights in settings.local_strategy_weights.items()},
        board_policy_version=settings.board_policy_version,
        board_candidate_weights={
            Strategy(strategy): {Board(board): weights for board, weights in boards.items()}
            for strategy, boards in settings.board_candidate_weights.items()
        },
        board_local_strategy_weights={
            Strategy(strategy): {Board(board): weights for board, weights in boards.items()}
            for strategy, boards in settings.board_local_strategy_weights.items()
        },
        risk_rules={
            rule.risk_code: RiskRule(
                risk_code=rule.risk_code,
                severity=rule.severity,
                penalty=rule.penalty,
                minimum_confidence=rule.minimum_confidence,
                group=rule.group,
                evidence_ttl_hours=rule.evidence_ttl_hours,
                veto=rule.veto,
                allowed_evidence_types=rule.allowed_evidence_types,
                strategies=rule.strategies,
                trigger_factor=rule.trigger_factor,
                trigger_operator=rule.trigger_operator,
                trigger_thresholds=rule.trigger_thresholds,
                combination_mode=rule.combination_mode,
                risk_fact_id_fields=rule.risk_fact_id_fields,
                local_trigger_enabled=rule.local_trigger_enabled,
            )
            for rule in settings.risk_rules
        },
        hard_filter=HardFilterPolicy(
            blacklist_codes=frozenset(settings.hard_filters.blacklist_codes),
            structured_risk_thresholds=settings.hard_filters.structured_risk_thresholds,
        ),
    )


def _fixed_cache_ttl(settings: RuntimeSettings, dataset: str) -> float:
    value = settings.market_data.cache_policy.datasets[dataset].refresh_ttl_seconds
    if value is None:
        raise ValueError(f"cache dataset {dataset} does not define a fixed TTL")
    return value


__all__ = ["ApplicationSystem", "build_system"]
