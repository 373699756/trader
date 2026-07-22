"""Recommendation finalization, deterministic merge, and frozen-board replay."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType

from trader.application.policy import RecommendationPolicy
from trader.application.ports.reviews import DeepSeekReviewPort
from trader.application.recommendation_replay import (
    REPLAY_ALGORITHM_VERSION,
    REPLAY_SCHEMA_VERSION,
    V15_REPLAY_ALGORITHM_VERSION,
)
from trader.application.recommendation_support import (
    _freeze_policy,
    _fusion_mode,
    _preselection_replay_feature,
    _review_contexts_for_candidates,
    _snapshot_id,
)
from trader.domain.market.models import (
    Board,
    FeatureSnapshot,
)
from trader.domain.market.tail import TAIL_SIGNAL_VALUE_FIELDS
from trader.domain.recommendation.downside import assess_downside
from trader.domain.recommendation.filters import FilterResult
from trader.domain.recommendation.fusion import FusionRequest, fuse_score
from trader.domain.recommendation.models import (
    BoardScoreBatch,
    BoardStrategyPolicy,
    FilterAudit,
    FusionMode,
    Recommendation,
    RecommendationAction,
    RecommendationReplayInput,
    RecommendationSnapshot,
    SelectionSkip,
    Strategy,
)
from trader.domain.recommendation.ranking import (
    ActionPolicy,
    SelectionPolicy,
    action_for,
    minimum_selection_score,
    select_top_k_with_audit,
)
from trader.domain.recommendation.strategies import score_strategy
from trader.domain.recommendation.strategies.composition import LocalScoreResult, compose
from trader.domain.review.models import (
    DeepSeekReview,
    ReviewCandidateContext,
    ReviewOutcome,
)
from trader.domain.review.rules import derive_local_risk_facts

_STRUCTURED_RISK_FIELDS = (
    "financial_deterioration",
    "negative_announcement_level",
    "pledge_risk",
    "shareholder_reduction_level",
    "unlock_risk",
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
    board_batches: tuple[BoardScoreBatch, ...] = ()
    board_scoring_complete: bool = True
    board_degraded_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "filter_reasons", MappingProxyType(dict(self.filter_reasons)))
        object.__setattr__(self, "target_prices", MappingProxyType(dict(self.target_prices)))

    @property
    def review_eligible(self) -> tuple[FeatureSnapshot, ...]:
        if not self.board_scoring_complete:
            return ()
        return tuple(
            feature
            for feature in self.eligible
            if self.strategy is Strategy.LONG or feature.board_data_reliability >= 0.85
        )


class RecommendationFinalizationMixin:
    _policy: RecommendationPolicy
    _hard_filter: Callable[..., FilterResult]

    def finalize_snapshot(
        self,
        prepared: PreparedSnapshot,
        reviews: Mapping[str, DeepSeekReview],
        *,
        legacy_v16: bool = False,
    ) -> RecommendationSnapshot:
        if not prepared.board_scoring_complete:
            reasons = ",".join(prepared.board_degraded_reasons) or "unknown"
            raise RuntimeError(f"v16 board scoring is incomplete: {reasons}")
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
            apply_downside=not legacy_v16,
        )
        minimum_score = minimum_selection_score(
            strategy,
            self._policy.selection.thresholds,
            phase=phase,
            observation_margin=self._policy.selection.observation_margin,
        )
        selected: tuple[Recommendation, ...]
        selection_skips: tuple[SelectionSkip, ...]
        if minimum_score is None:
            selected, selection_skips = (), ()
        elif strategy is Strategy.LONG or legacy_v16:
            selected, selection_skips = select_top_k_with_audit(
                merged,
                SelectionPolicy(
                    top_k=self._policy.selection.default_top_k,
                    maximum_per_industry=self._policy.selection.maximum_per_industry,
                    minimum_final_score=minimum_score,
                    maximum_board_fraction=self._policy.selection.maximum_board_fraction,
                    competition_group_limits=self._policy.selection.competition_group_limits,
                ),
            )
        else:
            executable, executable_skips = select_top_k_with_audit(
                (item for item in merged if item.action is RecommendationAction.EXECUTABLE),
                SelectionPolicy(
                    top_k=self._policy.selection.default_top_k,
                    maximum_per_industry=self._policy.selection.maximum_per_industry,
                    minimum_final_score=minimum_score,
                    maximum_board_fraction=self._policy.selection.maximum_board_fraction,
                    competition_group_limits=self._policy.selection.competition_group_limits,
                ),
            )
            watch, watch_skips = select_top_k_with_audit(
                (item for item in merged if item.action is RecommendationAction.OBSERVE),
                SelectionPolicy(
                    top_k=8,
                    maximum_per_industry=self._policy.selection.maximum_per_industry,
                    minimum_final_score=minimum_score,
                    maximum_board_fraction=self._policy.selection.maximum_board_fraction,
                    competition_group_limits=self._policy.selection.competition_group_limits,
                ),
            )
            selected = (*executable, *watch)
            selection_skips = (*executable_skips, *watch_skips)
        snapshot_id = _snapshot_id(strategy, prepared.trade_date, phase, prepared.data_version, now)
        degraded_reasons: list[str] = list(prepared.board_degraded_reasons)
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
                "board_batches": [
                    {
                        "board": batch.board.value,
                        "status": batch.status,
                        "policy_id": batch.policy_id,
                        "policy_version": batch.policy_version,
                        "merge_epoch": batch.merge_epoch,
                        "population_version": batch.population_version,
                        "degraded_reasons": batch.degraded_reasons,
                    }
                    for batch in prepared.board_batches
                ],
                "selection_skips": [
                    {
                        "stock_code": skip.stock_code,
                        "board": skip.board.value,
                        "competition_group_id": skip.competition_group_id,
                        "board_rank": skip.board_rank,
                        "global_rank": skip.global_rank,
                        "reason": skip.reason,
                        "limit": skip.limit,
                        "policy_version": skip.policy_version,
                        "observed_at": skip.observed_at.isoformat(),
                    }
                    for skip in selection_skips
                ],
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
                algorithm_version=(
                    REPLAY_ALGORITHM_VERSION if self._policy.board_candidate_weights else V15_REPLAY_ALGORITHM_VERSION
                ),
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
                board_batches=prepared.board_batches,
            ),
        )

    def prepare_frozen_board_replay(
        self,
        strategy: Strategy,
        replay_input: RecommendationReplayInput,
        *,
        phase: str,
        trade_date: str,
        data_version: str,
        filtered_count: int,
        filter_reasons: Mapping[str, int],
        filter_details: Sequence[FilterAudit],
    ) -> PreparedSnapshot:
        batches = replay_input.board_batches
        if len(batches) != 3 or {batch.board for batch in batches} != {Board.MAIN, Board.CHINEXT, Board.STAR}:
            raise ValueError("v16 frozen replay requires exactly three board batches")
        epochs = {batch.merge_epoch for batch in batches}
        if len(epochs) != 1 or any(batch.strategy is not strategy or batch.status == "failed" for batch in batches):
            raise ValueError("v16 frozen board batches are incomplete or have mixed epochs")
        for batch in batches:
            policy = self._policy.board_policy(strategy, batch.board)
            if policy is None or batch.policy_id != policy.policy_id:
                raise ValueError("v16 frozen board batch policy does not match replay policy")
        candidates = tuple(item for batch in batches for item in batch.recommendations)
        degraded = tuple(
            dict.fromkeys(f"{batch.board.value}:{reason}" for batch in batches for reason in batch.degraded_reasons)
        )
        return PreparedSnapshot(
            strategy=strategy,
            features=replay_input.candidate_features,
            eligible=tuple(item.features for item in candidates),
            local_candidates=candidates,
            now=replay_input.evaluated_at,
            phase=phase,
            trade_date=trade_date,
            data_version=data_version,
            review_deadline=replay_input.evaluated_at,
            max_age_seconds=replay_input.score_max_age_seconds,
            filtered_count=filtered_count,
            filter_reasons=filter_reasons,
            filter_details=tuple(filter_details),
            target_prices=replay_input.target_prices,
            market_features=replay_input.market_features,
            requested_codes=replay_input.requested_codes,
            preselect_max_age_seconds=replay_input.preselect_max_age_seconds,
            candidate_pool_size=replay_input.candidate_pool_size,
            board_batches=batches,
            board_degraded_reasons=degraded,
        )

    def review_contexts(self, prepared: PreparedSnapshot) -> Mapping[str, ReviewCandidateContext]:
        review_codes = {feature.quote.code for feature in prepared.review_eligible}
        return _review_contexts_for_candidates(
            prepared.strategy,
            tuple(
                candidate for candidate in prepared.local_candidates if candidate.features.quote.code in review_codes
            ),
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
        apply_downside: bool = True,
    ) -> tuple[tuple[Recommendation, ...], FusionMode]:
        fusion_candidates = tuple(
            candidate
            for candidate in local_candidates
            if strategy is Strategy.LONG
            or candidate.features.board_data_reliability >= self._policy.selection.minimum_board_reliability
        )
        fusion_mode = _fusion_mode(fusion_candidates, reviews, self._policy.selection.thresholds, strategy, phase)
        merged: list[Recommendation] = []
        for local in local_candidates:
            review = reviews.get(local.features.quote.code)
            board_policy = self._policy.board_policy(strategy, local.features.quote.board)
            if local.features.board_policy_id != (board_policy.policy_id if board_policy is not None else ""):
                board_policy = None
            local_score = (
                compose(local.score.components, board_policy.local_weights)
                if board_policy is not None
                else score_strategy(strategy, local.features, self._policy.local_strategy_weights)
            )
            fusion_result = fuse_score(
                FusionRequest(
                    local=local_score,
                    local_risk_facts=local.local_risk_facts,
                    review=review,
                    dimension_weights=self._policy.dimension_weights[strategy],
                    risk_rules=self._policy.risk_rules,
                    fusion_mode=fusion_mode,
                    policy=self._policy.fusion,
                    evidence=local.features.evidence,
                    evaluated_at=now,
                )
            )
            provisional = replace(
                local,
                score=fusion_result.score,
                deepseek_risk_facts=fusion_result.deepseek_risk_facts,
                review=review,
                veto=fusion_result.veto,
                target_price=(target_prices or {}).get(local.features.quote.code),
                downside=assess_downside(local.features, strategy) if apply_downside else None,
            )
            action, reason = action_for(
                provisional,
                ActionPolicy(
                    thresholds=self._policy.selection.thresholds,
                    phase=phase,
                    is_stale=local.features.quote.age_seconds(now) > max_age_seconds,
                    observation_margin=self._policy.selection.observation_margin,
                    minimum_board_reliability=self._policy.selection.minimum_board_reliability,
                ),
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
            FusionRequest(
                local=local,
                local_risk_facts=local_facts,
                review=None,
                dimension_weights=self._policy.dimension_weights[strategy],
                risk_rules=self._policy.risk_rules,
                fusion_mode=FusionMode.LOCAL_DEGRADED,
                policy=self._policy.fusion,
            )
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

    def _local_candidate_with_policy(
        self,
        strategy: Strategy,
        features: FeatureSnapshot,
        now: datetime,
        board_policy: BoardStrategyPolicy,
        local: LocalScoreResult,
    ) -> Recommendation:
        local_facts = derive_local_risk_facts(features, now, self._policy.risk_rules, strategy=strategy)
        local_result = fuse_score(
            FusionRequest(
                local=local,
                local_risk_facts=local_facts,
                review=None,
                dimension_weights=self._policy.dimension_weights[strategy],
                risk_rules=self._policy.risk_rules,
                fusion_mode=FusionMode.LOCAL_DEGRADED,
                policy=self._policy.fusion,
            )
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


__all__ = ["PreparedSnapshot", "RecommendationFinalizationMixin"]
