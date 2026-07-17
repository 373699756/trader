"""Composition root for the v2 application."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask

from trader.application.pipeline import RecommendationPipeline
from trader.application.policy import RecommendationPolicy, SelectionPolicy
from trader.application.publisher import SnapshotPublisher
from trader.application.queries import RecommendationQueries
from trader.application.recommendations import RecommendationEngine
from trader.application.runtime import RuntimeSupervisor, scheduler_interval_seconds
from trader.application.status import RuntimeState
from trader.domain.fusion import FusionPolicy
from trader.domain.models import RiskRule, Strategy
from trader.infrastructure.deepseek.budget import DeepSeekBudgetStore
from trader.infrastructure.deepseek.cache import ReviewCache
from trader.infrastructure.deepseek.client import DeepSeekHttpClient
from trader.infrastructure.deepseek.reviewer import DeepSeekReviewer
from trader.infrastructure.market_data.akshare import AkshareResearchClient
from trader.infrastructure.market_data.calendar import ChinaTradingCalendar
from trader.infrastructure.market_data.eastmoney import EastmoneyClient
from trader.infrastructure.market_data.features import FeatureBuilder
from trader.infrastructure.market_data.gateway import MarketDataGateway
from trader.infrastructure.market_data.service import MarketFeatureService
from trader.infrastructure.market_data.sina import SinaClient
from trader.infrastructure.market_data.tencent import TencentClient
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

    def start(self) -> bool:
        return self.supervisor.start()

    def stop(self) -> None:
        self.supervisor.stop()


def build_system(config_path: str | Path) -> ApplicationSystem:
    settings = load_runtime_settings(config_path)
    strategy = load_strategy_settings(settings.strategy_config_path)
    watchlist = load_long_watchlist(settings.long_watchlist_path)
    now = _utc_now

    eastmoney = EastmoneyClient(
        timeout_seconds=settings.market_data.eastmoney_timeout_seconds,
        workers=settings.pipeline.market_workers,
    )
    history = EastmoneyClient(
        timeout_seconds=settings.market_data.history_timeout_seconds,
        workers=settings.pipeline.market_workers,
    )
    intraday = EastmoneyClient(
        timeout_seconds=settings.market_data.candidate_timeout_seconds,
        workers=settings.pipeline.market_workers,
    )
    market_gateway = MarketDataGateway(
        eastmoney,
        SinaClient(timeout_seconds=settings.market_data.eastmoney_timeout_seconds),
        TencentClient(timeout_seconds=settings.market_data.candidate_timeout_seconds),
        minimum_market_rows=settings.market_data.minimum_market_rows,
        circuit_breaker_failures=settings.market_data.circuit_breaker_failures,
        circuit_breaker_seconds=settings.market_data.circuit_breaker_seconds,
    )
    market_data = MarketFeatureService(
        market_gateway,
        history,
        FeatureBuilder(strategy.today_news_signal, strategy.tomorrow_tail_signal),
        research_client=AkshareResearchClient(timeout_seconds=settings.market_data.research_timeout_seconds),
        intraday_client=intraday,
        history_workers=settings.pipeline.market_workers,
        research_workers=settings.pipeline.market_workers,
        intraday_workers=settings.pipeline.market_workers,
        intraday_batch_timeout_seconds=settings.market_data.candidate_timeout_seconds,
        intraday_cache_limit=settings.market_data.candidate_pool_size * 3,
        history_preload_limit=settings.market_data.candidate_pool_size * 3,
        market_ttl_seconds=settings.pipeline.full_market_refresh_seconds,
    )
    calendar = ChinaTradingCalendar(settings.runtime_dir / "calendar.json")
    effective_config_version = f"{settings.config_version}+{strategy.strategy_version}"
    repository = SnapshotRepository(settings.runtime_dir, config_version=effective_config_version)
    budget = DeepSeekBudgetStore(
        settings.runtime_dir / "runtime.sqlite3",
        daily_hard_limit=settings.deepseek.daily_hard_limit,
        strategy_limits=settings.deepseek.strategy_limits,
    )
    reviewer = DeepSeekReviewer(
        settings.deepseek,
        budget,
        DeepSeekHttpClient(),
        ReviewCache(maximum_entries=2000, ttl_seconds=600),
        now,
    )
    state = RuntimeState()
    publisher = SnapshotPublisher(
        history_size=settings.api.sse_history_size,
        client_queue_size=settings.api.sse_client_queue_size,
        maximum_subscribers=settings.api.sse_max_clients,
    )
    policy = _recommendation_policy(strategy)
    pipeline = RecommendationPipeline(
        market_data,
        calendar,
        reviewer,
        repository,
        repository,
        publisher,
        RecommendationEngine(policy),
        state,
        config_version=effective_config_version,
        candidate_pool_size=settings.market_data.candidate_pool_size,
        event_queue_size=settings.pipeline.event_queue_size,
        priority_queue_size=settings.pipeline.priority_queue_size,
        now=now,
        long_codes=tuple(item.code for item in watchlist.items),
        long_target_prices={item.code: item.target_price for item in watchlist.items},
    )
    supervisor = RuntimeSupervisor(
        pipeline,
        now=now,
        initializers=(pipeline.initialize, budget.initialize, budget.abandon_reserved),
        interval_seconds=scheduler_interval_seconds,
        shutdown_timeout_seconds=settings.pipeline.shutdown_timeout_seconds,
        record_error=state.record_error,
    )
    app = create_app(
        status_provider=pipeline.status,
        queries=RecommendationQueries(repository, repository, now=now),
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
        settings=settings,
        strategy=strategy,
        watchlist=watchlist,
        app=app,
        supervisor=supervisor,
        pipeline=pipeline,
        repository=repository,
        publisher=publisher,
        state=state,
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
        ),
        candidate_weights=settings.candidate_weights,
        dimension_weights={Strategy(name): weights for name, weights in settings.dimension_weights.items()},
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
            )
            for rule in settings.risk_rules
        },
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["ApplicationSystem", "build_system"]
