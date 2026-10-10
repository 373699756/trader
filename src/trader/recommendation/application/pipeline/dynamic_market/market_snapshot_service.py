"""Build the immutable stage-5 dynamic collection result."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from trader.recommendation.application.pipeline.stage_output import (
    PipelineStageOutput,
    require_previous_stage,
    stage_output,
)
from trader.recommendation.domain.evidence.pipeline import (
    PipelineStage,
    Severity,
    SourceHealth,
    SourceHealthState,
    StageReasonAggregate,
)
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.market.static import StaticIssuer


def build_dynamic_market_snapshot(
    source: PipelineStageOutput[StaticIssuer],
    loaded: tuple[FeatureSnapshot, ...],
    *,
    as_of: datetime,
    data_version: str,
    latency_ms: int,
) -> PipelineStageOutput[FeatureSnapshot]:
    require_previous_stage(source, PipelineStage.DYNAMIC_MARKET)
    requested = {item.code for item in source.records}
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
        source_health=dynamic_source_health(loaded, as_of),
        latency_ms=latency_ms,
        output_batch_id=f"{source.snapshot.output_batch_id}:dynamic_market:{data_version}:{as_of:%Y%m%dT%H%M%S%f}",
    )


def dynamic_source_health(features: tuple[FeatureSnapshot, ...], as_of: datetime) -> SourceHealth:
    """Observe accepted quote sources and their real ages; never invent a success."""
    sources = {feature.quote.source for feature in features if feature.quote.source}
    invalid = {
        feature.quote.source
        for feature in features
        if feature.quote.source and (feature.quote.source_time > as_of or feature.quote.age_seconds(as_of) > 30.0)
    }
    healthy = {
        feature.quote.source
        for feature in features
        if feature.quote.source and feature.quote.source_time <= as_of and feature.quote.age_seconds(as_of) <= 30.0
    } - invalid
    state = (
        SourceHealthState.READY
        if sources and sources == healthy
        else (SourceHealthState.DEGRADED if healthy else SourceHealthState.UNAVAILABLE)
    )
    latest = max(
        (
            feature.quote.received_time.astimezone(ZoneInfo("Asia/Shanghai"))
            for feature in features
            if feature.quote.received_time <= as_of
        ),
        default=None,
    )
    age = max((feature.quote.age_seconds(as_of) for feature in features), default=None)
    return SourceHealth(state, len(sources), len(healthy), latest, age)


__all__ = ["build_dynamic_market_snapshot", "dynamic_source_health"]
