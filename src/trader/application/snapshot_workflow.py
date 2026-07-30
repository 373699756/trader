"""Snapshot scoring, freezing and live-overlay workflow operations."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Protocol, cast

from trader.application.after_close_recovery import recover_after_close_snapshots
from trader.application.cadence import SchedulePointResult
from trader.application.candidate_features import fetch_strategy_features
from trader.application.freeze_attempts import FreezeAttempt, FreezeAttemptKey, FreezeAttemptStore
from trader.application.long_quotes import refresh_long_quotes
from trader.application.official_records import official_snapshot
from trader.application.pipeline_market_tasks import _refresh_intraday_tail_before_score
from trader.application.pipeline_review_updates import review_enabled_for_strategy_phase
from trader.application.pipeline_stages import (
    maximum_age_seconds,
    persist,
    remember_candidate_selection,
    review_deadline,
    strategies_for_phase,
    strategy_requires_scoring,
)
from trader.application.pipeline_workers import submit_required_urgent
from trader.application.ports.market import MarketDataUnavailableError
from trader.application.ports.reviews import DeepSeekReviewPort
from trader.application.recommendation_finalization import PreparedSnapshot
from trader.application.schedule import MarketPhase, SchedulePoint, shanghai_now, trade_date_at
from trader.application.snapshot_publication import admit_snapshot_to_p6
from trader.domain.market.models import LiveQuote, MarketQuote
from trader.domain.recommendation.models import (
    LiveOverlay,
    RecommendationSnapshot,
    Strategy,
)
from trader.domain.review.models import DeepSeekReview

if TYPE_CHECKING:
    from trader.application.pipeline import RecommendationPipeline

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _OverlayTarget:
    strategy: Strategy
    snapshot: RecommendationSnapshot
    codes: tuple[str, ...]
    existing: LiveOverlay | None


class _FreezeCommitOutcome(str, Enum):
    COMMITTED = "committed"
    RETRY = "retry"
    CONFLICT = "conflict"


class _FreezeAttemptOwner(Protocol):
    _freeze_attempts: FreezeAttemptStore


def process_schedule(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    freeze_strategies: Sequence[str],
) -> tuple[RecommendationSnapshot, ...]:
    snapshots: list[RecommendationSnapshot] = []
    trade_date = trade_date_at(now).isoformat()
    if phase in {
        MarketPhase.WARMUP,
        MarketPhase.TODAY_OBSERVE,
        MarketPhase.TODAY_MAIN,
        MarketPhase.TODAY_LATE,
        MarketPhase.MIDDAY,
        MarketPhase.AFTERNOON,
        MarketPhase.FINAL_REVIEW,
        MarketPhase.FINAL_QUOTE,
    }:
        refresh_candidates(pipeline, now, phase)
    if phase is MarketPhase.WARMUP and pipeline._reviews is not None and pipeline._candidate_features:
        pipeline._reviews.preheat(
            pipeline._candidate_features,
            phase=phase.value,
            deadline=shanghai_now(now).replace(hour=9, minute=30, second=0, microsecond=0),
        )
    _refresh_intraday_tail_before_score(pipeline, now, phase, on_workers=False)

    for strategy in strategies_for_phase(phase):
        if not strategy_requires_scoring(pipeline, strategy, phase, trade_date):
            continue
        if (strategy, trade_date) in pipeline._frozen_keys or pipeline._state.is_frozen(strategy, trade_date):
            continue
        snapshot = score_strategy(pipeline, strategy, now, phase, trade_date)
        if snapshot is not None:
            snapshots.append(snapshot)
    snapshots.extend(refresh_long_quotes(pipeline, now, phase))

    snapshots.extend(freeze_available_snapshots(pipeline, now, freeze_strategies))
    if phase is MarketPhase.AFTER_CLOSE:
        snapshots.extend(recover_after_close_snapshots(pipeline, now))
        refresh_live_overlays(pipeline, now, phase)
        pipeline._settle_outcomes(now)
    elif phase is MarketPhase.FROZEN:
        refresh_live_overlays(pipeline, now, phase)
    return tuple(snapshots)


def freeze_available_snapshots(
    pipeline: RecommendationPipeline,
    now: datetime,
    freeze_strategies: Sequence[str],
) -> tuple[RecommendationSnapshot, ...]:
    snapshots: list[RecommendationSnapshot] = []
    trade_date = trade_date_at(now).isoformat()
    attempts = _freeze_attempt_store(pipeline)
    for raw_strategy in freeze_strategies:
        strategy = Strategy(raw_strategy)
        if _restore_or_retry_frozen(pipeline, attempts, strategy, trade_date, now):
            continue
        attempt = _ensure_freeze_attempt(pipeline, attempts, strategy, trade_date, now)
        if attempt is None:
            continue
        committed = _commit_freeze_attempt(pipeline, attempts, now, attempt)
        snapshots.extend(committed)
    return tuple(snapshots)


def _restore_or_retry_frozen(
    pipeline: RecommendationPipeline,
    attempts: FreezeAttemptStore,
    strategy: Strategy,
    trade_date: str,
    now: datetime,
) -> bool:
    key = FreezeAttemptKey(strategy.value, trade_date)
    if (strategy, trade_date) in pipeline._frozen_keys or pipeline._state.is_frozen(strategy, trade_date):
        attempts.mark_completed(key)
        _record_freeze_result(pipeline, strategy, trade_date, SchedulePointResult.COMPLETED, now)
        return True
    restored = _restore_existing_frozen(pipeline, strategy, trade_date)
    if restored is True:
        attempts.mark_completed(key)
        _record_freeze_result(pipeline, strategy, trade_date, SchedulePointResult.COMPLETED, now)
        return True
    if restored is None:
        return False
    if attempts.get(key) is None:
        _seal_retryable_snapshot(pipeline, attempts, strategy, trade_date, now)
    _retry_freeze_attempt(pipeline, attempts, strategy, trade_date, now)
    return True


def _seal_retryable_snapshot(
    pipeline: RecommendationPipeline,
    attempts: FreezeAttemptStore,
    strategy: Strategy,
    trade_date: str,
    now: datetime,
) -> None:
    try:
        prepared = _prepare_frozen_snapshot(pipeline, strategy, trade_date, now)
    except (OSError, RuntimeError, ValueError) as exc:
        pipeline._state.record_error(f"{strategy.value} freeze preparation degraded: {type(exc).__name__}")
        return
    if prepared is not None:
        frozen, boundary = prepared
        attempts.seal_snapshot(
            strategy=strategy.value,
            trade_date=trade_date,
            boundary_at=boundary,
            frozen_snapshot=frozen,
        )


def _ensure_freeze_attempt(
    pipeline: RecommendationPipeline,
    attempts: FreezeAttemptStore,
    strategy: Strategy,
    trade_date: str,
    now: datetime,
) -> FreezeAttempt | None:
    key = FreezeAttemptKey(strategy.value, trade_date)
    attempt = attempts.get(key)
    if attempt is not None:
        return attempt
    try:
        prepared = _prepare_frozen_snapshot(pipeline, strategy, trade_date, now)
    except (OSError, RuntimeError, ValueError) as exc:
        pipeline._state.record_error(f"{strategy.value} freeze preparation degraded: {type(exc).__name__}")
        _record_freeze_result(pipeline, strategy, trade_date, SchedulePointResult.RETRY, now)
        return None
    if prepared is None:
        attempts.mark_missed(key)
        _record_freeze_result(pipeline, strategy, trade_date, SchedulePointResult.MISSED, now)
        return None
    frozen, boundary = prepared
    try:
        return attempts.seal_snapshot(
            strategy=strategy.value,
            trade_date=trade_date,
            boundary_at=boundary,
            frozen_snapshot=frozen,
        )
    except ValueError:
        pipeline._state.record_error(f"{strategy.value} freeze conflict: sealed object changed")
        attempts.mark_missed(key)
        _record_freeze_result(pipeline, strategy, trade_date, SchedulePointResult.MISSED, now)
        return None


def _commit_freeze_attempt(
    pipeline: RecommendationPipeline,
    attempts: FreezeAttemptStore,
    now: datetime,
    attempt: FreezeAttempt,
) -> tuple[RecommendationSnapshot, ...]:
    strategy = Strategy(attempt.strategy)
    trade_date = attempt.trade_date
    sealed_snapshot = attempt.frozen_snapshot
    if not isinstance(sealed_snapshot, RecommendationSnapshot):
        pipeline._state.record_error(f"{strategy.value} freeze attempt contains an invalid snapshot")
        attempts.mark_missed(attempt.key)
        _record_freeze_result(pipeline, strategy, trade_date, SchedulePointResult.MISSED, now)
        return ()
    commit_outcome, committed = _commit_frozen_snapshot(
        pipeline,
        sealed_snapshot,
        (strategy, trade_date),
        attempt.boundary_at,
    )
    if commit_outcome is _FreezeCommitOutcome.RETRY:
        _retry_freeze_attempt(pipeline, attempts, strategy, trade_date, now)
        return ()
    if commit_outcome is _FreezeCommitOutcome.CONFLICT:
        attempts.mark_missed(attempt.key)
        _record_freeze_result(pipeline, strategy, trade_date, SchedulePointResult.MISSED, now)
        return ()
    attempts.mark_completed(attempt.key)
    _record_freeze_result(pipeline, strategy, trade_date, SchedulePointResult.COMPLETED, now)
    return committed


def _prepare_frozen_snapshot(
    pipeline: RecommendationPipeline,
    strategy: Strategy,
    trade_date: str,
    now: datetime,
) -> tuple[RecommendationSnapshot, datetime] | None:
    boundary = _freeze_boundary(now, strategy)
    current = pipeline._state.latest(strategy)
    if current is None or current.trade_date != trade_date:
        current = pipeline._snapshot_writer.load_checkpoint(strategy, trade_date, boundary_at=boundary)
    error = _freeze_snapshot_error(current, trade_date, boundary)
    if error:
        if error == "no current pre-cutoff snapshot":
            pipeline._state.increment("freeze_missing_pre_cutoff_snapshot")
            return None
        pipeline._state.record_error(f"{strategy.value} freeze unavailable: {error}")
        return None
    assert current is not None
    current = official_snapshot(current)
    maximum_age = 20.0 if strategy is Strategy.TODAY else 30.0
    anchors, invalid_quotes = _freeze_anchors(current, boundary, maximum_age)
    if invalid_quotes:
        pipeline._state.record_error(
            f"{strategy.value} freeze unavailable: quote age outside 0-{maximum_age:.0f}s at cutoff "
            + ",".join(invalid_quotes)
        )
        return None
    return (
        replace(
            current,
            frozen=True,
            published_at=boundary,
            config_version=pipeline._config_version,
            metadata={**current.metadata, "freeze_anchor": anchors},
        ),
        boundary,
    )


def _freeze_snapshot_error(
    current: RecommendationSnapshot | None,
    trade_date: str,
    boundary: datetime,
) -> str:
    if current is None or current.trade_date != trade_date:
        return "no current pre-cutoff snapshot"
    if current.published_at > boundary:
        return "latest snapshot is after cutoff"
    if (boundary - current.published_at).total_seconds() > 30:
        return "latest snapshot is stale at cutoff"
    return ""


def _freeze_anchors(
    snapshot: RecommendationSnapshot,
    boundary: datetime,
    maximum_age: float,
) -> tuple[dict[str, object], list[str]]:
    anchors: dict[str, object] = {}
    invalid_quotes: list[str] = []
    for recommendation in snapshot.recommendations:
        quote = recommendation.features.quote
        age = (boundary - quote.source_time).total_seconds()
        anchors[quote.code] = {
            "source": quote.source,
            "source_time": quote.source_time.isoformat(),
            "age_seconds": round(age, 3),
        }
        if age < 0.0 or age > maximum_age:
            invalid_quotes.append(f"{quote.code}:{age:.3f}")
    return anchors, invalid_quotes


def _restore_existing_frozen(
    pipeline: RecommendationPipeline,
    strategy: Strategy,
    trade_date: str,
) -> bool | None:
    try:
        if trade_date not in pipeline._repository.recommendation_dates(strategy):
            return None
        existing = pipeline._repository.load_frozen(strategy, trade_date)
    except (OSError, RuntimeError, ValueError) as exc:
        pipeline._state.record_error(f"{strategy.value} frozen record recovery degraded: {type(exc).__name__}")
        return False
    if existing is None:
        pipeline._state.record_error(f"{strategy.value} frozen manifest exists without a readable snapshot")
        return False
    if admit_snapshot_to_p6(pipeline, existing):
        pipeline._state.restore_snapshot(existing)
        pipeline._state.restore_frozen(strategy, trade_date)
        pipeline._frozen_keys.add((strategy, trade_date))
        return True
    return False


def _commit_frozen_snapshot(
    pipeline: RecommendationPipeline,
    frozen: RecommendationSnapshot,
    key: tuple[Strategy, str],
    boundary: datetime,
) -> tuple[_FreezeCommitOutcome, tuple[RecommendationSnapshot, ...]]:
    try:
        persist(pipeline, pipeline._snapshot_writer.freeze, frozen)
    except AssertionError as exc:
        pipeline._state.record_error(f"{frozen.strategy.value} freeze conflict: {str(exc)[:350]}")
        return _FreezeCommitOutcome.CONFLICT, ()
    except TypeError as exc:
        pipeline._state.record_error(f"{frozen.strategy.value} freeze serialization invalid: {str(exc)[:350]}")
        return _FreezeCommitOutcome.CONFLICT, ()
    except (OSError, RuntimeError, ValueError) as exc:
        pipeline._state.increment("freeze_persistence_retryable")
        pipeline._state.record_error(f"{frozen.strategy.value} freeze persistence degraded: {type(exc).__name__}")
        return _FreezeCommitOutcome.RETRY, ()
    if not admit_snapshot_to_p6(pipeline, frozen):
        pipeline._state.increment("freeze_p6_retryable")
        return _FreezeCommitOutcome.RETRY, ()
    pipeline._state.mark_frozen(frozen)
    pipeline._publisher.publish(frozen)
    pipeline._frozen_keys.add(key)
    try:
        persist(
            pipeline,
            pipeline._snapshot_writer.consume_checkpoint,
            frozen.strategy,
            frozen.trade_date,
            boundary_at=boundary,
        )
    except Exception as exc:
        pipeline._state.increment("checkpoint_consume_failures")
        pipeline._state.record_error(f"{frozen.strategy.value} checkpoint cleanup degraded: {type(exc).__name__}")
    return _FreezeCommitOutcome.COMMITTED, (frozen,)


def _freeze_attempt_store(pipeline: RecommendationPipeline) -> FreezeAttemptStore:
    current = getattr(pipeline, "_freeze_attempts", None)
    if isinstance(current, FreezeAttemptStore):
        return current
    created = FreezeAttemptStore()
    cast(_FreezeAttemptOwner, pipeline)._freeze_attempts = created
    return created


def _retry_freeze_attempt(
    pipeline: RecommendationPipeline,
    attempts: FreezeAttemptStore,
    strategy: Strategy,
    trade_date: str,
    now: datetime,
) -> None:
    key = FreezeAttemptKey(strategy.value, trade_date)
    if attempts.get(key) is not None:
        attempts.retry(key, at=now)
    _record_freeze_result(pipeline, strategy, trade_date, SchedulePointResult.RETRY, now)


def _record_freeze_result(
    pipeline: RecommendationPipeline,
    strategy: Strategy,
    trade_date: str,
    result: SchedulePointResult,
    at: datetime,
) -> None:
    planner = pipeline._cadence
    if planner is None:
        return
    planner.record_point_result(
        trade_date,
        SchedulePoint.TODAY_FREEZE if strategy is Strategy.TODAY else SchedulePoint.AFTERNOON_FREEZE,
        strategy.value,
        result,
        at=at,
    )


def refresh_live_overlays(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    *,
    deadline: datetime | None = None,
) -> None:
    trade_date = trade_date_at(now).isoformat()
    active = _active_overlay_targets(pipeline, trade_date)
    if not active:
        return
    requested = tuple(dict.fromkeys(code for target in active for code in target.codes))
    fetched_quotes = _fetch_overlay_quotes(pipeline, requested, now, deadline)
    if fetched_quotes is None:
        return
    for target in active:
        _publish_overlay_update(pipeline, target, fetched_quotes, now, phase)


def _active_overlay_targets(
    pipeline: RecommendationPipeline,
    trade_date: str,
) -> tuple[_OverlayTarget, ...]:
    active: list[_OverlayTarget] = []
    for strategy in Strategy:
        if strategy is Strategy.LONG:
            continue
        snapshot = pipeline._state.latest(strategy)
        if snapshot is None or snapshot.trade_date != trade_date:
            continue
        key = (strategy, trade_date)
        existing = pipeline._live_overlays.get(key)
        if existing is None:
            existing = pipeline._repository.load_live_overlay(strategy, trade_date)
        if existing is not None and existing.snapshot_id != snapshot.snapshot_id:
            existing = None
        if existing is not None and existing.closing:
            pipeline._live_overlays[key] = existing
            continue
        codes = tuple(item.features.quote.code for item in snapshot.recommendations)
        if not codes:
            continue
        active.append(_OverlayTarget(strategy, snapshot, codes, existing))
    return tuple(active)


def _fetch_overlay_quotes(
    pipeline: RecommendationPipeline,
    requested: tuple[str, ...],
    now: datetime,
    deadline: datetime | None,
) -> Mapping[str, MarketQuote] | None:
    try:
        if pipeline._persistence_running and not pipeline._market_data_manages_workers:
            fetched = submit_required_urgent(
                pipeline,
                pipeline._data_pool,
                pipeline._quotes.refresh_candidate_quotes,
                requested,
                now,
                deadline=deadline,
            ).result()
        else:
            fetched = pipeline._quotes.refresh_candidate_quotes(requested, now, deadline=deadline)
        features = tuple(fetched)
    except (MarketDataUnavailableError, OSError, RuntimeError, ValueError) as exc:
        pipeline._state.record_error(f"TopK live overlay degraded: {str(exc)[:500]}")
        return None
    return {feature.quote.code: feature.quote for feature in features}


def _publish_overlay_update(
    pipeline: RecommendationPipeline,
    target: _OverlayTarget,
    fetched_quotes: Mapping[str, MarketQuote],
    now: datetime,
    phase: MarketPhase,
) -> None:
    quotes = dict(target.existing.quotes) if target.existing is not None else {}
    updated_codes: set[str] = set()
    for code in target.codes:
        quote = fetched_quotes.get(code)
        if quote is None:
            continue
        if quote.source_time > now or quote.price is None or quote.price <= 0:
            continue
        quotes[code] = LiveQuote(
            code=code,
            price=quote.price,
            pct_change=quote.pct_change,
            source=quote.source,
            source_time=quote.source_time,
            received_time=quote.received_time,
            data_version=quote.data_version,
        )
        updated_codes.add(code)
    if not updated_codes:
        return
    overlay = LiveOverlay(
        snapshot_id=target.snapshot.snapshot_id,
        strategy=target.strategy,
        trade_date=target.snapshot.trade_date,
        version=_overlay_version(target.snapshot.snapshot_id, now, quotes),
        observed_at=now,
        quotes=quotes,
        closing=phase is MarketPhase.AFTER_CLOSE and updated_codes == set(target.codes),
    )
    if overlay.closing and target.snapshot.frozen:
        saved = persist(pipeline, pipeline._snapshot_writer.save_live_overlay, overlay)
        if not saved:
            existing = pipeline._repository.load_live_overlay(target.strategy, target.snapshot.trade_date)
            if existing is None or existing.snapshot_id != target.snapshot.snapshot_id or not existing.closing:
                pipeline._state.record_error(f"{target.strategy.value} closing overlay persistence failed")
                return
            overlay = existing
    pipeline._live_overlays[(target.strategy, target.snapshot.trade_date)] = overlay
    pipeline._state.publish_overlay(overlay)
    pipeline._published_snapshots.publish_overlay(overlay)
    pipeline._publisher.publish_overlay(overlay)


def refresh_candidates(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
) -> None:
    try:
        market_features = tuple(pipeline._market_full.fetch_market_features(now))
    except MarketDataUnavailableError as exc:
        reason = str(exc)[:500]
        _LOGGER.warning("candidate refresh degraded during %s: %s", phase.value, reason)
        pipeline._state.increment("market_refresh_failures")
        pipeline._state.record_error(f"market data degraded during {phase.value}: {reason}")
        return

    candidates, reasons, details = pipeline._engine.preselect(
        market_features,
        now=now,
        max_age_seconds=maximum_age_seconds(phase),
        limit=pipeline._candidate_pool_size,
        strategies=tuple(strategy for strategy in strategies_for_phase(phase) if strategy is not Strategy.LONG),
        trade_date=trade_date_at(now).isoformat(),
        phase=phase.value,
    )
    remember_candidate_selection(pipeline, market_features, candidates, reasons, details)


def score_strategy(
    pipeline: RecommendationPipeline,
    strategy: Strategy,
    now: datetime,
    phase: MarketPhase,
    trade_date: str,
) -> RecommendationSnapshot | None:
    if strategy is Strategy.LONG:
        raise ValueError("long uses the quote projection lane and cannot enter strategy scoring")
    scoring_started = time.perf_counter()
    codes = pipeline._candidate_codes
    if not codes:
        return None
    features, data_version = fetch_strategy_features(pipeline._candidate_data, strategy, codes, now)
    if not features:
        return None
    deadline = review_deadline(now, phase)
    review_port = pipeline._reviews if phase not in {MarketPhase.DEEPSEEK_CUTOFF, MarketPhase.FINAL_QUOTE} else None
    prepared = pipeline._engine.prepare_snapshot(
        strategy,
        features,
        now=now,
        phase=phase.value,
        trade_date=trade_date,
        data_version=data_version,
        review_deadline=deadline,
        max_age_seconds=maximum_age_seconds(phase, strategy),
        filtered_count=pipeline._filtered_count,
        filter_reasons=pipeline._filter_reasons,
        filter_details=pipeline._filter_details,
        target_prices=None,
        long_groups=(),
        market_features=pipeline._market_features,
        requested_codes=codes,
        preselect_max_age_seconds=maximum_age_seconds(phase),
        candidate_pool_size=pipeline._candidate_pool_size,
    )
    if not prepared.board_scoring_complete:
        reasons = prepared.board_degraded_reasons or ("board_scoring_incomplete",)
        pipeline._state.increment("board_scoring_incomplete")
        pipeline._state.record_strategy_degraded(strategy, reasons)
        pipeline._state.record_error(
            f"{strategy.value} board scoring degraded; retained latest complete snapshot: " + ",".join(reasons)[:350]
        )
        return None
    local_snapshot = _decorate_live_snapshot(
        pipeline,
        pipeline._engine.finalize_snapshot(prepared, {}, projection_stage="local"),
    )
    if not _publish_live_snapshot(pipeline, local_snapshot, now):
        return None
    reviews = _review_local_snapshot(
        pipeline,
        prepared,
        review_port if review_enabled_for_strategy_phase(strategy, phase) else None,
    )
    snapshot = local_snapshot
    if reviews:
        hybrid_snapshot = _decorate_live_snapshot(
            pipeline,
            pipeline._engine.finalize_snapshot(
                prepared,
                reviews,
                projection_stage="hybrid",
            ),
        )
        if _publish_live_snapshot(pipeline, hybrid_snapshot, now):
            snapshot = hybrid_snapshot
    pipeline._state.record_strategy_latency(
        strategy,
        round((time.perf_counter() - scoring_started) * 1000.0, 3),
    )
    return snapshot


def _review_local_snapshot(
    pipeline: RecommendationPipeline,
    prepared: PreparedSnapshot,
    review_port: DeepSeekReviewPort | None,
) -> Mapping[str, DeepSeekReview]:
    if review_port is None or not prepared.review_eligible or prepared.strategy is Strategy.LONG:
        return {}
    try:
        return review_port.review(
            prepared.strategy,
            prepared.review_eligible,
            phase=prepared.phase,
            deadline=prepared.review_deadline,
            contexts=pipeline._engine.review_contexts(prepared),
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        pipeline._state.record_error(f"{prepared.strategy.value} DeepSeek review degraded: {str(exc)[:350]}")
        return {}


def _decorate_live_snapshot(
    pipeline: RecommendationPipeline,
    snapshot: RecommendationSnapshot,
) -> RecommendationSnapshot:
    snapshot = replace(snapshot, config_version=pipeline._config_version)
    market_metadata = pipeline._market_metadata.snapshot_metadata(
        tuple(item.features.quote.code for item in snapshot.recommendations)
    )
    if not market_metadata.merge_epoch:
        return snapshot
    return replace(
        snapshot,
        metadata={**snapshot.metadata, **market_metadata.to_json()},
        degraded_reasons=tuple(dict.fromkeys((*snapshot.degraded_reasons, *market_metadata.degraded_reasons))),
    )


def _publish_live_snapshot(
    pipeline: RecommendationPipeline,
    snapshot: RecommendationSnapshot,
    now: datetime,
) -> bool:
    if not admit_snapshot_to_p6(pipeline, snapshot):
        return False
    pipeline._state.publish(snapshot)
    save_checkpoint_if_due(pipeline, snapshot, now)
    pipeline._session_snapshot_ids.add(snapshot.snapshot_id)
    pipeline._publisher.publish(snapshot)
    return True


def _freeze_boundary(now: datetime, strategy: Strategy) -> datetime:
    local = shanghai_now(now)
    if strategy is Strategy.TODAY:
        return local.replace(hour=11, minute=20, second=0, microsecond=0)
    return local.replace(hour=14, minute=50, second=0, microsecond=0)


def save_checkpoint_if_due(
    pipeline: RecommendationPipeline,
    snapshot: RecommendationSnapshot,
    now: datetime,
) -> None:
    if snapshot.strategy is Strategy.LONG:
        return
    boundary = _freeze_boundary(now, snapshot.strategy)
    seconds_to_boundary = (boundary - shanghai_now(now)).total_seconds()
    if 0 <= seconds_to_boundary <= 10:
        try:
            persist(
                pipeline,
                pipeline._snapshot_writer.save_checkpoint,
                official_snapshot(snapshot),
                boundary_at=boundary,
            )
        except Exception as exc:
            pipeline._state.increment("checkpoint_save_failures")
            pipeline._state.record_error(
                f"{snapshot.strategy.value} checkpoint persistence degraded: {type(exc).__name__}"
            )


def _overlay_version(snapshot_id: str, observed_at: datetime, quotes: Mapping[str, LiveQuote]) -> str:
    values = [snapshot_id, observed_at.isoformat()]
    for code, quote in sorted(quotes.items()):
        values.extend(
            (code, quote.data_version, quote.source_time.isoformat(), str(quote.price), str(quote.pct_change))
        )
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()[:24]


__all__ = [
    "freeze_available_snapshots",
    "process_schedule",
    "refresh_live_overlays",
    "save_checkpoint_if_due",
]
