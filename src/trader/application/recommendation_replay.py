"""Deterministic frozen recommendation replay mixin."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from trader.application.recommendation_support import (
    _business_projection,
    _RecordedReviewPort,
    _restore_policy,
)
from trader.domain.filters import hard_filter, legacy_v14_hard_filter
from trader.domain.models import Recommendation, RecommendationSnapshot

REPLAY_SCHEMA_VERSION = "recommendation_replay_v1"
LEGACY_REPLAY_ALGORITHM_VERSION = "engine_v10_section9_hard_filter_2026_07"
REPLAY_ALGORITHM_VERSION = "engine_v15_parallel_market_data_2026_07"
_SUPPORTED_REPLAY_ALGORITHMS = frozenset({LEGACY_REPLAY_ALGORITHM_VERSION, REPLAY_ALGORITHM_VERSION})


class RecommendationReplayMixin:
    def replay(self: Any, snapshot: RecommendationSnapshot) -> RecommendationSnapshot:
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
        replay_engine = cast(Any, type(self))(
            _restore_policy(replay_input.policy),
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
            ),
        )

    @classmethod
    def verify_frozen(cls, snapshot: RecommendationSnapshot) -> Mapping[str, object]:
        replay_input = snapshot.replay_input
        if replay_input is None:
            raise ValueError("snapshot does not contain replay input")
        engine = cast(Callable[..., Any], cls)(
            _restore_policy(replay_input.policy),
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
            _restore_policy(replay_input.policy),
            hard_filter_function=(
                legacy_v14_hard_filter
                if replay_input.algorithm_version == LEGACY_REPLAY_ALGORITHM_VERSION
                else hard_filter
            ),
        )
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
        replayed = self.replay(snapshot)
        expected = _business_projection(snapshot)
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
