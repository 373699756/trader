"""Continuous diagnostic evidence must distinguish cache reuse from skipped work."""

import gc
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from tests.unit.entrypoints.test_performance_fixtures import _budgets, _history_sessions
from tests.unit.entrypoints.test_performance_models import _workload
from trader.entrypoints import performance
from trader.infra.settings import LoadRuntimeSettings, LoadStrategySettings
from trader.recommendation.infra.performance.cpu_trace import PerformanceCpuTrace
from trader.recommendation.infra.performance.fixtures import BuildPerformanceFixtures
from trader.recommendation.infra.performance.model_workload import PerformanceProfileWorkload
from trader.recommendation.infra.performance.pipeline_workload import MeasureContinuousPipeline, PipelineReplayWorkload
from trader.recommendation.infra.performance.process_memory import ReadPerformanceMemory


def _replay(profiles, ticks=2, trace=None, factor_reuse=False, scoring_candidate="baseline"):
    settings = LoadRuntimeSettings(Path("config/runtime.json"))
    workloads = tuple(
        PipelineReplayWorkload(
            _workload(profile), LoadStrategySettings(Path("config/strategy.json"), scoring_profile=profile)
        )
        for profile in profiles
    )
    with ThreadPoolExecutor(max_workers=len(workloads)) as executor, ThreadPoolExecutor(max_workers=2) as predictions:
        return MeasureContinuousPipeline(
            workloads, settings.market_data.cache_policy, executor, ReadPerformanceMemory,
            ticks=ticks, trace=trace, factor_reuse=factor_reuse,
            scoring_candidate=scoring_candidate,
            prediction_executor=predictions if scoring_candidate in {"v5_industry_parallel", "combined"} else None,
        )


@pytest.mark.parametrize("candidate", ("prepared_reuse", "v5_industry_parallel", "combined"))
def test_scoring_candidates_preserve_complete_four_profile_replay(candidate):
    baseline = _replay(("v1", "v2", "v3", "v5"), ticks=1)
    actual = _replay(("v1", "v2", "v3", "v5"), ticks=1, scoring_candidate=candidate)
    first = [baseline["cold"], *(case["rounds"][0] for case in baseline["scenarios"].values())]
    second = [actual["cold"], *(case["rounds"][0] for case in actual["scenarios"].values())]
    for original, changed in zip(first, second, strict=True):
        for left, right in zip(original["profiles"], changed["profiles"], strict=True):
            assert left["feature_hash"] == right["feature_hash"]
            assert left["input_version"] == right["input_version"]
            assert [head["result_hash"] for head in left["heads"]] == [head["result_hash"] for head in right["heads"]]
            assert [head["predictor_batches"] for head in left["heads"]] == [head["predictor_batches"] for head in right["heads"]]
    if candidate != "v5_industry_parallel":
        assert all(item["hits"] > 0 and item["calculations"] > 0 for item in actual["scoring_reuse"])
    if candidate != "prepared_reuse":
        assert actual["industry_prediction"]["in_flight"] == 0
        assert actual["industry_prediction"]["completed"] > 0


@pytest.mark.parametrize("profiles", (("v3",), ("v1", "v2", "v3", "v5")))
def test_raw_factor_candidate_preserves_complete_replay_outputs_and_one_budget(profiles):
    baseline = _replay(profiles, ticks=1)
    candidate = _replay(profiles, ticks=1, factor_reuse=True)
    assert baseline["factor_candidate"] == "baseline"
    assert candidate["factor_candidate"] == "raw_factor_reuse"
    first = [baseline["cold"], *(case["rounds"][0] for case in baseline["scenarios"].values())]
    second = [candidate["cold"], *(case["rounds"][0] for case in candidate["scenarios"].values())]
    for expected, actual in zip(first, second, strict=True):
        assert expected["raw_factor_computations"] is None
        assert actual["raw_factor_computations"] is not None
        for original, reused in zip(expected["profiles"], actual["profiles"], strict=True):
            assert original["feature_hash"] == reused["feature_hash"]
            assert original["input_version"] == reused["input_version"]
            assert [head["result_hash"] for head in original["heads"]] == [head["result_hash"] for head in reused["heads"]]
            assert [head["predictor_batches"] for head in original["heads"]] == [head["predictor_batches"] for head in reused["heads"]]
    for profile in profiles:
        assert candidate["cpu_probe"]["profiles"][profile]["raw_factor_requests"] < baseline["cpu_probe"]["profiles"][profile]["raw_factor_requests"]
        assert candidate["cpu_probe"]["profiles"][profile]["raw_factor_cache_requests"] == baseline["cpu_probe"]["profiles"][profile]["raw_factor_requests"]
    assert candidate["memory"]["factor_cache_max_bytes"] <= baseline["memory"]["factor_group_budget_bytes"]
    assert candidate["memory"]["feature_group_max_bytes"] <= baseline["memory"]["factor_group_budget_bytes"]


def test_factor_candidate_is_explicit_and_rejects_non_replay_before_loading(monkeypatch):
    assert performance.BuildParser().parse_args(["--config", "runtime.json"]).factor_candidate == "baseline"
    for options in ({"factor_candidate": "raw_factor_reuse"}, {"factor_candidate": "unknown"}):
        with pytest.raises(ValueError, match="factor candidate"):
            performance.Run(Path("missing-runtime.json"), **options)


@pytest.mark.parametrize("functions", (False, True))
def test_opt_in_trace_keeps_completed_rounds_and_releases_gc_callback(tmp_path, functions):
    before = tuple(gc.callbacks)
    with PerformanceCpuTrace(tmp_path, functions=functions) as trace:
        report = _replay(("v3",), ticks=1, trace=trace)
    assert tuple(gc.callbacks) == before
    events = [json.loads(line) for line in (tmp_path / "progress.jsonl").read_text().splitlines()]
    completed = [event for event in events if event["event"] == "round"]
    assert len(completed) == 8
    assert completed[0]["result"]["profiles"] == report["cold"]["profiles"]
    assert bool(tuple(tmp_path.glob("*.pstats"))) is functions


def test_trace_flushes_failed_phase_and_gc_without_leaking_callback(tmp_path):
    before = tuple(gc.callbacks)

    def fail():
        gc.collect(2)
        raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError, match="injected failure"), PerformanceCpuTrace(tmp_path) as trace:
        trace.run("failed", fail)
    assert tuple(gc.callbacks) == before
    events = [json.loads(line) for line in (tmp_path / "progress.jsonl").read_text().splitlines()]
    assert events[0]["event"] == "start"
    assert events[-1]["event"] == "end"
    assert any(event["phase"] == "generation_2" for event in events)
    assert (tmp_path / "failed.pstats").is_file()


def test_instrumented_results_cannot_be_used_as_latency_baseline(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"identity": {"instrumented": True}, "measurements": {}}))
    with pytest.raises(ValueError, match="instrumented"):
        performance._baseline(path, {})


@pytest.mark.parametrize("profile", ("v1", "v2", "v3", "v5"))
def test_continuous_chain_keeps_instances_and_fingerprints_while_counting_real_work(profile, monkeypatch):
    from trader.recommendation.application.pipeline.final_selection import freeze_projection
    from trader.recommendation.infra.performance import pipeline_workload

    calls = []
    original = PerformanceProfileWorkload.scoring
    projection_sources, model_sources = [], []
    original_projection = pipeline_workload.BuildScoredLocal
    original_population = freeze_projection.QualifiedPopulationFeatures

    def scoring(workload):
        result = original(workload)
        calls.append(result)
        return result

    def project(native, policy, *, sequence, runtime):
        source = runtime.population_qualifications
        assert source is not None
        assert source.request.features == native.market_features
        assert all(
            frozen is original
            for frozen, original in zip(source.request.features, native.market_features, strict=True)
        )
        assert source.request.policy.strategy is native.strategy
        projection_sources.append(source)
        return original_projection(native, policy, sequence=sequence, runtime=runtime)

    def population(source):
        model_sources.append(source)
        return original_population(source)

    monkeypatch.setattr(PerformanceProfileWorkload, "scoring", scoring)
    monkeypatch.setattr(pipeline_workload, "BuildScoredLocal", project)
    monkeypatch.setattr(freeze_projection, "QualifiedPopulationFeatures", population)
    report = _replay((profile,))
    assert len(calls) == 1
    assert projection_sources and len(projection_sources) == len(model_sources)
    assert all(projected is adopted for projected, adopted in zip(projection_sources, model_sources, strict=True))
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
        else:
            assert all(head["predictor_batches"] > 0 for head in current["heads"])
        assert round_["wall_ms"] >= current["wall_ms"] >= sum(current["stage_ms"].values())
    for scenario in ("observation_time", "delivery_time", "quote_time"):
        for round_ in report["scenarios"][scenario]["rounds"]:
            assert round_["factor_computations"] == round_["recomputed_stocks"] == 0
            current = round_["profiles"][0]
            assert current["input_revision_changed"] == (scenario == "quote_time")
            assert current["feature_hash"] == cold["profiles"][0]["feature_hash"]
            # Source timestamps are retained in filter audits; those hashes
            # change legitimately even when cached arithmetic stays equal.
            if scenario != "quote_time":
                assert [head["result_hash"] for head in current["heads"]] == [
                    head["result_hash"] for head in cold["profiles"][0]["heads"]
                ]
    # A fresh source timestamp and changed price must reach the real factor builder.
    changed = report["scenarios"]["one_price"]["rounds"][0]
    assert changed["recomputed_stocks"] == 1
    assert changed["profiles"][0]["feature_hash"] != cold["profiles"][0]["feature_hash"]
    stale = report["scenarios"]["stale"]["rounds"][0]
    assert all(head["input_count"] == 12 for head in stale["profiles"][0]["heads"])
    assert all(head["predicted_count"] == head["selected_count"] == 0 for head in stale["profiles"][0]["heads"])
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
    inputs, quotes, features = BuildPerformanceFixtures(
        budgets, profile, history_sessions=_history_sessions("v3"), rows=3
    )
    assert len(inputs) == len(quotes) == len(features) == 3
    assert budgets.workload.market_rows == 5500
    with pytest.raises(ValueError, match="configured population"):
        BuildPerformanceFixtures(budgets, profile, history_sessions=_history_sessions("v3"), rows=5501)
    with pytest.raises(ValueError, match="require continuous"):
        performance.Run(Path("missing.json"), replay_rows=3)
