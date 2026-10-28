"""Pure scored review selection, controlled fusion, and freeze epochs."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from functools import cached_property
from types import MappingProxyType
from typing import Literal, TypeAlias
from zoneinfo import ZoneInfo

from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.recommendation.domain.candidate.composition import LocalScoreResult
from trader.recommendation.domain.evidence.review import (
    DeepSeekReview,
    ReviewCandidateContext,
    ReviewOutcome,
    RiskFact,
    RiskRule,
)
from trader.recommendation.domain.market.factors import RoundScore
from trader.recommendation.domain.market.models import Board, FeatureSnapshot
from trader.recommendation.domain.publication.models import (
    FusionMode,
    RecommendationAction,
    ScoreBreakdown,
    ScoredDisposition,
    ScoredSelectionResult,
    ScoredStockEvaluation,
    Strategy,
)
from trader.recommendation.domain.publication.score_ledger import ScoreLedger
from trader.recommendation.domain.risk.downside import AssessDownside
from trader.recommendation.domain.risk.fusion import (
    DIMENSION_NAMES,
    ApplyStructuredReview,
    FusionPolicy,
    FusionRequest,
)

FREEZE_EPOCH_SCHEMA_VERSION = "freeze_epoch"
_SHANGHAI_TIMEZONE = "Asia/Shanghai"
_REASON_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_CanonicalValue: TypeAlias = str | int | float | bool | None | list["_CanonicalValue"] | dict[str, "_CanonicalValue"]


@dataclass(frozen=True)
class ScoredFreezePolicy:
    dimension_weights: Mapping[str, float]
    risk_rules: Mapping[str, RiskRule]
    executable_threshold: float
    observation_margin: float
    fusion: FusionPolicy
    review_candidate_limit: int = 6
    top_k: int = 10
    observation_limit: int = 8
    maximum_board_fraction: float = 0.60
    executable_enabled: bool = True
    strategy: Strategy = Strategy.TOMORROW

    def __post_init__(self) -> None:
        weights = dict(self.dimension_weights)
        rules = dict(self.risk_rules)
        _validate_freeze_weights(weights)
        _validate_freeze_limits(self)
        if self.strategy not in {Strategy.TOMORROW, Strategy.D25}:
            raise ValueError("scored freeze policy requires a scored strategy")
        object.__setattr__(self, "dimension_weights", MappingProxyType(weights))
        object.__setattr__(self, "risk_rules", MappingProxyType(rules))


@dataclass(frozen=True)
class ScoredReviewCandidate:
    evaluation: ScoredStockEvaluation
    context: ReviewCandidateContext

    @property
    def code(self) -> str:
        return self.evaluation.code

    @property
    def features(self) -> FeatureSnapshot:
        return self.evaluation.features


@dataclass(frozen=True)
class ScoredFreezeEntry:
    features: FeatureSnapshot
    disposition: ScoredDisposition
    score: ScoreBreakdown
    action: RecommendationAction
    action_reason: str
    selected: bool
    rank: int
    candidate_score: float | None
    candidate_rank: int
    board_rank: int
    local_risk_facts: tuple[RiskFact, ...]
    deepseek_risk_facts: tuple[RiskFact, ...]
    review: DeepSeekReview | None
    review_outcome: ReviewOutcome | None
    veto: bool
    score_ledger: ScoreLedger | None = field(default=None, kw_only=True)
    selection_rank: int = 0
    local_selection_skip_reason: str = ""
    freeze_skip_reason: str = ""

    @property
    def code(self) -> str:
        return self.features.quote.code


@dataclass(frozen=True)
class ReviewedScoredEvaluation:
    evaluation: ScoredStockEvaluation
    review: DeepSeekReview | None


@dataclass(frozen=True)
class FusedScoredEvaluation:
    evaluation: ScoredStockEvaluation
    score: ScoreBreakdown
    deepseek_risk_facts: tuple[RiskFact, ...]
    review: DeepSeekReview | None
    veto: bool


@dataclass(frozen=True)
class _NormalizedFreezePayload:
    entries: tuple[ScoredFreezeEntry, ...]
    codes: frozenset[str]
    review_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    selected: tuple[ScoredFreezeEntry, ...]
    reason_counts: dict[str, int]
    populations: dict[str, str]


@dataclass(frozen=True)
class FreezeSelectionLimits:
    top_k: int
    observation_limit: int
    maximum_board_fraction: float

    def __post_init__(self) -> None:
        if not 0 <= self.top_k <= 10 or not 0 <= self.observation_limit <= 8:
            raise ValueError("freeze selection pool limits are invalid")
        if not math.isfinite(self.maximum_board_fraction) or not 0.0 < self.maximum_board_fraction <= 0.60:
            raise ValueError("freeze selection board fraction is invalid")


@dataclass(frozen=True)
class FreezeEpoch:
    trade_date: date
    sequence: int
    observed_at: datetime
    config_version: str
    strategy_version: str
    fusion_version: str
    market_epoch_version: str
    candidate_epoch_version: str | None
    research_epoch_version: str | None
    projection_stage: Literal["local", "hybrid"]
    parent_freeze_version: str | None
    entries: tuple[ScoredFreezeEntry, ...]
    review_candidate_codes: tuple[str, ...]
    evaluated_count: int
    rejected_count: int
    unscored_count: int
    filter_reason_counts: Mapping[str, int]
    population_versions: Mapping[str, str]
    selection_limits: FreezeSelectionLimits
    degraded_reasons: tuple[str, ...] = ()
    schema_version: str = FREEZE_EPOCH_SCHEMA_VERSION
    version: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_freeze_coordinates(self)
        payload = _normalize_freeze_payload(self)
        _validate_freeze_entries(self, payload)
        _validate_freeze_metadata(self, payload)
        object.__setattr__(self, "entries", payload.entries)
        object.__setattr__(self, "review_candidate_codes", payload.review_codes)
        object.__setattr__(self, "filter_reason_counts", MappingProxyType(payload.reason_counts))
        object.__setattr__(self, "population_versions", MappingProxyType(payload.populations))
        object.__setattr__(self, "degraded_reasons", payload.reasons)
        object.__setattr__(
            self,
            "version",
            f"freeze:{self.trade_date.isoformat()}:{self.projection_stage}:{self.sequence}",
        )

    @cached_property
    def content_hash(self) -> str:
        return _freeze_epoch_hash(self, _normalize_freeze_payload(self))


@dataclass(frozen=True)
class ScoredFreezeRequest:
    selection: ScoredSelectionResult
    reviews: Mapping[str, DeepSeekReview]
    observed_at: datetime
    trade_date: date
    sequence: int
    config_version: str
    strategy_version: str
    fusion_version: str
    market_epoch_version: str
    candidate_epoch_version: str | None
    research_epoch_version: str | None
    projection_stage: Literal["local", "hybrid"]
    parent_freeze_version: str | None
    review_candidate_codes: tuple[str, ...]
    degraded_reasons: tuple[str, ...]
    policy: ScoredFreezePolicy

    def __post_init__(self) -> None:
        _require_shanghai_time(self.observed_at, "freeze request observed_at")
        reviews = dict(self.reviews)
        review_codes = _sorted_unique_codes(self.review_candidate_codes, "review_candidate_codes")
        evaluations = {item.code: item for item in self.selection.scored_candidates}
        if any(
            code not in evaluations
            or evaluations[code].disposition is not ScoredDisposition.PASS
            or evaluations[code].local_score is None
            or any(fact.veto for fact in evaluations[code].local_risk_facts)
            for code in review_codes
        ):
            raise ValueError("review candidates must be scored pass candidates without veto")
        if any(code != review.code for code, review in reviews.items()):
            raise ValueError("DeepSeek review keys must match review codes")
        for review in reviews.values():
            _require_shanghai_time(review.completed_at, "DeepSeek review completed_at")
            for fact in review.risk_facts:
                _require_shanghai_time(fact.observed_at, "DeepSeek risk observed_at")
        if any(code not in set(review_codes) for code in reviews):
            raise ValueError("DeepSeek review is outside the review candidate set")
        if self.projection_stage == "local" and reviews:
            raise ValueError("local freeze cannot contain DeepSeek reviews")
        if self.projection_stage == "hybrid" and not any(
            review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN} and review.completed_at <= self.observed_at
            for review in reviews.values()
        ):
            raise ValueError("hybrid freeze requires a current usable DeepSeek review")
        object.__setattr__(self, "reviews", MappingProxyType(reviews))
        object.__setattr__(self, "review_candidate_codes", review_codes)


def _validate_freeze_weights(weights: Mapping[str, float]) -> None:
    if set(weights) != set(DIMENSION_NAMES) or abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError("scored DeepSeek weights must contain five dimensions and sum to 1.0")
    if any(not math.isfinite(value) or value < 0.0 for value in weights.values()):
        raise ValueError("scored DeepSeek weights must be finite and non-negative")
    if weights["industry_policy"] != 0.0:
        raise ValueError("scored industry_policy DeepSeek weight must remain zero")


def _validate_freeze_limits(policy: ScoredFreezePolicy) -> None:
    if not math.isfinite(policy.executable_threshold) or not 0.0 <= policy.executable_threshold <= 100.0:
        raise ValueError("scored executable threshold must be in [0, 100]")
    if not math.isfinite(policy.observation_margin) or policy.observation_margin < 0.0:
        raise ValueError("scored observation margin must be finite and non-negative")
    if not 0 <= policy.review_candidate_limit <= 6:
        raise ValueError("scored review candidate limit must be in [0, 6]")
    if not 0 <= policy.top_k <= 10:
        raise ValueError("scored TopK must be in [0, 10]")
    if not 0 <= policy.observation_limit <= 8:
        raise ValueError("scored observation limit must be in [0, 8]")
    if not 0.0 < policy.maximum_board_fraction <= 0.60:
        raise ValueError("scored board fraction must be in (0, 0.60]")


def _validate_freeze_coordinates(epoch: FreezeEpoch) -> None:
    if epoch.sequence < 0:
        raise ValueError("freeze epoch sequence cannot be negative")
    _require_shanghai_time(epoch.observed_at, "freeze observed_at")
    if epoch.observed_at.date() != epoch.trade_date:
        raise ValueError("freeze trade date must match observed_at")
    for value, name in (
        (epoch.config_version, "config_version"),
        (epoch.strategy_version, "strategy_version"),
        (epoch.fusion_version, "fusion_version"),
        (epoch.market_epoch_version, "market_epoch_version"),
    ):
        _require_text(value, name)
    if epoch.schema_version != FREEZE_EPOCH_SCHEMA_VERSION:
        raise ValueError(f"freeze schema_version must be {FREEZE_EPOCH_SCHEMA_VERSION}")
    if epoch.projection_stage == "local" and epoch.parent_freeze_version is not None:
        raise ValueError("local freeze cannot reference a parent freeze")
    if epoch.projection_stage == "hybrid" and not epoch.parent_freeze_version:
        raise ValueError("hybrid freeze must reference its local parent")


def _normalize_freeze_payload(epoch: FreezeEpoch) -> _NormalizedFreezePayload:
    entries = tuple(sorted(epoch.entries, key=lambda item: item.code))
    codes = frozenset(item.code for item in entries)
    review_codes = _sorted_unique_codes(epoch.review_candidate_codes, "review_candidate_codes")
    reasons = tuple(sorted(set(epoch.degraded_reasons)))
    selected = tuple(sorted((item for item in entries if item.selected), key=lambda item: item.rank))
    return _NormalizedFreezePayload(
        entries=entries,
        codes=codes,
        review_codes=review_codes,
        reasons=reasons,
        selected=selected,
        reason_counts=dict(sorted(epoch.filter_reason_counts.items())),
        populations=dict(sorted(epoch.population_versions.items())),
    )


def _validate_freeze_entries(
    epoch: FreezeEpoch,
    payload: _NormalizedFreezePayload,
) -> None:
    if len(payload.entries) != len(payload.codes):
        raise ValueError("freeze entries must contain unique codes")
    if any(item.features.observed_at > epoch.observed_at for item in payload.entries):
        raise ValueError("freeze cannot contain future features")
    _validate_freeze_risk_times(payload.entries, epoch.observed_at)
    if len(payload.review_codes) > 6:
        raise ValueError("freeze epoch cannot exceed six review candidates")
    if any(code not in payload.codes for code in payload.review_codes):
        raise ValueError("freeze review candidates must reference freeze entries")
    if [item.rank for item in payload.selected] != list(range(1, len(payload.selected) + 1)):
        raise ValueError("selected freeze ranks must be contiguous")
    _validate_selection_ranks(payload.entries)
    _validate_stage_entries(epoch.projection_stage, payload.entries, set(payload.review_codes))
    _validate_selected_pools(list(payload.selected), epoch.selection_limits)


def _validate_freeze_risk_times(
    entries: tuple[ScoredFreezeEntry, ...],
    observed_at: datetime,
) -> None:
    for item in entries:
        for fact in (*item.local_risk_facts, *item.deepseek_risk_facts):
            _require_shanghai_time(fact.observed_at, "freeze risk observed_at")
            if fact.observed_at > observed_at:
                raise ValueError("freeze cannot contain future risk facts")


def _validate_freeze_metadata(
    epoch: FreezeEpoch,
    payload: _NormalizedFreezePayload,
) -> None:
    if any(_REASON_CODE.fullmatch(reason) is None for reason in payload.reasons):
        raise ValueError("freeze degraded reasons must be structured codes")
    if epoch.evaluated_count < len(payload.entries):
        raise ValueError("freeze evaluated count cannot be smaller than scored entries")
    if not 0 <= epoch.rejected_count <= epoch.evaluated_count:
        raise ValueError("freeze rejected count is invalid")
    if not 0 <= epoch.unscored_count <= epoch.evaluated_count:
        raise ValueError("freeze unscored count is invalid")
    if any(_REASON_CODE.fullmatch(reason) is None or count < 1 for reason, count in payload.reason_counts.items()):
        raise ValueError("freeze filter reason counts are invalid")
    if any(not board.strip() or not version.strip() for board, version in payload.populations.items()):
        raise ValueError("freeze population versions must not be empty")


def _freeze_epoch_hash(
    epoch: FreezeEpoch,
    payload: _NormalizedFreezePayload,
) -> str:
    return _content_hash(
        {
            "schema_version": epoch.schema_version,
            "trade_date": epoch.trade_date,
            "sequence": epoch.sequence,
            "observed_at": epoch.observed_at,
            "config_version": epoch.config_version,
            "strategy_version": epoch.strategy_version,
            "fusion_version": epoch.fusion_version,
            "market_epoch_version": epoch.market_epoch_version,
            "candidate_epoch_version": epoch.candidate_epoch_version,
            "research_epoch_version": epoch.research_epoch_version,
            "projection_stage": epoch.projection_stage,
            "parent_freeze_version": epoch.parent_freeze_version,
            "entries": tuple(_freeze_entry_identity(item) for item in payload.entries),
            "review_candidate_codes": payload.review_codes,
            "evaluated_count": epoch.evaluated_count,
            "rejected_count": epoch.rejected_count,
            "unscored_count": epoch.unscored_count,
            "filter_reason_counts": payload.reason_counts,
            "population_versions": payload.populations,
            "selection_limits": epoch.selection_limits,
            "degraded_reasons": payload.reasons,
        }
    )


def SelectScoredReviewCandidates(
    fixed_seats: tuple[ScoredStockEvaluation, ...],
    policy: ScoredFreezePolicy,
) -> tuple[ScoredReviewCandidate, ...]:
    eligible = tuple(
        item
        for item in fixed_seats
        if item.disposition is ScoredDisposition.PASS
        and not item.execution_gate_reason
        and item.local_score is not None
        and not any(fact.veto for fact in item.local_risk_facts)
    )
    ordered = sorted(eligible, key=_local_order)
    contexts = {item.code: _review_context(item, index + 1, policy) for index, item in enumerate(fixed_seats)}
    reviewable = tuple(item for item in ordered if _primary_review_predicate(contexts[item.code]))
    prioritized = sorted(
        reviewable,
        key=lambda item: (
            not contexts[item.code].has_new_high_risk,
            not contexts[item.code].near_action_threshold,
            not contexts[item.code].near_global_boundary,
            not contexts[item.code].evidence_conflict,
            contexts[item.code].local_rank,
            item.code,
        ),
    )
    return tuple(
        ScoredReviewCandidate(item, contexts[item.code]) for item in prioritized[: policy.review_candidate_limit]
    )


def BuildScoredFreezeEpoch(
    request: ScoredFreezeRequest,
    *,
    entries: tuple[ScoredFreezeEntry, ...] | None = None,
) -> FreezeEpoch:
    if entries is None:
        fixed = SelectFixedScoredSeats(request.selection.scored_candidates, request.policy)
        reviewed = ReviewScoredEvaluations(fixed, tuple(request.reviews.values()), request.observed_at)
        fused = FuseScoredEvaluations(reviewed, request.policy, request.observed_at)
        entries = tuple(
            replace(item, selected=True, rank=rank, selection_rank=rank)
            for rank, item in enumerate(ApplyScoredActions(fused, request.policy), start=1)
        )
    reason_counts = _filter_reason_counts(request.selection)
    return FreezeEpoch(
        trade_date=request.trade_date,
        sequence=request.sequence,
        observed_at=request.observed_at,
        config_version=request.config_version,
        strategy_version=request.strategy_version,
        fusion_version=request.fusion_version,
        market_epoch_version=request.market_epoch_version,
        candidate_epoch_version=request.candidate_epoch_version,
        research_epoch_version=request.research_epoch_version,
        projection_stage=request.projection_stage,
        parent_freeze_version=request.parent_freeze_version,
        entries=entries,
        review_candidate_codes=request.review_candidate_codes,
        evaluated_count=len(request.selection.evaluations),
        rejected_count=sum(item.disposition is ScoredDisposition.REJECT for item in request.selection.evaluations),
        unscored_count=sum(item.local_score is None for item in request.selection.evaluations),
        filter_reason_counts=reason_counts,
        population_versions={board.value: version for board, version in request.selection.population_versions.items()},
        selection_limits=FreezeSelectionLimits(
            top_k=request.policy.top_k,
            observation_limit=request.policy.observation_limit,
            maximum_board_fraction=request.policy.maximum_board_fraction,
        ),
        degraded_reasons=request.degraded_reasons,
    )


def ReviewScoredEvaluations(
    evaluations: tuple[ScoredStockEvaluation, ...],
    reviews: tuple[DeepSeekReview, ...],
    observed_at: datetime,
) -> tuple[ReviewedScoredEvaluation, ...]:
    result: list[ReviewedScoredEvaluation] = []
    for evaluation in evaluations:
        review = next((item for item in reviews if item.code == evaluation.code), None)
        if review is not None and review.completed_at > observed_at:
            review = replace(review, outcome=ReviewOutcome.LATE, error="review_completed_after_freeze")
        result.append(ReviewedScoredEvaluation(evaluation, review))
    return tuple(result)


def FuseScoredEvaluations(
    reviewed: tuple[ReviewedScoredEvaluation, ...],
    policy: ScoredFreezePolicy,
    observed_at: datetime,
) -> tuple[FusedScoredEvaluation, ...]:
    return tuple(_fuse_evaluation(item, policy, observed_at) for item in reviewed)


def _fuse_evaluation(
    reviewed: ReviewedScoredEvaluation,
    policy: ScoredFreezePolicy,
    observed_at: datetime,
) -> FusedScoredEvaluation:
    evaluation = reviewed.evaluation
    local_base = evaluation.local_base_score or 0.0
    local_components = evaluation.local_components
    review = reviewed.review
    effective_review = review if review is not None and review.outcome is ReviewOutcome.APPLIED else None
    fusion_mode = FusionMode.HYBRID if effective_review is not None else FusionMode.LOCAL_DEGRADED
    if evaluation.local_score is None or evaluation.local_risk_penalty is None:
        raise ValueError("review requires an already risk-deducted score")
    local = ScoreBreakdown(
        local_components,
        RoundScore(local_base),
        evaluation.local_risk_penalty,
        evaluation.local_score,
        None,
        0.0,
        0.0,
        evaluation.local_score,
        fusion_mode,
        False,
    )
    fused = ApplyStructuredReview(
        local,
        FusionRequest(
            local=LocalScoreResult(components=local_components, base_score=local_base),
            local_risk_facts=evaluation.local_risk_facts,
            review=effective_review,
            risk_rules=policy.risk_rules,
            fusion_mode=fusion_mode,
            policy=policy.fusion,
            evidence=evaluation.features.evidence,
            evaluated_at=observed_at,
        ),
    )
    if evaluation.local_score is not None and fused.score.local_score != RoundScore(evaluation.local_score):
        raise ValueError("scored local score changed before fusion")
    veto = fused.veto or any(fact.veto for fact in evaluation.local_risk_facts)
    score = (
        replace(fused.score, final_score=evaluation.ranking_score)
        if evaluation.ranking_score is not None
        else fused.score
    )
    return FusedScoredEvaluation(evaluation, score, fused.deepseek_risk_facts, review, veto)


def ApplyScoredActions(
    fused: tuple[FusedScoredEvaluation, ...],
    policy: ScoredFreezePolicy,
) -> tuple[ScoredFreezeEntry, ...]:
    return tuple(_action_entry(item, policy) for item in fused)


def _action_entry(fused: FusedScoredEvaluation, policy: ScoredFreezePolicy) -> ScoredFreezeEntry:
    evaluation = fused.evaluation
    review = fused.review
    action, action_reason = _action_for(evaluation, fused.score, fused.veto, policy)
    return ScoredFreezeEntry(
        score_ledger=evaluation.score_ledger,
        features=evaluation.features,
        disposition=evaluation.disposition,
        score=fused.score,
        action=action,
        action_reason=action_reason,
        selected=False,
        rank=0,
        candidate_score=evaluation.candidate_score,
        candidate_rank=evaluation.candidate_rank,
        board_rank=evaluation.board_rank,
        local_risk_facts=evaluation.local_risk_facts,
        deepseek_risk_facts=fused.deepseek_risk_facts,
        review=review,
        review_outcome=review.outcome if review is not None else None,
        veto=fused.veto,
        selection_rank=0,
        local_selection_skip_reason=evaluation.selection_skip_reason,
        freeze_skip_reason="" if action is not RecommendationAction.UNAVAILABLE else action_reason,
    )


def _action_for(
    evaluation: ScoredStockEvaluation,
    score: ScoreBreakdown,
    veto: bool,
    policy: ScoredFreezePolicy,
) -> tuple[RecommendationAction, str]:
    unavailable_reason = _unavailable_reason(evaluation, score, veto, policy)
    if unavailable_reason is not None:
        return RecommendationAction.UNAVAILABLE, unavailable_reason
    if evaluation.disposition is ScoredDisposition.OBSERVE_ONLY:
        return RecommendationAction.OBSERVE, "filter_observe_only"
    if not policy.executable_enabled:
        return RecommendationAction.OBSERVE, "observation_phase"
    if score.local_score >= policy.executable_threshold:
        downside = AssessDownside(
            evaluation.features,
            policy.strategy,
            require_industry_breadth=False,
        )
        if downside.status == "observe":
            return RecommendationAction.OBSERVE, f"downside_guard:{','.join(downside.reasons)}"
        if evaluation.shadow_only:
            return RecommendationAction.OBSERVE, (
                "learned_rank_shadow_only" if evaluation.ranking_score is not None else "engineering_shadow_only"
            )
        return RecommendationAction.EXECUTABLE, "score_threshold_met"
    return RecommendationAction.OBSERVE, "near_score_threshold"


def _unavailable_reason(
    evaluation: ScoredStockEvaluation,
    score: ScoreBreakdown,
    veto: bool,
    policy: ScoredFreezePolicy,
) -> str | None:
    if evaluation.local_score is None or evaluation.disposition is ScoredDisposition.REJECT:
        return evaluation.selection_skip_reason or "not_scored"
    if evaluation.execution_gate_reason:
        return evaluation.execution_gate_reason
    if veto:
        return "risk_veto"
    if score.local_score < policy.executable_threshold - policy.observation_margin:
        return "below_score_threshold"
    return None


def SelectFixedScoredSeats(
    evaluations: tuple[ScoredStockEvaluation, ...],
    policy: ScoredFreezePolicy,
) -> tuple[ScoredStockEvaluation, ...]:
    """Fix six seats before optional review and action evaluation."""
    selected: list[ScoredStockEvaluation] = []
    boards: Counter[Board] = Counter()
    maximum_per_board = math.ceil(6 * policy.maximum_board_fraction)
    for item in sorted(evaluations, key=_local_order):
        if item.local_score is None or item.execution_gate_reason or item.disposition is ScoredDisposition.REJECT:
            continue
        board = item.features.quote.board
        if boards[board] >= maximum_per_board:
            continue
        selected.append(item)
        boards[board] += 1
        if len(selected) == 6:
            break
    return tuple(selected)


def _review_context(
    item: ScoredStockEvaluation,
    rank: int,
    policy: ScoredFreezePolicy,
) -> ReviewCandidateContext:
    assert item.local_score is not None
    downside = AssessDownside(item.features, policy.strategy, require_industry_breadth=False)
    return ReviewCandidateContext(
        local_score=item.local_score,
        local_rank=rank,
        action_threshold=policy.executable_threshold,
        in_protection_set=downside.status == "observe",
        has_new_high_risk=any(
            fact.severity == "high" and fact.confidence >= 0.7
            for fact in (*item.local_risk_facts, *item.features.external_risk_facts)
        ),
        near_action_threshold=abs(item.local_score - policy.executable_threshold) <= policy.observation_margin,
        near_global_boundary=abs(rank - 6) <= 2,
        evidence_conflict=any(
            value in {"cross_source_deviation", "board_classification_conflict"}
            for value in item.features.quote.execution_restrictions
        ),
    )


def _primary_review_predicate(context: ReviewCandidateContext) -> bool:
    return any(
        (
            context.has_new_high_risk,
            context.near_action_threshold,
            context.near_global_boundary,
            context.evidence_conflict,
        )
    )


def _filter_reason_counts(selection: ScoredSelectionResult) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in selection.evaluations:
        counts.update(reason.code for reason in item.filter_reasons)
        counts.update(flag.code for flag in item.optional_flags)
        if item.selection_skip_reason:
            counts[item.selection_skip_reason] += 1
    return dict(counts)


def _validate_stage_entries(
    projection_stage: Literal["local", "hybrid"],
    entries: tuple[ScoredFreezeEntry, ...],
    review_codes: set[str],
) -> None:
    if projection_stage == "local" and any(
        item.review is not None
        or item.score.fusion_applied
        or item.score.deepseek_score is not None
        or item.deepseek_risk_facts
        for item in entries
    ):
        raise ValueError("local freeze entries cannot contain DeepSeek results")
    if any(item.review is not None and item.code not in review_codes for item in entries):
        raise ValueError("freeze review must belong to the review candidate set")
    if any(
        item.score.fusion_applied and (projection_stage != "hybrid" or item.review_outcome is not ReviewOutcome.APPLIED)
        for item in entries
    ):
        raise ValueError("fusion can only apply to an applied review in a hybrid freeze")


def _validate_selected_pools(
    selected: list[ScoredFreezeEntry],
    limits: FreezeSelectionLimits,
) -> None:
    if len(selected) > 6 or tuple(selected) != tuple(sorted(selected, key=_freeze_order)):
        raise ValueError("fixed freeze seats must preserve score order")
    board_limit = math.ceil(6 * limits.maximum_board_fraction)
    if any(count > board_limit for count in Counter(item.features.quote.board for item in selected).values()):
        raise ValueError("fixed freeze seats exceed the board limit")


def _validate_selection_ranks(entries: tuple[ScoredFreezeEntry, ...]) -> None:
    for item in entries:
        if item.selection_rank != item.rank:
            raise ValueError("selection rank must identify the fixed seat")


def _local_order(item: ScoredStockEvaluation) -> tuple[float, str]:
    score = item.ranking_score if item.ranking_score is not None else item.local_score or 0.0
    return (-score, item.code)


def _freeze_order(item: ScoredFreezeEntry) -> tuple[float, str]:
    return (-item.score.final_score, item.code)


def _freeze_entry_identity(item: ScoredFreezeEntry) -> dict[str, object]:
    population = item.features.board_population
    feature_identity = {
        "code": item.code,
        "merge_epoch": item.features.merge_epoch,
        "observed_at": item.features.observed_at,
        "quote_data_version": item.features.quote.data_version,
        "quote_source_time": item.features.quote.source_time,
        "board": item.features.quote.board,
        "industry": item.features.quote.industry,
        "board_policy_id": item.features.board_policy_id,
        "board_policy_version": item.features.board_policy_version,
        "population_version": population.population_version if population is not None else None,
        "evidence": tuple(
            (
                evidence.evidence_id,
                evidence.data_version,
                evidence.published_at,
                evidence.received_at,
            )
            for evidence in item.features.evidence
        ),
    }
    return {
        "feature_identity": feature_identity,
        "disposition": item.disposition,
        "score": item.score,
        "action": item.action,
        "action_reason": item.action_reason,
        "selected": item.selected,
        "rank": item.rank,
        "selection_rank": item.selection_rank,
        "candidate_score": item.candidate_score,
        "candidate_rank": item.candidate_rank,
        "board_rank": item.board_rank,
        "local_risk_facts": item.local_risk_facts,
        "deepseek_risk_facts": item.deepseek_risk_facts,
        "review": item.review,
        "review_outcome": item.review_outcome,
        "veto": item.veto,
        "local_selection_skip_reason": item.local_selection_skip_reason,
        "freeze_skip_reason": item.freeze_skip_reason,
    }


def _content_hash(value: object) -> str:
    return CanonicalDigest(value, include_derived=True)


def _sorted_unique_codes(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    normalized = tuple(sorted(values))
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must be unique")
    if any(len(value) != 6 or not value.isdigit() for value in normalized):
        raise ValueError(f"{name} must contain six-digit stock codes")
    return normalized


def _require_shanghai_time(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    if not (isinstance(value.tzinfo, ZoneInfo) and value.tzinfo.key == _SHANGHAI_TIMEZONE):
        raise ValueError(f"{name} must use Asia/Shanghai")


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


__all__ = [
    "FREEZE_EPOCH_SCHEMA_VERSION",
    "FreezeEpoch",
    "FreezeSelectionLimits",
    "ScoredFreezeEntry",
    "ScoredFreezePolicy",
    "ScoredFreezeRequest",
    "ScoredReviewCandidate",
    "BuildScoredFreezeEpoch",
    "SelectScoredReviewCandidates",
]
