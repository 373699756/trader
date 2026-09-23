"""Stage-7 dynamic hard-filter execution."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

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
    rules = level_two_filter_rules(max_age_seconds=max_age_seconds, policy=policy, finalized_inputs=True)
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


__all__ = ["filter_dynamic_market"]
