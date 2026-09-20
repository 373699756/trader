"""Shared immutable envelope for one ordered recommendation stage."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Generic, TypeVar

from trader.recommendation.domain.evidence.pipeline import (
    PIPELINE_STAGES,
    BusinessRejectionSummary,
    PipelineStage,
    PipelineStageSnapshot,
    SourceHealth,
    StageReasonAggregate,
    StageState,
)

_RecordT = TypeVar("_RecordT", covariant=True)


@dataclass(frozen=True, slots=True)
class PipelineStageOutput(Generic[_RecordT]):
    records: tuple[_RecordT, ...]
    snapshot: PipelineStageSnapshot
    business_rejections: BusinessRejectionSummary | None = None

    def __post_init__(self) -> None:
        if len(self.records) != self.snapshot.output_count:
            raise ValueError("stage output records must match the snapshot output count")
        is_filter = self.snapshot.stage in {PipelineStage.STATIC_FILTER, PipelineStage.DYNAMIC_FILTER}
        if (self.business_rejections is not None) != is_filter:
            raise ValueError("business rejection summaries belong only to hard-filter stages")
        if self.business_rejections is not None:
            if self.business_rejections.input_count != self.snapshot.input_count:
                raise ValueError("business rejection input count must match its stage")
            if self.business_rejections.rejected_count != self.snapshot.rejected_count:
                raise ValueError("business rejection count must match its stage")


def stage_output(
    stage: PipelineStage,
    records: Sequence[_RecordT],
    *,
    as_of: datetime,
    input_count: int,
    rejected_count: int = 0,
    pending_count: int = 0,
    failed_count: int = 0,
    reasons: tuple[StageReasonAggregate, ...] = (),
    business_reasons: tuple[StageReasonAggregate, ...] = (),
    source_health: SourceHealth,
    latency_ms: int,
) -> PipelineStageOutput[_RecordT]:
    values = tuple(records)
    state = _state(len(values), pending_count, failed_count, source_health)
    snapshot = PipelineStageSnapshot(
        stage=stage,
        stage_order=PIPELINE_STAGES.index(stage) + 1,
        as_of=as_of,
        state=state,
        input_count=input_count,
        output_count=len(values),
        rejected_count=rejected_count,
        pending_count=pending_count,
        failed_count=failed_count,
        reasons=reasons,
        source_health=source_health,
        latency_ms=latency_ms,
        degraded=state is StageState.DEGRADED,
    )
    rejections = None
    if stage in {PipelineStage.STATIC_FILTER, PipelineStage.DYNAMIC_FILTER}:
        rate = Decimal(rejected_count) / Decimal(input_count) if input_count else Decimal("0")
        rejections = BusinessRejectionSummary(input_count, rejected_count, rate, business_reasons)
    return PipelineStageOutput(values, snapshot, rejections)


def require_previous_stage(output: PipelineStageOutput[object], stage: PipelineStage) -> None:
    expected = PIPELINE_STAGES.index(stage) - 1
    if expected < 0 or output.snapshot.stage is not PIPELINE_STAGES[expected]:
        raise ValueError("pipeline stage input does not come from the preceding stage")


def _state(
    output_count: int,
    pending_count: int,
    failed_count: int,
    source_health: SourceHealth,
) -> StageState:
    if failed_count and not output_count:
        return StageState.FAILED
    if pending_count and not output_count:
        return StageState.NOT_READY
    if pending_count or failed_count or source_health.state.value == "degraded":
        return StageState.DEGRADED
    return StageState.READY


__all__ = ["PipelineStageOutput", "require_previous_stage", "stage_output"]
