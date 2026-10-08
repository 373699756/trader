"""Stage-3 static normalization boundary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from trader.recommendation.application.pipeline.stage_output import (
    PipelineStageOutput,
    require_previous_stage,
    stage_output,
)
from trader.recommendation.domain.evidence.pipeline import PipelineStage, Severity, StageReasonAggregate
from trader.recommendation.domain.market.static import StaticIssuer


def normalize_static_market(
    source: PipelineStageOutput[StaticIssuer],
    normalizer: Callable[[StaticIssuer], StaticIssuer | None],
    *,
    as_of: datetime,
    reasons: tuple[StageReasonAggregate, ...] = (),
    latency_ms: int,
) -> PipelineStageOutput[StaticIssuer]:
    require_previous_stage(source, PipelineStage.STATIC_STANDARDIZE)
    normalized = tuple(value for record in source.records if (value := normalizer(record)) is not None)
    pending = len(source.records) - len(normalized)
    return stage_output(
        PipelineStage.STATIC_STANDARDIZE,
        normalized,
        input_batch_id=source.snapshot.output_batch_id,
        as_of=as_of,
        input_count=len(source.records),
        pending_count=pending,
        reasons=(
            *reasons,
            *(
                (StageReasonAggregate("static_identity_pending", "static identity pending", pending, Severity.WARNING),)
                if pending
                else ()
            ),
        ),
        source_health=source.snapshot.source_health,
        latency_ms=latency_ms,
    )


__all__ = ["normalize_static_market"]
