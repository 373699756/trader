"""Snapshot scoring, freezing and live-overlay workflow operations."""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from trader.application.candidate_features import fetch_strategy_features
from trader.application.pipeline_stages import (
    maximum_age_seconds,
    persist,
    review_deadline,
    store_candidate_selection,
    strategies_for_phase,
)
from trader.application.pipeline_workers import submit_required_urgent
from trader.application.ports import MarketDataUnavailable
from trader.application.schedule import MarketPhase, shanghai_now, trade_date_at
from trader.application.status import RuntimeState
from trader.domain.models import LiveOverlay, LiveQuote, RecommendationSnapshot, Strategy

if TYPE_CHECKING:
    from trader.application.pipeline import RecommendationPipeline

_LOGGER = logging.getLogger(__name__)


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

    for strategy in strategies_for_phase(phase):
        if (strategy, trade_date) in pipeline._frozen_keys or pipeline._state.is_frozen(strategy, trade_date):
            continue
        snapshot = score_strategy(pipeline, strategy, now, phase, trade_date)
        if snapshot is not None:
            snapshots.append(snapshot)

    snapshots.extend(freeze_available_snapshots(pipeline, now, freeze_strategies))
    if phase in {MarketPhase.FROZEN, MarketPhase.AFTER_CLOSE}:
        refresh_live_overlays(pipeline, now, phase)
    return tuple(snapshots)


def freeze_available_snapshots(
    pipeline: RecommendationPipeline,
    now: datetime,
    freeze_strategies: Sequence[str],
) -> tuple[RecommendationSnapshot, ...]:
    snapshots: list[RecommendationSnapshot] = []
    trade_date = trade_date_at(now).isoformat()
    for raw_strategy in freeze_strategies:
        strategy = Strategy(raw_strategy)
        key = (strategy, trade_date)
        if key in pipeline._frozen_keys or pipeline._state.is_frozen(strategy, trade_date):
            continue

        if trade_date in pipeline._repository.recommendation_dates(strategy):
            pipeline._state.restore_frozen(strategy, trade_date)
            continue

        current = pipeline._state.latest(strategy)
        if current is None or current.trade_date != trade_date:
            fallback = pipeline._repository.latest(strategy)
            if fallback is None or fallback.trade_date != trade_date:
                pipeline._state.record_error(f"{strategy.value} freeze unavailable: no current pre-cutoff snapshot")
                continue
            current = fallback
            pipeline._state.restore_snapshot(current)

        if current is None or current.trade_date != trade_date:
            pipeline._state.record_error(f"{strategy.value} freeze unavailable: no current pre-cutoff snapshot")
            continue
        boundary = _freeze_boundary(now, strategy)
        if current.published_at > boundary:
            pipeline._state.record_error(f"{strategy.value} freeze unavailable: latest snapshot is after cutoff")
            continue
        if (boundary - current.published_at).total_seconds() > 30:
            pipeline._state.record_error(f"{strategy.value} freeze unavailable: latest snapshot is stale at cutoff")
            continue
        maximum_age = 20.0 if strategy is Strategy.TODAY else 30.0
        anchors: dict[str, object] = {}
        invalid_quotes: list[str] = []
        for recommendation in current.recommendations:
            quote = recommendation.features.quote
            age = (boundary - quote.source_time).total_seconds()
            anchors[quote.code] = {
                "source": quote.source,
                "source_time": quote.source_time.isoformat(),
                "age_seconds": round(age, 3),
            }
            if age < 0.0 or age > maximum_age:
                invalid_quotes.append(f"{quote.code}:{age:.3f}")
        if invalid_quotes:
            pipeline._state.record_error(
                f"{strategy.value} freeze unavailable: quote age outside 0-{maximum_age:.0f}s at cutoff "
                + ",".join(invalid_quotes)
            )
            continue
        frozen = replace(
            current,
            frozen=True,
            published_at=boundary,
            config_version=pipeline._config_version,
            metadata={**current.metadata, "freeze_anchor": anchors},
        )
        persist(pipeline, pipeline._repository.freeze, frozen)
        pipeline._state.mark_frozen(frozen)
        pipeline._publisher.publish(frozen)
        pipeline._frozen_keys.add(key)
        snapshots.append(frozen)
    return tuple(snapshots)


def refresh_live_overlays(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    *,
    deadline: datetime | None = None,
) -> None:
    trade_date = trade_date_at(now).isoformat()
    active: list[tuple[Strategy, RecommendationSnapshot, tuple[str, ...], LiveOverlay | None]] = []
    all_codes: list[str] = []
    for strategy in Strategy:
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
        active.append((strategy, snapshot, codes, existing))
        all_codes.extend(codes)
    if not active:
        return

    requested = tuple(dict.fromkeys(all_codes))
    try:
        if pipeline._persistence_running and not pipeline._market_data_manages_workers:
            fetched = submit_required_urgent(
                pipeline,
                pipeline._data_pool,
                pipeline._market_data.refresh_candidate_quotes,
                requested,
                now,
                deadline=deadline,
            ).result()
        else:
            fetched = pipeline._market_data.refresh_candidate_quotes(requested, now, deadline=deadline)
        features = tuple(fetched)
    except (MarketDataUnavailable, OSError, RuntimeError, ValueError) as exc:
        pipeline._state.record_error(f"TopK live overlay degraded: {str(exc)[:500]}")
        return
    fetched_quotes = {feature.quote.code: feature.quote for feature in features}

    for strategy, snapshot, codes, existing in active:
        key = (strategy, trade_date)
        quotes = dict(existing.quotes) if existing is not None else {}
        allowed = set(codes)
        updated_codes: set[str] = set()
        for code in codes:
            quote = fetched_quotes.get(code)
            if quote is None or quote.source_time > now or quote.price is None or quote.price <= 0:
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
            continue
        overlay = LiveOverlay(
            snapshot_id=snapshot.snapshot_id,
            strategy=strategy,
            trade_date=trade_date,
            version=_overlay_version(snapshot.snapshot_id, now, quotes),
            observed_at=now,
            quotes=quotes,
            closing=phase is MarketPhase.AFTER_CLOSE and updated_codes == allowed,
        )
        if not persist(pipeline, pipeline._repository.save_live_overlay, overlay):
            persisted = pipeline._repository.load_live_overlay(strategy, trade_date)
            if persisted is not None and persisted.snapshot_id == snapshot.snapshot_id:
                pipeline._live_overlays[key] = persisted
            continue
        pipeline._live_overlays[key] = overlay
        pipeline._publisher.publish_overlay(overlay)


def refresh_candidates(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
) -> None:
    try:
        market_features = tuple(pipeline._market_data.fetch_market_features(now))
    except MarketDataUnavailable as exc:
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
    )
    store_candidate_selection(pipeline, market_features, candidates, reasons, details)


def score_strategy(
    pipeline: RecommendationPipeline,
    strategy: Strategy,
    now: datetime,
    phase: MarketPhase,
    trade_date: str,
) -> RecommendationSnapshot | None:
    scoring_started = time.perf_counter()
    codes = pipeline._long_codes if strategy is Strategy.LONG else pipeline._candidate_codes
    if not codes:
        return None
    features, data_version = fetch_strategy_features(pipeline._market_data, strategy, codes, now)
    if not features:
        return None
    deadline = review_deadline(now, phase)
    review_port = pipeline._reviews if phase not in {MarketPhase.DEEPSEEK_CUTOFF, MarketPhase.FINAL_QUOTE} else None
    is_long = strategy is Strategy.LONG
    snapshot = pipeline._engine.build_snapshot(
        strategy,
        features,
        now=now,
        phase=phase.value,
        trade_date=trade_date,
        data_version=data_version,
        review_port=review_port,
        review_deadline=deadline,
        max_age_seconds=maximum_age_seconds(phase, strategy),
        filtered_count=0 if is_long else pipeline._filtered_count,
        filter_reasons={} if is_long else pipeline._filter_reasons,
        filter_details=() if is_long else pipeline._filter_details,
        target_prices=pipeline._long_target_prices if strategy is Strategy.LONG else None,
        market_features=pipeline._market_features,
        requested_codes=codes,
        preselect_max_age_seconds=maximum_age_seconds(phase),
        candidate_pool_size=pipeline._candidate_pool_size,
    )
    snapshot = replace(snapshot, config_version=pipeline._config_version)
    metadata_provider = getattr(pipeline._market_data, "snapshot_metadata", None)
    market_metadata = (
        metadata_provider(tuple(item.features.quote.code for item in snapshot.recommendations))
        if callable(metadata_provider)
        else {}
    )
    if isinstance(market_metadata, Mapping):
        market_degraded = market_metadata.get("market_degraded_reasons")
        extra_degraded = (
            tuple(str(reason) for reason in market_degraded if isinstance(reason, str))
            if isinstance(market_degraded, (list, tuple))
            else ()
        )
        snapshot = replace(
            snapshot,
            metadata={**snapshot.metadata, **dict(market_metadata)},
            degraded_reasons=tuple(dict.fromkeys((*snapshot.degraded_reasons, *extra_degraded))),
        )
    pipeline._state.record_strategy_latency(
        strategy,
        round((time.perf_counter() - scoring_started) * 1000.0, 3),
    )
    persist(pipeline, pipeline._repository.publish, snapshot)
    pipeline._state.publish(snapshot)
    pipeline._publisher.publish(snapshot)
    return snapshot


def topk_quote_age(
    state: RuntimeState,
    overlays: Mapping[tuple[Strategy, str], LiveOverlay],
    now: datetime,
    *,
    target_seconds: float = 10.0,
) -> Mapping[str, object]:
    per_strategy: dict[str, object] = {}
    active_ages: list[float] = []
    excluded_frozen: list[str] = []
    for strategy in Strategy:
        snapshot = state.latest(strategy)
        if snapshot is None:
            continue
        overlay = overlays.get((strategy, snapshot.trade_date))
        if overlay is not None and overlay.snapshot_id == snapshot.snapshot_id:
            ages = [quote.age_seconds(now) for quote in overlay.quotes.values()]
        elif snapshot.frozen:
            excluded_frozen.append(strategy.value)
            continue
        else:
            ages = [item.features.quote.age_seconds(now) for item in snapshot.recommendations]
        active_ages.extend(ages)
        per_strategy[strategy.value] = _age_summary(ages)
    return {
        "target_seconds": target_seconds,
        **_age_summary(active_ages, target_seconds=target_seconds),
        "per_strategy": per_strategy,
        "excluded_frozen_strategies": sorted(excluded_frozen),
        "measured_at": now.isoformat(),
    }


def _freeze_boundary(now: datetime, strategy: Strategy) -> datetime:
    local = shanghai_now(now)
    if strategy is Strategy.TODAY:
        return local.replace(hour=11, minute=20, second=0, microsecond=0)
    return local.replace(hour=14, minute=50, second=0, microsecond=0)


def _overlay_version(snapshot_id: str, observed_at: datetime, quotes: Mapping[str, LiveQuote]) -> str:
    values = [snapshot_id, observed_at.isoformat()]
    for code, quote in sorted(quotes.items()):
        values.extend(
            (code, quote.data_version, quote.source_time.isoformat(), str(quote.price), str(quote.pct_change))
        )
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()[:24]


def _age_summary(ages: Sequence[float], *, target_seconds: float = 10.0) -> dict[str, object]:
    if not ages:
        return {
            "sample_count": 0,
            "p50_seconds": None,
            "p95_seconds": None,
            "maximum_seconds": None,
            "meets_target": None,
        }
    ordered = sorted(max(0.0, float(age)) for age in ages)
    p50_index = max(0, math.ceil(len(ordered) * 0.50) - 1)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    p50 = round(ordered[p50_index], 3)
    p95 = round(ordered[p95_index], 3)
    return {
        "sample_count": len(ordered),
        "p50_seconds": p50,
        "p95_seconds": p95,
        "maximum_seconds": round(ordered[-1], 3),
        "meets_target": p95 <= target_seconds,
    }


__all__ = [
    "freeze_available_snapshots",
    "process_schedule",
    "refresh_live_overlays",
    "topk_quote_age",
]
