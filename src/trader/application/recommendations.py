"""Recommendation generation use case from normalized feature snapshots."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime

from trader.application.policy import RecommendationPolicy
from trader.application.ports import DeepSeekReviewPort
from trader.domain.filters import hard_filter
from trader.domain.fusion import fuse_score
from trader.domain.models import (
    DeepSeekReview,
    FeatureSnapshot,
    FusionMode,
    Recommendation,
    RecommendationAction,
    RecommendationSnapshot,
    ReviewOutcome,
    Strategy,
)
from trader.domain.ranking import action_for, candidate_score, select_top_k
from trader.domain.risk import derive_local_risk_facts
from trader.domain.strategies import score_strategy


class RecommendationEngine:
    def __init__(self, policy: RecommendationPolicy) -> None:
        self._policy = policy

    def preselect(
        self,
        features: Sequence[FeatureSnapshot],
        *,
        now: datetime,
        max_age_seconds: float,
        limit: int,
    ) -> tuple[tuple[FeatureSnapshot, ...], Mapping[str, int]]:
        accepted: list[tuple[float, FeatureSnapshot]] = []
        reasons: Counter[str] = Counter()
        for snapshot in features:
            result = hard_filter(snapshot, now, max_age_seconds=max_age_seconds)
            if not result.allowed:
                reasons.update(reason.code for reason in result.reasons)
                continue
            accepted.append((candidate_score(snapshot, self._policy.candidate_weights), snapshot))
        accepted.sort(key=lambda item: (-item[0], item[1].quote.code))
        return tuple(snapshot for _score, snapshot in accepted[:limit]), dict(reasons)

    def build_snapshot(
        self,
        strategy: Strategy,
        features: Sequence[FeatureSnapshot],
        *,
        now: datetime,
        phase: str,
        trade_date: str,
        data_version: str,
        review_port: DeepSeekReviewPort | None,
        review_deadline: datetime,
        max_age_seconds: float,
        filtered_count: int,
        filter_reasons: Mapping[str, int],
        target_prices: Mapping[str, float | None] | None = None,
    ) -> RecommendationSnapshot:
        eligible: list[FeatureSnapshot] = []
        refreshed_filter_reasons = Counter(filter_reasons)
        refreshed_filtered_count = filtered_count
        for feature in features:
            filter_result = hard_filter(feature, now, max_age_seconds=max_age_seconds)
            if filter_result.allowed:
                eligible.append(feature)
                continue
            refreshed_filter_reasons.update(reason.code for reason in filter_result.reasons)
            refreshed_filtered_count += 1

        local_candidates = [self._local_candidate(strategy, feature, now) for feature in eligible]
        reviews = (
            review_port.review(strategy, tuple(eligible), phase=phase, deadline=review_deadline)
            if review_port is not None and eligible
            else {}
        )
        fusion_mode = _fusion_mode(local_candidates, reviews, self._policy.selection.thresholds, strategy, phase)
        merged: list[Recommendation] = []
        for local in local_candidates:
            review = reviews.get(local.features.quote.code)
            fusion_result = fuse_score(
                score_strategy(strategy, local.features),
                local.local_risk_facts,
                review,
                self._policy.dimension_weights[strategy],
                self._policy.risk_rules,
                fusion_mode,
                self._policy.fusion,
            )
            stale = local.features.quote.age_seconds(now) > max_age_seconds
            provisional = replace(
                local,
                score=fusion_result.score,
                deepseek_risk_facts=fusion_result.deepseek_risk_facts,
                review=review,
                veto=fusion_result.veto,
                target_price=(target_prices or {}).get(local.features.quote.code),
            )
            action, reason = action_for(
                provisional,
                self._policy.selection.thresholds,
                phase=phase,
                is_stale=stale,
                observation_margin=self._policy.selection.observation_margin,
            )
            merged.append(replace(provisional, action=action, action_reason=reason))
        selected = select_top_k(
            merged,
            top_k=self._policy.selection.default_top_k,
            maximum_per_industry=self._policy.selection.maximum_per_industry,
        )
        snapshot_id = _snapshot_id(strategy, trade_date, phase, data_version, now)
        degraded_reasons = ("deepseek_incomplete",) if fusion_mode is FusionMode.LOCAL_DEGRADED else ()
        return RecommendationSnapshot(
            snapshot_id=snapshot_id,
            strategy=strategy,
            trade_date=trade_date,
            phase=phase,
            data_version=data_version,
            strategy_version=self._policy.strategy_version,
            fusion_version=self._policy.fusion_version,
            fusion_mode=fusion_mode,
            published_at=now,
            recommendations=selected,
            filtered_count=refreshed_filtered_count,
            filter_reasons=dict(refreshed_filter_reasons),
            stale=any(item.features.quote.age_seconds(now) > max_age_seconds for item in selected),
            degraded_reasons=degraded_reasons,
            metadata={
                "candidate_count": len(eligible),
                "reviewed_count": sum(
                    review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN} for review in reviews.values()
                ),
            },
        )

    def _local_candidate(
        self,
        strategy: Strategy,
        features: FeatureSnapshot,
        now: datetime,
    ) -> Recommendation:
        local_facts = derive_local_risk_facts(features, now, self._policy.risk_rules)
        local = score_strategy(strategy, features)
        local_result = fuse_score(
            local,
            local_facts,
            None,
            self._policy.dimension_weights[strategy],
            self._policy.risk_rules,
            FusionMode.LOCAL_DEGRADED,
            self._policy.fusion,
        )
        return Recommendation(
            strategy=strategy,
            features=features,
            score=local_result.score,
            local_risk_facts=local_facts,
            deepseek_risk_facts=(),
            review=None,
            action=RecommendationAction.OBSERVE,
            action_reason="pending_merge",
            veto=False,
        )


def _fusion_mode(
    local_candidates: Sequence[Recommendation],
    reviews: Mapping[str, DeepSeekReview],
    thresholds: Mapping[str, float],
    strategy: Strategy,
    phase: str,
) -> FusionMode:
    threshold_key = "today_late" if strategy is Strategy.TODAY and phase == "today_late" else strategy.value
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


def _snapshot_id(
    strategy: Strategy,
    trade_date: str,
    phase: str,
    data_version: str,
    now: datetime,
) -> str:
    material = f"{strategy.value}|{trade_date}|{phase}|{data_version}|{now.isoformat()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


__all__ = ["RecommendationEngine"]
