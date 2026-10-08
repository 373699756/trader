"""Build the immutable stage-2 static market collection result."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput, stage_output
from trader.recommendation.domain.evidence.pipeline import PipelineStage, SourceHealth, StageReasonAggregate
from trader.recommendation.domain.market.static import StaticIssuer


def load_static_market(
    records: Sequence[StaticIssuer],
    *,
    input_batch_id: str,
    as_of: datetime,
    source_health: SourceHealth,
    expected_count: int,
    pending_count: int,
    reasons: tuple[StageReasonAggregate, ...] = (),
    latency_ms: int,
) -> PipelineStageOutput[StaticIssuer]:
    return stage_output(
        PipelineStage.STATIC_MARKET,
        records,
        input_batch_id=input_batch_id,
        as_of=as_of,
        input_count=expected_count,
        pending_count=pending_count,
        reasons=reasons,
        source_health=source_health,
        latency_ms=latency_ms,
    )


__all__ = ["load_static_market"]
