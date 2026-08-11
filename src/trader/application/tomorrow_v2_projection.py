"""Native Tomorrow input projection to the unified V2 scored-decision identity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from trader.application.policy import RecommendationPolicy
from trader.application.ports.tomorrow import ScoredNativeInput, TomorrowNativeInput
from trader.application.recommendation_policy_codec import preselection_replay_feature
from trader.application.tomorrow_deepseek_fusion import (
    normalize_tomorrow_review_times,
    tomorrow_decision_policy,
)
from trader.application.tomorrow_quality import TomorrowInputQuality, assess_tomorrow_input_quality
from trader.application.tomorrow_selection import (
    TomorrowSelectionIdentity,
    TomorrowSelectionOptions,
    select_tomorrow_features,
)
from trader.domain.recommendation.decision_identity import DecisionItem, ScoredDecision
from trader.domain.recommendation.models import Strategy
from trader.domain.recommendation.tomorrow_fusion import (
    DecisionEpoch,
    TomorrowDecisionEntry,
    TomorrowDecisionPolicy,
    TomorrowDecisionRequest,
    TomorrowReviewCandidate,
    build_tomorrow_decision_epoch,
    select_tomorrow_review_candidates,
)
from trader.domain.recommendation.tomorrow_selection import TomorrowSelectionResult
from trader.domain.review.models import DeepSeekReview, ReviewOutcome


@dataclass(frozen=True)
class TomorrowV2LocalProjection:
    native_input: ScoredNativeInput
    selection: TomorrowSelectionResult
    input_quality: TomorrowInputQuality
    review_candidates: tuple[TomorrowReviewCandidate, ...]
    local_epoch: DecisionEpoch
    local: ScoredDecision


def build_tomorrow_v2_local(
    native_input: TomorrowNativeInput,
    policy: RecommendationPolicy,
    *,
    sequence: int,
) -> TomorrowV2LocalProjection:
    return build_scored_v2_local(
        native_input,
        policy,
        decision_policy=tomorrow_decision_policy(policy),
        strategy=Strategy.TOMORROW,
        sequence=sequence,
    )


def build_scored_v2_local(
    native_input: ScoredNativeInput,
    policy: RecommendationPolicy,
    *,
    decision_policy: TomorrowDecisionPolicy,
    strategy: Strategy,
    sequence: int,
) -> TomorrowV2LocalProjection:
    if sequence < 1:
        raise ValueError("Tomorrow V2 decision sequence must be positive")
    if native_input.strategy is not strategy:
        raise ValueError("native input strategy does not match projection strategy")
    selection = select_tomorrow_features(
        tuple(preselection_replay_feature(feature) for feature in native_input.market_features),
        policy,
        TomorrowSelectionOptions(
            evaluated_at=native_input.evaluated_at,
            max_age_seconds=native_input.score_max_age_seconds,
            phase=native_input.phase,
            candidate_features=native_input.candidate_features,
            normalize_discovery_source_time=True,
            strategy=strategy,
        ),
        TomorrowSelectionIdentity(
            trade_date=native_input.trade_date,
            data_version=native_input.data_version,
            merge_epoch=native_input.input_version,
        ),
    )
    quality = assess_tomorrow_input_quality(native_input, selection)
    candidates = select_tomorrow_review_candidates(selection, decision_policy)
    input_hash = native_input.input_version.removeprefix("native-input:")
    epoch = build_tomorrow_decision_epoch(
        TomorrowDecisionRequest(
            selection=selection,
            reviews={},
            observed_at=native_input.evaluated_at,
            trade_date=native_input.trade_date,
            sequence=sequence,
            config_version=native_input.config_version,
            strategy_version=policy.strategy_version,
            fusion_version=policy.fusion_version,
            market_epoch_version=f"native-market:{input_hash}",
            candidate_epoch_version=(f"native-candidate:{input_hash}" if native_input.candidate_features else None),
            research_epoch_version=None,
            projection_stage="local",
            parent_decision_version=None,
            review_candidate_codes=tuple(item.code for item in candidates),
            degraded_reasons=quality.degraded_reasons,
            policy=decision_policy,
        )
    )
    return TomorrowV2LocalProjection(
        native_input,
        selection,
        quality,
        candidates,
        epoch,
        _scored_decision(epoch, input_version=native_input.input_version, strategy=strategy),
    )


def build_tomorrow_v2_hybrid(
    projection: TomorrowV2LocalProjection,
    policy: RecommendationPolicy,
    reviews: Mapping[str, DeepSeekReview],
    *,
    review_deadline: datetime,
) -> ScoredDecision | None:
    if projection.local.strategy is not Strategy.TOMORROW:
        return None
    return build_scored_v2_hybrid(
        projection,
        policy,
        reviews,
        decision_policy=tomorrow_decision_policy(policy),
        review_deadline=review_deadline,
    )


def build_scored_v2_hybrid(
    projection: TomorrowV2LocalProjection,
    policy: RecommendationPolicy,
    reviews: Mapping[str, DeepSeekReview],
    *,
    decision_policy: TomorrowDecisionPolicy,
    review_deadline: datetime,
) -> ScoredDecision | None:
    strategy = projection.local.strategy
    if projection.native_input.strategy is not strategy:
        return None
    candidates = {item.code for item in projection.review_candidates}
    if any(code not in candidates or review.code != code for code, review in reviews.items()):
        return None
    normalized = normalize_tomorrow_review_times(reviews, review_deadline)
    if normalized is None:
        return None
    usable = {
        code: review
        for code, review in normalized.items()
        if review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN} and review.completed_at < review_deadline
    }
    if not usable:
        return None
    observed_at = max(
        projection.native_input.evaluated_at,
        *(review.completed_at for review in usable.values()),
    )
    epoch = build_tomorrow_decision_epoch(
        TomorrowDecisionRequest(
            selection=projection.selection,
            reviews=normalized,
            observed_at=observed_at,
            trade_date=projection.native_input.trade_date,
            sequence=projection.local.sequence + 1,
            config_version=projection.native_input.config_version,
            strategy_version=policy.strategy_version,
            fusion_version=policy.fusion_version,
            market_epoch_version=projection.local_epoch.market_epoch_version,
            candidate_epoch_version=projection.local_epoch.candidate_epoch_version,
            research_epoch_version=None,
            projection_stage="hybrid",
            parent_decision_version=projection.local_epoch.version,
            review_candidate_codes=tuple(item.code for item in projection.review_candidates),
            degraded_reasons=tuple(
                sorted(
                    {
                        *projection.input_quality.degraded_reasons,
                        *(() if set(usable) == candidates else ("deepseek_incomplete",)),
                    }
                )
            ),
            policy=decision_policy,
        )
    )
    return _scored_decision(
        epoch,
        input_version=projection.native_input.input_version,
        parent_version=projection.local.version,
        strategy=strategy,
    )


def _scored_decision(
    epoch: DecisionEpoch,
    *,
    input_version: str,
    strategy: Strategy,
    parent_version: str | None = None,
) -> ScoredDecision:
    return ScoredDecision(
        strategy=strategy,
        trade_date=epoch.trade_date,
        sequence=epoch.sequence,
        observed_at=epoch.observed_at,
        stage=epoch.projection_stage,
        parent_version=parent_version,
        input_versions=tuple(
            (name, value)
            for name, value in (
                ("native", input_version),
                ("market", epoch.market_epoch_version),
                ("candidate", epoch.candidate_epoch_version),
                ("research", epoch.research_epoch_version),
            )
            if value is not None
        ),
        config_version=epoch.config_version,
        strategy_version=epoch.strategy_version,
        fusion_version=epoch.fusion_version,
        items=tuple(_decision_item(item) for item in epoch.entries),
        filter_aggregates=tuple(epoch.filter_reason_counts.items()),
        degraded_reasons=epoch.degraded_reasons,
    )


def _decision_item(entry: TomorrowDecisionEntry) -> DecisionItem:
    reason = entry.decision_skip_reason or entry.action_reason or "not_selected"
    return DecisionItem(
        code=entry.code,
        action=entry.action,
        selected=entry.selected,
        rank=entry.rank,
        candidate_score=entry.candidate_score,
        local_score=entry.score.local_score,
        final_score=entry.score.final_score,
        score_components=(
            *tuple(entry.score.components.items()),
            ("deepseek_score", entry.score.deepseek_score),
            ("deepseek_risk_penalty", entry.score.deepseek_risk_penalty),
        ),
        risk_codes=tuple(fact.risk_code for fact in (*entry.local_risk_facts, *entry.deepseek_risk_facts)),
        reason=reason,
    )


def validate_review_manifests(
    projection: TomorrowV2LocalProjection,
    reviews: Mapping[str, DeepSeekReview],
    expected: Mapping[str, str],
) -> bool:
    candidate_codes = {item.code for item in projection.review_candidates}
    return not any(
        code not in candidate_codes
        or review.code != code
        or (
            review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN}
            and review.evidence_manifest_hash != expected.get(code)
        )
        for code, review in reviews.items()
    )


__all__ = [
    "TomorrowV2LocalProjection",
    "build_scored_v2_hybrid",
    "build_scored_v2_local",
    "build_tomorrow_v2_hybrid",
    "build_tomorrow_v2_local",
    "validate_review_manifests",
]
