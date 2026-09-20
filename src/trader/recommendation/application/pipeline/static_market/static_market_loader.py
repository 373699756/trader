"""Build the immutable stage-2 static market collection result."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput, stage_output
from trader.recommendation.application.pipeline.static_market.static_snapshot import StaticMarketSnapshot
from trader.recommendation.domain.evidence.pipeline import PipelineStage, SourceHealth, StageReasonAggregate
from trader.recommendation.domain.market.models import FeatureSnapshot


def load_static_market(
    records: Sequence[FeatureSnapshot],
    *,
    as_of: datetime,
    source_health: SourceHealth,
    expected_count: int,
    pending_count: int,
    reasons: tuple[StageReasonAggregate, ...] = (),
    latency_ms: int,
) -> StaticMarketSnapshot:
    return stage_output(
        PipelineStage.STATIC_MARKET,
        records,
        as_of=as_of,
        input_count=expected_count,
        pending_count=pending_count,
        reasons=reasons,
        source_health=source_health,
        latency_ms=latency_ms,
    )


__all__ = ["StaticMarketSnapshot", "load_static_market"]
