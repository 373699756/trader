"""Pure tomorrow review selection, controlled fusion, and decision epochs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Literal, TypeAlias

from trader.domain.market.factors import round_score
from trader.domain.market.models import Board, FeatureSnapshot
from trader.domain.recommendation.fusion import (
    DIMENSION_NAMES,
    FusionPolicy,
    FusionRequest,
    fuse_score,
)
from trader.domain.recommendation.models import (
    FusionMode,
    RecommendationAction,
    ScoreBreakdown,
)
from trader.domain.recommendation.strategies.composition import LocalScoreResult
from trader.domain.recommendation.tomorrow_selection import (
    TomorrowDisposition,
    TomorrowSelectionResult,
    TomorrowStockEvaluation,
)
from trader.domain.review.models import (
    DeepSeekReview,
    ReviewCandidateContext,
    ReviewOutcome,
    RiskFact,
    RiskRule,
)

DECISION_EPOCH_SCHEMA_VERSION = "decision_epoch_v1"
_SHANGHAI_TIMEZONE = "Asia/Shanghai"
_REASON_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_CanonicalValue: TypeAlias = str | int | float | bool | None | list["_CanonicalValue"] | dict[str, "_CanonicalValue"]


@dataclass(frozen=True)
class TomorrowDecisionPolicy:
    dimension_weights: Mapping[str, float]
    risk_rules: Mapping[str, RiskRule]
    executable_threshold: float
    observation_margin: float
    review_candidate_limit: int = 28
    top_k: int = 10
    observation_limit: int = 8
    maximum_per_industry: int = 2
    maximum_board_fraction: float = 0.60
    fusion: FusionPolicy = field(default_factory=FusionPolicy)
    executable_enabled: bool = True

    def __post_init__(self) -> None:
        weights = dict(self.dimension_weights)
        rules = dict(self.risk_rules)
        _validate_decision_weights(weights)
        _validate_decision_limits(self)
        object.__setattr__(self, "dimension_weights", MappingProxyType(weights))
        object.__setattr__(self, "risk_rules", MappingProxyType(rules))


@dataclass(frozen=True)
class TomorrowReviewCandidate:
    evaluation: TomorrowStockEvaluation
    context: ReviewCandidateContext

    @property
    def code(self) -> str:
        return self.evaluation.code

    @property
    def features(self) -> FeatureSnapshot:
        return self.evaluation.features


@dataclass(frozen=True)
class TomorrowDecisionEntry:
    features: FeatureSnapshot
    disposition: TomorrowDisposition
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
    local_selection_skip_reason: str = ""
    decision_skip_reason: str = ""

    @property
    def code(self) -> str:
        return self.features.quote.code


@dataclass(frozen=True)
class _NormalizedDecisionPayload:
    entries: tuple[TomorrowDecisionEntry, ...]
    codes: frozenset[str]
    review_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    selected: tuple[TomorrowDecisionEntry, ...]
    reason_counts: dict[str, int]
    populations: dict[str, str]


@dataclass(frozen=True)
class DecisionEpoch:
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
    parent_decision_version: str | None
    entries: tuple[TomorrowDecisionEntry, ...]
    review_candidate_codes: tuple[str, ...]
    evaluated_count: int
    rejected_count: int
    unscored_count: int
    filter_reason_counts: Mapping[str, int]
    population_versions: Mapping[str, str]
    degraded_reasons: tuple[str, ...] = ()
    schema_version: str = DECISION_EPOCH_SCHEMA_VERSION
    content_hash: str = field(init=False)
    version: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_decision_coordinates(self)
        payload = _normalize_decision_payload(self)
        _validate_decision_entries(self, payload)
        _validate_decision_metadata(self, payload)
        payload_hash = _decision_epoch_hash(self, payload)
        object.__setattr__(self, "entries", payload.entries)
        object.__setattr__(self, "review_candidate_codes", payload.review_codes)
        object.__setattr__(self, "filter_reason_counts", MappingProxyType(payload.reason_counts))
        object.__setattr__(self, "population_versions", MappingProxyType(payload.populations))
        object.__setattr__(self, "degraded_reasons", payload.reasons)
        object.__setattr__(self, "content_hash", payload_hash)
        object.__setattr__(
            self,
            "version",
            f"decision:{self.trade_date.isoformat()}:{self.sequence}:{payload_hash[:16]}",
        )


@dataclass(frozen=True)
class TomorrowDecisionRequest:
    selection: TomorrowSelectionResult
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
    parent_decision_version: str | None
    review_candidate_codes: tuple[str, ...]
    degraded_reasons: tuple[str, ...]
    policy: TomorrowDecisionPolicy

    def __post_init__(self) -> None:
        _require_shanghai_time(self.observed_at, "decision request observed_at")
        reviews = dict(self.reviews)
        review_codes = _sorted_unique_codes(self.review_candidate_codes, "review_candidate_codes")
        evaluations = {item.code: item for item in self.selection.scored_candidates}
        if any(
            code not in evaluations
            or evaluations[code].disposition is not TomorrowDisposition.PASS
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
            raise ValueError("local decision cannot contain DeepSeek reviews")
        if self.projection_stage == "hybrid" and not any(
            review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN} and review.completed_at <= self.observed_at
            for review in reviews.values()
        ):
            raise ValueError("hybrid decision requires a current usable DeepSeek review")
        object.__setattr__(self, "reviews", MappingProxyType(reviews))
        object.__setattr__(self, "review_candidate_codes", review_codes)


def _validate_decision_weights(weights: Mapping[str, float]) -> None:
    if set(weights) != set(DIMENSION_NAMES) or abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError("tomorrow DeepSeek weights must contain five dimensions and sum to 1.0")
    if any(not math.isfinite(value) or value < 0.0 for value in weights.values()):
        raise ValueError("tomorrow DeepSeek weights must be finite and non-negative")
    if weights["industry_policy"] != 0.0:
        raise ValueError("tomorrow industry_policy DeepSeek weight must remain zero")


def _validate_decision_limits(policy: TomorrowDecisionPolicy) -> None:
    if not math.isfinite(policy.executable_threshold) or not 0.0 <= policy.executable_threshold <= 100.0:
        raise ValueError("tomorrow executable threshold must be in [0, 100]")
    if not math.isfinite(policy.observation_margin) or policy.observation_margin < 0.0:
        raise ValueError("tomorrow observation margin must be finite and non-negative")
    if not 0 <= policy.review_candidate_limit <= 28:
        raise ValueError("tomorrow review candidate limit must be in [0, 28]")
    if not 0 <= policy.top_k <= 10:
        raise ValueError("tomorrow TopK must be in [0, 10]")
    if not 0 <= policy.observation_limit <= 8:
        raise ValueError("tomorrow observation limit must be in [0, 8]")
    if not 1 <= policy.maximum_per_industry <= 2:
        raise ValueError("tomorrow industry limit must be in [1, 2]")
    if not 0.0 < policy.maximum_board_fraction <= 0.60:
        raise ValueError("tomorrow board fraction must be in (0, 0.60]")


def _validate_decision_coordinates(epoch: DecisionEpoch) -> None:
    if epoch.sequence < 0:
        raise ValueError("decision epoch sequence cannot be negative")
    _require_shanghai_time(epoch.observed_at, "decision observed_at")
    if epoch.observed_at.date() != epoch.trade_date:
        raise ValueError("decision trade date must match observed_at")
    for value, name in (
        (epoch.config_version, "config_version"),
        (epoch.strategy_version, "strategy_version"),
        (epoch.fusion_version, "fusion_version"),
        (epoch.market_epoch_version, "market_epoch_version"),
    ):
        _require_text(value, name)
    if epoch.schema_version != DECISION_EPOCH_SCHEMA_VERSION:
        raise ValueError(f"decision schema_version must be {DECISION_EPOCH_SCHEMA_VERSION}")
    if epoch.projection_stage == "local" and epoch.parent_decision_version is not None:
        raise ValueError("local decision cannot reference a parent decision")
    if epoch.projection_stage == "hybrid" and not epoch.parent_decision_version:
        raise ValueError("hybrid decision must reference its local parent")


def _normalize_decision_payload(epoch: DecisionEpoch) -> _NormalizedDecisionPayload:
    entries = tuple(sorted(epoch.entries, key=lambda item: item.code))
    codes = frozenset(item.code for item in entries)
    review_codes = _sorted_unique_codes(epoch.review_candidate_codes, "review_candidate_codes")
    reasons = tuple(sorted(set(epoch.degraded_reasons)))
    selected = tuple(sorted((item for item in entries if item.selected), key=lambda item: item.rank))
    return _NormalizedDecisionPayload(
        entries=entries,
        codes=codes,
        review_codes=review_codes,
        reasons=reasons,
        selected=selected,
        reason_counts=dict(sorted(epoch.filter_reason_counts.items())),
        populations=dict(sorted(epoch.population_versions.items())),
    )


def _validate_decision_entries(
    epoch: DecisionEpoch,
    payload: _NormalizedDecisionPayload,
) -> None:
    if len(payload.entries) != len(payload.codes):
        raise ValueError("decision entries must contain unique codes")
    if len(payload.entries) > 360:
        raise ValueError("decision epoch cannot exceed 360 scored candidates")
    if any(item.features.observed_at > epoch.observed_at for item in payload.entries):
        raise ValueError("decision cannot contain future features")
    _validate_decision_risk_times(payload.entries, epoch.observed_at)
    if len(payload.review_codes) > 28:
        raise ValueError("decision epoch cannot exceed 28 review candidates")
    if any(code not in payload.codes for code in payload.review_codes):
        raise ValueError("decision review candidates must reference decision entries")
    if [item.rank for item in payload.selected] != list(range(1, len(payload.selected) + 1)):
        raise ValueError("selected decision ranks must be contiguous")
    if any(item.action is RecommendationAction.UNAVAILABLE for item in payload.selected):
        raise ValueError("unavailable decisions cannot be selected")
    _validate_stage_entries(epoch.projection_stage, payload.entries, set(payload.review_codes))
    _validate_selected_pools(list(payload.selected))


def _validate_decision_risk_times(
    entries: tuple[TomorrowDecisionEntry, ...],
    observed_at: datetime,
) -> None:
    for item in entries:
        for fact in (*item.local_risk_facts, *item.deepseek_risk_facts):
            _require_shanghai_time(fact.observed_at, "decision risk observed_at")
            if fact.observed_at > observed_at:
                raise ValueError("decision cannot contain future risk facts")


def _validate_decision_metadata(
    epoch: DecisionEpoch,
    payload: _NormalizedDecisionPayload,
) -> None:
    if any(_REASON_CODE.fullmatch(reason) is None for reason in payload.reasons):
        raise ValueError("decision degraded reasons must be structured codes")
    if epoch.evaluated_count < len(payload.entries):
        raise ValueError("decision evaluated count cannot be smaller than scored entries")
    if not 0 <= epoch.rejected_count <= epoch.evaluated_count:
        raise ValueError("decision rejected count is invalid")
    if not 0 <= epoch.unscored_count <= epoch.evaluated_count:
        raise ValueError("decision unscored count is invalid")
    if any(_REASON_CODE.fullmatch(reason) is None or count < 1 for reason, count in payload.reason_counts.items()):
        raise ValueError("decision filter reason counts are invalid")
    if any(not board.strip() or not version.strip() for board, version in payload.populations.items()):
        raise ValueError("decision population versions must not be empty")


def _decision_epoch_hash(
    epoch: DecisionEpoch,
    payload: _NormalizedDecisionPayload,
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
            "parent_decision_version": epoch.parent_decision_version,
            "entries": tuple(_decision_entry_identity(item) for item in payload.entries),
            "review_candidate_codes": payload.review_codes,
            "evaluated_count": epoch.evaluated_count,
            "rejected_count": epoch.rejected_count,
            "unscored_count": epoch.unscored_count,
            "filter_reason_counts": payload.reason_counts,
            "population_versions": payload.populations,
            "degraded_reasons": payload.reasons,
        }
    )


def select_tomorrow_review_candidates(
    selection: TomorrowSelectionResult,
    policy: TomorrowDecisionPolicy,
) -> tuple[TomorrowReviewCandidate, ...]:
    eligible = tuple(
        item
        for item in selection.scored_candidates
        if item.disposition is TomorrowDisposition.PASS
        and item.local_score is not None
        and not any(fact.veto for fact in item.local_risk_facts)
    )
    ordered = sorted(eligible, key=_local_order)
    contexts = {item.code: _review_context(item, index + 1, policy) for index, item in enumerate(ordered)}
    prioritized = sorted(
        ordered,
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
        TomorrowReviewCandidate(item, replace(contexts[item.code], in_protection_set=True))
        for item in prioritized[: policy.review_candidate_limit]
    )


def build_tomorrow_decision_epoch(request: TomorrowDecisionRequest) -> DecisionEpoch:
    entries = tuple(_fuse_evaluation(item, request) for item in request.selection.scored_candidates)
    entries = _select_action_pools(entries, request.policy)
    reason_counts = _filter_reason_counts(request.selection)
    return DecisionEpoch(
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
        parent_decision_version=request.parent_decision_version,
        entries=entries,
        review_candidate_codes=request.review_candidate_codes,
        evaluated_count=len(request.selection.evaluations),
        rejected_count=sum(item.disposition is TomorrowDisposition.REJECT for item in request.selection.evaluations),
        unscored_count=sum(item.local_score is None for item in request.selection.evaluations),
        filter_reason_counts=reason_counts,
        population_versions={board.value: version for board, version in request.selection.population_versions.items()},
        degraded_reasons=request.degraded_reasons,
    )


def _fuse_evaluation(
    evaluation: TomorrowStockEvaluation,
    request: TomorrowDecisionRequest,
) -> TomorrowDecisionEntry:
    local_base = evaluation.local_base_score or 0.0
    local_components = evaluation.local_components
    review = request.reviews.get(evaluation.code)
    effective_review = review if review is not None and review.outcome is ReviewOutcome.APPLIED else None
    if review is not None and review.completed_at > request.observed_at:
        review = replace(review, outcome=ReviewOutcome.LATE, error="review_completed_after_decision")
        effective_review = None
    fusion_mode = FusionMode.HYBRID if effective_review is not None else FusionMode.LOCAL_DEGRADED
    fused = fuse_score(
        FusionRequest(
            local=LocalScoreResult(components=local_components, base_score=local_base),
            local_risk_facts=evaluation.local_risk_facts,
            review=effective_review,
            dimension_weights=request.policy.dimension_weights,
            risk_rules=request.policy.risk_rules,
            fusion_mode=fusion_mode,
            policy=request.policy.fusion,
            evidence=evaluation.features.evidence,
            evaluated_at=request.observed_at,
        )
    )
    if evaluation.local_score is not None and fused.score.local_score != round_score(evaluation.local_score):
        raise ValueError("tomorrow local score changed before fusion")
    veto = fused.veto or any(fact.veto for fact in evaluation.local_risk_facts)
    action, action_reason = _action_for(evaluation, fused.score, veto, request.policy)
    return TomorrowDecisionEntry(
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
        veto=veto,
        local_selection_skip_reason=evaluation.selection_skip_reason,
        decision_skip_reason="" if action is not RecommendationAction.UNAVAILABLE else action_reason,
    )


def _action_for(
    evaluation: TomorrowStockEvaluation,
    score: ScoreBreakdown,
    veto: bool,
    policy: TomorrowDecisionPolicy,
) -> tuple[RecommendationAction, str]:
    unavailable_reason = _unavailable_reason(evaluation, score, veto, policy)
    if unavailable_reason is not None:
        return RecommendationAction.UNAVAILABLE, unavailable_reason
    if evaluation.disposition is TomorrowDisposition.OBSERVE_ONLY:
        return RecommendationAction.OBSERVE, "filter_observe_only"
    if not policy.executable_enabled:
        return RecommendationAction.OBSERVE, "observation_phase"
    if score.final_score >= policy.executable_threshold:
        return RecommendationAction.EXECUTABLE, "score_threshold_met"
    return RecommendationAction.OBSERVE, "near_score_threshold"


def _unavailable_reason(
    evaluation: TomorrowStockEvaluation,
    score: ScoreBreakdown,
    veto: bool,
    policy: TomorrowDecisionPolicy,
) -> str | None:
    if evaluation.local_score is None or evaluation.disposition is TomorrowDisposition.REJECT:
        return evaluation.selection_skip_reason or "not_scored"
    if veto:
        return "risk_veto"
    if score.final_score < policy.executable_threshold - policy.observation_margin:
        return "below_score_threshold"
    return None


def _select_action_pools(
    entries: tuple[TomorrowDecisionEntry, ...],
    policy: TomorrowDecisionPolicy,
) -> tuple[TomorrowDecisionEntry, ...]:
    by_code = {item.code: item for item in entries}
    executable = _select_pool(
        tuple(item for item in entries if item.action is RecommendationAction.EXECUTABLE),
        limit=policy.top_k,
        policy=policy,
    )
    observations = _select_pool(
        tuple(item for item in entries if item.action is RecommendationAction.OBSERVE),
        limit=policy.observation_limit,
        policy=policy,
    )
    rank = 0
    for item, skip_reason in (*executable, *observations):
        if skip_reason:
            by_code[item.code] = replace(item, decision_skip_reason=skip_reason)
            continue
        rank += 1
        by_code[item.code] = replace(item, selected=True, rank=rank, decision_skip_reason="")
    return tuple(by_code[code] for code in sorted(by_code))


def _select_pool(
    entries: tuple[TomorrowDecisionEntry, ...],
    *,
    limit: int,
    policy: TomorrowDecisionPolicy,
) -> tuple[tuple[TomorrowDecisionEntry, str], ...]:
    ordered = sorted(entries, key=_decision_order)
    selected_count = 0
    board_counts: Counter[Board] = Counter()
    industry_counts: Counter[str] = Counter()
    maximum_per_board = math.ceil(limit * policy.maximum_board_fraction) if limit else 0
    result: list[tuple[TomorrowDecisionEntry, str]] = []
    for item in ordered:
        board = item.features.quote.board
        industry = item.features.quote.industry.strip() or "unknown"
        if selected_count >= limit:
            result.append(
                (item, "top_k_limit" if item.action is RecommendationAction.EXECUTABLE else "observation_limit")
            )
            continue
        if board_counts[board] >= maximum_per_board:
            result.append((item, "board_concentration_limit"))
            continue
        if industry_counts[industry] >= policy.maximum_per_industry:
            result.append((item, "industry_limit"))
            continue
        selected_count += 1
        board_counts[board] += 1
        industry_counts[industry] += 1
        result.append((item, ""))
    return tuple(result)


def _review_context(
    item: TomorrowStockEvaluation,
    rank: int,
    policy: TomorrowDecisionPolicy,
) -> ReviewCandidateContext:
    assert item.local_score is not None
    return ReviewCandidateContext(
        local_score=item.local_score,
        local_rank=rank,
        action_threshold=policy.executable_threshold,
        in_protection_set=False,
        has_new_high_risk=any(
            fact.severity == "high" and fact.confidence >= 0.7
            for fact in (*item.local_risk_facts, *item.features.external_risk_facts)
        ),
        near_action_threshold=abs(item.local_score - policy.executable_threshold) <= policy.observation_margin,
        near_global_boundary=abs(rank - policy.top_k) <= 2,
        evidence_conflict=any(
            value in {"cross_source_deviation", "board_classification_conflict"}
            for value in item.features.quote.execution_restrictions
        ),
    )


def _filter_reason_counts(selection: TomorrowSelectionResult) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in selection.evaluations:
        counts.update(reason.code for reason in item.filter_reasons)
        counts.update(flag.code for flag in item.optional_flags)
        if item.selection_skip_reason:
            counts[item.selection_skip_reason] += 1
    return dict(counts)


def _validate_stage_entries(
    projection_stage: Literal["local", "hybrid"],
    entries: tuple[TomorrowDecisionEntry, ...],
    review_codes: set[str],
) -> None:
    if projection_stage == "local" and any(
        item.review is not None
        or item.score.fusion_applied
        or item.score.deepseek_score is not None
        or item.deepseek_risk_facts
        for item in entries
    ):
        raise ValueError("local decision entries cannot contain DeepSeek results")
    if any(item.review is not None and item.code not in review_codes for item in entries):
        raise ValueError("decision review must belong to the review candidate set")
    if any(
        item.score.fusion_applied and (projection_stage != "hybrid" or item.review_outcome is not ReviewOutcome.APPLIED)
        for item in entries
    ):
        raise ValueError("fusion can only apply to an applied review in a hybrid decision")


def _validate_selected_pools(selected: list[TomorrowDecisionEntry]) -> None:
    executable = tuple(item for item in selected if item.action is RecommendationAction.EXECUTABLE)
    observations = tuple(item for item in selected if item.action is RecommendationAction.OBSERVE)
    if len(executable) > 10 or len(observations) > 8:
        raise ValueError("decision selected pools exceed their fixed limits")
    if selected != [*executable, *observations]:
        raise ValueError("selected executable decisions must precede observations")
    for pool, board_limit in ((executable, 6), (observations, 5)):
        if tuple(sorted(pool, key=_decision_order)) != pool:
            raise ValueError("selected decision pool order is unstable")
        board_counts = Counter(item.features.quote.board for item in pool)
        industry_counts = Counter(item.features.quote.industry.strip() or "unknown" for item in pool)
        if any(count > board_limit for count in board_counts.values()):
            raise ValueError("selected decision pool exceeds its board limit")
        if any(count > 2 for count in industry_counts.values()):
            raise ValueError("selected decision pool exceeds its industry limit")


def _local_order(item: TomorrowStockEvaluation) -> tuple[float, float, str]:
    return (-(item.local_score or 0.0), -(item.candidate_score or 0.0), item.code)


def _decision_order(item: TomorrowDecisionEntry) -> tuple[float, float, str]:
    return (-item.score.final_score, -item.score.local_score, item.code)


def _decision_entry_identity(item: TomorrowDecisionEntry) -> dict[str, object]:
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
        "candidate_score": item.candidate_score,
        "candidate_rank": item.candidate_rank,
        "board_rank": item.board_rank,
        "local_risk_facts": item.local_risk_facts,
        "deepseek_risk_facts": item.deepseek_risk_facts,
        "review": item.review,
        "review_outcome": item.review_outcome,
        "veto": item.veto,
        "local_selection_skip_reason": item.local_selection_skip_reason,
        "decision_skip_reason": item.decision_skip_reason,
    }


def _content_hash(value: object) -> str:
    payload = _canonical_value(value)
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _canonical_value(value: object) -> _CanonicalValue:
    if value is None or isinstance(value, (str, int, bool)):
        result: _CanonicalValue = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("decision epoch cannot hash non-finite values")
        result = value
    elif isinstance(value, (date, datetime)):
        result = value.isoformat()
    elif isinstance(value, Enum):
        result = str(value.value)
    elif is_dataclass(value):
        result = {item.name: _canonical_value(getattr(value, item.name)) for item in fields(value)}
    elif isinstance(value, Mapping):
        result = {str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda row: str(row[0]))}
    elif isinstance(value, (tuple, list)):
        result = [_canonical_value(item) for item in value]
    else:
        raise TypeError(f"unsupported decision epoch value: {type(value).__name__}")
    return result


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
    if getattr(value.tzinfo, "key", None) != _SHANGHAI_TIMEZONE:
        raise ValueError(f"{name} must use Asia/Shanghai")


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


__all__ = [
    "DECISION_EPOCH_SCHEMA_VERSION",
    "DecisionEpoch",
    "TomorrowDecisionEntry",
    "TomorrowDecisionPolicy",
    "TomorrowDecisionRequest",
    "TomorrowReviewCandidate",
    "build_tomorrow_decision_epoch",
    "select_tomorrow_review_candidates",
]
