"""Stage-6 dynamic normalization boundary."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime

from trader.recommendation.application.pipeline.stage_output import (
    PipelineStageOutput,
    RequirePreviousStage,
    StageOutput,
)
from trader.recommendation.domain.evidence.pipeline import PipelineStage, Severity, StageReasonAggregate
from trader.recommendation.domain.market.models import FeatureSnapshot


def NormalizeDynamicMarket(
    source: PipelineStageOutput[FeatureSnapshot],
    normalizer: Callable[[FeatureSnapshot], FeatureSnapshot | None],
    *,
    as_of: datetime,
    latency_ms: int,
) -> PipelineStageOutput[FeatureSnapshot]:
    RequirePreviousStage(source, PipelineStage.DYNAMIC_STANDARDIZE)
    normalized = tuple(value for record in source.records if (value := normalizer(record)) is not None)
    pending = len(source.records) - len(normalized)
    reasons = (
        (StageReasonAggregate("refresh_pending", "refresh pending", pending, Severity.WARNING),) if pending else ()
    )
    return StageOutput(
        PipelineStage.DYNAMIC_STANDARDIZE,
        normalized,
        input_batch_id=source.snapshot.output_batch_id,
        as_of=as_of,
        input_count=len(source.records),
        pending_count=pending,
        reasons=reasons,
        source_health=source.snapshot.source_health,
        latency_ms=latency_ms,
    )


def NormalizeCandidateDiscoveryPopulation(
    features: Sequence[FeatureSnapshot],
    evaluated_at: datetime,
) -> tuple[FeatureSnapshot, ...]:
    """Normalize one discovery population once before strategy-specific planning."""

    normalized: list[FeatureSnapshot] = []
    for feature in features:
        source_time = min(evaluated_at, feature.quote.received_time)
        normalized.append(
            feature
            if feature.quote.source_time == source_time
            else replace(feature, quote=replace(feature.quote, source_time=source_time))
        )
    return tuple(normalized)


__all__ = ["NormalizeDynamicMarket", "NormalizeCandidateDiscoveryPopulation"]
