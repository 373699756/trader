"""Verify real scoring paths with fixture predictors, without claiming native-model timing."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from tests.unit.entrypoints.test_performance_fixtures import _budgets, _profile
from trader.infra.settings import LoadStrategySettings
from trader.recommendation.application.ports.loaded_profile import ModelPrediction
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.recommendation.infra.performance.fixtures import BuildPerformanceFixtures
from trader.recommendation.infra.performance.model_workload import (
    MeasureModelWorkload,
    MeasureSteadyState,
    PerformanceProfileWorkload,
)
from trader.recommendation.infra.performance.process_memory import PerformanceMemorySample
from trader.recommendation.infra.policy_settings import _recommendation_policy


class _Predictor:
    def __init__(self, base):
        self.profile_id = base.profile_id
        self.feature_ids = base.feature_ids
        self.industry_ids = base.industry_ids
        self.model_id = "learned_rank" if base.profile_id == "v1" else base.model_id
        self.model_hash = base.model_hash
        self.exposure_contract = base.exposure_contract

    def predict(self, inputs):
        if self.profile_id == "v5":
            assert all(item.sequence is not None for item in inputs)
        return tuple(
            ModelPrediction(
                item.code,
                0.02 + item.alpha_features[1] * 0.0001,
                item.alpha_features[0] if self.profile_id == "v1" else None,
            )
            for item in inputs
        )


def _workload(profile_id):
    from pathlib import Path

    profile = _profile(profile_id)
    profile = replace(
        profile,
        heads={
            strategy: replace(head, predictor=_Predictor(head.predictor)) for strategy, head in profile.heads.items()
        },
    )
    _, _, features = BuildPerformanceFixtures(_budgets(), profile)
    policy = _recommendation_policy(LoadStrategySettings(Path("config/strategy.json"), scoring_profile=profile_id))
    return PerformanceProfileWorkload(profile, policy, features, "fixture-config", ExecutionCostPolicy(0.002, 1.0))


@pytest.mark.parametrize("profile_id", ("v1", "v2", "v3", "v5"))
def test_model_scenarios_execute_complete_dual_heads_and_preserve_reuse_semantics(profile_id):
    with ThreadPoolExecutor(max_workers=2) as executor:
        measured = MeasureModelWorkload((_workload(profile_id),), executor, warmup=0, rounds=1)
    assert tuple(item.scenario for item in measured) == (
        "first_computation",
        "exact_reuse",
        "single_stock_change",
        "quote_to_draft",
    )
    for case in measured:
        assert len(case.durations_ms) == 1 and case.durations_ms[0] > 0
        assert len(case.heads) == 2
        assert all(head.input_count == head.predicted_count == 12 for head in case.heads)
    fresh, reused, changed, pipeline = measured
    assert all(head.predictor_calls > 0 for head in fresh.heads)
    assert all(head.predictor_calls > 0 for head in changed.heads)
    assert [head.result_hash for head in fresh.heads] == [head.result_hash for head in reused.heads]
    assert any(a.result_hash != b.result_hash for a, b in zip(fresh.heads, changed.heads, strict=True))
    if profile_id != "v1":
        assert all(head.predictor_calls == 0 and head.cache_hits == 1 for head in reused.heads)
    else:
        assert all(head.predictor_calls > 0 for head in reused.heads)
    # These fixture predictors are intentionally not reported as native LightGBM/TCN calls.
    assert all(
        not any(
            dict(head.native_calls)[name]
            for name in ("lightgbm_predict_calls", "tcn_predict_calls", "ridge_batch_evaluations")
        )
        for head in fresh.heads
    )
    assert all(head.predictor_calls > 0 for head in pipeline.heads)


def test_four_profiles_share_one_concurrent_eight_head_measurement():
    workloads = tuple(_workload(profile) for profile in ("v1", "v2", "v3", "v5"))
    with ThreadPoolExecutor(max_workers=8) as executor:
        measured = MeasureModelWorkload(workloads, executor, warmup=0, rounds=1)
    for scenario in measured:
        assert len(scenario.heads) == 8
        assert {head.profile_id for head in scenario.heads} == {"v1", "v2", "v3", "v5"}
        assert scenario.project()["samples"] == 1


def test_missing_model_sequence_cannot_produce_a_successful_performance_sample():
    workload = _workload("v5")
    workload = replace(workload, features=(replace(workload.features[0], model_sequence=None), *workload.features[1:]))
    with ThreadPoolExecutor(max_workers=2) as executor, pytest.raises(ValueError, match="not eligible"):
        MeasureModelWorkload((workload,), executor, warmup=0, rounds=1)


def test_steady_state_uses_current_rss_growth_and_keeps_same_scoring_instances():
    samples = iter(PerformanceMemorySample(current, 1000) for current in (100, 110, 130, 120))
    with ThreadPoolExecutor(max_workers=2) as executor:
        measured = MeasureSteadyState((_workload("v3"),), executor, lambda: next(samples), ticks=3)
    assert measured.ticks == 3
    assert measured.rss_before_bytes == 100 and measured.rss_after_bytes == 120
    assert measured.peak_rss_bytes == 1000
    assert measured.growth_percent == pytest.approx(30)
