"""Recommendation generation use case from normalized feature snapshots."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType

from trader.application.policy import RecommendationPolicy
from trader.application.ports import DeepSeekReviewPort
from trader.application.recommendation_replay import (
    REPLAY_ALGORITHM_VERSION,
    REPLAY_SCHEMA_VERSION,
    RecommendationReplayMixin,
)
from trader.application.recommendation_support import (
    _freeze_policy,
    _fusion_mode,
    _preselection_replay_feature,
    _review_contexts_for_candidates,
    _snapshot_id,
)
from trader.domain.filters import FilterResult, hard_filter
from trader.domain.fusion import fuse_score
from trader.domain.models import (
    DeepSeekReview,
    FeatureSnapshot,
    FilterAudit,
    FusionMode,
    Recommendation,
    RecommendationAction,
    RecommendationReplayInput,
    RecommendationSnapshot,
    ReviewCandidateContext,
    ReviewOutcome,
    Strategy,
)
from trader.domain.ranking import CORE_FIELDS, action_for, candidate_score, minimum_selection_score, select_top_k
from trader.domain.risk import derive_local_risk_facts
from trader.domain.strategies import score_strategy
from trader.domain.tail import TAIL_SIGNAL_VALUE_FIELDS

_PRESELECTION_VALUE_FIELDS = (*CORE_FIELDS, "amount_median_20d", "trend_score")
_STRUCTURED_RISK_FIELDS = (
    "financial_deterioration",
    "negative_announcement_level",
    "pledge_risk",
    "reduction_or_unlock",
)
_LONG_RESEARCH_FIELDS = (
    "value_score",
    "growth_score",
    "quality_score",
    "industry_policy_score",
    "risk_protection_score",
    *_STRUCTURED_RISK_FIELDS,
)


@dataclass(frozen=True)
class PreparedSnapshot:
    strategy: Strategy
    features: tuple[FeatureSnapshot, ...]
    eligible: tuple[FeatureSnapshot, ...]
    local_candidates: tuple[Recommendation, ...]
    now: datetime
    phase: str
    trade_date: str
    data_version: str
    review_deadline: datetime
    max_age_seconds: float
    filtered_count: int
    filter_reasons: Mapping[str, int]
    filter_details: tuple[FilterAudit, ...]
    target_prices: Mapping[str, float | None]
    market_features: tuple[FeatureSnapshot, ...]
    requested_codes: tuple[str, ...]
    preselect_max_age_seconds: float
    candidate_pool_size: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "filter_reasons", MappingProxyType(dict(self.filter_reasons)))
        object.__setattr__(self, "target_prices", MappingProxyType(dict(self.target_prices)))


class RecommendationEngine(RecommendationReplayMixin):
    def __init__(
        self,
        policy: RecommendationPolicy,
        *,
        hard_filter_function: Callable[..., FilterResult] = hard_filter,
    ) -> None:
        self._policy = policy
        self._hard_filter = hard_filter_function

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
            result = self._hard_filter(
                snapshot,
                now,
                max_age_seconds=max_age_seconds,
                policy=self._policy.hard_filter,
            )
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
        prepared = self.prepare_snapshot(
            strategy,
            features,
            now=now,
            phase=phase,
            trade_date=trade_date,
            data_version=data_version,
            review_deadline=review_deadline,
            max_age_seconds=max_age_seconds,
            filtered_count=filtered_count,
            filter_reasons=filter_reasons,
            filter_details=filter_details,
            target_prices=target_prices,
            market_features=market_features,
            requested_codes=requested_codes,
            preselect_max_age_seconds=preselect_max_age_seconds,
            candidate_pool_size=candidate_pool_size,
        )
        reviews = (
            review_port.review(
                strategy,
                prepared.eligible,
                phase=phase,
                deadline=review_deadline,
                contexts=self.review_contexts(prepared),
            )
            if review_port is not None and prepared.eligible
            else {}
        )
        return self.finalize_snapshot(prepared, reviews)

    def prepare_snapshot(
        self,
        strategy: Strategy,
        features: Sequence[FeatureSnapshot],
        *,
        now: datetime,
        phase: str,
        trade_date: str,
        data_version: str,
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
    ) -> PreparedSnapshot:
        eligible: list[FeatureSnapshot] = []
        refreshed_filter_reasons = Counter(filter_reasons)
        refreshed_filter_details = list(filter_details)
        refreshed_filtered_count = filtered_count
        for feature in features:
            filter_result = self._hard_filter(
                feature,
                now,
                max_age_seconds=max_age_seconds,
                policy=self._policy.hard_filter,
            )
            if filter_result.allowed:
                refreshed_filter_details.extend(filter_result.optional_flags)
                eligible.append(feature)
                continue
            refreshed_filter_reasons.update(reason.code for reason in filter_result.reasons)
            refreshed_filter_details.extend(filter_result.reasons)
            refreshed_filtered_count += 1

        return PreparedSnapshot(
            strategy=strategy,
            features=tuple(features),
            eligible=tuple(eligible),
            local_candidates=tuple(self._local_candidate(strategy, feature, now) for feature in eligible),
            now=now,
            phase=phase,
            trade_date=trade_date,
            data_version=data_version,
            review_deadline=review_deadline,
            max_age_seconds=max_age_seconds,
            filtered_count=refreshed_filtered_count,
            filter_reasons=dict(refreshed_filter_reasons),
            filter_details=tuple(refreshed_filter_details),
            target_prices=dict(target_prices or {}),
            market_features=tuple(market_features),
            requested_codes=tuple(requested_codes),
            preselect_max_age_seconds=preselect_max_age_seconds
            if preselect_max_age_seconds is not None
            else max_age_seconds,
            candidate_pool_size=candidate_pool_size,
        )

    def finalize_snapshot(
        self,
        prepared: PreparedSnapshot,
        reviews: Mapping[str, DeepSeekReview],
    ) -> RecommendationSnapshot:
        strategy = prepared.strategy
        eligible = prepared.eligible
        now = prepared.now
        phase = prepared.phase
        merged, fusion_mode = self._merge_reviewed_candidates(
            strategy,
            prepared.local_candidates,
            reviews,
            now=now,
            phase=phase,
            max_age_seconds=prepared.max_age_seconds,
            target_prices=prepared.target_prices,
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
        snapshot_id = _snapshot_id(strategy, prepared.trade_date, phase, prepared.data_version, now)
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
        research_covered_count = 0
        research_fields: tuple[str, ...] = ()
        if strategy in {Strategy.D25, Strategy.LONG}:
            research_fields = _LONG_RESEARCH_FIELDS if strategy is Strategy.LONG else _STRUCTURED_RISK_FIELDS
            research_covered_count = sum(
                all(feature.optional_value(field) is not None for field in research_fields) for feature in eligible
            )
            if research_covered_count != len(eligible):
                degraded_reasons.append(
                    "long_research_incomplete" if strategy is Strategy.LONG else "d25_structured_research_incomplete"
                )
        return RecommendationSnapshot(
            snapshot_id=snapshot_id,
            strategy=strategy,
            trade_date=prepared.trade_date,
            phase=phase,
            data_version=prepared.data_version,
            strategy_version=self._policy.strategy_version,
            fusion_version=self._policy.fusion_version,
            fusion_mode=fusion_mode,
            published_at=now,
            recommendations=selected,
            filtered_count=prepared.filtered_count,
            filter_reasons=dict(prepared.filter_reasons),
            filter_details=prepared.filter_details,
            stale=any(item.features.quote.age_seconds(now) > prepared.max_age_seconds for item in selected),
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
                **(
                    {
                        "research_data_covered_count": research_covered_count,
                        "research_data_coverage_ratio": research_covered_count / len(eligible) if eligible else 0.0,
                        "research_data_required_fields": research_fields,
                    }
                    if research_fields
                    else {}
                ),
            },
            replay_input=RecommendationReplayInput(
                schema_version=REPLAY_SCHEMA_VERSION,
                algorithm_version=REPLAY_ALGORITHM_VERSION,
                policy=_freeze_policy(self._policy),
                evaluated_at=now,
                market_features=tuple(_preselection_replay_feature(feature) for feature in prepared.market_features),
                requested_codes=prepared.requested_codes or tuple(feature.quote.code for feature in prepared.features),
                candidate_features=prepared.features,
                reviews=dict(reviews),
                preselect_max_age_seconds=prepared.preselect_max_age_seconds,
                score_max_age_seconds=prepared.max_age_seconds,
                candidate_pool_size=prepared.candidate_pool_size,
                target_prices=dict(prepared.target_prices),
            ),
        )

    def review_contexts(self, prepared: PreparedSnapshot) -> Mapping[str, ReviewCandidateContext]:
        return _review_contexts_for_candidates(
            prepared.strategy,
            prepared.local_candidates,
            prepared.phase,
            self._policy.selection,
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
        local_candidates = tuple(self._local_candidate(strategy, feature, now) for feature in eligible)
        reviews = (
            review_port.review(
                strategy,
                tuple(eligible),
                phase=phase,
                deadline=review_deadline,
                contexts=_review_contexts_for_candidates(
                    strategy,
                    local_candidates,
                    phase,
                    self._policy.selection,
                ),
            )
            if review_port is not None and eligible
            else {}
        )
        merged, fusion_mode = self._merge_reviewed_candidates(
            strategy,
            local_candidates,
            reviews,
            now=now,
            phase=phase,
            max_age_seconds=max_age_seconds,
            target_prices=target_prices,
        )
        return merged, reviews, fusion_mode

    def _merge_reviewed_candidates(
        self,
        strategy: Strategy,
        local_candidates: Sequence[Recommendation],
        reviews: Mapping[str, DeepSeekReview],
        *,
        now: datetime,
        phase: str,
        max_age_seconds: float,
        target_prices: Mapping[str, float | None] | None = None,
    ) -> tuple[tuple[Recommendation, ...], FusionMode]:
        fusion_mode = _fusion_mode(local_candidates, reviews, self._policy.selection.thresholds, strategy, phase)
        merged: list[Recommendation] = []
        for local in local_candidates:
            review = reviews.get(local.features.quote.code)
            fusion_result = fuse_score(
                score_strategy(strategy, local.features, self._policy.local_strategy_weights),
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
            restrictions = local.features.quote.execution_restrictions
            if restrictions and action is RecommendationAction.EXECUTABLE:
                action = RecommendationAction.OBSERVE
                reason = "market_data_observe_only:" + ",".join(sorted(restrictions))
            merged.append(replace(provisional, action=action, action_reason=reason))
        return tuple(merged), fusion_mode

    def _local_candidate(
        self,
        strategy: Strategy,
        features: FeatureSnapshot,
        now: datetime,
    ) -> Recommendation:
        local_facts = derive_local_risk_facts(features, now, self._policy.risk_rules, strategy=strategy)
        local = score_strategy(strategy, features, self._policy.local_strategy_weights)
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


__all__ = ["PreparedSnapshot", "RecommendationEngine"]
