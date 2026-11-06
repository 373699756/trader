"""Execute complete-population qualification and feature preparation."""

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
    RequiredCandidateHistorySessions,
    MaximumCandidateQuoteAgeSeconds,
    FindModelEligibleCodes,
)
from trader.recommendation.application.pipeline.dynamic_filter.dynamic_hard_filter import QualifyCandidateInputs
from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import (
    ScoredSelectionIdentity,
    ScoredSelectionOptions,
)
from trader.recommendation.application.pipeline.stage_output import MeasuredOutput, PipelineStageOutput, StageOutput
from trader.recommendation.domain.candidate.filters import QuoteFreshnessBasis
from trader.recommendation.domain.evidence.pipeline import (
    PipelineStage,
    PipelineStageSnapshot,
    Severity,
    StageReasonAggregate,
)
from trader.recommendation.domain.market.history_alignment import HISTORY_INPUT_FAILURES
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.selection.scored_selection import (
    BuildScoredCandidateReferences,
    FilteredCandidateInputs,
    RankFilteredCandidates,
    ScoredCandidatePlan,
    SelectedCandidateInput,
)


@dataclass(frozen=True)
class CandidatePipelineResult:
    plans: CandidatePlanSet
    filtered: Mapping[Strategy, PipelineStageOutput[FeatureSnapshot]]
    candidates: Mapping[Strategy, PipelineStageOutput[SelectedCandidateInput]]
    qualifications: Mapping[Strategy, FilteredCandidateInputs]

    def __post_init__(self) -> None:
        object.__setattr__(self, "filtered", MappingProxyType(dict(self.filtered)))
        object.__setattr__(self, "candidates", MappingProxyType(dict(self.candidates)))
        object.__setattr__(self, "qualifications", MappingProxyType(dict(self.qualifications)))

    def stages(self, strategy: Strategy) -> tuple[PipelineStageSnapshot, ...]:
        return (self.filtered[strategy].snapshot, self.candidates[strategy].snapshot)


def ExecuteCandidatePipeline(
    source: PipelineStageOutput[FeatureSnapshot],
    context: CandidatePlanningContext,
    monotonic: Callable[[], float],
) -> CandidatePipelineResult:
    plans: dict[Strategy, ScoredCandidatePlan] = {}
    filtered: dict[Strategy, PipelineStageOutput[FeatureSnapshot]] = {}
    candidates: dict[Strategy, PipelineStageOutput[SelectedCandidateInput]] = {}
    qualifications: dict[Strategy, FilteredCandidateInputs] = {}
    for strategy in SCORED_STRATEGIES:
        started = monotonic()
        qualified = QualifyCandidateInputs(
            source,
            context.policy,
            ScoredSelectionOptions(
                evaluated_at=context.evaluated_at,
                max_age_seconds=MaximumCandidateQuoteAgeSeconds(strategy),
                population_evaluated_at=context.evaluated_at,
                population_max_age_seconds=MaximumCandidateQuoteAgeSeconds(strategy),
                population_quote_freshness_basis=QuoteFreshnessBasis.RECEIPT,
                phase="candidate_discovery",
                strategy=strategy,
                minimum_history_sessions=RequiredCandidateHistorySessions(
                    context.model_scoring if context.apply_model_eligibility else None,
                    strategy,
                ),
                model_input_eligible_codes=FindModelEligibleCodes(
                    context.model_scoring if context.apply_model_eligibility else None,
                    strategy,
                    source.records,
                ),
            ),
            ScoredSelectionIdentity(context.evaluated_at.date(), context.data_version, context.data_version),
        )
        filtered[strategy] = MeasuredOutput(qualified.output, started, monotonic)
        qualifications[strategy] = qualified.qualifications
        started = monotonic()
        plan = RankFilteredCandidates(BuildScoredCandidateReferences(qualified.qualifications))
        plans[strategy] = plan
        candidates[strategy] = MeasuredOutput(
            SelectCandidateOutput(filtered[strategy], plan, context.evaluated_at),
            started,
            monotonic,
        )
    return CandidatePipelineResult(CandidatePlanSet(plans, context.evaluated_at), filtered, candidates, qualifications)


def SelectCandidateOutput(
    source: PipelineStageOutput[FeatureSnapshot],
    plan: ScoredCandidatePlan,
    as_of: datetime,
    *,
    latency_ms: int = 0,
) -> PipelineStageOutput[SelectedCandidateInput]:
    codes = plan.qualified_codes()
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
        reason = item.selection_skip_reason or item.candidate_audit_pruning_reason
        if not reason:
            raise ValueError("unqualified input requires an explicit reason")
        reasons[reason] += 1
    output = StageOutput(
        PipelineStage.MARKET_FEATURES,
        tuple(SelectedCandidateInput(code) for code in codes),
        input_batch_id=source.snapshot.output_batch_id,
        as_of=as_of,
        input_count=len(source.records),
        pending_count=sum(
            count
            for reason, count in reasons.items()
            if reason
            in {
                "production_model_features_missing",
                "strategy_history_insufficient",
                "refresh_pending",
                "data_pending",
            }
            | HISTORY_INPUT_FAILURES
        ),
        reasons=ReasonAggregates(reasons),
        source_health=source.snapshot.source_health,
        latency_ms=latency_ms,
    )
    return replace(
        output,
        snapshot=replace(
            output.snapshot,
            output_batch_id=f"{source.snapshot.output_batch_id}:market_features:{as_of:%Y%m%dT%H%M%S%f}",
        ),
    )


def AssembleCandidateInputs(
    source: PipelineStageOutput[SelectedCandidateInput],
    available: tuple[FeatureSnapshot, ...],
    *,
    as_of: datetime,
    data_version: str,
) -> PipelineStageOutput[SelectedCandidateInput]:
    """Attach refreshed observations before the market-feature input check."""
    by_code = {feature.quote.code: feature for feature in available}
    records = tuple(SelectedCandidateInput(item.code, by_code.get(item.code)) for item in source.records)
    identity = f"accepted:{data_version}:{as_of:%Y%m%dT%H%M%S%f}"
    return replace(
        source,
        records=records,
        snapshot=replace(
            source.snapshot, as_of=as_of, output_batch_id=f"{source.snapshot.input_batch_id}:market_features:{identity}"
        ),
    )


def ReasonAggregates(counts: Counter[str]) -> tuple[StageReasonAggregate, ...]:
    return tuple(
        StageReasonAggregate(code, code.replace("_", " "), count, Severity.WARNING)
        for code, count in sorted(counts.items())
        if count
    )
