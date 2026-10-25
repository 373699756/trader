"""Continuous diagnostic evidence must distinguish cache reuse from skipped work."""

import socket
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from tests.unit.entrypoints.test_performance_models import _workload
from tests.unit.entrypoints.test_performance_fixtures import _budgets
from trader.entrypoints import performance
from trader.infra.settings import LoadRuntimeSettings, LoadStrategySettings
from trader.recommendation.infra.performance.model_workload import PerformanceProfileWorkload
from trader.recommendation.infra.performance.fixtures import BuildPerformanceFixtures
from trader.recommendation.infra.performance.pipeline_workload import MeasureContinuousPipeline, PipelineReplayWorkload
from trader.recommendation.infra.performance.process_memory import ReadPerformanceMemory


def _replay(profiles, ticks=2):
    settings = LoadRuntimeSettings(Path("config/runtime.json"))
    workloads = tuple(
        PipelineReplayWorkload(
            _workload(profile), LoadStrategySettings(Path("config/strategy.json"), scoring_profile=profile)
        )
        for profile in profiles
    )
    with ThreadPoolExecutor(max_workers=len(workloads)) as executor:
        return MeasureContinuousPipeline(
            workloads, settings.market_data.cache_policy, executor, ReadPerformanceMemory, ticks=ticks
        )


@pytest.mark.parametrize("profile", ("v1", "v2", "v3", "v5"))
def test_continuous_chain_keeps_instances_and_fingerprints_while_counting_real_work(profile, monkeypatch):
    calls = []
    original = PerformanceProfileWorkload.scoring

    def scoring(workload):
        result = original(workload)
        calls.append(result)
        return result

    monkeypatch.setattr(PerformanceProfileWorkload, "scoring", scoring)
    report = _replay((profile,))
    assert len(calls) == 1
    cold = report["cold"]
    assert cold["recomputed_stocks"] == 12
    assert all(head["predicted_count"] == 12 for head in cold["profiles"][0]["heads"])
    assert all(head["predictor_batches"] > 0 for head in cold["profiles"][0]["heads"])
    for round_ in report["scenarios"]["unchanged"]["rounds"]:
        current = round_["profiles"][0]
        assert round_["factor_computations"] == round_["recomputed_stocks"] == 0
        assert not current["input_revision_changed"]
        assert current["feature_hash"] == cold["profiles"][0]["feature_hash"]
        assert [head["result_hash"] for head in current["heads"]] == [
            head["result_hash"] for head in cold["profiles"][0]["heads"]
        ]
        if profile != "v1":
            assert all(head["predictor_batches"] == 0 for head in current["heads"])
        assert round_["wall_ms"] >= current["wall_ms"] >= sum(current["stage_ms"].values())
    observation = report["scenarios"]["observation_time"]["rounds"][0]
    assert observation["recomputed_stocks"] == 0
    assert observation["profiles"][0]["feature_hash"] == cold["profiles"][0]["feature_hash"]
    # A fresh source timestamp and changed price must reach the real factor builder.
    changed = report["scenarios"]["one_price"]["rounds"][0]
    assert changed["recomputed_stocks"] == 1
    assert changed["profiles"][0]["feature_hash"] != cold["profiles"][0]["feature_hash"]
    stale = report["scenarios"]["stale"]["rounds"][0]
    assert all(head["input_count"] == 0 for head in stale["profiles"][0]["heads"])
    assert report["memory"]["factor_cache_max_bytes"] <= report["memory"]["factor_group_budget_bytes"]


def test_combined_continuous_chain_preserves_independent_heads_and_one_shared_byte_budget():
    report = _replay(("v1", "v2", "v3", "v5"), ticks=1)
    assert len(report["cold"]["profiles"]) == 4
    assert sum(len(profile["heads"]) for profile in report["cold"]["profiles"]) == 8
    assert report["memory"]["factor_group_budget_bytes"] == 51380224
    for scenario in report["scenarios"].values():
        assert scenario["samples"] == 1
        assert len(scenario["rounds"][0]["profiles"]) == 4


def test_replay_entrypoint_does_not_run_old_microbenchmarks_or_access_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected network or old benchmark")

    settings = LoadRuntimeSettings(Path("config/runtime.json"))
    monkeypatch.setattr(performance, "LoadRuntimeSettings", lambda _: replace(settings, performance_budgets=_budgets()))
    monkeypatch.setattr(performance, "LoadScoringProfile", lambda profile, **kwargs: _workload(profile).profile)
    monkeypatch.setattr(performance, "_operations", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    report = performance.Run(Path("config/runtime.json"), scoring_profile="v3", replay_ticks=1, replay_rows=6)
    assert report["status"] == "passed"
    assert report["network_calls"] == 0
    assert report["identity"]["replay_ticks"] == 1
    assert report["workload"]["market_rows"] == 6
    assert report["workload"]["configured_market_rows"] == 12
    assert report["continuous_pipeline"]["cold"]["profiles"][0]["heads"][0]["predictor_batches"] > 0
    assert report["hot_path_baseline"]["status"] == "unverified"


@pytest.mark.parametrize("ticks", (-1, 1001))
def test_invalid_replay_bounds_are_rejected_before_loading(ticks):
    with pytest.raises(ValueError, match="replay ticks"):
        performance.Run(Path("missing-runtime.json"), replay_ticks=ticks)


@pytest.mark.parametrize("changed", ("replay_ticks", "continuous_contract", "replay_rows"))
def test_continuous_baseline_rejects_different_replay_inputs(tmp_path, changed):
    from tests.unit.entrypoints.test_performance import _baseline_identity

    identity = {
        **_baseline_identity(),
        "replay_ticks": 3,
        "continuous_contract": "acquired_quotes_to_paired_memory_publication",
    }
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"identity": {**identity, changed: "different"}, "measurements": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        performance._baseline(path, identity)


def test_bounded_replay_does_not_change_the_fixed_production_population():
    settings = LoadRuntimeSettings(Path("config/runtime.json"))
    budgets = settings.performance_budgets
    assert budgets.workload.market_rows == budgets.workload.candidate_rows == 5500
    profile = _workload("v3").profile
    inputs, quotes, features = BuildPerformanceFixtures(budgets, profile, rows=3)
    assert len(inputs) == len(quotes) == len(features) == 3
    assert budgets.workload.market_rows == 5500
    with pytest.raises(ValueError, match="configured population"):
        BuildPerformanceFixtures(budgets, profile, rows=5501)
    with pytest.raises(ValueError, match="require continuous"):
        performance.Run(Path("missing.json"), replay_rows=3)
