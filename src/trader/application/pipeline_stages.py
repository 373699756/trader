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
from trader.application.events import EventPriority, EventSpec, PipelineEvent, new_event
from trader.application.pipeline_review_updates import (
    ScoringContext,
    publish_pending_hybrid,
    publish_prepared_snapshots,
    schedule_async_reviews,
)
from trader.application.ports.market import MarketDataUnavailableError
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
    _refresh_intraday_tail_before_score,
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
_TRIGGERED_SCORE_QUEUE_BUDGET_SECONDS = 38.0


_StrategyInput = tuple[
    Strategy,
    Sequence[str],
    Future[tuple[tuple[FeatureSnapshot, ...], str]],
]
_PreparedFuture = tuple[Strategy, Future[PreparedSnapshot]]


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
    _refresh_intraday_tail_before_score(pipeline, now, phase)
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
    if pipeline._decision_execution_mode == "versioned_dag":
        _submit_triggered_score(pipeline, event)
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
    pipeline._submit_overlay_event(_overlay_event_for_close(pipeline, event))
    snapshots = recover_after_close_snapshots(pipeline, now, deadline=event.deadline)
    pipeline._submit_overlay_event(_overlay_event_for_close(pipeline, event))
    settlement_started = time.perf_counter()
    try:
        pipeline._settle_outcomes(now)
    finally:
        pipeline._latency.record_duration(
            "close_quotes:outcome_settlement",
            (time.perf_counter() - settlement_started) * 1000.0,
        )
    return snapshots


def _overlay_event_for_close(
    pipeline: RecommendationPipeline,
    event: PipelineEvent,
) -> PipelineEvent:
    observed_at = max(event.created_at, pipeline._now())
    return new_event(
        EventSpec(
            event_type=PipelineTask.TOPK_QUOTES.value,
            subject_key="market",
            trade_date=event.trade_date,
            phase=event.phase,
            strategy=None,
            priority=EventPriority.LIVE_QUOTES,
            data_version=f"close-overlay:{observed_at.isoformat()}",
            config_version=event.config_version,
            created_at=observed_at,
            deadline=observed_at + timedelta(seconds=task_execution_budget_seconds(PipelineTask.TOPK_QUOTES) or 3.0),
            payload={"overlay_trigger": PipelineTask.CLOSE_QUOTES.value},
        )
    )


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
            pipeline._market_full.fetch_market_features,
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
    except (MarketDataUnavailableError, OSError, RuntimeError, TypeError, ValueError) as exc:
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
    _refresh_intraday_tail_before_score(pipeline, now, phase)
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
    market_features = tuple(_run_market_data_task(pipeline, pipeline._research.refresh_industry_heat, now))
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
    if pipeline._decision_execution_mode == "versioned_dag":
        _submit_triggered_score(pipeline, event)
    return ()


@_register_task(PipelineTask.STOCK_RISK)
def _handle_stock_risk(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    _refresh_stock_risk_on_workers(pipeline, now, event.deadline)
    if pipeline._decision_execution_mode == "versioned_dag":
        _submit_triggered_score(pipeline, event)
    return ()


def _submit_triggered_score(
    pipeline: RecommendationPipeline,
    source_event: PipelineEvent,
) -> None:
    completed_at = max(source_event.created_at, pipeline._now())
    priority = (
        EventPriority.RISK if source_event.event_type == PipelineTask.STOCK_RISK.value else EventPriority.MARKET_QUOTES
    )
    event = new_event(
        EventSpec(
            event_type=PipelineTask.SCORE.value,
            subject_key="market",
            trade_date=source_event.trade_date,
            phase=source_event.phase,
            strategy=None,
            priority=priority,
            data_version=f"{source_event.event_type}:{source_event.data_version}",
            config_version=source_event.config_version,
            created_at=completed_at,
            deadline=completed_at + timedelta(seconds=_TRIGGERED_SCORE_QUEUE_BUDGET_SECONDS),
            payload={
                "schedule_task": PipelineTask.SCORE.value,
                "trigger_event_type": source_event.event_type,
            },
        )
    )
    if pipeline.submit_event(event):
        pipeline._state.increment("triggered_scores_submitted")
    else:
        pipeline._state.increment("triggered_scores_dropped")


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
    _refresh_intraday_tail_before_score(pipeline, now, phase)
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


@_register_task(PipelineTask.HYBRID_READY)
def _handle_hybrid_ready(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    return publish_pending_hybrid(pipeline, phase, event)


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
    context = ScoringContext(
        now,
        phase,
        trade_date_at(now).isoformat(),
        time.perf_counter(),
        completion_deadline,
    )
    _preheat_reviews(pipeline, context)
    strategy_inputs = _strategy_inputs(pipeline, context, use_cached_data=use_cached_data)
    local_started = time.perf_counter()
    prepared_futures = _prepare_strategy_futures(pipeline, context, strategy_inputs)
    prepared_snapshots = _resolve_prepared_snapshots(pipeline, prepared_futures)
    pipeline._latency.record_duration(
        "local_scoring",
        (time.perf_counter() - local_started) * 1000.0,
    )
    local_snapshots = publish_prepared_snapshots(
        pipeline,
        context,
        prepared_snapshots,
        {},
        projection_stage="local",
    )
    if pipeline._decision_execution_mode == "versioned_dag":
        schedule_async_reviews(pipeline, context, prepared_snapshots, local_snapshots)
        return local_snapshots
    measure_deepseek = (
        pipeline._reviews is not None
        and context.phase not in {MarketPhase.DEEPSEEK_CUTOFF, MarketPhase.FINAL_QUOTE}
        and any(prepared.review_eligible for prepared in prepared_snapshots if prepared.strategy is not Strategy.LONG)
    )
    deepseek_started = time.perf_counter()
    review_results = _review_prepared_snapshots(pipeline, context, prepared_snapshots)
    if measure_deepseek:
        pipeline._latency.record_duration(
            "deepseek_review",
            (time.perf_counter() - deepseek_started) * 1000.0,
        )
    hybrid_prepared = tuple(
        prepared
        for prepared in prepared_snapshots
        if prepared.strategy is not Strategy.LONG and prepared.strategy in review_results
    )
    hybrid_snapshots = publish_prepared_snapshots(
        pipeline,
        context,
        hybrid_prepared,
        review_results,
        projection_stage="hybrid",
    )
    latest = {snapshot.strategy: snapshot for snapshot in local_snapshots}
    latest.update((snapshot.strategy, snapshot) for snapshot in hybrid_snapshots)
    return tuple(latest[prepared.strategy] for prepared in prepared_snapshots if prepared.strategy in latest)


def _preheat_reviews(
    pipeline: RecommendationPipeline,
    context: ScoringContext,
) -> None:
    if context.phase is MarketPhase.WARMUP and pipeline._reviews is not None and pipeline._candidate_features:
        preheat = submit_required(
            pipeline,
            pipeline._deepseek_pool,
            pipeline._reviews.preheat,
            pipeline._candidate_features,
            phase=context.phase.value,
            deadline=shanghai_now(context.now).replace(hour=9, minute=30, second=0, microsecond=0),
        )
        preheat.result()


def _strategy_inputs(
    pipeline: RecommendationPipeline,
    context: ScoringContext,
    *,
    use_cached_data: bool,
) -> list[_StrategyInput]:
    inputs: list[_StrategyInput] = []
    feature_reader = read_strategy_features if use_cached_data else fetch_strategy_features
    for strategy in strategies_for_phase(context.phase):
        if (strategy, context.trade_date) in pipeline._frozen_keys or pipeline._state.is_frozen(
            strategy,
            context.trade_date,
        ):
            continue
        codes = pipeline._long_codes if strategy is Strategy.LONG else pipeline._candidate_codes
        if not codes:
            continue
        strategy_future = data_future(
            pipeline,
            feature_reader,
            pipeline._candidate_data,
            strategy,
            codes,
            context.now,
        )
        inputs.append((strategy, codes, strategy_future))
    return inputs


def _prepare_strategy_futures(
    pipeline: RecommendationPipeline,
    context: ScoringContext,
    strategy_inputs: Sequence[_StrategyInput],
) -> list[_PreparedFuture]:
    prepared_futures: list[_PreparedFuture] = []
    for strategy, requested_codes, strategy_data_future in strategy_inputs:
        try:
            features, data_version = strategy_data_future.result()
        except (MarketDataUnavailableError, OSError, RuntimeError, TypeError, ValueError) as exc:
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
                    now=context.now,
                    phase=context.phase.value,
                    trade_date=context.trade_date,
                    data_version=data_version,
                    review_deadline=review_deadline(context.now, context.phase),
                    max_age_seconds=maximum_age_seconds(context.phase, strategy),
                    filtered_count=0 if is_long else pipeline._filtered_count,
                    filter_reasons={} if is_long else pipeline._filter_reasons,
                    filter_details=() if is_long else pipeline._filter_details,
                    target_prices=pipeline._long_target_prices if is_long else None,
                    market_features=pipeline._market_features,
                    requested_codes=requested_codes,
                    preselect_max_age_seconds=maximum_age_seconds(context.phase),
                    candidate_pool_size=pipeline._candidate_pool_size,
                ),
            )
        )
    return prepared_futures


def _resolve_prepared_snapshots(
    pipeline: RecommendationPipeline,
    prepared_futures: Sequence[_PreparedFuture],
) -> list[PreparedSnapshot]:
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
    return prepared_snapshots


def _review_prepared_snapshots(
    pipeline: RecommendationPipeline,
    context: ScoringContext,
    prepared_snapshots: Sequence[PreparedSnapshot],
) -> dict[Strategy, Mapping[str, DeepSeekReview]]:
    review_results: dict[Strategy, Mapping[str, DeepSeekReview]] = {}
    review_enabled = pipeline._reviews is not None and context.phase not in {
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
    return review_results


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
