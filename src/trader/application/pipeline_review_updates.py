"""Asynchronous review coordination and snapshot publication."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from trader.application.cadence import PipelineTask
from trader.application.events import EventDeadlineExpiredError, EventPriority, EventSpec, PipelineEvent, new_event
from trader.application.pipeline_workers import persist, submit_required
from trader.application.recommendation_finalization import PreparedSnapshot
from trader.application.schedule import MarketPhase, shanghai_now
from trader.application.snapshot_publication import admit_snapshot_to_p6
from trader.domain.recommendation.models import RecommendationSnapshot, Strategy
from trader.domain.review.models import DeepSeekReview

if TYPE_CHECKING:
    from trader.application.pipeline import RecommendationPipeline

_HYBRID_QUEUE_BUDGET_SECONDS = 38.0


@dataclass(frozen=True)
class ScoringContext:
    now: datetime
    phase: MarketPhase
    trade_date: str
    started_at: float
    completion_deadline: datetime | None


@dataclass(frozen=True)
class _PendingHybrid:
    prepared: PreparedSnapshot
    reviews: Mapping[str, DeepSeekReview]
    base_snapshot_id: str


@dataclass(frozen=True)
class _PendingReview:
    prepared: PreparedSnapshot
    base_snapshot_id: str


def publish_pending_hybrid(
    pipeline: RecommendationPipeline,
    phase: MarketPhase,
    event: PipelineEvent,
) -> tuple[RecommendationSnapshot, ...]:
    token = str(event.payload.get("review_token") or "")
    with pipeline._pending_hybrid_lock:
        pending = pipeline._pending_hybrids.pop(token, None)
    if not isinstance(pending, _PendingHybrid):
        pipeline._state.increment("hybrid_results_missing")
        return ()
    current = pipeline._state.latest(pending.prepared.strategy)
    if (
        current is None
        or current.frozen
        or current.trade_date != pending.prepared.trade_date
        or current.snapshot_id != pending.base_snapshot_id
        or current.data_version != pending.prepared.data_version
        or current.config_version != pipeline._config_version
    ):
        pipeline._state.increment("hybrid_results_discarded_stale")
        return ()
    context = ScoringContext(
        pending.prepared.now,
        phase,
        pending.prepared.trade_date,
        time.perf_counter(),
        event.deadline,
    )
    return publish_prepared_snapshots(
        pipeline,
        context,
        (pending.prepared,),
        {pending.prepared.strategy: pending.reviews},
        projection_stage="hybrid",
    )


def schedule_async_reviews(
    pipeline: RecommendationPipeline,
    context: ScoringContext,
    prepared_snapshots: Sequence[PreparedSnapshot],
    local_snapshots: Sequence[RecommendationSnapshot],
) -> None:
    if pipeline._reviews is None or context.phase in {MarketPhase.DEEPSEEK_CUTOFF, MarketPhase.FINAL_QUOTE}:
        return
    local_by_strategy = {snapshot.strategy: snapshot for snapshot in local_snapshots}
    for prepared in prepared_snapshots:
        base = local_by_strategy.get(prepared.strategy)
        if prepared.strategy is Strategy.LONG or not prepared.review_eligible or base is None:
            continue
        _offer_async_review(pipeline, _PendingReview(prepared, base.snapshot_id))


def _offer_async_review(
    pipeline: RecommendationPipeline,
    pending: _PendingReview,
) -> None:
    strategy = pending.prepared.strategy
    with pipeline._async_review_lock:
        if strategy in pipeline._async_review_inflight:
            if strategy in pipeline._async_review_pending:
                pipeline._state.increment("async_reviews_superseded")
            pipeline._async_review_pending[strategy] = pending
            return
        pipeline._async_review_inflight.add(strategy)
    _launch_async_review(pipeline, pending)


def _launch_async_review(
    pipeline: RecommendationPipeline,
    pending: _PendingReview,
) -> None:
    prepared = pending.prepared
    with pipeline._lifecycle_lock:
        accepting = pipeline._accepting
    if not accepting:
        with pipeline._async_review_lock:
            pipeline._async_review_pending.pop(prepared.strategy, None)
            pipeline._async_review_inflight.discard(prepared.strategy)
        pipeline._state.increment("async_reviews_dropped_shutdown")
        return
    current = pipeline._state.latest(prepared.strategy)
    if (
        current is None
        or current.frozen
        or current.snapshot_id != pending.base_snapshot_id
        or current.data_version != prepared.data_version
    ):
        pipeline._state.increment("async_reviews_discarded_stale")
        _advance_async_review(pipeline, prepared.strategy)
        return
    review_started = time.perf_counter()
    reviewer = pipeline._reviews
    if reviewer is None:
        _queue_hybrid_completion(pipeline, pending, {})
        _advance_async_review(pipeline, prepared.strategy)
        return
    try:
        future = submit_required(
            pipeline,
            pipeline._deepseek_pool,
            reviewer.review,
            prepared.strategy,
            prepared.review_eligible,
            phase=prepared.phase,
            deadline=prepared.review_deadline,
            contexts=pipeline._engine.review_contexts(prepared),
        )
    except RuntimeError:
        pipeline._state.increment("async_review_queue_rejections")
        _queue_hybrid_completion(pipeline, pending, {})
        _advance_async_review(pipeline, prepared.strategy)
        return
    future.add_done_callback(
        lambda completed: _complete_async_review(
            pipeline,
            pending,
            review_started,
            completed,
        )
    )


def _complete_async_review(
    pipeline: RecommendationPipeline,
    pending: _PendingReview,
    review_started: float,
    future: Future[Mapping[str, DeepSeekReview]],
) -> None:
    pipeline._latency.record_duration(
        "deepseek_review",
        (time.perf_counter() - review_started) * 1000.0,
    )
    reviews = _resolve_review(pipeline, pending.prepared.strategy, future)
    _queue_hybrid_completion(pipeline, pending, reviews)
    _advance_async_review(pipeline, pending.prepared.strategy)


def _advance_async_review(
    pipeline: RecommendationPipeline,
    strategy: Strategy,
) -> None:
    with pipeline._async_review_lock:
        next_pending = pipeline._async_review_pending.pop(strategy, None)
        if not isinstance(next_pending, _PendingReview):
            pipeline._async_review_inflight.discard(strategy)
            return
    _launch_async_review(pipeline, next_pending)


def _queue_hybrid_completion(
    pipeline: RecommendationPipeline,
    pending_review: _PendingReview,
    reviews: Mapping[str, DeepSeekReview],
) -> None:
    prepared = pending_review.prepared
    token = uuid4().hex
    pending = _PendingHybrid(prepared, reviews, pending_review.base_snapshot_id)
    with pipeline._pending_hybrid_lock:
        stale_tokens = tuple(
            key
            for key, value in pipeline._pending_hybrids.items()
            if isinstance(value, _PendingHybrid) and value.prepared.strategy is prepared.strategy
        )
        for stale_token in stale_tokens:
            pipeline._pending_hybrids.pop(stale_token, None)
            pipeline._state.increment("hybrid_results_superseded")
        pipeline._pending_hybrids[token] = pending
    completed_at = pipeline._now()
    event = new_event(
        EventSpec(
            event_type=PipelineTask.HYBRID_READY.value,
            subject_key="market",
            trade_date=prepared.trade_date,
            phase=prepared.phase,
            strategy=prepared.strategy,
            priority=EventPriority.DEEPSEEK,
            data_version=prepared.data_version,
            config_version=pipeline._config_version,
            created_at=completed_at,
            deadline=completed_at + timedelta(seconds=_HYBRID_QUEUE_BUDGET_SECONDS),
            payload={
                "review_token": token,
                "base_snapshot_id": pending_review.base_snapshot_id,
            },
        )
    )
    if pipeline.submit_event(event):
        pipeline._state.increment("hybrid_results_queued")
        return
    with pipeline._pending_hybrid_lock:
        pipeline._pending_hybrids.pop(token, None)
    pipeline._state.increment("hybrid_results_dropped")


def publish_prepared_snapshots(
    pipeline: RecommendationPipeline,
    context: ScoringContext,
    prepared_snapshots: Sequence[PreparedSnapshot],
    review_results: Mapping[Strategy, Mapping[str, DeepSeekReview]],
    *,
    projection_stage: str,
) -> tuple[RecommendationSnapshot, ...]:
    snapshots: list[RecommendationSnapshot] = []
    for prepared in prepared_snapshots:
        if context.completion_deadline is not None and pipeline._now() >= context.completion_deadline:
            pipeline._state.increment("score_results_discarded_late")
            raise EventDeadlineExpiredError(f"event deadline expired during execution: {PipelineTask.SCORE.value}")
        reviews = review_results.get(prepared.strategy, {})
        snapshot = replace(
            pipeline._engine.finalize_snapshot(
                prepared,
                reviews,
                projection_stage=projection_stage,
            ),
            config_version=pipeline._config_version,
        )
        pipeline._state.record_strategy_latency(
            prepared.strategy,
            round((time.perf_counter() - context.started_at) * 1000.0, 3),
        )
        admission_started = time.perf_counter()
        if not admit_snapshot_to_p6(pipeline, snapshot):
            pipeline._latency.record_duration(
                "p6_admission",
                (time.perf_counter() - admission_started) * 1000.0,
            )
            continue
        pipeline._latency.record_duration(
            "p6_admission",
            (time.perf_counter() - admission_started) * 1000.0,
        )
        pipeline._state.publish(snapshot)
        _save_checkpoint_if_due(pipeline, snapshot, context.now)
        pipeline._session_snapshot_ids.add(snapshot.snapshot_id)
        sse_started = time.perf_counter()
        pipeline._publisher.publish(snapshot)
        pipeline._latency.record_duration(
            "sse_enqueue",
            (time.perf_counter() - sse_started) * 1000.0,
        )
        snapshots.append(snapshot)
    return tuple(snapshots)


def _save_checkpoint_if_due(
    pipeline: RecommendationPipeline,
    snapshot: RecommendationSnapshot,
    now: datetime,
) -> None:
    if snapshot.strategy is Strategy.LONG:
        return
    local = shanghai_now(now)
    if snapshot.strategy is Strategy.TODAY:
        boundary = local.replace(hour=11, minute=20, second=0, microsecond=0)
    else:
        boundary = local.replace(hour=14, minute=50, second=0, microsecond=0)
    if 0 <= (boundary - local).total_seconds() <= 10:
        try:
            persist(
                pipeline,
                pipeline._snapshot_writer.save_checkpoint,
                snapshot,
                boundary_at=boundary,
            )
        except Exception as exc:
            pipeline._state.increment("checkpoint_save_failures")
            pipeline._state.record_error(
                f"{snapshot.strategy.value} checkpoint persistence degraded: {type(exc).__name__}"
            )


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
