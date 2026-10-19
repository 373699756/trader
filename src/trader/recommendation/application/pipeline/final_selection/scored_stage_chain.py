"""Execute the immutable review, fusion, action and final-selection handoffs."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from trader.recommendation.application.pipeline.stage_output import (
    MeasuredOutput,
    PipelineStageOutput,
    RequirePreviousStage,
    StageOutput,
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
    ApplyScoredActions,
    FusedScoredEvaluation,
    FuseScoredEvaluations,
    ReviewedScoredEvaluation,
    ReviewScoredEvaluations,
    ScoredDecisionEntry,
    ScoredDecisionPolicy,
    SelectScoredActionPools,
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


def ReasonAggregates(counts: Counter[str]) -> tuple[StageReasonAggregate, ...]:
    return tuple(
        StageReasonAggregate(code, code.replace("_", " "), count, Severity.WARNING)
        for code, count in sorted(counts.items())
        if count
    )


def CompleteScoredStages(
    source: PipelineStageOutput[ScoredStockEvaluation],
    request: ScoredStageContext,
    monotonic: Callable[[], float],
    *,
    review_latency_ms: int = 0,
    review_attempted: bool = False,
) -> ScoredStageChain:
    RequirePreviousStage(source, PipelineStage.RISK_REVIEW)
    if review_latency_ms < 0:
        raise ValueError("review latency cannot be negative")
    started = monotonic()
    reviewed = ReviewScoredEvaluations(source.records, request.reviews, request.observed_at)
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
    risk = MeasuredOutput(
        StageOutput(
            PipelineStage.RISK_REVIEW,
            reviewed,
            input_batch_id=source.snapshot.output_batch_id,
            output_batch_id=f"{source.snapshot.output_batch_id}:risk_review:{request.observation_identity}",
            as_of=request.observed_at,
            input_count=len(source.records),
            reasons=ReasonAggregates(review_reasons),
            source_health=health,
            latency_ms=0,
        ),
        started,
        monotonic,
    )
    risk = replace(risk, snapshot=replace(risk.snapshot, latency_ms=risk.snapshot.latency_ms + review_latency_ms))
    if review_degraded and reviewed:
        risk = replace(risk, snapshot=replace(risk.snapshot, state=StageState.DEGRADED, degraded=True))

    RequirePreviousStage(risk, PipelineStage.SCORE_MERGE)
    started = monotonic()
    fused = FuseScoredEvaluations(risk.records, request.policy, request.observed_at)
    merge = MeasuredOutput(
        StageOutput(
            PipelineStage.SCORE_MERGE,
            fused,
            input_batch_id=risk.snapshot.output_batch_id,
            as_of=request.observed_at,
            input_count=len(risk.records),
            reasons=ReasonAggregates(
                Counter("hybrid" if item.score.fusion_applied else "local_only" for item in fused)
            ),
            source_health=risk.snapshot.source_health,
            latency_ms=0,
        ),
        started,
        monotonic,
    )

    RequirePreviousStage(merge, PipelineStage.DOWNSIDE_ACTION)
    started = monotonic()
    actions = ApplyScoredActions(merge.records, request.policy)
    eligible = tuple(item for item in actions if item.action is not RecommendationAction.UNAVAILABLE)
    action = MeasuredOutput(
        StageOutput(
            PipelineStage.DOWNSIDE_ACTION,
            eligible,
            input_batch_id=merge.snapshot.output_batch_id,
            as_of=request.observed_at,
            input_count=len(merge.records),
            reasons=ReasonAggregates(Counter(item.action_reason.split(":", 1)[0] for item in actions)),
            source_health=merge.snapshot.source_health,
            latency_ms=0,
        ),
        started,
        monotonic,
    )

    RequirePreviousStage(action, PipelineStage.FINAL_SELECTION)
    started = monotonic()
    ranked = SelectScoredActionPools(action.records, request.policy, scored_population=actions)
    selected = tuple(sorted((item for item in ranked if item.selected), key=lambda item: item.rank))
    final = MeasuredOutput(
        StageOutput(
            PipelineStage.FINAL_SELECTION,
            selected,
            input_batch_id=action.snapshot.output_batch_id,
            as_of=request.observed_at,
            input_count=len(action.records),
            reasons=ReasonAggregates(
                Counter(item.decision_skip_reason for item in ranked if item.decision_skip_reason)
            ),
            source_health=action.snapshot.source_health,
            latency_ms=0,
        ),
        started,
        monotonic,
    )
    # A completed action gate can legitimately leave nothing to rank. This is
    # different from an upstream stage which never obtained usable input.
    if not action.records and action.snapshot.state is StageState.READY:
        final = replace(final, snapshot=replace(final.snapshot, state=StageState.READY, degraded=False))
    unavailable = tuple(item for item in actions if item.action is RecommendationAction.UNAVAILABLE)
    return ScoredStageChain(source, risk, merge, action, final, (*ranked, *unavailable))
