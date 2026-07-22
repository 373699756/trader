"""Unique composition root for the v2 application."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask

from trader.application.board_scoring import BoardScoringCoordinator
from trader.application.board_scoring_cache import BoardScoringCache
from trader.application.cadence import CadencePolicy, PipelineTask
from trader.application.pipeline import RecommendationPipeline
from trader.application.policy import RecommendationPolicy, SelectionPolicy
from trader.application.publisher import SnapshotPublisher
from trader.application.queries import RecommendationQueries
from trader.application.recommendations import RecommendationEngine
from trader.application.runtime import RuntimeSupervisor, scheduler_interval_seconds
from trader.application.source_lanes import SourceLaneRegistry
from trader.application.status import RuntimeState
from trader.application.workers import BoundedExecutor
from trader.domain.filters import HardFilterPolicy
from trader.domain.fusion import FusionPolicy
from trader.domain.models import Board, RiskRule, Strategy
from trader.infrastructure.cache import BoundedLruCache
from trader.infrastructure.deepseek.budget import DeepSeekBudgetStore
from trader.infrastructure.deepseek.cache import ReviewCache
from trader.infrastructure.deepseek.factory import create_deepseek_client
from trader.infrastructure.deepseek.reviewer import DeepSeekReviewer
from trader.infrastructure.market_data.akshare import AkshareResearchClient
from trader.infrastructure.market_data.calendar import ChinaTradingCalendar
from trader.infrastructure.market_data.eastmoney import EastmoneyClient
from trader.infrastructure.market_data.features import FeatureBuilder
from trader.infrastructure.market_data.gateway import MarketDataGateway
from trader.infrastructure.market_data.service import MarketFeatureService
from trader.infrastructure.market_data.sina import SinaClient
from trader.infrastructure.market_data.tencent import TencentClient
from trader.infrastructure.market_data.tushare import TushareClient
from trader.infrastructure.persistence.runtime_json import RuntimeJsonWriter
from trader.infrastructure.persistence.writer import SnapshotRepository
from trader.infrastructure.settings import (
    LongWatchlist,
    RuntimeSettings,
    StrategySettings,
    load_long_watchlist,
    load_runtime_settings,
    load_strategy_settings,
)
from trader.web import create_app
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
    state: RuntimeState
    market_cache: BoundedLruCache[object]
    source_lanes: SourceLaneRegistry

    def start(self) -> bool:
        return self.supervisor.start()

    def stop(self) -> None:
        self.source_lanes.stop(wait=False)
        self.supervisor.stop()
        self.source_lanes.stop(wait=True, timeout_seconds=self.settings.pipeline.shutdown_timeout_seconds)
        self.market_cache.stop(wait=True, timeout_seconds=self.settings.pipeline.shutdown_timeout_seconds)


def build_system(config_path: str | Path) -> ApplicationSystem:
    settings = load_runtime_settings(config_path)
    strategy = load_strategy_settings(settings.strategy_config_path)
    watchlist = load_long_watchlist(settings.long_watchlist_path)
    effective_config_version = f"{settings.config_version}+{strategy.strategy_version}"
    now = _utc_now
    cadence_policy = CadencePolicy.from_seconds(settings.pipeline.cadence_seconds)
    urgent_worker_count = 1 if settings.pipeline.market_workers > 1 else 0
    data_pool = BoundedExecutor(
        worker_count=settings.pipeline.market_workers + urgent_worker_count,
        urgent_worker_count=urgent_worker_count,
        queue_capacity=5,
        thread_name_prefix="source-data",
    )
    source_lanes = SourceLaneRegistry(data_pool)
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

    eastmoney = EastmoneyClient(
        timeout_seconds=settings.market_data.eastmoney_timeout_seconds,
        workers=settings.pipeline.market_workers,
        worker_pool=data_pool,
        cancel_requested=lambda: source_lanes.is_stopped("eastmoney"),
        wall_clock=now,
    )
    history = EastmoneyClient(
        timeout_seconds=settings.market_data.history_timeout_seconds,
        workers=settings.pipeline.market_workers,
        worker_pool=data_pool,
        cancel_requested=lambda: source_lanes.is_stopped("eastmoney"),
        wall_clock=now,
    )
    intraday = EastmoneyClient(
        timeout_seconds=settings.market_data.candidate_timeout_seconds,
        workers=settings.pipeline.market_workers,
        worker_pool=data_pool,
        cancel_requested=lambda: source_lanes.is_stopped("eastmoney"),
        wall_clock=now,
    )
    gateway = MarketDataGateway(
        eastmoney,
        SinaClient(
            timeout_seconds=settings.market_data.eastmoney_timeout_seconds,
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
        worker_pool=data_pool,
        source_lanes=source_lanes,
        cache=market_cache,
        source_contract_versions=settings.market_data.source_contract_versions,
        config_version=settings.config_version,
        schema_version="market_snapshot_v15",
        wall_clock=now,
    )
    evidence_cache_dir = settings.runtime_dir / "evidence_cache"
    market_data = MarketFeatureService(
        gateway,
        history,
        FeatureBuilder(
            strategy.today_news_signal,
            strategy.tomorrow_tail_signal,
            strategy.d25_signal,
            strategy.long_research,
        ),
        research_client=AkshareResearchClient(
            timeout_seconds=settings.market_data.research_timeout_seconds,
            long_research_policy=strategy.long_research,
            evidence_cache_dir=evidence_cache_dir,
            json_writer=json_writer,
            cancel_requested=lambda: source_lanes.is_stopped("akshare"),
        ),
        intraday_client=intraday,
        tushare_client=TushareClient(
            token=settings.market_data.tushare.token if settings.market_data.tushare.enabled else "",
            timeout_seconds=settings.market_data.tushare.timeout_seconds,
            circuit_breaker_failures=settings.market_data.circuit_breaker_failures,
            circuit_breaker_seconds=settings.market_data.circuit_breaker_seconds,
            cancel_requested=lambda: source_lanes.is_stopped("tushare"),
            wall_clock=now,
        ),
        history_workers=settings.pipeline.market_workers,
        research_workers=settings.pipeline.market_workers,
        intraday_workers=settings.pipeline.market_workers,
        intraday_batch_timeout_seconds=settings.market_data.candidate_timeout_seconds,
        intraday_cache_limit=settings.market_data.cache_policy.datasets["intraday_minutes"].capacity,
        history_cache_limit=settings.market_data.cache_policy.datasets["daily_history"].capacity,
        research_cache_limit=settings.market_data.cache_policy.datasets["research_success"].capacity,
        history_preload_limit=settings.market_data.candidate_pool_size * 3,
        history_ttl_seconds=_fixed_cache_ttl(settings, "daily_history"),
        research_ttl_seconds=_fixed_cache_ttl(settings, "research_success"),
        research_circuit_breaker_failures=settings.market_data.circuit_breaker_failures,
        research_circuit_breaker_seconds=settings.market_data.circuit_breaker_seconds,
        intraday_ttl_seconds=_fixed_cache_ttl(settings, "intraday_minutes"),
        research_cache_dir=evidence_cache_dir,
        json_writer=json_writer,
        market_ttl_seconds=min(cadence_policy.intervals[PipelineTask.FULL_MARKET].values()),
        worker_pool=data_pool,
        source_lanes=source_lanes,
        cache=market_cache,
        source_contract_versions=settings.market_data.source_contract_versions,
        config_version=settings.config_version,
        schema_version="market_snapshot_v15",
        wall_clock=now,
    )
    calendar = ChinaTradingCalendar(settings.runtime_dir / "calendar.json")
    repository = SnapshotRepository(settings.runtime_dir, config_version=effective_config_version)
    budget = DeepSeekBudgetStore(
        settings.runtime_dir / "runtime.sqlite3",
        daily_hard_limit=settings.deepseek.daily_hard_limit,
        strategy_limits=settings.deepseek.strategy_limits,
        stage_targets=settings.deepseek.stage_targets,
        stage_limits=settings.deepseek.stage_limits,
        challenger_limits=settings.deepseek.challenger_limits,
    )
    reviewer = DeepSeekReviewer(
        settings.deepseek,
        budget,
        create_deepseek_client(),
        ReviewCache(
            maximum_entries=2000,
            ttl_seconds=600,
            shared_cache=market_cache,
            config_version=effective_config_version,
            seen_capacity=6000,
        ),
        dimension_weights={Strategy(name): weights for name, weights in strategy.dimension_weights.items()},
        strategy_version=strategy.strategy_version,
        confidence_coverage_min=strategy.fusion.confidence_coverage_min,
        minimum_known_dimensions=strategy.fusion.minimum_known_dimensions,
        now=now,
    )
    state = RuntimeState()
    publisher = SnapshotPublisher(
        history_size=settings.api.sse_history_size,
        client_queue_size=settings.api.sse_client_queue_size,
        maximum_subscribers=settings.api.sse_max_clients,
    )
    pipeline = RecommendationPipeline(
        market_data,
        calendar,
        reviewer,
        repository,
        repository,
        publisher,
        RecommendationEngine(
            _recommendation_policy(strategy),
            board_scoring=BoardScoringCoordinator(
                BoardScoringCache(
                    market_cache,
                    config_version=effective_config_version,
                    session_distance=calendar.session_distance,
                )
            ),
        ),
        state,
        config_version=effective_config_version,
        candidate_pool_size=settings.market_data.candidate_pool_size,
        event_queue_size=settings.pipeline.event_queue_size,
        priority_queue_size=settings.pipeline.priority_queue_size,
        now=now,
        market_workers=settings.pipeline.market_workers,
        normalization_workers=settings.pipeline.normalization_workers,
        strategy_workers=settings.pipeline.strategy_workers,
        deepseek_workers=settings.pipeline.deepseek_workers,
        data_pool=data_pool,
        persistence_pool=persistence_pool,
        market_data_manages_workers=True,
        cadence_policy=cadence_policy,
        long_codes=tuple(item.code for item in watchlist.items),
        long_target_prices={item.code: item.target_price for item in watchlist.items},
    )
    supervisor = RuntimeSupervisor(
        pipeline,
        now=now,
        initializers=(pipeline.initialize, budget.initialize, lambda: budget.recover_incomplete(now())),
        interval_seconds=scheduler_interval_seconds,
        shutdown_timeout_seconds=settings.pipeline.shutdown_timeout_seconds,
        record_error=state.record_error,
    )
    app = create_app(
        status_provider=pipeline.status,
        queries=RecommendationQueries(
            repository,
            repository,
            now=now,
            current_quote_reader=market_data,
        ),
        publisher=publisher,
        api_config=WebApiConfig(
            default_top_n=settings.api.default_top_n,
            maximum_top_n=settings.api.maximum_top_n,
            default_event_limit=settings.api.event_page_limit,
            maximum_event_limit=settings.api.maximum_event_page_limit,
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
        repository,
        publisher,
        state,
        market_cache,
        source_lanes,
    )


def _fixed_cache_ttl(settings: RuntimeSettings, dataset: str) -> float:
    value = settings.market_data.cache_policy.datasets[dataset].refresh_ttl_seconds
    if value is None:
        raise ValueError(f"cache dataset {dataset} does not define a fixed TTL")
    return value


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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["ApplicationSystem", "build_system"]
