"""Build the immutable stage-5 dynamic collection result."""

from __future__ import annotations

from datetime import datetime

from trader.recommendation.application.pipeline.stage_output import (
    PipelineStageOutput,
    require_previous_stage,
    stage_output,
)
from trader.recommendation.domain.evidence.pipeline import PipelineStage, Severity, StageReasonAggregate
from trader.recommendation.domain.market.models import FeatureSnapshot


def build_dynamic_market_snapshot(
    source: PipelineStageOutput[FeatureSnapshot],
    loaded: tuple[FeatureSnapshot, ...],
    *,
    as_of: datetime,
    latency_ms: int,
) -> PipelineStageOutput[FeatureSnapshot]:
    require_previous_stage(source, PipelineStage.DYNAMIC_MARKET)
    requested = {item.quote.code for item in source.records}
    loaded_codes = tuple(item.quote.code for item in loaded)
    if len(loaded_codes) != len(set(loaded_codes)) or not set(loaded_codes) <= requested:
        raise ValueError("dynamic market output must be a unique subset of eligible issuers")
    pending = len(requested) - len(loaded_codes)
    reasons = (
        (StageReasonAggregate("refresh_pending", "refresh pending", pending, Severity.WARNING),) if pending else ()
    )
    return stage_output(
        PipelineStage.DYNAMIC_MARKET,
        loaded,
        input_batch_id=source.snapshot.output_batch_id,
        as_of=as_of,
        input_count=len(source.records),
        pending_count=pending,
        reasons=reasons,
        source_health=source.snapshot.source_health,
        latency_ms=latency_ms,
    )


__all__ = ["build_dynamic_market_snapshot"]
