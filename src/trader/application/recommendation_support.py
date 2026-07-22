"""Shared recommendation context and frozen-policy helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime

from trader.application.policy import RecommendationPolicy, SelectionPolicy
from trader.domain.filters import HardFilterPolicy
from trader.domain.fusion import FusionPolicy
from trader.domain.models import (
    Board,
    DeepSeekReview,
    FeatureSnapshot,
    FrozenReplayPolicy,
    FusionMode,
    Recommendation,
    RecommendationSnapshot,
    ReviewCandidateContext,
    ReviewOutcome,
    Strategy,
)
from trader.domain.ranking import CORE_FIELDS

_PRESELECTION_VALUE_FIELDS = (*CORE_FIELDS, "amount_median_20d", "trend_score")
_NON_ALGORITHM_METADATA_KEYS = frozenset(
    {
        "field_sources",
        "close_anchors",
        "deepseek_mode",
        "freeze_anchor",
        "market_conflicts",
        "market_degraded_reasons",
        "market_missing_reasons",
        "market_observed_at",
        "merge_epoch",
        "price_basis",
        "recovery_path",
        "scoring_phase",
        "source_data_version",
        "source_snapshot_id",
        "source_versions",
        "tushare_reference_versions",
    }
)


def _fusion_mode(
    local_candidates: Sequence[Recommendation],
    reviews: Mapping[str, DeepSeekReview],
    thresholds: Mapping[str, float],
    strategy: Strategy,
    phase: str,
) -> FusionMode:
    threshold_key = (
        "today_late" if strategy is Strategy.TODAY and phase in {"today_late", "close_fallback"} else strategy.value
    )
    if threshold_key == Strategy.TODAY.value:
        threshold_key = "today_main"
    threshold = thresholds.get(threshold_key, 100.0)
    ordered = sorted(local_candidates, key=lambda item: (-item.score.local_score, item.features.quote.code))
    protected = {
        item.features.quote.code
        for index, item in enumerate(ordered)
        if index < 18 or item.score.local_score >= threshold - 5.0
    }
    for code in protected:
        review = reviews.get(code)
        if review is None or review.outcome not in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN}:
            return FusionMode.LOCAL_DEGRADED
    return FusionMode.HYBRID if protected else FusionMode.LOCAL_DEGRADED


def _review_contexts_for_candidates(
    strategy: Strategy,
    candidates: Sequence[Recommendation],
    phase: str,
    selection: SelectionPolicy,
) -> Mapping[str, ReviewCandidateContext]:
    threshold_key = (
        "today_late" if strategy is Strategy.TODAY and phase in {"today_late", "close_fallback"} else strategy.value
    )
    if threshold_key == Strategy.TODAY.value:
        threshold_key = "today_main"
    threshold = selection.thresholds.get(threshold_key)
    ordered = sorted(candidates, key=lambda item: (-item.score.local_score, item.features.quote.code))
    return {
        item.features.quote.code: ReviewCandidateContext(
            local_score=item.score.local_score,
            local_rank=index + 1,
            action_threshold=threshold,
            in_protection_set=index < 18
            or (threshold is not None and item.score.local_score >= threshold - selection.observation_margin),
            has_new_high_risk=any(
                fact.severity == "high" and fact.confidence >= 0.7
                for fact in (*item.local_risk_facts, *item.features.external_risk_facts)
            ),
            near_action_threshold=bool(
                threshold is not None and abs(item.score.local_score - threshold) <= selection.observation_margin
            ),
            near_global_boundary=abs(index + 1 - selection.default_top_k) <= 2,
            evidence_conflict=any(
                restriction in {"cross_source_deviation", "board_classification_conflict"}
                for restriction in item.features.quote.execution_restrictions
            ),
        )
        for index, item in enumerate(ordered)
    }


def _snapshot_id(
    strategy: Strategy,
    trade_date: str,
    phase: str,
    data_version: str,
    now: datetime,
) -> str:
    material = f"{strategy.value}|{trade_date}|{phase}|{data_version}|{now.isoformat()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


class _RecordedReviewPort:
    def __init__(self, reviews: Mapping[str, DeepSeekReview]) -> None:
        self._reviews = dict(reviews)

    def review(
        self,
        _strategy: Strategy,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
        contexts: Mapping[str, ReviewCandidateContext] | None = None,
    ) -> Mapping[str, DeepSeekReview]:
        del phase, deadline, contexts
        codes = {candidate.quote.code for candidate in candidates}
        return {code: review for code, review in self._reviews.items() if code in codes}

    def preheat(
        self,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
    ) -> Mapping[str, DeepSeekReview]:
        return self.review(Strategy.TODAY, candidates, phase=phase, deadline=deadline)

    @staticmethod
    def status() -> Mapping[str, object]:
        return {"source": "frozen_replay"}


def _business_projection(snapshot: RecommendationSnapshot) -> tuple[object, ...]:
    market_degraded_raw = snapshot.metadata.get("market_degraded_reasons")
    market_degraded = (
        {reason for reason in market_degraded_raw if isinstance(reason, str)}
        if isinstance(market_degraded_raw, (list, tuple))
        else set()
    )
    return (
        snapshot.snapshot_id,
        snapshot.strategy,
        snapshot.trade_date,
        snapshot.phase,
        snapshot.data_version,
        snapshot.strategy_version,
        snapshot.fusion_version,
        snapshot.fusion_mode,
        snapshot.recommendations,
        snapshot.filtered_count,
        dict(snapshot.filter_reasons),
        snapshot.filter_details,
        snapshot.stale,
        tuple(reason for reason in snapshot.degraded_reasons if reason not in market_degraded),
        {key: value for key, value in snapshot.metadata.items() if key not in _NON_ALGORITHM_METADATA_KEYS},
    )


def _freeze_policy(policy: RecommendationPolicy) -> FrozenReplayPolicy:
    return FrozenReplayPolicy(
        strategy_version=policy.strategy_version,
        fusion_version=policy.fusion_version,
        local_weight=policy.fusion.local_weight,
        deepseek_weight=policy.fusion.deepseek_weight,
        confidence_coverage_min=policy.fusion.confidence_coverage_min,
        minimum_known_dimensions=policy.fusion.minimum_known_dimensions,
        local_risk_cap=policy.fusion.local_risk_cap,
        deepseek_risk_cap=policy.fusion.deepseek_risk_cap,
        default_top_k=policy.selection.default_top_k,
        maximum_top_k=policy.selection.maximum_top_k,
        maximum_per_industry=policy.selection.maximum_per_industry,
        observation_margin=policy.selection.observation_margin,
        thresholds=policy.selection.thresholds,
        candidate_weights=policy.candidate_weights,
        dimension_weights={strategy.value: weights for strategy, weights in policy.dimension_weights.items()},
        local_strategy_weights={strategy.value: weights for strategy, weights in policy.local_strategy_weights.items()},
        risk_rules=policy.risk_rules,
        blacklist_codes=tuple(sorted(policy.hard_filter.blacklist_codes)),
        structured_risk_thresholds=policy.hard_filter.structured_risk_thresholds,
        maximum_board_fraction=policy.selection.maximum_board_fraction,
        competition_group_limits={
            board.value: limit for board, limit in policy.selection.competition_group_limits.items()
        },
        candidate_min_score=policy.selection.candidate_min_score,
        minimum_board_reliability=policy.selection.minimum_board_reliability,
        board_policy_version=policy.board_policy_version,
        board_candidate_weights={
            strategy.value: {board.value: weights for board, weights in boards.items()}
            for strategy, boards in policy.board_candidate_weights.items()
        },
        board_local_strategy_weights={
            strategy.value: {board.value: weights for board, weights in boards.items()}
            for strategy, boards in policy.board_local_strategy_weights.items()
        },
    )


def _restore_policy(policy: FrozenReplayPolicy) -> RecommendationPolicy:
    return RecommendationPolicy(
        strategy_version=policy.strategy_version,
        fusion_version=policy.fusion_version,
        fusion=FusionPolicy(
            local_weight=policy.local_weight,
            deepseek_weight=policy.deepseek_weight,
            confidence_coverage_min=policy.confidence_coverage_min,
            minimum_known_dimensions=policy.minimum_known_dimensions,
            local_risk_cap=policy.local_risk_cap,
            deepseek_risk_cap=policy.deepseek_risk_cap,
        ),
        selection=SelectionPolicy(
            default_top_k=policy.default_top_k,
            maximum_top_k=policy.maximum_top_k,
            maximum_per_industry=policy.maximum_per_industry,
            observation_margin=policy.observation_margin,
            thresholds=policy.thresholds,
            maximum_board_fraction=policy.maximum_board_fraction,
            competition_group_limits={Board(name): limit for name, limit in policy.competition_group_limits.items()},
            candidate_min_score=policy.candidate_min_score,
            minimum_board_reliability=policy.minimum_board_reliability,
        ),
        candidate_weights=policy.candidate_weights,
        dimension_weights={Strategy(name): weights for name, weights in policy.dimension_weights.items()},
        local_strategy_weights={Strategy(name): weights for name, weights in policy.local_strategy_weights.items()},
        risk_rules=policy.risk_rules,
        board_policy_version=policy.board_policy_version,
        board_candidate_weights={
            Strategy(strategy): {Board(board): weights for board, weights in boards.items()}
            for strategy, boards in policy.board_candidate_weights.items()
        },
        board_local_strategy_weights={
            Strategy(strategy): {Board(board): weights for board, weights in boards.items()}
            for strategy, boards in policy.board_local_strategy_weights.items()
        },
        hard_filter=HardFilterPolicy(
            blacklist_codes=frozenset(policy.blacklist_codes),
            structured_risk_thresholds=policy.structured_risk_thresholds,
        ),
    )


def _preselection_replay_feature(feature: FeatureSnapshot) -> FeatureSnapshot:
    return replace(
        feature,
        values={name: feature.values.get(name) for name in dict.fromkeys(_PRESELECTION_VALUE_FIELDS)},
        normalization=feature.normalization,
        evidence=(),
        external_risk_facts=(),
    )
