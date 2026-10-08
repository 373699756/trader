"""Execute the immutable review, fusion, action and final-selection handoffs."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from trader.recommendation.application.pipeline.stage_output import (
    PipelineStageOutput,
    measured_output,
    require_previous_stage,
    stage_output,
)
from trader.recommendation.domain.evidence.pipeline import (
    PipelineStage,
    PipelineStageSnapshot,
    Severity,
    SourceHealth,
    SourceHealthState,
    StageReasonAggregate,
    StageState,
)
from trader.recommendation.domain.evidence.review import DeepSeekReview, ReviewOutcome
from trader.recommendation.domain.publication.models import RecommendationAction, ScoredStockEvaluation
from trader.recommendation.domain.risk.scored_fusion import (
    FusedScoredEvaluation,
    ReviewedScoredEvaluation,
    ScoredDecisionEntry,
    ScoredDecisionPolicy,
    apply_scored_actions,
    fuse_scored_evaluations,
    review_scored_evaluations,
    select_scored_action_pools,
)


@dataclass(frozen=True)
class ScoredStageContext:
    policy: ScoredDecisionPolicy
    observed_at: datetime
    review_candidate_codes: tuple[str, ...]
    reviews: tuple[DeepSeekReview, ...]
    observation_identity: str


@dataclass(frozen=True)
class ScoredStageChain:
    local_score: PipelineStageOutput[ScoredStockEvaluation]
    risk_review: PipelineStageOutput[ReviewedScoredEvaluation]
    score_merge: PipelineStageOutput[FusedScoredEvaluation]
    downside_action: PipelineStageOutput[ScoredDecisionEntry]
    final_selection: PipelineStageOutput[ScoredDecisionEntry]
    entries: tuple[ScoredDecisionEntry, ...]

    @property
    def snapshots(self) -> tuple[PipelineStageSnapshot, ...]:
        return (
            self.local_score.snapshot,
            self.risk_review.snapshot,
            self.score_merge.snapshot,
            self.downside_action.snapshot,
            self.final_selection.snapshot,
        )


def reason_aggregates(counts: Counter[str]) -> tuple[StageReasonAggregate, ...]:
    return tuple(
        StageReasonAggregate(code, code.replace("_", " "), count, Severity.WARNING)
        for code, count in sorted(counts.items())
        if count
    )


def complete_scored_stages(
    source: PipelineStageOutput[ScoredStockEvaluation],
    request: ScoredStageContext,
    monotonic: Callable[[], float],
    *,
    review_latency_ms: int = 0,
    review_attempted: bool = False,
) -> ScoredStageChain:
    require_previous_stage(source, PipelineStage.RISK_REVIEW)
    if review_latency_ms < 0:
        raise ValueError("review latency cannot be negative")
    started = monotonic()
    reviewed = review_scored_evaluations(source.records, request.reviews, request.observed_at)
    review_reasons: Counter[str] = Counter()
    for item in reviewed:
        if item.review is not None:
            review_reasons[f"deepseek_{item.review.outcome.value}"] += 1
            if item.review.error in {
                "deepseek_review_unavailable",
                "deepseek_manifest_mismatch",
                "deepseek_manifest_validation_failed",
                "deepseek_review_time_invalid",
            }:
                review_reasons[item.review.error] += 1
        elif item.evaluation.code in request.review_candidate_codes:
            review_reasons["deepseek_incomplete" if review_attempted else "deepseek_pending"] += 1
    reviews = tuple(item.review for item in reviewed if item.review is not None)
    successful = tuple(item for item in reviews if item.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN})
    review_degraded = bool(review_reasons["deepseek_pending"] or review_reasons["deepseek_incomplete"]) or len(
        successful
    ) != len(reviews)
    latest = max((item.completed_at for item in successful), default=None)
    health = SourceHealth(
        SourceHealthState.DEGRADED if review_degraded else SourceHealthState.READY,
        1 if request.review_candidate_codes else 0,
        1 if successful else 0,
        latest,
        max(0.0, (request.observed_at - latest).total_seconds()) if latest is not None else None,
    )
    # A legal local fallback remains an output. Review failure is a reason and
    # source degradation, never a business rejection or a lost scoring record.
    risk = measured_output(
        stage_output(
            PipelineStage.RISK_REVIEW,
            reviewed,
            input_batch_id=source.snapshot.output_batch_id,
            output_batch_id=f"{source.snapshot.output_batch_id}:risk_review:{request.observation_identity}",
            as_of=request.observed_at,
            input_count=len(source.records),
            reasons=reason_aggregates(review_reasons),
            source_health=health,
            latency_ms=0,
        ),
        started,
        monotonic,
    )
    risk = replace(risk, snapshot=replace(risk.snapshot, latency_ms=risk.snapshot.latency_ms + review_latency_ms))
    if review_degraded and reviewed:
        risk = replace(risk, snapshot=replace(risk.snapshot, state=StageState.DEGRADED, degraded=True))

    require_previous_stage(risk, PipelineStage.SCORE_MERGE)
    started = monotonic()
    fused = fuse_scored_evaluations(risk.records, request.policy, request.observed_at)
    merge = measured_output(
        stage_output(
            PipelineStage.SCORE_MERGE,
            fused,
            input_batch_id=risk.snapshot.output_batch_id,
            as_of=request.observed_at,
            input_count=len(risk.records),
            reasons=reason_aggregates(
                Counter("hybrid" if item.score.fusion_applied else "local_only" for item in fused)
            ),
            source_health=risk.snapshot.source_health,
            latency_ms=0,
        ),
        started,
        monotonic,
    )

    require_previous_stage(merge, PipelineStage.DOWNSIDE_ACTION)
    started = monotonic()
    actions = apply_scored_actions(merge.records, request.policy)
    eligible = tuple(item for item in actions if item.action is not RecommendationAction.UNAVAILABLE)
    action = measured_output(
        stage_output(
            PipelineStage.DOWNSIDE_ACTION,
            eligible,
            input_batch_id=merge.snapshot.output_batch_id,
            as_of=request.observed_at,
            input_count=len(merge.records),
            reasons=reason_aggregates(Counter(item.action_reason.split(":", 1)[0] for item in actions)),
            source_health=merge.snapshot.source_health,
            latency_ms=0,
        ),
        started,
        monotonic,
    )

    require_previous_stage(action, PipelineStage.FINAL_SELECTION)
    started = monotonic()
    ranked = select_scored_action_pools(action.records, request.policy)
    selected = tuple(sorted((item for item in ranked if item.selected), key=lambda item: item.rank))
    final = measured_output(
        stage_output(
            PipelineStage.FINAL_SELECTION,
            selected,
            input_batch_id=action.snapshot.output_batch_id,
            as_of=request.observed_at,
            input_count=len(action.records),
            reasons=reason_aggregates(
                Counter(item.decision_skip_reason for item in ranked if item.decision_skip_reason)
            ),
            source_health=action.snapshot.source_health,
            latency_ms=0,
        ),
        started,
        monotonic,
    )
    unavailable = tuple(item for item in actions if item.action is RecommendationAction.UNAVAILABLE)
    return ScoredStageChain(source, risk, merge, action, final, (*ranked, *unavailable))
