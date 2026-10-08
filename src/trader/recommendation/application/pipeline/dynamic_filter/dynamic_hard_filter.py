"""Stage-7 dynamic hard-filter execution."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from dataclasses import dataclass, replace

from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import (
    ScoredSelectionIdentity,
    ScoredSelectionOptions,
    filter_feature_candidates,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.domain.selection.scored_selection import FilteredCandidateInputs
from trader.recommendation.domain.publication.models import ScoredDisposition

from trader.recommendation.application.pipeline.stage_output import (
    PipelineStageOutput,
    require_previous_stage,
    stage_output,
)
from trader.recommendation.domain.candidate.filters import HardFilterPolicy, apply_filters, level_two_filter_rules
from trader.recommendation.domain.evidence.pipeline import PipelineStage, Severity, StageReasonAggregate
from trader.recommendation.domain.market.models import FeatureSnapshot


def filter_dynamic_market(
    source: PipelineStageOutput[FeatureSnapshot],
    *,
    as_of: datetime,
    max_age_seconds: float,
    policy: HardFilterPolicy,
    latency_ms: int,
) -> PipelineStageOutput[FeatureSnapshot]:
    require_previous_stage(source, PipelineStage.DYNAMIC_FILTER)
    rules = level_two_filter_rules(max_age_seconds=max_age_seconds, policy=policy)
    accepted: list[FeatureSnapshot] = []
    rejected: Counter[str] = Counter()
    pending: Counter[str] = Counter()
    for record in source.records:
        result = apply_filters(record, rules, now=as_of)
        if result.reasons:
            rejected[result.reasons[0].filter_code] += 1
        elif result.deferred:
            pending[result.deferred[0].filter_code] += 1
        else:
            accepted.append(record)
    business_reasons = tuple(
        StageReasonAggregate(code, code.replace("_", " "), count, Severity.WARNING)
        for code, count in sorted(rejected.items())
    )
    pending_reasons = tuple(
        StageReasonAggregate(code, code.replace("_", " "), count, Severity.WARNING)
        for code, count in sorted(pending.items())
    )
    return stage_output(
        PipelineStage.DYNAMIC_FILTER,
        accepted,
        input_batch_id=source.snapshot.output_batch_id,
        as_of=as_of,
        input_count=len(source.records),
        rejected_count=sum(rejected.values()),
        pending_count=sum(pending.values()),
        reasons=(*business_reasons, *pending_reasons),
        business_reasons=business_reasons,
        source_health=source.snapshot.source_health,
        latency_ms=latency_ms,
    )


@dataclass(frozen=True)
class QualifiedCandidateInputs:
    output: PipelineStageOutput[FeatureSnapshot]
    qualifications: FilteredCandidateInputs


def qualify_candidate_inputs(
    source: PipelineStageOutput[FeatureSnapshot],
    policy: RecommendationPolicy,
    options: ScoredSelectionOptions,
    identity: ScoredSelectionIdentity,
) -> QualifiedCandidateInputs:
    """Execute qualification once and retain the immutable evaluations for stage 8."""
    require_previous_stage(source, PipelineStage.DYNAMIC_FILTER)
    prepared = filter_feature_candidates(source.records, policy, options, identity)
    accepted: list[FeatureSnapshot] = []
    rejected: Counter[str] = Counter()
    pending: Counter[str] = Counter()
    for evaluation in prepared.population.evaluations.values():
        if evaluation.disposition is ScoredDisposition.REJECT:
            rejected[evaluation.filter_reasons[0].code] += 1
        elif evaluation.filter_reasons:
            codes = tuple(reason.code for reason in evaluation.filter_reasons)
            pending[next((code for code in codes if code in {"stale_quote", "future_quote"}), codes[0])] += 1
        else:
            accepted.append(evaluation.features)
    business = tuple(
        StageReasonAggregate(code, code.replace("_", " "), count, Severity.WARNING)
        for code, count in sorted(rejected.items())
    )
    deferred = tuple(
        StageReasonAggregate(code, code.replace("_", " "), count, Severity.WARNING)
        for code, count in sorted(pending.items())
    )
    output = stage_output(
        PipelineStage.DYNAMIC_FILTER,
        accepted,
        input_batch_id=source.snapshot.output_batch_id,
        as_of=options.evaluated_at,
        input_count=len(source.records),
        rejected_count=sum(rejected.values()),
        pending_count=sum(pending.values()),
        reasons=(*business, *deferred),
        business_reasons=business,
        source_health=source.snapshot.source_health,
        latency_ms=0,
    )
    output = replace(
        output,
        snapshot=replace(
            output.snapshot,
            output_batch_id=f"{source.snapshot.output_batch_id}:{options.strategy.value}:dynamic_filter",
        ),
    )
    return QualifiedCandidateInputs(output, prepared)


__all__ = ["filter_dynamic_market", "qualify_candidate_inputs", "QualifiedCandidateInputs"]
