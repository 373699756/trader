"""Recover missing same-day recommendations from P6 or closing market data."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, time
from typing import TYPE_CHECKING

from trader.application.candidate_features import bind_strategy_input_version, fetch_strategy_features
from trader.application.pipeline_workers import data_future, persist, store_candidate_selection, submit_required
from trader.application.ports.market import MarketDataUnavailableError
from trader.application.recommendation_support import _snapshot_id
from trader.application.schedule import shanghai_now, trade_date_at
from trader.application.snapshot_publication import admit_snapshot_to_p6
from trader.domain.market.models import (
    Board,
    FeatureSnapshot,
    LiveQuote,
)
from trader.domain.recommendation.filters import board_for_snapshot
from trader.domain.recommendation.models import (
    FilterAudit,
    LiveOverlay,
    Recommendation,
    RecommendationSnapshot,
    Strategy,
)

if TYPE_CHECKING:
    from trader.application.pipeline import RecommendationPipeline

_SHORT_STRATEGIES = (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
_MINIMUM_CLOSE_TIME = time(14, 59)


def recover_after_close_snapshots(
    pipeline: RecommendationPipeline,
    now: datetime,
    *,
    deadline: datetime | None = None,
) -> tuple[RecommendationSnapshot, ...]:
    """Create only missing same-day records after the market has closed."""

    local = shanghai_now(now)
    trade_date = trade_date_at(local).isoformat()
    if local.time().replace(tzinfo=None) < time(15, 0):
        return ()

    with pipeline._after_close_lock:
        missing = _restore_existing(pipeline, trade_date)
        if not missing:
            pipeline._record_after_close_recovery(local, complete=True)
            return ()

        published: list[RecommendationSnapshot] = []
        runtime_sources = {
            strategy: snapshot
            for strategy in missing
            if (snapshot := pipeline._state.latest(strategy)) is not None
            and snapshot.trade_date == trade_date
            and not snapshot.frozen
            and snapshot.snapshot_id in pipeline._session_snapshot_ids
        }
        if runtime_sources:
            published.extend(_persist_runtime_results(pipeline, runtime_sources, local, deadline=deadline))

        missing = _restore_existing(pipeline, trade_date)
        rebuild = tuple(strategy for strategy in missing if strategy not in runtime_sources)
        if rebuild:
            published.extend(_rebuild_from_close(pipeline, rebuild, local, deadline=deadline))

        complete = not _restore_existing(pipeline, trade_date)
        pipeline._record_after_close_recovery(local, complete=complete)
        return tuple(published)


def _restore_existing(pipeline: RecommendationPipeline, trade_date: str) -> tuple[Strategy, ...]:
    missing: list[Strategy] = []
    for strategy in _SHORT_STRATEGIES:
        snapshot = pipeline._repository.load_frozen(strategy, trade_date)
        if snapshot is None:
            missing.append(strategy)
            continue
        if not admit_snapshot_to_p6(pipeline, snapshot):
            missing.append(strategy)
            continue
        pipeline._frozen_keys.add((strategy, trade_date))
        pipeline._state.restore_snapshot(snapshot)
        pipeline._state.restore_frozen(strategy, trade_date)
    return tuple(missing)


def _persist_runtime_results(
    pipeline: RecommendationPipeline,
    sources: Mapping[Strategy, RecommendationSnapshot],
    now: datetime,
    *,
    deadline: datetime | None,
) -> tuple[RecommendationSnapshot, ...]:
    codes = tuple(
        dict.fromkeys(item.features.quote.code for snapshot in sources.values() for item in snapshot.recommendations)
    )
    close_features = _fetch_close_candidates(pipeline, codes, now, deadline=deadline) if codes else ()
    close_by_code = {feature.quote.code: feature for feature in close_features}
    published: list[RecommendationSnapshot] = []
    for strategy, source in sources.items():
        strategy_codes = tuple(item.features.quote.code for item in source.recommendations)
        if any(code not in close_by_code for code in strategy_codes):
            pipeline._state.record_error(f"{strategy.value} close recovery waiting for complete P6 closing quotes")
            continue
        fallback = _runtime_close_snapshot(source, close_by_code, now, pipeline._config_version)
        if not _commit_fallback(pipeline, fallback):
            continue
        _save_closing_overlay(pipeline, fallback, close_by_code, now)
        published.append(fallback)
    return tuple(published)


def _runtime_close_snapshot(
    source: RecommendationSnapshot,
    close_by_code: Mapping[str, FeatureSnapshot],
    now: datetime,
    config_version: str,
) -> RecommendationSnapshot:
    recommendations = tuple(
        _recommendation_with_close(item, close_by_code[item.features.quote.code]) for item in source.recommendations
    )
    close_anchors = _close_anchors(recommendations)
    close_version = _close_data_version(source.data_version, close_anchors)
    return replace(
        source,
        snapshot_id=_snapshot_id(
            source.strategy, source.trade_date, "close_fallback", close_version, source.published_at
        ),
        phase="close_fallback",
        data_version=close_version,
        published_at=now,
        recommendations=recommendations,
        config_version=config_version,
        frozen=True,
        metadata={
            **source.metadata,
            "source_snapshot_id": source.snapshot_id,
            "source_data_version": source.data_version,
            "scoring_phase": source.phase,
            "recovery_path": "p6",
            "price_basis": "official_close",
            "deepseek_mode": "preserved",
            "close_anchors": close_anchors,
        },
    )


def _recommendation_with_close(item: Recommendation, close_feature: FeatureSnapshot) -> Recommendation:
    close = close_feature.quote
    original = item.features.quote
    quote = replace(
        original,
        price=close.price,
        pct_change=close.pct_change,
        source=close.source,
        source_time=close.source_time,
        received_time=close.received_time,
        data_version=close.data_version,
    )
    return replace(item, features=replace(item.features, quote=quote, observed_at=close_feature.observed_at))


def _rebuild_from_close(
    pipeline: RecommendationPipeline,
    strategies: Sequence[Strategy],
    now: datetime,
    *,
    deadline: datetime | None,
) -> tuple[RecommendationSnapshot, ...]:
    try:
        market_features = _fetch_close_market(pipeline, now, deadline=deadline)
    except (MarketDataUnavailableError, OSError, RuntimeError, TypeError, ValueError) as exc:
        pipeline._state.increment("after_close_rebuild_failures")
        pipeline._state.record_error(f"after-close market recovery degraded: {str(exc)[:400]}")
        return ()
    if not _complete_close_market(market_features, now):
        pipeline._state.increment("after_close_incomplete_market")
        pipeline._state.record_error("after-close market recovery waiting for a complete three-board close snapshot")
        return ()

    max_age = _close_max_age_seconds(now)
    candidates, reasons, details = _preselect_close(
        pipeline,
        market_features,
        strategies,
        now,
        max_age,
    )
    store_candidate_selection(pipeline, market_features, candidates, reasons, details)

    prepared: list[RecommendationSnapshot] = []
    codes = tuple(feature.quote.code for feature in candidates)
    for strategy in strategies:
        try:
            candidate_features, data_version = _strategy_close_features(pipeline, strategy, codes, now)
            if codes and not _complete_requested_close_features(candidate_features, codes, now):
                raise MarketDataUnavailableError(f"{strategy.value} closing candidate quotes are incomplete")
            snapshot = _build_local_close_snapshot(
                pipeline,
                strategy,
                candidate_features,
                data_version,
                market_features,
                codes,
                reasons,
                details,
                now,
                max_age,
            )
        except (MarketDataUnavailableError, OSError, RuntimeError, TypeError, ValueError) as exc:
            pipeline._state.increment("after_close_strategy_failures")
            pipeline._state.record_error(f"{strategy.value} close rebuild degraded: {str(exc)[:400]}")
            return ()
        prepared.append(snapshot)

    committed: list[RecommendationSnapshot] = []
    for snapshot in prepared:
        if not _commit_fallback(pipeline, snapshot):
            continue
        close_by_code = {item.features.quote.code: item.features for item in snapshot.recommendations}
        _save_closing_overlay(pipeline, snapshot, close_by_code, now)
        committed.append(snapshot)
    return tuple(committed)


def _build_local_close_snapshot(
    pipeline: RecommendationPipeline,
    strategy: Strategy,
    features: Sequence[FeatureSnapshot],
    data_version: str,
    market_features: Sequence[FeatureSnapshot],
    codes: Sequence[str],
    reasons: Mapping[str, int],
    details: Sequence[FilterAudit],
    now: datetime,
    max_age: float,
) -> RecommendationSnapshot:
    if pipeline._persistence_running:
        prepared = submit_required(
            pipeline,
            pipeline._strategy_pool,
            pipeline._engine.prepare_snapshot,
            strategy,
            tuple(features),
            now=now,
            phase="close_fallback",
            trade_date=trade_date_at(now).isoformat(),
            data_version=data_version,
            review_deadline=now,
            max_age_seconds=max_age,
            filtered_count=len({item.stock_code for item in details}),
            filter_reasons=reasons,
            filter_details=tuple(details),
            market_features=tuple(market_features),
            requested_codes=tuple(codes),
            preselect_max_age_seconds=max_age,
            candidate_pool_size=pipeline._candidate_pool_size,
        ).result()
    else:
        prepared = pipeline._engine.prepare_snapshot(
            strategy,
            tuple(features),
            now=now,
            phase="close_fallback",
            trade_date=trade_date_at(now).isoformat(),
            data_version=data_version,
            review_deadline=now,
            max_age_seconds=max_age,
            filtered_count=len({item.stock_code for item in details}),
            filter_reasons=reasons,
            filter_details=tuple(details),
            market_features=tuple(market_features),
            requested_codes=tuple(codes),
            preselect_max_age_seconds=max_age,
            candidate_pool_size=pipeline._candidate_pool_size,
        )
    if not prepared.board_scoring_complete:
        raise RuntimeError("three-board scoring is incomplete")
    snapshot = pipeline._engine.finalize_snapshot(prepared, {})
    recommendations = snapshot.recommendations
    return replace(
        snapshot,
        config_version=pipeline._config_version,
        frozen=True,
        metadata={
            **snapshot.metadata,
            "recovery_path": "full_rebuild",
            "price_basis": "official_close",
            "deepseek_mode": "local_only",
            "close_anchors": _close_anchors(recommendations),
        },
    )


def _fetch_close_market(
    pipeline: RecommendationPipeline,
    now: datetime,
    *,
    deadline: datetime | None,
) -> tuple[FeatureSnapshot, ...]:
    cached = tuple(pipeline._market_features)
    if (
        pipeline._after_close_retry_attempt == 0
        and cached
        and all(_valid_close_feature(feature, now) for feature in cached)
    ):
        return cached
    if pipeline._persistence_running:
        return tuple(
            data_future(
                pipeline,
                pipeline._market_full.fetch_market_features,
                now,
                force=True,
                deadline=deadline,
            ).result()
        )
    return tuple(pipeline._market_full.fetch_market_features(now, force=True, deadline=deadline))


def _fetch_close_candidates(
    pipeline: RecommendationPipeline,
    codes: Sequence[str],
    now: datetime,
    *,
    deadline: datetime | None,
) -> tuple[FeatureSnapshot, ...]:
    if pipeline._persistence_running:
        features = data_future(
            pipeline,
            pipeline._quotes.refresh_candidate_quotes,
            tuple(codes),
            now,
            force=True,
            deadline=deadline,
        ).result()
    else:
        features = pipeline._quotes.refresh_candidate_quotes(
            tuple(codes),
            now,
            force=True,
            deadline=deadline,
        )
    return tuple(feature for feature in features if _valid_close_feature(feature, now))


def _strategy_close_features(
    pipeline: RecommendationPipeline,
    strategy: Strategy,
    codes: Sequence[str],
    now: datetime,
) -> tuple[tuple[FeatureSnapshot, ...], str]:
    if not codes:
        return bind_strategy_input_version(strategy, ())
    if pipeline._persistence_running:
        return data_future(
            pipeline,
            fetch_strategy_features,
            pipeline._candidate_data,
            strategy,
            tuple(codes),
            now,
        ).result()
    return fetch_strategy_features(pipeline._candidate_data, strategy, tuple(codes), now)


def _preselect_close(
    pipeline: RecommendationPipeline,
    market_features: Sequence[FeatureSnapshot],
    strategies: Sequence[Strategy],
    now: datetime,
    max_age: float,
) -> tuple[tuple[FeatureSnapshot, ...], Mapping[str, int], tuple[FilterAudit, ...]]:
    if pipeline._persistence_running:
        return submit_required(
            pipeline,
            pipeline._normalization_pool,
            pipeline._engine.preselect,
            tuple(market_features),
            now=now,
            max_age_seconds=max_age,
            limit=pipeline._candidate_pool_size,
            strategies=tuple(strategies),
            trade_date=trade_date_at(now).isoformat(),
            phase="close_fallback",
        ).result()
    return pipeline._engine.preselect(
        tuple(market_features),
        now=now,
        max_age_seconds=max_age,
        limit=pipeline._candidate_pool_size,
        strategies=tuple(strategies),
        trade_date=trade_date_at(now).isoformat(),
        phase="close_fallback",
    )


def _commit_fallback(pipeline: RecommendationPipeline, snapshot: RecommendationSnapshot) -> bool:
    if pipeline._repository.load_frozen(snapshot.strategy, snapshot.trade_date) is not None:
        return False
    persist(pipeline, pipeline._snapshot_writer.freeze, snapshot)
    if not admit_snapshot_to_p6(pipeline, snapshot):
        return False
    pipeline._frozen_keys.add((snapshot.strategy, snapshot.trade_date))
    pipeline._state.mark_frozen(snapshot)
    pipeline._publisher.publish(snapshot)
    pipeline._state.increment("after_close_recommendations_recovered")
    return True


def _save_closing_overlay(
    pipeline: RecommendationPipeline,
    snapshot: RecommendationSnapshot,
    close_by_code: Mapping[str, FeatureSnapshot],
    now: datetime,
) -> None:
    selected_codes = {item.features.quote.code for item in snapshot.recommendations}
    quotes = {
        code: LiveQuote(
            code=code,
            price=feature.quote.price,
            pct_change=feature.quote.pct_change,
            source=feature.quote.source,
            source_time=feature.quote.source_time,
            received_time=feature.quote.received_time,
            data_version=feature.quote.data_version,
        )
        for code, feature in close_by_code.items()
        if code in selected_codes
    }
    if not quotes:
        return
    overlay = LiveOverlay(
        snapshot_id=snapshot.snapshot_id,
        strategy=snapshot.strategy,
        trade_date=snapshot.trade_date,
        version=_close_data_version(snapshot.snapshot_id, _close_anchors(snapshot.recommendations)),
        observed_at=now,
        quotes=quotes,
        closing=len(quotes) == len(snapshot.recommendations),
    )
    if persist(pipeline, pipeline._snapshot_writer.save_live_overlay, overlay):
        pipeline._live_overlays[(snapshot.strategy, snapshot.trade_date)] = overlay
        pipeline._state.publish_overlay(overlay)
        pipeline._published_snapshots.publish_overlay(overlay)
        pipeline._publisher.publish_overlay(overlay)


def _complete_close_market(features: Sequence[FeatureSnapshot], now: datetime) -> bool:
    boards = {board_for_snapshot(feature) for feature in features if _valid_close_feature(feature, now)}
    return {Board.MAIN, Board.CHINEXT, Board.STAR} <= boards


def _complete_requested_close_features(
    features: Sequence[FeatureSnapshot],
    requested: Sequence[str],
    now: datetime,
) -> bool:
    valid_codes = {feature.quote.code for feature in features if _valid_close_feature(feature, now)}
    return set(requested) <= valid_codes


def _valid_close_feature(feature: FeatureSnapshot, now: datetime) -> bool:
    quote = feature.quote
    price = quote.price
    source = shanghai_now(quote.source_time)
    return bool(
        source.date() == trade_date_at(now)
        and source.time().replace(tzinfo=None) >= _MINIMUM_CLOSE_TIME
        and source <= shanghai_now(now)
        and price is not None
        and math.isfinite(price)
        and price > 0.0
    )


def _close_max_age_seconds(now: datetime) -> float:
    local = shanghai_now(now)
    boundary = local.replace(hour=15, minute=0, second=0, microsecond=0)
    return max(30.0, (local - boundary).total_seconds() + 60.0)


def _close_anchors(recommendations: Sequence[Recommendation]) -> dict[str, object]:
    return {
        item.features.quote.code: {
            "price": item.features.quote.price,
            "pct_change": item.features.quote.pct_change,
            "source": item.features.quote.source,
            "source_time": item.features.quote.source_time.isoformat(),
            "received_time": item.features.quote.received_time.isoformat(),
            "data_version": item.features.quote.data_version,
        }
        for item in recommendations
    }


def _close_data_version(prefix: str, anchors: Mapping[str, object]) -> str:
    material = "|".join((prefix, *(f"{code}:{anchors[code]}" for code in sorted(anchors))))
    return "close:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


__all__ = ["recover_after_close_snapshots"]
