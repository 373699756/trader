"""Execute fixed-seat review and action handoffs without reselection."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from trader.recommendation.application.pipeline.stage_output import MeasuredOutput, PipelineStageOutput, StageOutput
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
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import ScoredStockEvaluation
from trader.recommendation.domain.risk.scored_fusion import (
    ApplyScoredActions,
    FuseScoredEvaluations,
    ReviewedScoredEvaluation,
    ReviewScoredEvaluations,
    ScoredFreezeEntry,
    ScoredFreezePolicy,
    SelectFixedScoredSeats,
)


@dataclass(frozen=True)
class ScoredStageContext:
    policy: ScoredFreezePolicy
    observed_at: datetime
    review_candidate_codes: tuple[str, ...]
    reviews: tuple[DeepSeekReview, ...]
    observation_identity: str
    fixed_selection: PipelineStageOutput[ScoredStockEvaluation]
    review_latency_ms: int | None = None
    review_attempted: bool = False


@dataclass(frozen=True)
class ScoredStageChain:
    model_filter: PipelineStageOutput[FeatureSnapshot]
    local_score: PipelineStageOutput[ScoredStockEvaluation]
    local_risk: PipelineStageOutput[ScoredStockEvaluation]
    fixed_selection: PipelineStageOutput[ScoredStockEvaluation]
    risk_review: PipelineStageOutput[ReviewedScoredEvaluation]
    action_publish: PipelineStageOutput[ScoredFreezeEntry]
    entries: tuple[ScoredFreezeEntry, ...]

    @property
    def snapshots(self) -> tuple[PipelineStageSnapshot, ...]:
        return tuple(
            output.snapshot
            for output in (
                self.model_filter,
                self.local_score,
                self.local_risk,
                self.fixed_selection,
                self.risk_review,
                self.action_publish,
            )
        )


def ReasonAggregates(counts: Counter[str]) -> tuple[StageReasonAggregate, ...]:
    return tuple(
        StageReasonAggregate(code, code.replace("_", " "), count, Severity.WARNING)
        for code, count in sorted(counts.items())
        if count
    )


def CompleteScoredStages(
    model_filter: PipelineStageOutput[FeatureSnapshot],
    local_score: PipelineStageOutput[ScoredStockEvaluation],
    local_risk: PipelineStageOutput[ScoredStockEvaluation],
    request: ScoredStageContext,
    monotonic: Callable[[], float],
) -> ScoredStageChain:
    source = request.fixed_selection
    review_latency_ms = request.review_latency_ms
    review_attempted = request.review_attempted
    seat_codes = frozenset(item.code for item in source.records)
    if not set(request.review_candidate_codes) <= seat_codes:
        raise ValueError("review candidates must belong to the fixed seats")
    if any(item.code not in request.review_candidate_codes for item in request.reviews):
        raise ValueError("review result exceeds its submitted fixed seats")
    started = monotonic()
    reviewed = ReviewScoredEvaluations(source.records, request.reviews, request.observed_at)
    review_reasons: Counter[str] = Counter()
    review_reasons["deepseek_eligible"] = len(request.review_candidate_codes)
    review_reasons["deepseek_submitted"] = len(request.review_candidate_codes) if review_attempted else 0
    for item in reviewed:
        if item.review is not None:
            review_reasons[f"deepseek_{item.review.outcome.value}"] += 1
            if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item.review.error):
                review_reasons[item.review.error] += 1
            review_reasons["deepseek_cache_reused"] += int(item.review.cache_reused)
        elif item.evaluation.code in request.review_candidate_codes:
            review_reasons["deepseek_incomplete" if review_attempted else "deepseek_pending"] += 1
    fused = FuseScoredEvaluations(reviewed, request.policy, request.observed_at)
    review_reasons["deepseek_veto"] = sum(bool(item.deepseek_risk_facts) and item.veto for item in fused)
    reviews = tuple(item.review for item in reviewed if item.review is not None)
    successful = tuple(item for item in reviews if item.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN})
    review_reasons["deepseek_completed"] = len(successful)
    review_reasons["deepseek_source_failed"] = len(reviews) - len(successful) + review_reasons["deepseek_incomplete"]
    degraded = bool(review_reasons["deepseek_pending"] or review_reasons["deepseek_incomplete"]) or len(
        successful
    ) != len(reviews)
    latest = max((item.completed_at for item in successful), default=None)
    expected = bool(request.review_candidate_codes)
    health = SourceHealth(
        SourceHealthState.DEGRADED if degraded else SourceHealthState.READY,
        int(expected),
        int(bool(successful) or (expected and not degraded)),
        latest,
        max(0.0, (request.observed_at - latest).total_seconds()) if latest is not None else None,
    )
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
    if review_latency_ms is not None:
        risk = replace(
            risk,
            snapshot=replace(
                risk.snapshot,
                latency_ms=(risk.snapshot.latency_ms or 0) + review_latency_ms,
                operation_durations=(*risk.snapshot.operation_durations, ("review_wait", review_latency_ms)),
            ),
        )
    if degraded:
        risk = replace(risk, snapshot=replace(risk.snapshot, state=StageState.DEGRADED, degraded=True))
    started = monotonic()
    actions = ApplyScoredActions(fused, request.policy)
    # A veto changes the action, never membership or rank.
    entries = tuple(
        replace(item, selected=True, rank=index, selection_rank=index, freeze_skip_reason="")
        for index, item in enumerate(actions, start=1)
    )
    action = MeasuredOutput(
        StageOutput(
            PipelineStage.ACTION_PUBLISH,
            entries,
            input_batch_id=risk.snapshot.output_batch_id,
            as_of=request.observed_at,
            input_count=len(risk.records),
            reasons=ReasonAggregates(Counter(f"action_{item.action.value}" for item in entries)),
            source_health=risk.snapshot.source_health,
            latency_ms=0,
        ),
        started,
        monotonic,
    )
    return ScoredStageChain(model_filter, local_score, local_risk, source, risk, action, entries)


def FixScoredSeats(
    source: PipelineStageOutput[ScoredStockEvaluation],
    policy: ScoredFreezePolicy,
    at: datetime,
    monotonic: Callable[[], float],
) -> PipelineStageOutput[ScoredStockEvaluation]:
    started = monotonic()
    seats = SelectFixedScoredSeats(source.records, policy)
    return MeasuredOutput(
        StageOutput(
            PipelineStage.FIXED_SELECTION,
            seats,
            input_batch_id=source.snapshot.output_batch_id,
            as_of=at,
            input_count=len(source.records),
            not_selected_count=len(source.records) - len(seats),
            source_health=source.snapshot.source_health,
            latency_ms=0,
        ),
        started,
        monotonic,
    )
