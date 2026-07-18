"""Bounded worker-stage orchestration for the recommendation pipeline."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from trader.application.cadence import PipelineTask
from trader.application.candidate_features import fetch_strategy_features, read_strategy_features
from trader.application.events import PipelineEvent
from trader.application.ports import MarketDataUnavailable
from trader.application.recommendations import PreparedSnapshot
from trader.application.schedule import MarketPhase, shanghai_now, trade_date_at
from trader.application.workers import BoundedExecutor
from trader.domain.models import DeepSeekReview, FeatureSnapshot, FilterAudit, RecommendationSnapshot, Strategy

if TYPE_CHECKING:
    from trader.application.pipeline import RecommendationPipeline

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
    if phase in {MarketPhase.FROZEN, MarketPhase.AFTER_CLOSE}:
        pipeline._refresh_live_overlays(now, phase)
    return tuple(snapshots)


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

    now = event.created_at
    phase = MarketPhase(event.phase)
    if task is PipelineTask.FULL_MARKET:
        _refresh_candidates_on_workers(pipeline, now, phase, deadline=event.deadline)
        return ()
    if task is PipelineTask.CANDIDATE_QUOTES:
        _refresh_candidate_quotes_on_workers(pipeline, now, phase, deadline=event.deadline)
        return ()
    if task is PipelineTask.TOPK_QUOTES:
        pipeline._refresh_live_overlays(now, phase, deadline=event.deadline)
        return ()
    if task is PipelineTask.SCORE:
        return _score_strategies_on_workers(
            pipeline,
            now,
            phase,
            use_cached_data=True,
            completion_deadline=event.deadline,
        )
    if task is PipelineTask.INDUSTRY_HEAT:
        market_features = tuple(_run_market_data_task(pipeline, pipeline._market_data.refresh_industry_heat, now))
        if market_features:
            pipeline._market_features = market_features
        return ()
    if task is PipelineTask.MARKET_NEWS:
        _refresh_market_news_on_workers(pipeline, now, event.deadline)
        return ()
    if task is PipelineTask.STOCK_RISK:
        _refresh_stock_risk_on_workers(pipeline, now, event.deadline)
        return ()
    if task is PipelineTask.REFERENCE_DATA:
        _refresh_reference_data_on_workers(pipeline, now, phase)
        return ()
    if task is PipelineTask.DEEPSEEK_CUTOFF:
        pipeline._state.increment("deepseek_cutoff_events")
        return ()
    if task is PipelineTask.FINAL_CANDIDATE_QUOTES:
        _refresh_candidates_on_workers(pipeline, now, phase, force=True, deadline=event.deadline)
        _refresh_candidate_quotes_on_workers(pipeline, now, phase, deadline=event.deadline)
        return _score_strategies_on_workers(
            pipeline,
            now,
            phase,
            use_cached_data=True,
            completion_deadline=event.deadline,
        )
    if task is PipelineTask.FREEZE:
        freeze_raw = event.payload.get("freeze_strategies")
        freezes = tuple(str(value) for value in freeze_raw) if isinstance(freeze_raw, (list, tuple)) else ()
        return pipeline._freeze_available_snapshots(now, freezes)
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
        future = data_future(
            pipeline,
            feature_reader,
            pipeline._market_data,
            strategy,
            codes,
            now,
        )
        strategy_inputs.append((strategy, codes, future))

    prepared_futures: list[Future[PreparedSnapshot]] = []
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
            )
        )

    prepared_snapshots = tuple(future.result() for future in prepared_futures)
    review_results: dict[Strategy, Mapping[str, DeepSeekReview]] = {}
    review_enabled = pipeline._reviews is not None and phase not in {
        MarketPhase.DEEPSEEK_CUTOFF,
        MarketPhase.FINAL_QUOTE,
    }
    if review_enabled and pipeline._reviews is not None:
        review_futures: dict[Strategy, Future[Mapping[str, DeepSeekReview]]] = {}
        for prepared in prepared_snapshots:
            if prepared.strategy is Strategy.LONG or not prepared.eligible:
                continue
            review_futures[prepared.strategy] = submit_required(
                pipeline,
                pipeline._deepseek_pool,
                pipeline._reviews.review,
                prepared.strategy,
                prepared.eligible,
                phase=prepared.phase,
                deadline=prepared.review_deadline,
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
            )
            review_results[Strategy.LONG] = _resolve_review(pipeline, Strategy.LONG, long_review)

    for prepared in prepared_snapshots:
        if completion_deadline is not None and pipeline._now() >= completion_deadline:
            pipeline._state.increment("score_results_discarded_late")
            raise RuntimeError(f"score result completed after deadline: {phase.value}")
        reviews = review_results.get(prepared.strategy, {})
        snapshot = replace(
            pipeline._engine.finalize_snapshot(prepared, reviews),
            config_version=pipeline._config_version,
        )
        persist(pipeline, pipeline._repository.publish, snapshot)
        pipeline._state.publish(snapshot)
        pipeline._publisher.publish(snapshot)
        snapshots.append(snapshot)

    return tuple(snapshots)


def store_candidate_selection(
    pipeline: RecommendationPipeline,
    market_features: Sequence[FeatureSnapshot],
    candidates: tuple[FeatureSnapshot, ...],
    reasons: Mapping[str, int],
    details: tuple[FilterAudit, ...],
) -> None:
    pipeline._market_features = tuple(market_features)
    pipeline._candidate_codes = tuple(feature.quote.code for feature in candidates)
    pipeline._candidate_features = candidates
    pipeline._filter_reasons = reasons
    pipeline._filter_details = details
    pipeline._filtered_count = len({item.stock_code for item in details})


def submit_required(
    pipeline: RecommendationPipeline,
    pool: BoundedExecutor,
    function: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> Future[_T]:
    future = pool.submit(function, *args, **kwargs)
    if future is None:
        pipeline._state.increment("worker_queue_rejections")
        raise RuntimeError("bounded worker queue is full or stopped")
    return future


def data_future(
    pipeline: RecommendationPipeline,
    function: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> Future[_T]:
    if not pipeline._market_data_manages_workers:
        return submit_required(pipeline, pipeline._data_pool, function, *args, **kwargs)
    future: Future[_T] = Future()
    try:
        future.set_result(function(*args, **kwargs))
    except BaseException as exc:
        future.set_exception(exc)
    return future


def persist(
    pipeline: RecommendationPipeline,
    function: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> _T:
    if not pipeline._persistence_running:
        return function(*args, **kwargs)
    future = pipeline._persistence_pool.submit(function, *args, **kwargs)
    if future is None:
        pipeline._state.increment("persistence_queue_rejections")
        raise RuntimeError("persistence queue is full or stopped")
    return future.result()


def worker_status(pipeline: RecommendationPipeline) -> dict[str, object]:
    worker = pipeline._worker
    queue_status = pipeline._queue.status()
    with pipeline._merge_status_lock:
        merge_status = {
            "workers": 1,
            "queue_capacity": queue_status["capacity"],
            "inflight": pipeline._merge_inflight,
            "submitted_count": pipeline._merge_submitted_count,
            "completed_count": pipeline._merge_completed_count,
            "rejected_count": queue_status["rejected_count"],
            "running": bool(worker is not None and worker.is_alive()),
        }
    return {
        "data": pipeline._data_pool.status(),
        "normalization": pipeline._normalization_pool.status(),
        "strategy": pipeline._strategy_pool.status(),
        "deepseek": pipeline._deepseek_pool.status(),
        "long": pipeline._long_pool.status(),
        "merge": merge_status,
        "persistence": pipeline._persistence_pool.status(),
    }


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


def _refresh_candidate_quotes_on_workers(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    *,
    deadline: datetime | None = None,
) -> None:
    codes = _active_codes(pipeline)
    if not codes:
        return
    features = tuple(
        _run_market_data_task(
            pipeline,
            pipeline._market_data.refresh_candidate_quotes,
            codes,
            now,
            deadline=deadline,
        )
    )
    candidate_set = set(pipeline._candidate_codes)
    pipeline._candidate_features = tuple(feature for feature in features if feature.quote.code in candidate_set)
    if phase in {MarketPhase.AFTERNOON, MarketPhase.FINAL_REVIEW, MarketPhase.FINAL_QUOTE}:
        _run_market_data_task(
            pipeline,
            pipeline._market_data.refresh_intraday_tail,
            pipeline._candidate_codes,
            now,
        )


def _refresh_market_news_on_workers(
    pipeline: RecommendationPipeline,
    now: datetime,
    deadline: datetime | None,
) -> None:
    codes = _active_codes(pipeline)
    if not codes:
        return
    _run_market_data_task(
        pipeline,
        pipeline._market_data.refresh_market_news,
        codes,
        now,
        deadline=deadline,
    )
    pipeline._candidate_features = tuple(pipeline._market_data.read_candidate_features(pipeline._candidate_codes, now))


def _refresh_stock_risk_on_workers(
    pipeline: RecommendationPipeline,
    now: datetime,
    deadline: datetime | None,
) -> None:
    codes = _active_codes(pipeline)
    if codes:
        _run_market_data_task(
            pipeline,
            pipeline._market_data.refresh_stock_risk,
            codes,
            now,
            deadline=deadline,
        )


def _refresh_reference_data_on_workers(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
) -> None:
    codes = _active_codes(pipeline)
    if codes:
        _run_market_data_task(
            pipeline,
            pipeline._market_data.refresh_reference_data,
            codes,
            now,
            force=phase is MarketPhase.AFTER_CLOSE,
        )


def _active_codes(pipeline: RecommendationPipeline) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*pipeline._candidate_codes, *pipeline._long_codes)))


def _run_market_data_task(
    pipeline: RecommendationPipeline,
    function: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> _T:
    return data_future(pipeline, function, *args, **kwargs).result()


def _refresh_candidates_on_workers(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    *,
    force: bool = False,
    deadline: datetime | None = None,
) -> None:
    market_future = data_future(
        pipeline,
        pipeline._market_data.fetch_market_features,
        now,
        force=force,
        deadline=deadline,
    )
    try:
        market_features = tuple(market_future.result())
    except MarketDataUnavailable as exc:
        reason = str(exc)[:500]
        _LOGGER.warning("candidate refresh degraded during %s: %s", phase.value, reason)
        pipeline._state.increment("market_refresh_failures")
        pipeline._state.record_error(f"market data degraded during {phase.value}: {reason}")
        return
    selection = submit_required(
        pipeline,
        pipeline._normalization_pool,
        pipeline._engine.preselect,
        market_features,
        now=now,
        max_age_seconds=maximum_age_seconds(phase),
        limit=pipeline._candidate_pool_size,
    )
    candidates, reasons, details = selection.result()
    store_candidate_selection(pipeline, market_features, candidates, reasons, details)


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
