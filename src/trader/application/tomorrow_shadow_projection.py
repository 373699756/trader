"""Project existing point-in-time replay input into tomorrow v2 decisions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.application.policy import RecommendationPolicy
from trader.application.ports.tomorrow import TomorrowNativeInput
from trader.application.recommendation_policy_codec import preselection_replay_feature
from trader.application.tomorrow_deepseek_fusion import (
    normalize_tomorrow_review_times,
    tomorrow_decision_policy,
)
from trader.application.tomorrow_quality import (
    TomorrowInputQuality,
    assess_tomorrow_input_quality,
)
from trader.application.tomorrow_selection import (
    TomorrowSelectionIdentity,
    TomorrowSelectionOptions,
    select_tomorrow_features,
)
from trader.domain.recommendation.models import RecommendationSnapshot, Strategy
from trader.domain.recommendation.tomorrow_fusion import (
    DecisionEpoch,
    TomorrowDecisionRequest,
    build_tomorrow_decision_epoch,
    select_tomorrow_review_candidates,
)
from trader.domain.recommendation.tomorrow_selection import TomorrowSelectionResult
from trader.domain.review.models import DeepSeekReview, ReviewOutcome

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class TomorrowShadowProjection:
    input_version: str
    received_at: datetime
    phase: str
    data_version: str
    local: DecisionEpoch
    hybrid: DecisionEpoch | None
    hard_filter_reason_counts: Mapping[str, int]
    input_quality: TomorrowInputQuality
    selection: TomorrowSelectionResult
    production_candidate_codes: frozenset[str]

    @property
    def effective(self) -> DecisionEpoch:
        return self.hybrid or self.local


def project_tomorrow_snapshot(
    snapshot: RecommendationSnapshot,
    policy: RecommendationPolicy,
    *,
    decision_sequence: int,
) -> TomorrowShadowProjection:
    if snapshot.strategy is not Strategy.TOMORROW:
        raise ValueError("tomorrow shadow projection requires a tomorrow snapshot")
    replay = snapshot.replay_input
    if replay is None:
        raise ValueError("tomorrow shadow projection requires point-in-time replay input")
    return project_tomorrow_input(
        native_input_from_snapshot(snapshot),
        policy,
        decision_sequence=decision_sequence,
        reviews=replay.reviews,
    )


def project_tomorrow_input(
    native_input: TomorrowNativeInput,
    policy: RecommendationPolicy,
    *,
    decision_sequence: int,
    reviews: Mapping[str, DeepSeekReview] | None = None,
) -> TomorrowShadowProjection:
    if decision_sequence < 0:
        raise ValueError("tomorrow shadow decision sequence cannot be negative")
    evaluated_at = _shanghai(native_input.evaluated_at)
    selection = select_tomorrow_features(
        tuple(preselection_replay_feature(feature) for feature in native_input.market_features),
        policy,
        TomorrowSelectionOptions(
            evaluated_at=evaluated_at,
            max_age_seconds=native_input.score_max_age_seconds,
            phase=native_input.phase,
            candidate_features=native_input.candidate_features,
            normalize_discovery_source_time=True,
        ),
        TomorrowSelectionIdentity(
            trade_date=native_input.trade_date,
            data_version=native_input.data_version,
            merge_epoch=native_input.input_version,
        ),
    )
    decision_policy = tomorrow_decision_policy(policy)
    input_quality = assess_tomorrow_input_quality(native_input, selection)
    review_codes = tuple(item.code for item in select_tomorrow_review_candidates(selection, decision_policy))
    input_hash = native_input.input_version.removeprefix("native-input:")
    market_version = f"native-market:{input_hash}"
    candidate_version = f"native-candidate:{input_hash}" if native_input.candidate_features else None
    local = build_tomorrow_decision_epoch(
        TomorrowDecisionRequest(
            selection=selection,
            reviews={},
            observed_at=evaluated_at,
            trade_date=native_input.trade_date,
            sequence=decision_sequence,
            config_version=native_input.config_version,
            strategy_version=policy.strategy_version,
            fusion_version=policy.fusion_version,
            market_epoch_version=market_version,
            candidate_epoch_version=candidate_version,
            research_epoch_version=None,
            projection_stage="local",
            parent_decision_version=None,
            review_candidate_codes=review_codes,
            degraded_reasons=input_quality.degraded_reasons,
            policy=decision_policy,
        )
    )
    normalized = normalize_tomorrow_review_times(
        {code: review for code, review in (reviews or {}).items() if code in set(review_codes)},
        evaluated_at,
    )
    usable = _usable_reviews(normalized or {})
    hybrid = None
    if usable:
        observed_at = max(evaluated_at, *(review.completed_at for review in usable.values()))
        hybrid = build_tomorrow_decision_epoch(
            TomorrowDecisionRequest(
                selection=selection,
                reviews=normalized or {},
                observed_at=observed_at,
                trade_date=native_input.trade_date,
                sequence=decision_sequence + 1,
                config_version=native_input.config_version,
                strategy_version=policy.strategy_version,
                fusion_version=policy.fusion_version,
                market_epoch_version=market_version,
                candidate_epoch_version=candidate_version,
                research_epoch_version=None,
                projection_stage="hybrid",
                parent_decision_version=local.version,
                review_candidate_codes=review_codes,
                degraded_reasons=tuple(
                    sorted(
                        {
                            *input_quality.degraded_reasons,
                            *(() if set(usable) == set(review_codes) else ("deepseek_incomplete",)),
                        }
                    )
                ),
                policy=decision_policy,
            )
        )
    return TomorrowShadowProjection(
        input_version=native_input.input_version,
        received_at=evaluated_at,
        phase=native_input.phase,
        data_version=native_input.data_version,
        local=local,
        hybrid=hybrid,
        hard_filter_reason_counts=selection.hard_filter_reason_counts,
        input_quality=input_quality,
        selection=selection,
        production_candidate_codes=frozenset(feature.quote.code for feature in native_input.candidate_features),
    )


def native_input_from_snapshot(snapshot: RecommendationSnapshot) -> TomorrowNativeInput:
    if snapshot.strategy is not Strategy.TOMORROW:
        raise ValueError("tomorrow native input requires a tomorrow snapshot")
    replay = snapshot.replay_input
    if replay is None:
        raise ValueError("tomorrow native input requires point-in-time replay input")
    return TomorrowNativeInput(
        trade_date=date.fromisoformat(snapshot.trade_date),
        phase=snapshot.phase,
        data_version=snapshot.data_version,
        config_version=snapshot.config_version,
        evaluated_at=replay.evaluated_at,
        market_features=replay.market_features,
        requested_codes=replay.requested_codes,
        candidate_features=replay.candidate_features,
        preselect_max_age_seconds=replay.preselect_max_age_seconds,
        score_max_age_seconds=replay.score_max_age_seconds,
        candidate_pool_size=replay.candidate_pool_size,
    )


def _usable_reviews(reviews: Mapping[str, DeepSeekReview]) -> dict[str, DeepSeekReview]:
    return {
        code: review
        for code, review in reviews.items()
        if review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN}
    }


def _shanghai(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("tomorrow shadow input time must be timezone-aware")
    return value.astimezone(SHANGHAI)


__all__ = [
    "TomorrowShadowProjection",
    "native_input_from_snapshot",
    "project_tomorrow_input",
    "project_tomorrow_snapshot",
]
