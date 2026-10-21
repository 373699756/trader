"""Stage-4 permanent issuer qualification."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime

from trader.recommendation.application.pipeline.stage_output import (
    PipelineStageOutput,
    RequirePreviousStage,
    StageOutput,
)
from trader.recommendation.domain.evidence.pipeline import PipelineStage, Severity, StageReasonAggregate
from trader.recommendation.domain.market.eligibility import IssuerEligibilityDecision, IssuerEligibilityState
from trader.recommendation.domain.market.static import StaticIssuer


def FilterPermanentEligibility(
    source: PipelineStageOutput[StaticIssuer],
    decisions: Sequence[IssuerEligibilityDecision],
    *,
    as_of: datetime,
    latency_ms: int,
    company_exclusions: Mapping[str, str] | None = None,
) -> PipelineStageOutput[StaticIssuer]:
    RequirePreviousStage(source, PipelineStage.STATIC_FILTER)
    by_code = {item.code: item for item in decisions}
    if len(by_code) != len(decisions) or set(by_code) != {item.code for item in source.records}:
        raise ValueError("permanent eligibility must cover the complete static population")
    accepted: list[StaticIssuer] = []
    rejected: Counter[str] = Counter()
    pending = 0
    for record in source.records:
        decision = by_code[record.code]
        if decision.state is IssuerEligibilityState.PERMANENTLY_EXCLUDED:
            rejected[decision.reason.value if decision.reason is not None else "permanent_excluded"] += 1
        elif decision.state is IssuerEligibilityState.QUALIFICATION_PENDING:
            pending += 1
        elif company_exclusions is not None and record.code in company_exclusions:
            rejected[company_exclusions[record.code]] += 1
        else:
            accepted.append(record)
    business_reasons = tuple(
        StageReasonAggregate(code, code.replace("_", " "), count, Severity.WARNING)
        for code, count in sorted(rejected.items())
    )
    pending_reasons = (
        (StageReasonAggregate("data_pending", "data pending", pending, Severity.WARNING),) if pending else ()
    )
    return StageOutput(
        PipelineStage.STATIC_FILTER,
        accepted,
        input_batch_id=source.snapshot.output_batch_id,
        as_of=as_of,
        input_count=len(source.records),
        rejected_count=sum(rejected.values()),
        pending_count=pending,
        reasons=(*business_reasons, *pending_reasons),
        business_reasons=business_reasons,
        source_health=source.snapshot.source_health,
        latency_ms=latency_ms,
    )


__all__ = ["FilterPermanentEligibility"]
