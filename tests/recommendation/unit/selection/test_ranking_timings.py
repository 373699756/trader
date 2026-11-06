"""Local score operation timing must preserve complete deterministic results."""

from dataclasses import replace
from itertools import count

import pytest

from tests.helpers.recommendation.runtime.recommendation_boundaries import _features, _runtime_policy
from tests.helpers.recommendation.publication.tomorrow_projection import EVALUATED_AT, _native_input
from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import (
    ScoreFeatureEvidence,
    ScoredSelectionIdentity,
    ScoredSelectionOptions,
)
from trader.recommendation.application.pipeline.dynamic_standardize.dynamic_feature_builder import ScoredSelectionNotReadyError
from trader.recommendation.application.pipeline.final_selection.freeze_projection import BuildScoredLocal, ScoredProjectionInputs
from trader.recommendation.application.pipeline.final_selection.grouped_ranking import RankingSelectionService
from trader.recommendation.application.ports.scoring import D25NativeInput, TomorrowNativeInput
from trader.recommendation.domain.evidence.pipeline import PipelineStage
from trader.recommendation.domain.publication.models import Strategy


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
@pytest.mark.parametrize("scenario", ("complete", "directed", "empty", "stale", "risk"))
def test_four_ranking_operations_preserve_full_selection(application_feature_factory, strategy, scenario):
    features = _features(application_feature_factory)
    if scenario == "empty":
        features = ()
    elif scenario == "stale":
        features = tuple(replace(item, quote=replace(item.quote, source_time=EVALUATED_AT.replace(hour=9))) for item in features)
    elif scenario == "risk":
        features = (replace(features[0], values={**features[0].values, "pledge_risk": 100.0}), *features[1:])
    policy = _runtime_policy()
    options = ScoredSelectionOptions(
        evaluated_at=EVALUATED_AT,
        max_age_seconds=30.0,
        strategy=strategy,
        candidate_features=features[:6] if scenario == "directed" else None,
    )
    identity = ScoredSelectionIdentity(EVALUATED_AT.date(), "timing-fixture", "timing-epoch")
    if scenario == "empty":
        with pytest.raises(ScoredSelectionNotReadyError, match="coherent_market_epoch_unavailable"):
            ScoreFeatureEvidence(features, policy, options, identity)
        with pytest.raises(ScoredSelectionNotReadyError, match="coherent_market_epoch_unavailable"):
            RankingSelectionService().select(features, policy, options, identity)
        return
    expected = ScoreFeatureEvidence(features, policy, options, identity)
    clock = iter((1.0, 2.0, 4.0, 7.0, 11.0))
    actual = RankingSelectionService(monotonic=lambda: next(clock)).select(features, policy, options, identity)

    assert actual.selection == expected
    assert actual.operation_durations == (
        ("candidate_preparation_and_qualification", 1000),
        ("board_reference_build", 2000),
        ("candidate_policy_and_ranking", 3000),
        ("local_component_scoring", 4000),
    )


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
def test_ranking_timings_reach_stage_without_changing_freeze_reasons_or_seats(application_feature_factory, native_type):
    native = _native_input(_features(application_feature_factory), native_type)
    policy = _runtime_policy()
    expected = BuildScoredLocal(native, policy, sequence=1, runtime=ScoredProjectionInputs(monotonic=lambda: 0.0))
    actual = BuildScoredLocal(
        native,
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(monotonic=count(0.0, 0.125).__next__),
    )

    assert actual.local == expected.local
    assert actual.local.version == expected.local.version
    assert actual.selection == expected.selection
    for baseline, measured in zip(expected.stages.snapshots, actual.stages.snapshots, strict=True):
        assert replace(measured, latency_ms=None, operation_durations=()) == replace(
            baseline, latency_ms=None, operation_durations=()
        )
    local_stage = next(stage for stage in actual.stages.snapshots if stage.stage is PipelineStage.LOCAL_SCORE)
    assert local_stage.operation_durations == (
        ("candidate_preparation_and_qualification", 125),
        ("board_reference_build", 125),
        ("candidate_policy_and_ranking", 125),
        ("local_component_scoring", 125),
    )
    assert sum(value for _, value in local_stage.operation_durations) <= local_stage.latency_ms
