"""Stage-4 permanent issuer qualification."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime

from trader.recommendation.application.pipeline.stage_output import (
    PipelineStageOutput,
    require_previous_stage,
    stage_output,
)
from trader.recommendation.domain.evidence.pipeline import PipelineStage, Severity, StageReasonAggregate
from trader.recommendation.domain.market.eligibility import IssuerEligibilityDecision, IssuerEligibilityState
from trader.recommendation.domain.market.models import FeatureSnapshot


def filter_permanent_eligibility(
    source: PipelineStageOutput[FeatureSnapshot],
    decisions: Sequence[IssuerEligibilityDecision],
    *,
    as_of: datetime,
    latency_ms: int,
) -> PipelineStageOutput[FeatureSnapshot]:
    require_previous_stage(source, PipelineStage.STATIC_FILTER)
    by_code = {item.code: item for item in decisions}
    if len(by_code) != len(decisions) or set(by_code) != {item.quote.code for item in source.records}:
        raise ValueError("permanent eligibility must cover the complete static population")
    accepted: list[FeatureSnapshot] = []
    rejected: Counter[str] = Counter()
    pending = 0
    for record in source.records:
        decision = by_code[record.quote.code]
        if decision.state is IssuerEligibilityState.PERMANENTLY_EXCLUDED:
            rejected[decision.reason.value if decision.reason is not None else "permanent_excluded"] += 1
        elif decision.state is IssuerEligibilityState.QUALIFICATION_PENDING:
            pending += 1
        else:
            accepted.append(record)
    business_reasons = tuple(
        StageReasonAggregate(code, code.replace("_", " "), count, Severity.WARNING)
        for code, count in sorted(rejected.items())
    )
    pending_reasons = (
        (StageReasonAggregate("data_pending", "data pending", pending, Severity.WARNING),) if pending else ()
    )
    return stage_output(
        PipelineStage.STATIC_FILTER,
        accepted,
        as_of=as_of,
        input_count=len(source.records),
        rejected_count=sum(rejected.values()),
        pending_count=pending,
        reasons=(*business_reasons, *pending_reasons),
        business_reasons=business_reasons,
        source_health=source.snapshot.source_health,
        latency_ms=latency_ms,
    )


__all__ = ["filter_permanent_eligibility"]
