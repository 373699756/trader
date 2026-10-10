"""Explicit observed static-stage fixtures for application boundary contracts."""

from collections.abc import Sequence
from datetime import datetime

from trader.recommendation.application.pipeline.candidate_pool.candidate_builder import CandidatePlanningContext
from trader.recommendation.application.pipeline.candidate_pool.candidate_pipeline import (
    assemble_candidate_inputs,
    execute_candidate_pipeline,
)
from trader.recommendation.application.pipeline.dynamic_market.market_snapshot_service import (
    build_dynamic_market_snapshot,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.pipeline.quality_check.input_quality_service import assess_candidate_input_stage
from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput, stage_output
from trader.recommendation.application.ports.market_data import FullMarketFeatureBatch
from trader.recommendation.domain.evidence.pipeline import (
    PIPELINE_STAGES,
    PipelineStageSnapshot,
    SourceHealth,
    SourceHealthState,
)
from trader.recommendation.domain.market.eligibility import IssuerEligibilityBatch
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.market.static import StaticIssuer
from trader.recommendation.domain.publication.models import Strategy


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


def observed_market_batch(features: Sequence[FeatureSnapshot], as_of: datetime) -> FullMarketFeatureBatch:
    records = tuple(features)
    snapshots = observed_static_stages(len(records), as_of)
    static = PipelineStageOutput(
        tuple(
            StaticIssuer(
                item.quote.code,
                item.quote.name,
                item.quote.board,
                item.quote.exchange,
                item.quote.listing_date,
                item.quote.is_st,
                item.quote.is_blacklisted,
            )
            for item in records
        ),
        snapshots[-1],
        stage_output(
            PIPELINE_STAGES[3],
            records,
            input_batch_id=snapshots[-1].input_batch_id,
            as_of=as_of,
            input_count=len(records),
            source_health=snapshots[-1].source_health,
            latency_ms=14,
        ).business_rejections,
    )
    dynamic = build_dynamic_market_snapshot(static, records, as_of=as_of, data_version="fixture:dynamic", latency_ms=21)
    return FullMarketFeatureBatch(records, IssuerEligibilityBatch(len(records), len(records)), snapshots, dynamic)


def observed_input_stages(
    features: Sequence[FeatureSnapshot],
    policy: RecommendationPolicy,
    as_of: datetime,
    strategy: Strategy = Strategy.TOMORROW,
) -> tuple[PipelineStageSnapshot, ...]:
    return observed_quality_input(features, policy, as_of, strategy)[0]


def observed_quality_input(
    features: Sequence[FeatureSnapshot],
    policy: RecommendationPolicy,
    as_of: datetime,
    strategy: Strategy = Strategy.TOMORROW,
) -> tuple[tuple[PipelineStageSnapshot, ...], PipelineStageOutput[FeatureSnapshot]]:
    batch = observed_market_batch(features, as_of)
    ticks = iter(index / 100 for index in range(100))
    pipeline = execute_candidate_pipeline(
        batch.dynamic_stage,
        CandidatePlanningContext(as_of, "fixture", policy, None, 120),
        lambda: next(ticks),
    )
    candidates = assemble_candidate_inputs(
        pipeline.candidates[strategy], tuple(features), as_of=as_of, data_version="fixture:available"
    )
    quality = assess_candidate_input_stage(candidates, as_of=as_of, minimum_history_sessions=20, latency_ms=17)
    snapshots = (
        *batch.static_stages,
        batch.dynamic_stage.snapshot,
        *pipeline.stages(strategy)[:2],
        candidates.snapshot,
        quality.snapshot,
    )
    return snapshots, quality
