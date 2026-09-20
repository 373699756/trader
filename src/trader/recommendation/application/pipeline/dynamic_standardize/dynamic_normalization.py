"""Stage-6 dynamic normalization boundary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from trader.recommendation.application.pipeline.stage_output import (
    PipelineStageOutput,
    require_previous_stage,
    stage_output,
)
from trader.recommendation.domain.evidence.pipeline import PipelineStage, Severity, StageReasonAggregate
from trader.recommendation.domain.market.models import FeatureSnapshot


def normalize_dynamic_market(
    source: PipelineStageOutput[FeatureSnapshot],
    normalizer: Callable[[FeatureSnapshot], FeatureSnapshot | None],
    *,
    as_of: datetime,
    latency_ms: int,
) -> PipelineStageOutput[FeatureSnapshot]:
    require_previous_stage(source, PipelineStage.DYNAMIC_STANDARDIZE)
    normalized = tuple(value for record in source.records if (value := normalizer(record)) is not None)
    pending = len(source.records) - len(normalized)
    reasons = (
        (StageReasonAggregate("refresh_pending", "refresh pending", pending, Severity.WARNING),) if pending else ()
    )
    return stage_output(
        PipelineStage.DYNAMIC_STANDARDIZE,
        normalized,
        as_of=as_of,
        input_count=len(source.records),
        pending_count=pending,
        reasons=reasons,
        source_health=source.snapshot.source_health,
        latency_ms=latency_ms,
    )


__all__ = ["normalize_dynamic_market"]
