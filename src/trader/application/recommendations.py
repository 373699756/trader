"""Recommendation generation use case from normalized feature snapshots."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime

from trader.application.policy import RecommendationPolicy, SelectionPolicy
from trader.application.ports import DeepSeekReviewPort
from trader.domain.filters import hard_filter
from trader.domain.fusion import FusionPolicy, fuse_score
from trader.domain.models import (
    DeepSeekReview,
    FeatureSnapshot,
    FilterAudit,
    FrozenReplayPolicy,
    FusionMode,
    Recommendation,
    RecommendationAction,
    RecommendationReplayInput,
    RecommendationSnapshot,
    ReviewOutcome,
    Strategy,
)
from trader.domain.ranking import CORE_FIELDS, action_for, candidate_score, minimum_selection_score, select_top_k
from trader.domain.risk import derive_local_risk_facts
from trader.domain.strategies import score_strategy
from trader.domain.tail import TAIL_SIGNAL_VALUE_FIELDS

REPLAY_SCHEMA_VERSION = "recommendation_replay_v1"
REPLAY_ALGORITHM_VERSION = "engine_v8_section12_2026_07"
_PRESELECTION_VALUE_FIELDS = (*CORE_FIELDS, "amount_median_20d", "trend_score")


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
    ) -> tuple[tuple[FeatureSnapshot, ...], Mapping[str, int], tuple[FilterAudit, ...]]:
        accepted: list[tuple[float, FeatureSnapshot]] = []
        reasons: Counter[str] = Counter()
        details: list[FilterAudit] = []
        for snapshot in features:
            result = hard_filter(snapshot, now, max_age_seconds=max_age_seconds)
            if not result.allowed:
                reasons.update(reason.code for reason in result.reasons)
                details.extend(result.reasons)
                continue
            if snapshot.missing_ratio(CORE_FIELDS) > 0.30:
                reasons["insufficient_candidate_history"] += 1
                details.append(
                    FilterAudit(
                        stock_code=snapshot.quote.code,
                        filter_code="insufficient_candidate_history",
                        threshold="<= 0.30",
                        actual=round(snapshot.missing_ratio(CORE_FIELDS), 6),
                        source=snapshot.quote.source,
                        observed_at=snapshot.quote.source_time,
                    )
                )
                continue
            accepted.append((candidate_score(snapshot, self._policy.candidate_weights), snapshot))
        accepted.sort(key=lambda item: (-item[0], item[1].quote.code))
        return tuple(snapshot for _score, snapshot in accepted[:limit]), dict(reasons), tuple(details)

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
        filter_details: Sequence[FilterAudit] = (),
        target_prices: Mapping[str, float | None] | None = None,
        market_features: Sequence[FeatureSnapshot] = (),
        requested_codes: Sequence[str] = (),
        preselect_max_age_seconds: float | None = None,
        candidate_pool_size: int = 0,
    ) -> RecommendationSnapshot:
        eligible: list[FeatureSnapshot] = []
        refreshed_filter_reasons = Counter(filter_reasons)
        refreshed_filter_details = list(filter_details)
        refreshed_filtered_count = filtered_count
        for feature in features:
            filter_result = hard_filter(feature, now, max_age_seconds=max_age_seconds)
            if filter_result.allowed:
                eligible.append(feature)
                continue
            refreshed_filter_reasons.update(reason.code for reason in filter_result.reasons)
            refreshed_filter_details.extend(filter_result.reasons)
            refreshed_filtered_count += 1

        merged, reviews, fusion_mode = self._merge_candidates(
            strategy,
            eligible,
            now=now,
            phase=phase,
            review_port=review_port,
            review_deadline=review_deadline,
            max_age_seconds=max_age_seconds,
            target_prices=target_prices,
        )
        minimum_score = minimum_selection_score(
            strategy,
            self._policy.selection.thresholds,
            phase=phase,
            observation_margin=self._policy.selection.observation_margin,
        )
        selected = (
            select_top_k(
                merged,
                top_k=self._policy.selection.default_top_k,
                maximum_per_industry=self._policy.selection.maximum_per_industry,
                minimum_final_score=minimum_score,
            )
            if minimum_score is not None
            else ()
        )
        snapshot_id = _snapshot_id(strategy, trade_date, phase, data_version, now)
        degraded_reasons: list[str] = []
        if fusion_mode is FusionMode.LOCAL_DEGRADED:
            degraded_reasons.append("deepseek_incomplete")
        tail_covered_count = 0
        if strategy is Strategy.TOMORROW:
            tail_covered_count = sum(
                all(feature.optional_value(field) is not None for field in TAIL_SIGNAL_VALUE_FIELDS)
                for feature in eligible
            )
            if tail_covered_count != len(eligible):
                degraded_reasons.append("tomorrow_tail_data_incomplete")
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
            filter_details=tuple(refreshed_filter_details),
            stale=any(item.features.quote.age_seconds(now) > max_age_seconds for item in selected),
            degraded_reasons=tuple(degraded_reasons),
            metadata={
                "candidate_count": len(eligible),
                "reviewed_count": sum(
                    review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN} for review in reviews.values()
                ),
                **(
                    {
                        "tail_data_covered_count": tail_covered_count,
                        "tail_data_coverage_ratio": tail_covered_count / len(eligible) if eligible else 0.0,
                    }
                    if strategy is Strategy.TOMORROW
                    else {}
                ),
            },
            replay_input=RecommendationReplayInput(
                schema_version=REPLAY_SCHEMA_VERSION,
                algorithm_version=REPLAY_ALGORITHM_VERSION,
                policy=_freeze_policy(self._policy),
                evaluated_at=now,
                market_features=tuple(_preselection_replay_feature(feature) for feature in market_features),
                requested_codes=tuple(requested_codes) or tuple(feature.quote.code for feature in features),
                candidate_features=tuple(features),
                reviews=reviews,
                preselect_max_age_seconds=preselect_max_age_seconds
                if preselect_max_age_seconds is not None
                else max_age_seconds,
                score_max_age_seconds=max_age_seconds,
                candidate_pool_size=candidate_pool_size,
                target_prices=dict(target_prices or {}),
            ),
        )

    def _merge_candidates(
        self,
        strategy: Strategy,
        eligible: Sequence[FeatureSnapshot],
        *,
        now: datetime,
        phase: str,
        review_port: DeepSeekReviewPort | None,
        review_deadline: datetime,
        max_age_seconds: float,
        target_prices: Mapping[str, float | None] | None = None,
    ) -> tuple[tuple[Recommendation, ...], Mapping[str, DeepSeekReview], FusionMode]:
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
                evidence=local.features.evidence,
                evaluated_at=now,
            )
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
                is_stale=local.features.quote.age_seconds(now) > max_age_seconds,
                observation_margin=self._policy.selection.observation_margin,
            )
            merged.append(replace(provisional, action=action, action_reason=reason))
        return tuple(merged), reviews, fusion_mode

    def replay(self, snapshot: RecommendationSnapshot) -> RecommendationSnapshot:
        replay_input = snapshot.replay_input
        if replay_input is None:
            raise ValueError("snapshot does not contain replay input")
        if replay_input.schema_version != REPLAY_SCHEMA_VERSION:
            raise ValueError("snapshot replay schema is unsupported")
        if replay_input.algorithm_version != REPLAY_ALGORITHM_VERSION:
            raise ValueError("snapshot replay algorithm is unsupported")
        if not replay_input.market_features:
            raise ValueError("snapshot replay input does not contain the frozen market universe")
        if replay_input.candidate_pool_size < 1:
            raise ValueError("snapshot replay input has an invalid candidate pool size")
        replay_engine = RecommendationEngine(_restore_policy(replay_input.policy))
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

        return replay_engine.build_snapshot(
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
        )

    @classmethod
    def verify_frozen(cls, snapshot: RecommendationSnapshot) -> Mapping[str, object]:
        replay_input = snapshot.replay_input
        if replay_input is None:
            raise ValueError("snapshot does not contain replay input")
        return cls(_restore_policy(replay_input.policy)).verify_replay(snapshot)

    @classmethod
    def replay_candidates(cls, snapshot: RecommendationSnapshot) -> tuple[Recommendation, ...]:
        if not snapshot.frozen:
            raise ValueError("only frozen snapshots can provide threshold-report candidates")
        cls.verify_frozen(snapshot)
        replay_input = snapshot.replay_input
        if replay_input is None:
            raise ValueError("snapshot does not contain replay input")
        engine = cls(_restore_policy(replay_input.policy))
        eligible = tuple(
            feature
            for feature in replay_input.candidate_features
            if hard_filter(
                feature,
                replay_input.evaluated_at,
                max_age_seconds=replay_input.score_max_age_seconds,
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
        return merged

    def verify_replay(self, snapshot: RecommendationSnapshot) -> Mapping[str, object]:
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

    def _local_candidate(
        self,
        strategy: Strategy,
        features: FeatureSnapshot,
        now: datetime,
    ) -> Recommendation:
        local_facts = derive_local_risk_facts(features, now, self._policy.risk_rules, strategy=strategy)
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
    ) -> Mapping[str, DeepSeekReview]:
        del phase, deadline
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
        snapshot.degraded_reasons,
        dict(snapshot.metadata),
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
        risk_rules=policy.risk_rules,
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
        ),
        candidate_weights=policy.candidate_weights,
        dimension_weights={Strategy(name): weights for name, weights in policy.dimension_weights.items()},
        risk_rules=policy.risk_rules,
    )


def _preselection_replay_feature(feature: FeatureSnapshot) -> FeatureSnapshot:
    return replace(
        feature,
        values={name: feature.values.get(name) for name in dict.fromkeys(_PRESELECTION_VALUE_FIELDS)},
        normalization=feature.normalization,
        evidence=(),
        external_risk_facts=(),
    )


__all__ = ["RecommendationEngine"]
