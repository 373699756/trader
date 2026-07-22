"""Bounded worker-stage orchestration for the recommendation pipeline."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from trader.application.after_close_recovery import recover_after_close_snapshots
from trader.application.cadence import PipelineTask, task_execution_budget_seconds
from trader.application.candidate_features import fetch_strategy_features, read_strategy_features
from trader.application.events import EventDeadlineExpired, PipelineEvent
from trader.application.ports import MarketDataUnavailable
from trader.application.recommendations import PreparedSnapshot
from trader.application.schedule import MarketPhase, shanghai_now, trade_date_at
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import (
    RecommendationSnapshot,
    Strategy,
)
from trader.domain.review.models import DeepSeekReview

if TYPE_CHECKING:
    from trader.application.pipeline import RecommendationPipeline

from trader.application.pipeline_market_tasks import (
    _event_result,
    _refresh_candidate_quotes_on_workers,
    _refresh_candidates_on_workers,
    _refresh_market_news_on_workers,
    _refresh_reference_data_on_workers,
    _refresh_stock_risk_on_workers,
    _run_market_data_task,
)
from trader.application.pipeline_workers import (
    data_future,
    persist,
    store_candidate_selection,
    submit_required,
    worker_status,
)

_P = ParamSpec("_P")
_T = TypeVar("_T")
_LOGGER = logging.getLogger(__name__)


def process_schedule_on_workers(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    freeze_strategies: Sequence[str],
) -> tuple[RecommendationSnapshot, ...]:
    if phase in {
        MarketPhase.WARMUP,
        MarketPhase.TODAY_OBSERVE,
        MarketPhase.TODAY_MAIN,
        MarketPhase.TODAY_LATE,
        MarketPhase.AFTERNOON,
        MarketPhase.FINAL_REVIEW,
        MarketPhase.FINAL_QUOTE,
    }:
        _refresh_candidates_on_workers(pipeline, now, phase)
    snapshots = list(_score_strategies_on_workers(pipeline, now, phase, use_cached_data=False))
    snapshots.extend(pipeline._freeze_available_snapshots(now, freeze_strategies))
    if phase is MarketPhase.AFTER_CLOSE:
        snapshots.extend(recover_after_close_snapshots(pipeline, now))
        pipeline._refresh_live_overlays(now, phase)
        pipeline._settle_outcomes(now)
    elif phase is MarketPhase.FROZEN:
        pipeline._refresh_live_overlays(now, phase)
    return tuple(snapshots)


_TaskHandler = Callable[
    ["RecommendationPipeline", datetime, MarketPhase, PipelineEvent], tuple[RecommendationSnapshot, ...]
]
_TASK_DISPATCH: dict[PipelineTask, _TaskHandler] = {}


def _register_task(*tasks: PipelineTask) -> Callable[[_TaskHandler], _TaskHandler]:
    def decorator(handler: _TaskHandler) -> _TaskHandler:
        for task in tasks:
            _TASK_DISPATCH[task] = handler
        return handler

    return decorator


@_register_task(PipelineTask.FULL_MARKET)
def _handle_full_market(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    _refresh_candidates_on_workers(pipeline, now, phase, force=True, deadline=event.deadline)
    return ()


@_register_task(PipelineTask.CANDIDATE_QUOTES)
def _handle_candidate_quotes(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    _refresh_candidate_quotes_on_workers(pipeline, now, phase, deadline=event.deadline)
    return ()


@_register_task(PipelineTask.TOPK_QUOTES)
def _handle_topk_quotes(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    pipeline._refresh_live_overlays(now, phase, deadline=event.deadline)
    return ()


@_register_task(PipelineTask.CLOSE_QUOTES)
def _handle_close_quotes(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    snapshots = recover_after_close_snapshots(pipeline, now, deadline=event.deadline)
    pipeline._refresh_live_overlays(now, phase, deadline=event.deadline)
    pipeline._settle_outcomes(now)
    return snapshots


@_register_task(PipelineTask.CURRENT_QUOTES)
def _handle_current_quotes(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    try:
        future = data_future(
            pipeline,
            pipeline._market_data.fetch_market_features,
            now,
            force=True,
            deadline=event.deadline,
        )
        market_result = _event_result(
            pipeline,
            future,
            deadline=event.deadline,
            event_type=PipelineTask.CURRENT_QUOTES.value,
        )
        features = tuple(market_result)
    except (MarketDataUnavailable, OSError, RuntimeError, TypeError, ValueError) as exc:
        pipeline._state.increment("current_quote_recovery_failures")
        pipeline._state.record_error(f"current quote index recovery degraded: {str(exc)[:400]}")
        return ()
    pipeline._market_features = features
    pipeline._state.increment("current_quote_recoveries")
    return ()


@_register_task(PipelineTask.SCORE)
def _handle_score(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    return _score_strategies_on_workers(
        pipeline,
        now,
        phase,
        use_cached_data=True,
        completion_deadline=event.deadline,
    )


@_register_task(PipelineTask.INDUSTRY_HEAT)
def _handle_industry_heat(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    market_features = tuple(_run_market_data_task(pipeline, pipeline._market_data.refresh_industry_heat, now))
    if market_features:
        pipeline._market_features = market_features
    return ()


@_register_task(PipelineTask.MARKET_NEWS)
def _handle_market_news(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    _refresh_market_news_on_workers(pipeline, now, event.deadline)
    return ()


@_register_task(PipelineTask.STOCK_RISK)
def _handle_stock_risk(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    _refresh_stock_risk_on_workers(pipeline, now, event.deadline)
    return ()


@_register_task(PipelineTask.REFERENCE_DATA)
def _handle_reference_data(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    _refresh_reference_data_on_workers(pipeline, now, phase)
    return ()


@_register_task(PipelineTask.DEEPSEEK_CUTOFF)
def _handle_deepseek_cutoff(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    pipeline._state.increment("deepseek_cutoff_events")
    return ()


@_register_task(PipelineTask.FINAL_CANDIDATE_QUOTES)
def _handle_final_candidate_quotes(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    _refresh_candidates_on_workers(pipeline, now, phase, force=True, deadline=event.deadline)
    _refresh_candidate_quotes_on_workers(pipeline, now, phase, deadline=event.deadline)
    return _score_strategies_on_workers(
        pipeline,
        now,
        phase,
        use_cached_data=True,
        completion_deadline=event.deadline,
    )


@_register_task(PipelineTask.FREEZE)
def _handle_freeze(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    freeze_raw = event.payload.get("freeze_strategies")
    freezes = tuple(str(value) for value in freeze_raw) if isinstance(freeze_raw, (list, tuple)) else ()
    return pipeline._freeze_available_snapshots(now, freezes)


def process_event_on_workers(
    pipeline: RecommendationPipeline,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    task_raw = str(event.payload.get("schedule_task") or event.event_type)
    try:
        task = PipelineTask(task_raw)
    except ValueError:
        freeze_raw = event.payload.get("freeze_strategies")
        freezes = tuple(str(value) for value in freeze_raw) if isinstance(freeze_raw, (list, tuple)) else ()
        return process_schedule_on_workers(pipeline, event.created_at, MarketPhase(event.phase), freezes)

    now = event.created_at if task is PipelineTask.FREEZE else max(event.created_at, pipeline._now())
    phase = MarketPhase(event.phase)
    handler = _TASK_DISPATCH.get(task)
    if handler is not None:
        execution_budget = task_execution_budget_seconds(task)
        execution_event = event
        if execution_budget is not None:
            execution_deadline = now + timedelta(seconds=execution_budget)
            if event.deadline is not None:
                execution_deadline = min(event.deadline, execution_deadline)
            execution_event = replace(event, deadline=execution_deadline)
        return handler(pipeline, now, phase, execution_event)
    pipeline._refresh_live_overlays(now, MarketPhase.AFTER_CLOSE, deadline=event.deadline)
    return ()


def _score_strategies_on_workers(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    *,
    use_cached_data: bool,
    completion_deadline: datetime | None = None,
) -> tuple[RecommendationSnapshot, ...]:
    scoring_started = time.perf_counter()
    snapshots: list[RecommendationSnapshot] = []
    trade_date = trade_date_at(now).isoformat()
    if phase is MarketPhase.WARMUP and pipeline._reviews is not None and pipeline._candidate_features:
        preheat = submit_required(
            pipeline,
            pipeline._deepseek_pool,
            pipeline._reviews.preheat,
            pipeline._candidate_features,
            phase=phase.value,
            deadline=shanghai_now(now).replace(hour=9, minute=30, second=0, microsecond=0),
        )
        preheat.result()

    strategy_inputs: list[tuple[Strategy, Sequence[str], Future[tuple[tuple[FeatureSnapshot, ...], str]]]] = []
    for strategy in strategies_for_phase(phase):
        if (strategy, trade_date) in pipeline._frozen_keys or pipeline._state.is_frozen(strategy, trade_date):
            continue
        codes = pipeline._long_codes if strategy is Strategy.LONG else pipeline._candidate_codes
        if not codes:
            continue
        feature_reader = read_strategy_features if use_cached_data else fetch_strategy_features
        strategy_future = data_future(
            pipeline,
            feature_reader,
            pipeline._market_data,
            strategy,
            codes,
            now,
        )
        strategy_inputs.append((strategy, codes, strategy_future))

    prepared_futures: list[tuple[Strategy, Future[PreparedSnapshot]]] = []
    for strategy, requested_codes, strategy_data_future in strategy_inputs:
        try:
            features, data_version = strategy_data_future.result()
        except (MarketDataUnavailable, OSError, RuntimeError, TypeError, ValueError) as exc:
            pipeline._state.increment("strategy_data_failures")
            pipeline._state.record_error(f"{strategy.value} data degraded: {str(exc)[:400]}")
            continue
        if not features:
            continue
        is_long = strategy is Strategy.LONG
        pool = pipeline._long_pool if is_long else pipeline._strategy_pool
        prepared_futures.append(
            (
                strategy,
                submit_required(
                    pipeline,
                    pool,
                    pipeline._engine.prepare_snapshot,
                    strategy,
                    features,
                    now=now,
                    phase=phase.value,
                    trade_date=trade_date,
                    data_version=data_version,
                    review_deadline=review_deadline(now, phase),
                    max_age_seconds=maximum_age_seconds(phase, strategy),
                    filtered_count=0 if is_long else pipeline._filtered_count,
                    filter_reasons={} if is_long else pipeline._filter_reasons,
                    filter_details=() if is_long else pipeline._filter_details,
                    target_prices=pipeline._long_target_prices if is_long else None,
                    market_features=pipeline._market_features,
                    requested_codes=requested_codes,
                    preselect_max_age_seconds=maximum_age_seconds(phase),
                    candidate_pool_size=pipeline._candidate_pool_size,
                ),
            )
        )

    prepared_snapshots: list[PreparedSnapshot] = []
    for strategy, prepared_future in prepared_futures:
        try:
            prepared = prepared_future.result()
        except Exception as exc:
            pipeline._state.increment("strategy_scoring_failures")
            pipeline._state.record_error(f"{strategy.value} scoring degraded: {str(exc)[:400]}")
            continue
        if not prepared.board_scoring_complete:
            _record_incomplete_board_score(pipeline, prepared)
            continue
        prepared_snapshots.append(prepared)
    review_results: dict[Strategy, Mapping[str, DeepSeekReview]] = {}
    review_enabled = pipeline._reviews is not None and phase not in {
        MarketPhase.DEEPSEEK_CUTOFF,
        MarketPhase.FINAL_QUOTE,
    }
    if review_enabled and pipeline._reviews is not None:
        review_futures: dict[Strategy, Future[Mapping[str, DeepSeekReview]]] = {}
        for prepared in prepared_snapshots:
            if prepared.strategy is Strategy.LONG or not prepared.review_eligible:
                continue
            review_futures[prepared.strategy] = submit_required(
                pipeline,
                pipeline._deepseek_pool,
                pipeline._reviews.review,
                prepared.strategy,
                prepared.review_eligible,
                phase=prepared.phase,
                deadline=prepared.review_deadline,
                contexts=pipeline._engine.review_contexts(prepared),
            )
        for strategy, review_future in review_futures.items():
            review_results[strategy] = _resolve_review(pipeline, strategy, review_future)
        long_prepared = next(
            (prepared for prepared in prepared_snapshots if prepared.strategy is Strategy.LONG and prepared.eligible),
            None,
        )
        if long_prepared is not None:
            long_review = submit_required(
                pipeline,
                pipeline._deepseek_pool,
                pipeline._reviews.review,
                Strategy.LONG,
                long_prepared.eligible,
                phase=long_prepared.phase,
                deadline=long_prepared.review_deadline,
                contexts=pipeline._engine.review_contexts(long_prepared),
            )
            review_results[Strategy.LONG] = _resolve_review(pipeline, Strategy.LONG, long_review)

    for prepared in prepared_snapshots:
        if completion_deadline is not None and pipeline._now() >= completion_deadline:
            pipeline._state.increment("score_results_discarded_late")
            raise EventDeadlineExpired(f"event deadline expired during execution: {PipelineTask.SCORE.value}")
        reviews = review_results.get(prepared.strategy, {})
        snapshot = replace(
            pipeline._engine.finalize_snapshot(prepared, reviews),
            config_version=pipeline._config_version,
        )
        pipeline._state.record_strategy_latency(
            prepared.strategy,
            round((time.perf_counter() - scoring_started) * 1000.0, 3),
        )
        persist(pipeline, pipeline._repository.publish, snapshot)
        pipeline._state.publish(snapshot)
        pipeline._session_snapshot_ids.add(snapshot.snapshot_id)
        pipeline._publisher.publish(snapshot)
        snapshots.append(snapshot)

    return tuple(snapshots)


def _record_incomplete_board_score(
    pipeline: RecommendationPipeline,
    prepared: PreparedSnapshot,
) -> None:
    reasons = prepared.board_degraded_reasons or ("board_scoring_incomplete",)
    pipeline._state.increment("board_scoring_incomplete")
    pipeline._state.record_strategy_degraded(prepared.strategy, reasons)
    pipeline._state.record_error(
        f"{prepared.strategy.value} board scoring degraded; retained latest complete snapshot: "
        + ",".join(reasons)[:350]
    )


def strategies_for_phase(phase: MarketPhase) -> tuple[Strategy, ...]:
    if phase in {MarketPhase.TODAY_OBSERVE, MarketPhase.TODAY_MAIN, MarketPhase.TODAY_LATE}:
        return (Strategy.TODAY,)
    if phase in {MarketPhase.AFTERNOON, MarketPhase.FINAL_REVIEW, MarketPhase.FINAL_QUOTE}:
        return (Strategy.TOMORROW, Strategy.D25, Strategy.LONG)
    return ()


def maximum_age_seconds(phase: MarketPhase, strategy: Strategy | None = None) -> float:
    if strategy is Strategy.TODAY or phase in {
        MarketPhase.TODAY_OBSERVE,
        MarketPhase.TODAY_MAIN,
        MarketPhase.TODAY_LATE,
    }:
        return 20.0
    return 30.0


def review_deadline(now: datetime, phase: MarketPhase) -> datetime:
    local = shanghai_now(now)
    if phase in {MarketPhase.TODAY_OBSERVE, MarketPhase.TODAY_MAIN, MarketPhase.TODAY_LATE}:
        return local.replace(hour=11, minute=20, second=0, microsecond=0)
    return local.replace(hour=14, minute=48, second=0, microsecond=0)


def _resolve_review(
    pipeline: RecommendationPipeline,
    strategy: Strategy,
    future: Future[Mapping[str, DeepSeekReview]],
) -> Mapping[str, DeepSeekReview]:
    try:
        return future.result()
    except Exception as exc:
        pipeline._state.increment("deepseek_review_failures")
        pipeline._state.record_error(f"DeepSeek review degraded for {strategy.value}: {str(exc)[:400]}")
        return {}


__all__ = [
    "data_future",
    "maximum_age_seconds",
    "persist",
    "process_event_on_workers",
    "process_schedule_on_workers",
    "review_deadline",
    "store_candidate_selection",
    "strategies_for_phase",
    "submit_required",
    "worker_status",
]
