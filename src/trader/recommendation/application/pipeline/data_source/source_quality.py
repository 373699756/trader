"""Quality degradation and failure projection for data-source refreshes."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput, StageOutput
from trader.recommendation.domain.evidence.pipeline import PipelineStage, SourceHealth, StageReasonAggregate
from trader.recommendation.domain.market.models import FeatureSnapshot


def UsesFallback(features: tuple[FeatureSnapshot, ...], *, expected_source: str | None) -> bool:
    return any(
        (expected_source is not None and feature.quote.source != expected_source)
        or "market_data_degraded" in feature.quote.execution_restrictions
        for feature in features
    )


def FailureCode(exc: BaseException) -> str:
    value = str(exc).strip().lower()
    if re.fullmatch(r"[a-z0-9_]{1,64}", value) is not None:
        return value
    name = type(exc).__name__
    return "".join((f"_{character.lower()}" if character.isupper() else character) for character in name).lstrip("_")


def DecisionFailureCode(exc: BaseException) -> str:
    if str(exc) == "scored native input cannot contain future features":
        return "future_input_time"
    return FailureCode(exc)


def BuildSourceStageOutput(
    records: Sequence[FeatureSnapshot],
    *,
    input_batch_id: str,
    as_of: datetime,
    expected_count: int,
    failed_count: int,
    reasons: tuple[StageReasonAggregate, ...],
    source_health: SourceHealth,
    latency_ms: int,
) -> PipelineStageOutput[FeatureSnapshot]:
    received = tuple(records)
    pending_count = expected_count - len(received) - failed_count
    if pending_count < 0:
        raise ValueError("source stage outcomes exceed the expected population")
    return StageOutput(
        PipelineStage.DATA_SOURCE,
        received,
        input_batch_id=input_batch_id,
        as_of=as_of,
        input_count=expected_count,
        pending_count=pending_count,
        failed_count=failed_count,
        reasons=reasons,
        source_health=source_health,
        latency_ms=latency_ms,
    )
