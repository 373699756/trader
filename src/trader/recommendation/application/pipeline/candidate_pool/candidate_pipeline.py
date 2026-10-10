"""Execute and retain the immutable discovery normalization, qualification and reserve stages."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType

from trader.recommendation.application.pipeline.candidate_pool.candidate_builder import (
    SCORED_STRATEGIES,
    CandidatePlanningContext,
    CandidatePlanSet,
    _history_required_sessions,
    _maximum_age_seconds,
    _model_eligible_codes,
)
from trader.recommendation.application.pipeline.dynamic_filter.dynamic_hard_filter import qualify_candidate_inputs
from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import (
    ScoredSelectionIdentity,
    ScoredSelectionOptions,
)
from trader.recommendation.application.pipeline.dynamic_standardize.dynamic_normalization import (
    normalize_candidate_discovery_population,
    normalize_dynamic_market,
)
from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput, measured_output, stage_output
from trader.recommendation.domain.evidence.pipeline import (
    PipelineStage,
    PipelineStageSnapshot,
    Severity,
    StageReasonAggregate,
)
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.selection.scored_selection import (
    ScoredCandidatePlan,
    SelectedCandidateInput,
    rank_filtered_candidates,
)


@dataclass(frozen=True)
class CandidatePipelineResult:
    plans: CandidatePlanSet
    normalized: PipelineStageOutput[FeatureSnapshot]
    filtered: Mapping[Strategy, PipelineStageOutput[FeatureSnapshot]]
    candidates: Mapping[Strategy, PipelineStageOutput[SelectedCandidateInput]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "filtered", MappingProxyType(dict(self.filtered)))
        object.__setattr__(self, "candidates", MappingProxyType(dict(self.candidates)))

    def stages(self, strategy: Strategy) -> tuple[PipelineStageSnapshot, ...]:
        return (self.normalized.snapshot, self.filtered[strategy].snapshot, self.candidates[strategy].snapshot)


def execute_candidate_pipeline(
    source: PipelineStageOutput[FeatureSnapshot],
    context: CandidatePlanningContext,
    monotonic: Callable[[], float],
) -> CandidatePipelineResult:
    started = monotonic()
    normalized = normalize_dynamic_market(
        source,
        lambda feature: normalize_candidate_discovery_population((feature,), context.evaluated_at)[0],
        as_of=context.evaluated_at,
        latency_ms=0,
    )
    normalized = measured_output(normalized, started, monotonic)
    plans: dict[Strategy, ScoredCandidatePlan] = {}
    filtered: dict[Strategy, PipelineStageOutput[FeatureSnapshot]] = {}
    candidates: dict[Strategy, PipelineStageOutput[SelectedCandidateInput]] = {}
    for strategy in SCORED_STRATEGIES:
        started = monotonic()
        qualified = qualify_candidate_inputs(
            normalized,
            context.policy,
            ScoredSelectionOptions(
                evaluated_at=context.evaluated_at,
                max_age_seconds=_maximum_age_seconds(strategy),
                population_evaluated_at=context.evaluated_at,
                population_max_age_seconds=_maximum_age_seconds(strategy),
                phase="candidate_discovery",
                normalize_discovery_source_time=False,
                strategy=strategy,
                minimum_history_sessions=_history_required_sessions(
                    context.model_scoring if context.apply_model_eligibility else None,
                    strategy,
                ),
                model_input_eligible_codes=_model_eligible_codes(
                    context.model_scoring if context.apply_model_eligibility else None,
                    strategy,
                    normalized.records,
                ),
                candidate_limit_per_board=context.limit_per_board,
            ),
            ScoredSelectionIdentity(context.evaluated_at.date(), context.data_version, context.data_version),
        )
        filtered[strategy] = measured_output(qualified.output, started, monotonic)
        started = monotonic()
        plan = rank_filtered_candidates(qualified.qualifications)
        plans[strategy] = plan
        candidates[strategy] = measured_output(
            select_candidate_output(filtered[strategy], plan, context.limit_per_board, context.evaluated_at),
            started,
            monotonic,
        )
    return CandidatePipelineResult(CandidatePlanSet(plans, context.limit_per_board), normalized, filtered, candidates)


def select_candidate_output(
    source: PipelineStageOutput[FeatureSnapshot],
    plan: ScoredCandidatePlan,
    limit: int,
    as_of: datetime,
    *,
    latency_ms: int = 0,
) -> PipelineStageOutput[SelectedCandidateInput]:
    codes = plan.limited_codes(limit)
    by_code = {feature.quote.code: feature for feature in source.records}
    if not set(codes) <= set(by_code):
        raise ValueError("candidate selection exceeds the observed dynamic population")
    selected = set(codes)
    evaluations = {item.code: item for item in plan.evaluations}
    reasons: Counter[str] = Counter()
    for feature in source.records:
        code = feature.quote.code
        if code in selected:
            continue
        item = evaluations[code]
        reasons[item.selection_skip_reason or item.candidate_audit_pruning_reason or "board_limit"] += 1
    output = stage_output(
        PipelineStage.CANDIDATE_POOL,
        tuple(SelectedCandidateInput(code) for code in codes),
        input_batch_id=source.snapshot.output_batch_id,
        as_of=as_of,
        input_count=len(source.records),
        pending_count=sum(
            count
            for reason, count in reasons.items()
            if reason
            in {
                "candidate_core_missing",
                "production_model_features_missing",
                "strategy_history_insufficient",
            }
        ),
        reasons=reason_aggregates(reasons),
        source_health=source.snapshot.source_health,
        latency_ms=latency_ms,
    )
    return replace(
        output,
        snapshot=replace(
            output.snapshot,
            output_batch_id=f"{source.snapshot.output_batch_id}:candidate_pool:{as_of:%Y%m%dT%H%M%S%f}",
        ),
    )


def assemble_candidate_inputs(
    source: PipelineStageOutput[SelectedCandidateInput],
    available: tuple[FeatureSnapshot, ...],
    *,
    as_of: datetime,
    data_version: str,
) -> PipelineStageOutput[SelectedCandidateInput]:
    """Complete a new stage-8 batch; stage 9 reads only this immutable output."""
    by_code = {feature.quote.code: feature for feature in available}
    records = tuple(SelectedCandidateInput(item.code, by_code.get(item.code)) for item in source.records)
    identity = f"accepted:{data_version}:{as_of:%Y%m%dT%H%M%S%f}"
    return replace(
        source,
        records=records,
        snapshot=replace(
            source.snapshot, as_of=as_of, output_batch_id=f"{source.snapshot.input_batch_id}:candidate_pool:{identity}"
        ),
    )


def reason_aggregates(counts: Counter[str]) -> tuple[StageReasonAggregate, ...]:
    return tuple(
        StageReasonAggregate(code, code.replace("_", " "), count, Severity.WARNING)
        for code, count in sorted(counts.items())
        if count
    )
