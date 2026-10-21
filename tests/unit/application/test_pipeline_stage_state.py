from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.unit.application.pipeline_helpers import observed_input_stages, observed_market_batch
from tests.unit.application.test_tomorrow_projection import _verified_feature
from trader.bootstrap import _recommendation_policy
from trader.infra.settings import LoadStrategySettings
from trader.recommendation.application.pipeline.candidate_pool.candidate_builder import CandidatePlanningContext
from trader.recommendation.application.pipeline.candidate_pool.candidate_pipeline import (
    AssembleCandidateInputs,
    ExecuteCandidatePipeline,
)
from trader.recommendation.application.pipeline.dynamic_market.market_snapshot_service import (
    BuildDynamicMarketSnapshot,
    DynamicSourceHealth,
)
from trader.recommendation.application.pipeline.quality_check.input_quality_service import AssessCandidateInputStage
from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput, StageOutput
from trader.recommendation.domain.evidence.pipeline import PIPELINE_STAGES, SourceHealth, SourceHealthState, StageState
from trader.recommendation.domain.market.static import StaticIssuer
from trader.recommendation.domain.publication.models import Strategy

AS_OF = datetime(2026, 9, 23, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
ROOT = Path(__file__).resolve().parents[3]


def _policy():
    return _recommendation_policy(LoadStrategySettings(ROOT / "config" / "strategy.json"))


def test_zero_input_stages_are_not_ready_instead_of_completed() -> None:
    for stage in PIPELINE_STAGES:
        output = StageOutput(
            stage,
            (),
            input_batch_id="batch",
            as_of=AS_OF,
            input_count=0,
            source_health=SourceHealth(SourceHealthState.UNAVAILABLE, 0, 0),
            latency_ms=0,
        )
        assert output.snapshot.state is StageState.NOT_READY


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_real_dynamic_pass_preserves_input_and_measures_each_stage(application_feature_factory, strategy) -> None:
    valid = _verified_feature(application_feature_factory("600001", AS_OF - timedelta(seconds=10)))
    invalid = replace(valid, quote=replace(valid.quote, code="600002", price=None))
    blocked = replace(valid, quote=replace(valid.quote, code="600003", is_suspended=True, price=None))
    features = (valid, invalid, blocked)
    before = tuple(feature.quote for feature in features)
    snapshots = observed_input_stages(features, _policy(), AS_OF, strategy)
    dynamic = snapshots[3]
    assert tuple(feature.quote for feature in features) == before
    assert (dynamic.input_count, dynamic.output_count, dynamic.rejected_count, dynamic.pending_count) == (3, 1, 1, 1)
    assert all(stage.latency_ms > 0 for stage in snapshots[2:5])
    assert snapshots[2].source_health.age_seconds == 10.0
    assert snapshots[2].source_health.latest_success_at != AS_OF
    assert all(
        left.output_batch_id == right.input_batch_id and left.output_count == right.input_count
        for left, right in zip(snapshots[:-1], snapshots[1:], strict=True)
    )


def test_source_age_is_not_reset_by_discovery_normalization(application_feature_factory) -> None:
    old = _verified_feature(application_feature_factory("600001", AS_OF - timedelta(minutes=10)))
    batch = observed_market_batch((old,), AS_OF)
    ticks = iter(index / 10 for index in range(100))
    result = ExecuteCandidatePipeline(
        batch.dynamic_stage, CandidatePlanningContext(AS_OF, "batch", _policy(), None), lambda: next(ticks)
    )
    assert result.filtered[Strategy.TOMORROW].snapshot.source_health == batch.dynamic_stage.snapshot.source_health
    assert result.filtered[Strategy.TOMORROW].snapshot.source_health.age_seconds == 600.0
    assert result.filtered[Strategy.TOMORROW].snapshot.source_health.state is SourceHealthState.UNAVAILABLE


def test_quality_checks_actual_missing_directed_quote(application_feature_factory) -> None:
    feature = _verified_feature(application_feature_factory("600001", AS_OF - timedelta(seconds=10)))
    batch = observed_market_batch((feature,), AS_OF)
    ticks = iter(index / 10 for index in range(100))
    result = ExecuteCandidatePipeline(
        batch.dynamic_stage, CandidatePlanningContext(AS_OF, "batch", _policy(), None), lambda: next(ticks)
    )
    candidate = result.candidates[Strategy.TOMORROW]
    output = AssessCandidateInputStage(candidate, as_of=AS_OF, minimum_history_sessions=20, latency_ms=13)
    assert candidate.snapshot.output_count == 1
    assert (output.snapshot.input_count, output.snapshot.output_count, output.snapshot.pending_count) == (1, 0, 1)
    assert output.snapshot.state is StageState.NOT_READY
    assert output.snapshot.source_health.latest_success_at is None
    assert output.snapshot.source_health.state is SourceHealthState.UNAVAILABLE
    assert output.business_rejections is None


def test_business_empty_is_ready_after_actual_filtering(application_feature_factory) -> None:
    feature = _verified_feature(application_feature_factory("600001", AS_OF - timedelta(seconds=10)))
    blocked = replace(feature, quote=replace(feature.quote, is_suspended=True))
    snapshots = observed_input_stages((blocked,), _policy(), AS_OF)
    assert (snapshots[3].input_count, snapshots[3].output_count, snapshots[3].rejected_count) == (1, 0, 1)
    assert snapshots[3].state is StageState.READY
    assert snapshots[4].state is StageState.READY


def test_partial_dynamic_collection_retains_static_input_population(application_feature_factory) -> None:
    feature = _verified_feature(application_feature_factory("600001", AS_OF))
    complete = observed_market_batch((feature,), AS_OF)
    static = PipelineStageOutput(
        (
            StaticIssuer(
                feature.quote.code,
                feature.quote.name,
                feature.quote.board,
                feature.quote.exchange,
                feature.quote.listing_date,
                feature.quote.is_st,
                feature.quote.is_blacklisted,
            ),
        ),
        complete.static_stages[-1],
        StageOutput(
            PIPELINE_STAGES[3],
            (feature,),
            input_batch_id="static",
            as_of=AS_OF,
            input_count=1,
            source_health=complete.static_stages[-1].source_health,
            latency_ms=1,
        ).business_rejections,
    )
    result = BuildDynamicMarketSnapshot(static, (), as_of=AS_OF, data_version="fixture:empty", latency_ms=5)
    assert (result.snapshot.input_count, result.snapshot.output_count, result.snapshot.pending_count) == (1, 0, 1)
    assert result.snapshot.reasons[0].code == "refresh_pending"
    assert result.snapshot.source_health.latest_success_at is None
    assert complete.dynamic_stage.records == (feature,)


def test_directed_quality_reads_new_immutable_candidate_batch(application_feature_factory) -> None:
    feature = _verified_feature(application_feature_factory("600001", AS_OF))
    batch = observed_market_batch((feature,), AS_OF)
    ticks = iter(index / 10 for index in range(100))
    pipeline = ExecuteCandidatePipeline(
        batch.dynamic_stage, CandidatePlanningContext(AS_OF, "batch", _policy(), None), lambda: next(ticks)
    )
    original = pipeline.candidates[Strategy.TOMORROW]
    stale = replace(feature, quote=replace(feature.quote, source_time=AS_OF - timedelta(minutes=10)))
    directed = AssembleCandidateInputs(original, (stale,), as_of=AS_OF, data_version="fixture:correction")
    result = AssessCandidateInputStage(directed, as_of=AS_OF, minimum_history_sessions=20, latency_ms=7)
    assert original.records[0].features is None
    assert directed.snapshot.output_batch_id != original.snapshot.output_batch_id
    assert result.snapshot.input_batch_id == directed.snapshot.input_batch_id
    assert (result.snapshot.output_count, result.snapshot.pending_count, result.snapshot.rejected_count) == (0, 1, 0)
    assert result.snapshot.reasons[0].code == "stale_quote"
    assert result.snapshot.source_health.age_seconds == 600
    health = DynamicSourceHealth((feature, stale), AS_OF)
    assert health.state is SourceHealthState.UNAVAILABLE
    assert health.healthy_source_count == 0
