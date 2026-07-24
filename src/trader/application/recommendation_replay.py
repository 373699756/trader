"""Deterministic frozen recommendation replay mixin."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, cast

from trader.application.policy import RecommendationPolicy
from trader.application.recommendation_support import (
    _business_projection,
    _RecordedReviewPort,
    _restore_policy,
)
from trader.domain.recommendation.filters import hard_filter, legacy_v14_hard_filter
from trader.domain.recommendation.models import (
    Recommendation,
    RecommendationReplayInput,
    RecommendationSnapshot,
)

REPLAY_SCHEMA_VERSION = "recommendation_replay_v1"
LEGACY_REPLAY_ALGORITHM_VERSION = "engine_v10_section9_hard_filter_2026_07"
V15_REPLAY_ALGORITHM_VERSION = "engine_v15_parallel_market_data_2026_07"
V16_REPLAY_ALGORITHM_VERSION = "engine_v16_board_scoring_ttd25_2026_07"
V17_REPLAY_ALGORITHM_VERSION = "engine_v17_downside_guard_ttd25_2026_07"
V18_REPLAY_ALGORITHM_VERSION = "engine_v18_score_first_risk_history_2026_07"
V19_REPLAY_ALGORITHM_VERSION = "engine_v19_bounded_review_2026_07"
REPLAY_ALGORITHM_VERSION = "engine_v20_review28_2026_07"
_SUPPORTED_REPLAY_ALGORITHMS = frozenset(
    {
        LEGACY_REPLAY_ALGORITHM_VERSION,
        V15_REPLAY_ALGORITHM_VERSION,
        V16_REPLAY_ALGORITHM_VERSION,
        V17_REPLAY_ALGORITHM_VERSION,
        V18_REPLAY_ALGORITHM_VERSION,
        V19_REPLAY_ALGORITHM_VERSION,
        REPLAY_ALGORITHM_VERSION,
    }
)


class RecommendationReplayMixin:
    def replay(self: Any, snapshot: RecommendationSnapshot) -> RecommendationSnapshot:
        replay_input = _validated_replay_input(snapshot)
        replay_engine = cast(Any, type(self))(
            _replay_policy(replay_input),
            hard_filter_function=(
                legacy_v14_hard_filter
                if replay_input.algorithm_version == LEGACY_REPLAY_ALGORITHM_VERSION
                else hard_filter
            ),
        )
        if snapshot.strategy_version != replay_input.policy.strategy_version:
            raise ValueError("snapshot strategy version does not match its frozen replay policy")
        if snapshot.fusion_version != replay_input.policy.fusion_version:
            raise ValueError("snapshot fusion version does not match its frozen replay policy")

        legacy_replay = "projection_stage" not in snapshot.metadata
        if replay_input.algorithm_version in {
            V16_REPLAY_ALGORITHM_VERSION,
            V17_REPLAY_ALGORITHM_VERSION,
            V18_REPLAY_ALGORITHM_VERSION,
            V19_REPLAY_ALGORITHM_VERSION,
            REPLAY_ALGORITHM_VERSION,
        }:
            recorded_codes = replay_input.requested_codes
            candidate_codes = tuple(feature.quote.code for feature in replay_input.candidate_features)
            if candidate_codes != recorded_codes:
                raise ValueError("board-scored frozen candidate features do not reproduce the targeted candidate pool")
            return cast(
                RecommendationSnapshot,
                replay_engine.finalize_snapshot(
                    replay_engine.prepare_frozen_board_replay(
                        snapshot.strategy,
                        replay_input,
                        phase=snapshot.phase,
                        trade_date=snapshot.trade_date,
                        data_version=snapshot.data_version,
                        filtered_count=snapshot.filtered_count,
                        filter_reasons=snapshot.filter_reasons,
                        filter_details=snapshot.filter_details,
                    ),
                    replay_input.reviews,
                    legacy_v16=replay_input.algorithm_version == V16_REPLAY_ALGORITHM_VERSION,
                    legacy_replay=legacy_replay,
                    projection_stage=str(snapshot.metadata.get("projection_stage") or "hybrid"),
                ),
            )

        candidates, filter_reasons, filter_details = replay_engine.preselect(
            replay_input.market_features,
            now=replay_input.evaluated_at,
            max_age_seconds=replay_input.preselect_max_age_seconds,
            limit=replay_input.candidate_pool_size,
        )
        expected_codes = tuple(feature.quote.code for feature in candidates)
        recorded_codes = replay_input.requested_codes
        if expected_codes != recorded_codes:
            raise ValueError("frozen market universe does not reproduce the targeted candidate pool")

        return cast(
            RecommendationSnapshot,
            replay_engine.build_snapshot(
                snapshot.strategy,
                replay_input.candidate_features,
                now=replay_input.evaluated_at,
                phase=snapshot.phase,
                trade_date=snapshot.trade_date,
                data_version=snapshot.data_version,
                review_port=_RecordedReviewPort(replay_input.reviews),
                review_deadline=replay_input.evaluated_at,
                max_age_seconds=replay_input.score_max_age_seconds,
                filtered_count=len({item.stock_code for item in filter_details}),
                filter_reasons=filter_reasons,
                filter_details=filter_details,
                target_prices=replay_input.target_prices,
                market_features=replay_input.market_features,
                requested_codes=replay_input.requested_codes,
                preselect_max_age_seconds=replay_input.preselect_max_age_seconds,
                candidate_pool_size=replay_input.candidate_pool_size,
                legacy_replay=legacy_replay,
            ),
        )

    @classmethod
    def verify_frozen(cls, snapshot: RecommendationSnapshot) -> Mapping[str, object]:
        replay_input = snapshot.replay_input
        if replay_input is None:
            raise ValueError("snapshot does not contain replay input")
        engine = cast(Callable[..., Any], cls)(
            _replay_policy(replay_input),
            hard_filter_function=(
                legacy_v14_hard_filter
                if replay_input.algorithm_version == LEGACY_REPLAY_ALGORITHM_VERSION
                else hard_filter
            ),
        )
        return cast(Mapping[str, object], engine.verify_replay(snapshot))

    @classmethod
    def replay_candidates(cls, snapshot: RecommendationSnapshot) -> tuple[Recommendation, ...]:
        if not snapshot.frozen:
            raise ValueError("only frozen snapshots can provide threshold-report candidates")
        cls.verify_frozen(snapshot)
        replay_input = snapshot.replay_input
        if replay_input is None:
            raise ValueError("snapshot does not contain replay input")
        engine = cast(Callable[..., Any], cls)(
            _replay_policy(replay_input),
            hard_filter_function=(
                legacy_v14_hard_filter
                if replay_input.algorithm_version == LEGACY_REPLAY_ALGORITHM_VERSION
                else hard_filter
            ),
        )
        if replay_input.algorithm_version in {
            V17_REPLAY_ALGORITHM_VERSION,
            V18_REPLAY_ALGORITHM_VERSION,
            V19_REPLAY_ALGORITHM_VERSION,
            REPLAY_ALGORITHM_VERSION,
        }:
            merged, _mode = engine._merge_reviewed_candidates(
                snapshot.strategy,
                tuple(item for batch in replay_input.board_batches for item in batch.recommendations),
                replay_input.reviews,
                now=replay_input.evaluated_at,
                phase=snapshot.phase,
                max_age_seconds=replay_input.score_max_age_seconds,
                target_prices=replay_input.target_prices,
                require_industry_breadth=replay_input.algorithm_version == V17_REPLAY_ALGORITHM_VERSION,
            )
            return cast(tuple[Recommendation, ...], merged)

        eligible = tuple(
            feature
            for feature in replay_input.candidate_features
            if engine._hard_filter(
                feature,
                replay_input.evaluated_at,
                max_age_seconds=replay_input.score_max_age_seconds,
                policy=engine._policy.hard_filter,
            ).allowed
        )
        merged, _reviews, _fusion_mode = engine._merge_candidates(
            snapshot.strategy,
            eligible,
            now=replay_input.evaluated_at,
            phase=snapshot.phase,
            review_port=_RecordedReviewPort(replay_input.reviews),
            review_deadline=replay_input.evaluated_at,
            max_age_seconds=replay_input.score_max_age_seconds,
            target_prices=replay_input.target_prices,
        )
        return cast(tuple[Recommendation, ...], merged)

    def verify_replay(self: Any, snapshot: RecommendationSnapshot) -> Mapping[str, object]:
        if not snapshot.frozen:
            raise ValueError("only frozen snapshots can be verified")
        if snapshot.phase == "close_fallback" and snapshot.metadata.get("recovery_path") == "p6":
            source_snapshot = _p6_source_snapshot(snapshot)
            replayed = self.replay(source_snapshot)
            expected_snapshot = _normalize_p6_close_projection(source_snapshot, replayed)
            _verify_close_anchors(snapshot)
        else:
            replayed = self.replay(snapshot)
            expected_snapshot = snapshot
        expected = _business_projection(expected_snapshot)
        actual = _business_projection(replayed)
        if actual != expected:
            raise ValueError("frozen snapshot does not match deterministic replay")
        replay_input = snapshot.replay_input
        if replay_input is None:
            raise ValueError("snapshot does not contain replay input")
        return {
            "status": "verified",
            "snapshot_id": snapshot.snapshot_id,
            "strategy": snapshot.strategy.value,
            "market_input_count": len(replay_input.market_features),
            "candidate_input_count": len(replay_input.candidate_features),
            "recommendation_count": len(snapshot.recommendations),
        }


def _validated_replay_input(snapshot: RecommendationSnapshot) -> RecommendationReplayInput:
    replay_input = snapshot.replay_input
    if replay_input is None:
        raise ValueError("snapshot does not contain replay input")
    if replay_input.schema_version != REPLAY_SCHEMA_VERSION:
        raise ValueError("snapshot replay schema is unsupported")
    if replay_input.algorithm_version not in _SUPPORTED_REPLAY_ALGORITHMS:
        raise ValueError("snapshot replay algorithm is unsupported")
    if not replay_input.market_features:
        raise ValueError("snapshot replay input does not contain the frozen market universe")
    if replay_input.candidate_pool_size < 1:
        raise ValueError("snapshot replay input has an invalid candidate pool size")
    return replay_input


def _replay_policy(replay_input: RecommendationReplayInput) -> RecommendationPolicy:
    policy = _restore_policy(replay_input.policy)
    if replay_input.algorithm_version in {V19_REPLAY_ALGORITHM_VERSION, REPLAY_ALGORITHM_VERSION}:
        return policy
    return replace(policy, selection=replace(policy.selection, review_candidate_limit=0))


def _normalize_p6_close_projection(
    snapshot: RecommendationSnapshot,
    replayed: RecommendationSnapshot,
) -> RecommendationSnapshot:
    replayed_by_code = {item.features.quote.code: item for item in replayed.recommendations}
    if set(replayed_by_code) != {item.features.quote.code for item in snapshot.recommendations}:
        raise ValueError("P6 close fallback changed the selected stock set")
    normalized: list[Recommendation] = []
    for item in snapshot.recommendations:
        original = replayed_by_code[item.features.quote.code]
        normalized.append(
            replace(
                item,
                features=replace(
                    item.features,
                    quote=original.features.quote,
                    observed_at=original.features.observed_at,
                ),
            )
        )
    return replace(snapshot, recommendations=tuple(normalized))


def _p6_source_snapshot(snapshot: RecommendationSnapshot) -> RecommendationSnapshot:
    source_snapshot_id = snapshot.metadata.get("source_snapshot_id")
    source_data_version = snapshot.metadata.get("source_data_version")
    scoring_phase = snapshot.metadata.get("scoring_phase")
    if not isinstance(source_snapshot_id, str) or not source_snapshot_id:
        raise ValueError("P6 close fallback does not identify its source snapshot")
    if not isinstance(source_data_version, str) or not source_data_version:
        raise ValueError("P6 close fallback does not identify its source snapshot")
    if not isinstance(scoring_phase, str) or not scoring_phase:
        raise ValueError("P6 close fallback does not identify its source snapshot")
    return replace(
        snapshot,
        snapshot_id=source_snapshot_id,
        phase=scoring_phase,
        data_version=source_data_version,
    )


def _verify_close_anchors(snapshot: RecommendationSnapshot) -> None:
    anchors = snapshot.metadata.get("close_anchors")
    if not isinstance(anchors, Mapping):
        raise ValueError("P6 close fallback does not contain closing anchors")
    for item in snapshot.recommendations:
        quote = item.features.quote
        raw = anchors.get(quote.code)
        if not isinstance(raw, Mapping):
            raise ValueError(f"P6 close fallback is missing closing anchor for {quote.code}")
        expected = {
            "price": quote.price,
            "pct_change": quote.pct_change,
            "source": quote.source,
            "source_time": quote.source_time.isoformat(),
            "received_time": quote.received_time.isoformat(),
            "data_version": quote.data_version,
        }
        if dict(raw) != expected:
            raise ValueError(f"P6 close fallback anchor does not match recommendation {quote.code}")
