"""Recommendation finalization, deterministic merge, and frozen-board replay."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.application.long_groups import LongGroupDefinition, long_groups_metadata
from trader.application.policy import RecommendationPolicy
from trader.application.policy import SelectionPolicy as AppSelectionPolicy
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
    _selection_diagnostics,
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
    action_for,
    minimum_selection_score,
    select_top_k_with_audit,
)
from trader.domain.recommendation.ranking import (
    SelectionPolicy as RankingSelectionPolicy,
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
    "financial_fraud_history",
    "forced_delisting_risk",
    "fund_occupation_history",
    "illegal_guarantee_history",
    "major_illegal_history",
    "major_shareholder_reduction",
    "official_investigation_history",
    "pledge_risk",
    "unlock_risk",
    "corporate_risk_history_unavailable",
)
_LEGACY_STRUCTURED_RISK_FIELDS = (
    "financial_deterioration",
    "negative_announcement_level",
    "pledge_risk",
    "shareholder_reduction_level",
    "unlock_risk",
)
_CLOSE_FALLBACK_OBSERVE_TOP_K = 8
_CLOSE_FALLBACK_OBSERVATION_FLOOR_REASON = "close_fallback_observation_floor_relaxed"


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
    long_groups: tuple[LongGroupDefinition, ...]
    market_features: tuple[FeatureSnapshot, ...]
    requested_codes: tuple[str, ...]
    preselect_max_age_seconds: float
    candidate_pool_size: int
    board_batches: tuple[BoardScoreBatch, ...] = ()
    board_scoring_complete: bool = True
    board_degraded_reasons: tuple[str, ...] = ()
    review_candidate_codes: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "filter_reasons", MappingProxyType(dict(self.filter_reasons)))
        object.__setattr__(self, "target_prices", MappingProxyType(dict(self.target_prices)))
        object.__setattr__(self, "long_groups", tuple(self.long_groups))

    @property
    def review_eligible(self) -> tuple[FeatureSnapshot, ...]:
        if not self.board_scoring_complete or self.strategy is Strategy.LONG:
            return ()
        eligible = tuple(feature for feature in self.eligible if feature.board_data_reliability >= 0.85)
        if self.review_candidate_codes is None:
            return eligible
        allowed = frozenset(self.review_candidate_codes)
        return tuple(feature for feature in eligible if feature.quote.code in allowed)


class _FrozenReplayOptions(TypedDict):
    phase: str
    trade_date: str
    data_version: str
    filtered_count: int
    filter_reasons: Mapping[str, int]
    filter_details: Sequence[FilterAudit]


class _MergeRequiredOptions(TypedDict):
    now: datetime
    phase: str
    max_age_seconds: float


class _MergeOptionalOptions(TypedDict, total=False):
    target_prices: Mapping[str, float | None] | None


class _MergeOptions(_MergeRequiredOptions, _MergeOptionalOptions):
    pass


class _MergeCandidatesOptions(_MergeOptions):
    review_port: DeepSeekReviewPort | None
    review_deadline: datetime


class _MergeReviewedOptionalOptions(_MergeOptionalOptions, total=False):
    apply_downside: bool
    require_industry_breadth: bool


class _MergeReviewedOptions(_MergeRequiredOptions, _MergeReviewedOptionalOptions):
    pass


@dataclass(frozen=True)
class _SelectionResult:
    selected: tuple[Recommendation, ...]
    skips: tuple[SelectionSkip, ...]
    close_fallback_floor_relaxed: bool = False


@dataclass(frozen=True)
class _SelectionRequest:
    strategy: Strategy
    phase: str
    minimum_score: float | None
    selection: AppSelectionPolicy
    legacy_v16: bool
    legacy_replay: bool


def _select_final_recommendations(
    merged: Sequence[Recommendation],
    request: _SelectionRequest,
) -> _SelectionResult:
    if request.minimum_score is None:
        return _SelectionResult((), ())
    if request.strategy is Strategy.LONG or request.legacy_v16:
        selected, skips = select_top_k_with_audit(
            merged,
            _ranking_policy(
                request.selection,
                top_k=request.selection.default_top_k,
                minimum_final_score=request.minimum_score,
                enforce_competition_group_limits=request.legacy_replay,
            ),
        )
        return _SelectionResult(selected, skips)
    return _select_short_recommendations(
        merged,
        phase=request.phase,
        minimum_score=request.minimum_score,
        selection=request.selection,
        enforce_competition_group_limits=request.legacy_replay,
    )


def _select_short_recommendations(
    merged: Sequence[Recommendation],
    *,
    phase: str,
    minimum_score: float,
    selection: AppSelectionPolicy,
    enforce_competition_group_limits: bool,
) -> _SelectionResult:
    executable, executable_skips = select_top_k_with_audit(
        (item for item in merged if item.action is RecommendationAction.EXECUTABLE),
        _ranking_policy(
            selection,
            top_k=selection.default_top_k,
            minimum_final_score=minimum_score,
            enforce_competition_group_limits=enforce_competition_group_limits,
        ),
    )
    watch, watch_skips = select_top_k_with_audit(
        (item for item in merged if item.action is RecommendationAction.OBSERVE),
        _ranking_policy(
            selection,
            top_k=_CLOSE_FALLBACK_OBSERVE_TOP_K,
            minimum_final_score=minimum_score,
            enforce_competition_group_limits=enforce_competition_group_limits,
        ),
    )
    selected = (*executable, *watch)
    skips = (*executable_skips, *watch_skips)
    if selected or phase != "close_fallback":
        return _SelectionResult(selected, skips)
    fallback, fallback_skips = select_top_k_with_audit(
        _close_fallback_observe_pool(merged),
        _ranking_policy(
            selection,
            top_k=_CLOSE_FALLBACK_OBSERVE_TOP_K,
            minimum_final_score=0.0,
            enforce_competition_group_limits=enforce_competition_group_limits,
        ),
    )
    return _SelectionResult(
        fallback,
        (*skips, *fallback_skips),
        close_fallback_floor_relaxed=bool(fallback),
    )


def _ranking_policy(
    selection: AppSelectionPolicy,
    *,
    top_k: int,
    minimum_final_score: float,
    enforce_competition_group_limits: bool = False,
) -> RankingSelectionPolicy:
    return RankingSelectionPolicy(
        top_k=top_k,
        maximum_per_industry=selection.maximum_per_industry,
        minimum_final_score=minimum_final_score,
        maximum_board_fraction=selection.maximum_board_fraction,
        competition_group_limits=selection.competition_group_limits,
        enforce_competition_group_limits=enforce_competition_group_limits,
    )


def _close_fallback_observe_pool(candidates: Sequence[Recommendation]) -> tuple[Recommendation, ...]:
    return tuple(
        replace(
            item,
            action=RecommendationAction.OBSERVE,
            action_reason=_close_fallback_action_reason(item),
        )
        for item in candidates
        if not item.veto
    )


def _close_fallback_action_reason(item: Recommendation) -> str:
    if item.action is RecommendationAction.OBSERVE and item.action_reason:
        return item.action_reason
    reason = item.action_reason or "below_observation_floor"
    return f"close_fallback_observe_only:{reason}"


class RecommendationFinalizationMixin:
    _policy: RecommendationPolicy
    _hard_filter: Callable[..., FilterResult]

    def finalize_snapshot(
        self,
        prepared: PreparedSnapshot,
        reviews: Mapping[str, DeepSeekReview],
        *,
        legacy_v16: bool = False,
        legacy_replay: bool = False,
        projection_stage: str = "hybrid",
    ) -> RecommendationSnapshot:
        if projection_stage not in {"local", "hybrid"}:
            raise ValueError("projection_stage must be local or hybrid")
        if not prepared.board_scoring_complete:
            reasons = ",".join(prepared.board_degraded_reasons) or "unknown"
            raise RuntimeError(f"v16 board scoring is incomplete: {reasons}")
        strategy = prepared.strategy
        eligible = prepared.eligible
        now = prepared.now
        phase = prepared.phase
        selection_skips: tuple[SelectionSkip, ...]
        if strategy is Strategy.LONG:
            merged = tuple(prepared.local_candidates)
            fusion_mode = FusionMode.LOCAL_DEGRADED
            minimum_score = None
            selected = tuple(
                replace(
                    item,
                    rank=index,
                    target_price=prepared.target_prices.get(item.features.quote.code),
                )
                for index, item in enumerate(merged, start=1)
            )
            selection = _SelectionResult(selected, ())
            selection_skips = ()
        else:
            merged, fusion_mode = self._merge_reviewed_candidates(
                strategy,
                prepared.local_candidates,
                reviews,
                now=now,
                phase=phase,
                max_age_seconds=prepared.max_age_seconds,
                target_prices=prepared.target_prices,
                apply_downside=not legacy_v16,
                require_industry_breadth=legacy_replay,
            )
            minimum_score = minimum_selection_score(
                strategy,
                self._policy.selection.thresholds,
                phase=phase,
                observation_margin=self._policy.selection.observation_margin,
            )
            selection = _select_final_recommendations(
                merged,
                _SelectionRequest(
                    strategy=strategy,
                    phase=phase,
                    minimum_score=minimum_score,
                    selection=self._policy.selection,
                    legacy_v16=legacy_v16,
                    legacy_replay=legacy_replay,
                ),
            )
            selected = selection.selected
            selection_skips = selection.skips
        identity_data_version = (
            prepared.data_version if legacy_replay else f"{prepared.data_version}|projection={projection_stage}"
        )
        snapshot_id = _snapshot_id(strategy, prepared.trade_date, phase, identity_data_version, now)
        degraded_reasons: list[str] = list(prepared.board_degraded_reasons)
        if selection.close_fallback_floor_relaxed:
            degraded_reasons.append(_CLOSE_FALLBACK_OBSERVATION_FLOOR_REASON)
        if strategy is not Strategy.LONG and fusion_mode is FusionMode.LOCAL_DEGRADED:
            degraded_reasons.append(
                "deepseek_pending"
                if projection_stage == "local" and prepared.review_eligible
                else "deepseek_incomplete"
                if prepared.review_eligible
                else "deepseek_skipped_no_eligible_candidates"
            )
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
        if strategy is Strategy.D25:
            structured_fields = _LEGACY_STRUCTURED_RISK_FIELDS if legacy_replay else _STRUCTURED_RISK_FIELDS
            research_fields = structured_fields
            research_covered_count = sum(
                all(feature.optional_value(field) is not None for field in research_fields) for feature in eligible
            )
            if research_covered_count != len(eligible):
                degraded_reasons.append("d25_structured_research_incomplete")
        corporate_risk_covered_count = sum(
            feature.value("corporate_risk_history_unavailable", 1.0) == 0.0 for feature in eligible
        )
        corporate_risk_registry_versions = sorted(
            {
                evidence.data_version
                for feature in eligible
                for evidence in feature.evidence
                if evidence.source == "issuer_disclosure" and evidence.data_version
            }
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
                **({} if legacy_replay else {"projection_stage": projection_stage}),
                "candidate_count": len(eligible),
                "reviewed_count": sum(
                    review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN} for review in reviews.values()
                ),
                **(
                    {}
                    if legacy_replay
                    else {
                        "corporate_risk_covered_count": corporate_risk_covered_count,
                        "corporate_risk_coverage_ratio": (
                            corporate_risk_covered_count / len(eligible) if eligible else 0.0
                        ),
                        "corporate_risk_registry_versions": corporate_risk_registry_versions,
                    }
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
                "selection_diagnostics": _selection_diagnostics(
                    merged,
                    selected,
                    minimum_score,
                    strategy,
                    phase,
                ),
                **(
                    {"long_groups": long_groups_metadata(prepared.long_groups, selected)}
                    if strategy is Strategy.LONG
                    else {}
                ),
                **(
                    {"close_fallback_observation_floor_relaxed": True} if selection.close_fallback_floor_relaxed else {}
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
        **options: Unpack[_FrozenReplayOptions],
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
            phase=options["phase"],
            trade_date=options["trade_date"],
            data_version=options["data_version"],
            review_deadline=replay_input.evaluated_at,
            max_age_seconds=replay_input.score_max_age_seconds,
            filtered_count=options["filtered_count"],
            filter_reasons=options["filter_reasons"],
            filter_details=tuple(options["filter_details"]),
            target_prices=replay_input.target_prices,
            long_groups=(),
            market_features=replay_input.market_features,
            requested_codes=replay_input.requested_codes,
            preselect_max_age_seconds=replay_input.preselect_max_age_seconds,
            candidate_pool_size=replay_input.candidate_pool_size,
            board_batches=batches,
            board_degraded_reasons=degraded,
            review_candidate_codes=None,
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
        **options: Unpack[_MergeCandidatesOptions],
    ) -> tuple[tuple[Recommendation, ...], Mapping[str, DeepSeekReview], FusionMode]:
        now = options["now"]
        phase = options["phase"]
        review_port = options["review_port"]
        local_candidates = tuple(self._local_candidate(strategy, feature, now) for feature in eligible)
        reviews = (
            review_port.review(
                strategy,
                tuple(eligible),
                phase=phase,
                deadline=options["review_deadline"],
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
            max_age_seconds=options["max_age_seconds"],
            target_prices=options.get("target_prices"),
        )
        return merged, reviews, fusion_mode

    def _merge_reviewed_candidates(
        self,
        strategy: Strategy,
        local_candidates: Sequence[Recommendation],
        reviews: Mapping[str, DeepSeekReview],
        **options: Unpack[_MergeReviewedOptions],
    ) -> tuple[tuple[Recommendation, ...], FusionMode]:
        now = options["now"]
        phase = options["phase"]
        max_age_seconds = options["max_age_seconds"]
        target_prices = options.get("target_prices")
        apply_downside = options.get("apply_downside", True)
        require_industry_breadth = options.get("require_industry_breadth", False)
        fusion_candidates = tuple(
            candidate
            for candidate in local_candidates
            if candidate.features.board_data_reliability >= self._policy.selection.minimum_board_reliability
        )
        fusion_mode = _fusion_mode(
            fusion_candidates,
            reviews,
            self._policy.selection,
            strategy,
            phase,
        )
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
                downside=(
                    assess_downside(
                        local.features,
                        strategy,
                        require_industry_breadth=require_industry_breadth,
                    )
                    if apply_downside
                    else None
                ),
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
