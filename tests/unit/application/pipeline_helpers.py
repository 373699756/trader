"""Explicit observed static-stage fixtures for application boundary contracts."""

from datetime import datetime

from trader.recommendation.application.pipeline.stage_output import stage_output
from trader.recommendation.domain.evidence.pipeline import (
    PIPELINE_STAGES,
    PipelineStageSnapshot,
    SourceHealth,
    SourceHealthState,
)


def observed_static_stages(population: int, as_of: datetime) -> tuple[PipelineStageSnapshot, ...]:
    snapshots = []
    input_id = "observed-static-fixture"
    for index, stage in enumerate(PIPELINE_STAGES[:4]):
        output = stage_output(
            stage,
            tuple(range(population)),
            input_batch_id=input_id,
            as_of=as_of,
            input_count=population,
            source_health=SourceHealth(SourceHealthState.READY, 1, 1),
            latency_ms=11 + index,
        )
        snapshots.append(output.snapshot)
        input_id = output.snapshot.output_batch_id
    return tuple(snapshots)
