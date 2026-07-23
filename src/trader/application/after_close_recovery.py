"""Recover missing same-day recommendations from P6 or closing market data."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import TYPE_CHECKING

from trader.application.candidate_features import bind_strategy_input_version, read_strategy_features
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
from trader.domain.recommendation.scoring_support import MIN_BOARD_SAMPLE, reliability_fields

if TYPE_CHECKING:
    from trader.application.pipeline import RecommendationPipeline
    from trader.application.recommendation_finalization import PreparedSnapshot

_SHORT_STRATEGIES = (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
_MINIMUM_CLOSE_TIME = time(14, 59)


@dataclass(frozen=True)
class _CloseSnapshotRequest:
    strategy: Strategy
    features: Sequence[FeatureSnapshot]
    data_version: str
    market_features: Sequence[FeatureSnapshot]
    codes: Sequence[str]
    reasons: Mapping[str, int]
    details: Sequence[FilterAudit]
    now: datetime
    max_age: float


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

        published.extend(_refresh_after_close_long(pipeline, local, deadline=deadline))

        complete = not _restore_existing(pipeline, trade_date) and _after_close_long_ready(pipeline, trade_date)
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
    validation_at = max(now, shanghai_now(pipeline._now()))
    close_counts, history_counts = _close_market_readiness(market_features, validation_at)
    if not all(count >= MIN_BOARD_SAMPLE for count in history_counts.values()):
        pipeline._state.increment("after_close_incomplete_market")
        pipeline._state.record_error(
            "after-close market recovery waiting for complete three-board close quotes and history: "
            f"close_quotes={_format_board_counts(close_counts)}; "
            f"history={_format_board_counts(history_counts)}"
        )
        return ()

    max_age = _close_max_age_seconds(validation_at)
    candidates, reasons, details = _preselect_close(
        pipeline,
        market_features,
        strategies,
        validation_at,
        max_age,
    )
    store_candidate_selection(pipeline, market_features, candidates, reasons, details)

    prepared: list[RecommendationSnapshot] = []
    codes = tuple(feature.quote.code for feature in candidates)
    for strategy in strategies:
        try:
            candidate_features, data_version = _strategy_close_features(pipeline, strategy, codes, validation_at)
            strategy_validation_at = max(validation_at, shanghai_now(pipeline._now()))
            if codes and not _complete_requested_close_features(candidate_features, codes, strategy_validation_at):
                raise MarketDataUnavailableError(f"{strategy.value} closing candidate quotes are incomplete")
            snapshot = _build_local_close_snapshot(
                pipeline,
                _CloseSnapshotRequest(
                    strategy,
                    candidate_features,
                    data_version,
                    market_features,
                    codes,
                    reasons,
                    details,
                    strategy_validation_at,
                    _close_max_age_seconds(strategy_validation_at),
                ),
            )
        except (MarketDataUnavailableError, OSError, RuntimeError, TypeError, ValueError) as exc:
            pipeline._state.increment("after_close_strategy_failures")
            pipeline._state.record_error(f"{strategy.value} close rebuild degraded: {str(exc)[:400]}")
            continue
        prepared.append(snapshot)

    committed: list[RecommendationSnapshot] = []
    for snapshot in prepared:
        if not _commit_fallback(pipeline, snapshot):
            continue
        close_by_code = {item.features.quote.code: item.features for item in snapshot.recommendations}
        _save_closing_overlay(pipeline, snapshot, close_by_code, snapshot.published_at)
        committed.append(snapshot)
    return tuple(committed)


def _build_local_close_snapshot(
    pipeline: RecommendationPipeline,
    request: _CloseSnapshotRequest,
) -> RecommendationSnapshot:
    strategy = request.strategy
    features = request.features
    now = request.now
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
            data_version=request.data_version,
            review_deadline=now,
            max_age_seconds=request.max_age,
            filtered_count=len({item.stock_code for item in request.details}),
            filter_reasons=request.reasons,
            filter_details=tuple(request.details),
            market_features=tuple(request.market_features),
            requested_codes=tuple(request.codes),
            preselect_max_age_seconds=request.max_age,
            candidate_pool_size=pipeline._candidate_pool_size,
        ).result()
    else:
        prepared = pipeline._engine.prepare_snapshot(
            strategy,
            tuple(features),
            now=now,
            phase="close_fallback",
            trade_date=trade_date_at(now).isoformat(),
            data_version=request.data_version,
            review_deadline=now,
            max_age_seconds=request.max_age,
            filtered_count=len({item.stock_code for item in request.details}),
            filter_reasons=request.reasons,
            filter_details=tuple(request.details),
            market_features=tuple(request.market_features),
            requested_codes=tuple(request.codes),
            preselect_max_age_seconds=request.max_age,
            candidate_pool_size=pipeline._candidate_pool_size,
        )
    if not prepared.board_scoring_complete:
        raise RuntimeError("three-board scoring is incomplete")
    blocking_reasons = tuple(
        reason for reason in prepared.board_degraded_reasons if reason.endswith("board_population_insufficient")
    )
    if blocking_reasons:
        raise RuntimeError(
            "three-board scoring is not ready: "
            f"{','.join(blocking_reasons)}; "
            f"reliability={_format_reliability_diagnostics(pipeline, prepared)}"
        )
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


def _format_reliability_diagnostics(
    pipeline: RecommendationPipeline,
    prepared: PreparedSnapshot,
) -> str:
    diagnostics: list[str] = []
    for batch in prepared.board_batches:
        policy = pipeline._engine._policy.board_policy(prepared.strategy, batch.board)
        if policy is None or not batch.recommendations:
            diagnostics.append(f"{batch.board.value}:none")
            continue
        best = max(
            batch.recommendations,
            key=lambda item: item.features.board_data_reliability,
        )
        fields = reliability_fields(
            prepared.strategy,
            policy.local_weights,
            phase=prepared.phase,
        )
        missing = tuple(name for name in fields if best.features.optional_value(name) is None)
        diagnostics.append(
            f"{batch.board.value}:{best.features.board_data_reliability:.3f}:"
            f"{best.features.quote.code}:{'|'.join(missing) or 'complete'}"
        )
    return ",".join(diagnostics)


def _refresh_after_close_long(
    pipeline: RecommendationPipeline,
    now: datetime,
    *,
    deadline: datetime | None,
) -> tuple[RecommendationSnapshot, ...]:
    trade_date = trade_date_at(now).isoformat()
    if _after_close_long_ready(pipeline, trade_date):
        return ()
    codes = pipeline._long_codes
    if not codes:
        return ()
    try:
        close_features = _fetch_close_candidates(pipeline, codes, now, deadline=deadline)
        close_by_code = {feature.quote.code: feature for feature in close_features}
        candidate_features, data_version = _strategy_close_features(pipeline, Strategy.LONG, codes, now)
        candidate_by_code = {feature.quote.code: feature for feature in candidate_features}
        missing = tuple(code for code in codes if code not in close_by_code or code not in candidate_by_code)
        if missing:
            raise MarketDataUnavailableError("long closing watchlist quotes are incomplete: " + ",".join(missing[:8]))
        features = tuple(_feature_with_close(candidate_by_code[code], close_by_code[code]) for code in codes)
        snapshot = _build_long_close_snapshot(pipeline, features, data_version, now)
    except (MarketDataUnavailableError, OSError, RuntimeError, TypeError, ValueError) as exc:
        pipeline._state.increment("after_close_long_failures")
        pipeline._state.record_error(f"long close rebuild degraded: {str(exc)[:400]}")
        return ()
    if not admit_snapshot_to_p6(pipeline, snapshot):
        return ()
    pipeline._state.publish(snapshot)
    pipeline._session_snapshot_ids.add(snapshot.snapshot_id)
    pipeline._publisher.publish(snapshot)
    pipeline._state.increment("after_close_long_recovered")
    return (snapshot,)


def _after_close_long_ready(pipeline: RecommendationPipeline, trade_date: str) -> bool:
    if not pipeline._long_codes:
        return True
    snapshot = pipeline._state.latest(Strategy.LONG)
    return snapshot is not None and snapshot.trade_date == trade_date and bool(snapshot.recommendations)


def _feature_with_close(base: FeatureSnapshot, close_feature: FeatureSnapshot) -> FeatureSnapshot:
    close = close_feature.quote
    original = base.quote
    quote = replace(
        original,
        price=close.price,
        pct_change=close.pct_change,
        source=close.source,
        source_time=close.source_time,
        received_time=close.received_time,
        data_version=close.data_version,
    )
    return replace(base, quote=quote, observed_at=close_feature.observed_at)


def _build_long_close_snapshot(
    pipeline: RecommendationPipeline,
    features: Sequence[FeatureSnapshot],
    data_version: str,
    now: datetime,
) -> RecommendationSnapshot:
    max_age = _close_max_age_seconds(now)
    if pipeline._persistence_running:
        prepared = submit_required(
            pipeline,
            pipeline._long_pool,
            pipeline._engine.prepare_snapshot,
            Strategy.LONG,
            tuple(features),
            now=now,
            phase="close_fallback",
            trade_date=trade_date_at(now).isoformat(),
            data_version=data_version,
            review_deadline=now,
            max_age_seconds=max_age,
            filtered_count=0,
            filter_reasons={},
            filter_details=(),
            target_prices=pipeline._long_target_prices,
            market_features=tuple(features),
            requested_codes=tuple(pipeline._long_codes),
            preselect_max_age_seconds=max_age,
            candidate_pool_size=pipeline._candidate_pool_size,
        ).result()
    else:
        prepared = pipeline._engine.prepare_snapshot(
            Strategy.LONG,
            tuple(features),
            now=now,
            phase="close_fallback",
            trade_date=trade_date_at(now).isoformat(),
            data_version=data_version,
            review_deadline=now,
            max_age_seconds=max_age,
            filtered_count=0,
            filter_reasons={},
            filter_details=(),
            target_prices=pipeline._long_target_prices,
            market_features=tuple(features),
            requested_codes=tuple(pipeline._long_codes),
            preselect_max_age_seconds=max_age,
            candidate_pool_size=pipeline._candidate_pool_size,
        )
    snapshot = pipeline._engine.finalize_snapshot(prepared, {})
    recommendations = snapshot.recommendations
    close_anchors = _close_anchors(recommendations)
    close_version = _close_data_version(data_version, close_anchors)
    return replace(
        snapshot,
        snapshot_id=_snapshot_id(
            Strategy.LONG,
            snapshot.trade_date,
            "close_fallback",
            close_version,
            snapshot.published_at,
        ),
        data_version=close_version,
        config_version=pipeline._config_version,
        frozen=False,
        metadata={
            **snapshot.metadata,
            "recovery_path": "after_close_current",
            "price_basis": "official_close",
            "deepseek_mode": "local_only",
            "close_anchors": close_anchors,
        },
    )


def _fetch_close_market(
    pipeline: RecommendationPipeline,
    now: datetime,
    *,
    deadline: datetime | None,
) -> tuple[FeatureSnapshot, ...]:
    cached = tuple(pipeline._market_features)
    if cached and _cached_close_market_is_reusable(pipeline, cached, now):
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


def _cached_close_market_is_reusable(
    pipeline: RecommendationPipeline,
    features: Sequence[FeatureSnapshot],
    now: datetime,
) -> bool:
    if not all(_valid_close_feature(feature, now) for feature in features):
        return False
    last_error = str(pipeline._state.snapshot().get("last_error") or "")
    if any(
        reason in last_error
        for reason in (
            "board_data_reliability_below_threshold",
            "complete three-board close quotes and history",
            "board_population_insufficient",
        )
    ):
        return False
    _, history_counts = _close_market_readiness(features, now)
    return all(count >= MIN_BOARD_SAMPLE for count in history_counts.values())


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
    validation_at = max(now, shanghai_now(pipeline._now()))
    return tuple(feature for feature in features if _valid_close_feature(feature, validation_at))


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
            read_strategy_features,
            pipeline._candidate_data,
            strategy,
            tuple(codes),
            now,
        ).result()
    return read_strategy_features(pipeline._candidate_data, strategy, tuple(codes), now)


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


def _close_market_readiness(
    features: Sequence[FeatureSnapshot],
    now: datetime,
) -> tuple[dict[Board, int], dict[Board, int]]:
    close_by_board = {Board.MAIN: 0, Board.CHINEXT: 0, Board.STAR: 0}
    history_by_board = {Board.MAIN: 0, Board.CHINEXT: 0, Board.STAR: 0}
    for feature in features:
        board = board_for_snapshot(feature)
        if board not in close_by_board or not _valid_close_feature(feature, now):
            continue
        close_by_board[board] += 1
        amount_median = feature.optional_value("amount_median_20d")
        if (
            feature.history_days >= 20
            and amount_median is not None
            and math.isfinite(amount_median)
            and amount_median > 0.0
        ):
            history_by_board[board] += 1
    return close_by_board, history_by_board


def _format_board_counts(counts: Mapping[Board, int]) -> str:
    return ",".join(f"{board.value}:{counts[board]}" for board in (Board.MAIN, Board.CHINEXT, Board.STAR))


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
