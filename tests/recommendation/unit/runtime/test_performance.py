from __future__ import annotations

import json

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.recommendation.runtime.performance_fixtures import _budgets, _memory_with_growth
from tests.helpers.recommendation.scoring.performance_models import _install_performance_models

from trader.entrypoints import performance

from trader.recommendation.infra.scoring.profile_factory import ScoringModelsMissingError
from trader.recommendation.domain.publication.models import Strategy

from tests.helpers.recommendation.runtime.performance import (
    _baseline_identity,
)


@pytest.fixture
def performance_models(monkeypatch):
    _install_performance_models(monkeypatch)


def test_missing_model_produces_bounded_performance_report(tmp_path, monkeypatch, capsys) -> None:
    def unavailable(*_args, **_kwargs):
        raise ScoringModelsMissingError()

    monkeypatch.setattr(performance, "Run", unavailable)
    assert performance.Main(["--config", str(tmp_path / "runtime.json"), "--profile", "v2"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert "schema_version" not in report
    assert report["status"] == "failed"
    assert report["failures"] == ["model_unavailable"]
    assert "private model path" not in captured.out + captured.err


def test_replay_identity_binds_each_loaded_tomorrow_and_d25_model(monkeypatch) -> None:
    config = Path("config/runtime.json")
    profiles = ("v1", "v2", "v3", "v5")
    tomorrow_models = {profile: f"tomorrow-{profile}" for profile in profiles}
    calls = []

    def load_tomorrow(_root, profile, target, **_settings):
        calls.append((profile, target))
        identity = tomorrow_models.get(profile)
        return SimpleNamespace(identity=identity) if identity is not None else None

    def bind_d25(_root, profile, _target):
        head = SimpleNamespace(predictor=SimpleNamespace(model_hash=f"bound-d25-{profile}"))
        return SimpleNamespace(profile_id=profile, heads={Strategy.D25: head})

    def report_preparation(_settings, prepared, identity, *_options):
        return {"identity": identity, "profile_results": performance._project_profile_loads(prepared.profiles)}

    monkeypatch.setattr(performance, "LoadProductionD25Profile", bind_d25)
    monkeypatch.setattr("trader.training.infra.tomorrow.artifact.LoadTomorrowProbabilityModel", load_tomorrow)
    monkeypatch.setattr(performance, "BuildPerformanceFixtures", lambda *_args, **_options: ((), (), (1,)))
    monkeypatch.setattr(performance, "_continuous_report", report_preparation)
    first = performance.Run(config, scoring_profile=",".join(profiles), replay_ticks=1)
    assert [profile for profile, _ in calls] == list(profiles)
    assert all(target.strategy is Strategy.TOMORROW and target.threshold_bps == 500 for _, target in calls)
    for profile in profiles:
        assert first["profile_results"][profile]["models"] == {
            "tomorrow": f"tomorrow-{profile}", "d25": f"bound-d25-{profile}",
        }

    tomorrow_models["v3"] = "updated-tomorrow-v3"
    second = performance.Run(config, scoring_profile=",".join(profiles), replay_ticks=1)
    assert second["identity"]["fixture_sha256"] == first["identity"]["fixture_sha256"]
    assert second["identity"]["models_sha256"] != first["identity"]["models_sha256"]

    del tomorrow_models["v3"]
    missing = performance.Run(config, scoring_profile="v3", replay_ticks=1)
    assert missing["failures"] == ["model_unavailable"]
    assert missing["workload"]["model_heads"] == 1
    assert missing["profile_results"]["v3"] == {
        "status": "unverified", "reason": "tomorrow_target_model_unavailable", "models": {"d25": "bound-d25-v3"},
    }


@pytest.mark.usefixtures("performance_models")
def test_offline_tomorrow_counts_actual_v5_batches_without_changing_scoring_status() -> None:
    config = Path("config/runtime.json")
    settings = replace(performance.LoadRuntimeSettings(config), performance_budgets=_budgets())
    workload, = performance._prepare_profiles(settings, ("v5",), config).workloads
    scoring = workload.scoring()
    template = workload.features[0]
    sequence = template.model_sequence
    assert sequence is not None
    features = tuple(
        replace(template, quote=replace(template.quote, code=code), model_sequence=replace(sequence, code=code))
        for code in (f"{index:06}" for index in range(1, 258))
    )
    assert workload.computation_status(scoring, Strategy.TOMORROW).predictor_batch_count == 0
    empty = scoring.score(Strategy.TOMORROW, ())
    assert empty is not None and empty.predictions == ()
    assert workload.computation_status(scoring, Strategy.TOMORROW).predictor_batch_count == 0
    result = scoring.score(Strategy.TOMORROW, features)
    assert result is not None and len(result.predictions) == 257
    measured = workload.computation_status(scoring, Strategy.TOMORROW)
    assert measured.predictor_batch_count == 2
    stage, = measured.stage_durations
    assert (stage.calculator_group, stage.execution_count) == ("batch_prediction", 2)
    assert stage.cumulative_duration_ms >= stage.last_duration_ms >= 0
    runtime = scoring.status()
    assert runtime is not None and runtime.heads[Strategy.TOMORROW].computation.predictor_batch_count == 0


@pytest.mark.usefixtures("performance_models")
def test_steady_only_uses_existing_100_tick_measurement_without_claiming_pipeline_latency(monkeypatch, tmp_path):
    settings = replace(performance.LoadRuntimeSettings(Path("config/runtime.json")), performance_budgets=_budgets())
    monkeypatch.setattr(performance, "LoadRuntimeSettings", lambda _: settings)
    peak = settings.performance_budgets.memory.process_peak_rss_bytes
    monkeypatch.setattr(performance, "ReadPerformanceMemory", _memory_with_growth(peak))
    monkeypatch.setattr(performance, "_operations", lambda *_: pytest.fail("steady-only must not run cold pipeline probes"))
    report = performance.Run(Path("config/runtime.json"), scoring_profile="v1,v2,v3,v5", steady_only=True)
    assert report["status"] == "passed"
    assert report["workload"]["tick_count"] == 100
    assert report["workload"]["model_heads"] == 8
    assert all(set(result["models"]) == {"tomorrow", "d25"} for result in report["profile_results"].values())
    assert report["memory"]["peak_rss_bytes"] == peak
    assert report["memory"]["growth_percent"] == 25
    assert report["memory"]["growth_reference_percent"] == 20
    assert report["measurements"] == {}
    assert report["hot_path_baseline"]["status"] == "unverified"
    baseline = tmp_path / "steady.json"
    baseline.write_text(json.dumps(report))
    identity = dict(report["identity"])
    identity.pop("measurement_scope")
    with pytest.raises(ValueError, match="measurement scope"):
        performance._baseline(baseline, identity)
    with pytest.raises(ValueError, match="steady-only"):
        performance.Run(Path("config/runtime.json"), steady_only=True, replay_ticks=1)


@pytest.mark.parametrize("skip_pipeline", (False, True))
@pytest.mark.usefixtures("performance_models")
def test_full_four_profile_report_runs_real_scoring_and_never_claims_unmeasured_equivalence(monkeypatch, skip_pipeline):
    import socket

    def no_network(*_args, **_kwargs):
        raise AssertionError("offline performance must not connect to a network")

    monkeypatch.setattr(socket.socket, "connect", no_network)

    original = performance.LoadRuntimeSettings(Path("config/runtime.json").resolve())
    settings = replace(original, performance_budgets=_budgets())
    monkeypatch.setattr(performance, "LoadRuntimeSettings", lambda _path: settings)
    peak = settings.performance_budgets.memory.process_peak_rss_bytes
    monkeypatch.setattr(performance, "ReadPerformanceMemory", _memory_with_growth(peak))
    if skip_pipeline:
        measure = performance.MeasureModelWorkload

        def skipped(*args, **kwargs):
            scenarios = measure(*args, **kwargs)
            last = scenarios[-1]
            return (
                *scenarios[:-1],
                replace(last, heads=tuple(replace(head, predicted_count=0, predictor_calls=0) for head in last.heads)),
            )

        monkeypatch.setattr(performance, "MeasureModelWorkload", skipped)
    report = performance.Run(Path("config/runtime.json"), scoring_profile="v1,v2,v3,v5")
    assert report["status"] == ("failed" if skip_pipeline else "passed")
    assert report["failures"] == (["quote_to_draft:model_population_not_exercised"] if skip_pipeline else [])
    assert report["network_calls"] == 0
    assert report["workload"]["model_heads"] == 8
    assert report["workload"]["tick_count"] == 100
    assert report["workload"]["execution_mode"] == "concurrent_heads_same_process"
    assert report["hot_path_baseline"]["status"] == "unverified"
    assert report["memory"]["sampling"] == "current_rss_each_tick_and_process_peak"
    assert report["memory"]["peak_rss_bytes"] == peak
    assert report["memory"]["growth_percent"] == 25
    assert report["memory"]["growth_reference_percent"] == 20
    for name, scenario in report["model_workload"].items():
        assert len(scenario["heads"]) == 8
        if not skip_pipeline or name != "quote_to_draft":
            assert all(head["input_count"] == head["predicted_count"] == 12 for head in scenario["heads"])
            assert all(head["predictor_calls"] > 0 for head in scenario["heads"] if head["strategy"] == "tomorrow")
    assert report["identity"]["fixture_sha256"] and report["identity"]["models_sha256"]


@pytest.mark.parametrize("reason", ("missing", "dependency_missing", "contract_conflict", "corrupt"))
def test_all_profiles_report_load_failures_without_fabricating_measurements(monkeypatch, reason):
    from pathlib import Path

    from trader.recommendation.infra.scoring.profile_factory import ScoringModelsUnavailableError

    def unavailable(*_args, **_kwargs):
        raise ScoringModelsUnavailableError(reason)

    monkeypatch.setattr(performance, "LoadProductionD25Profile", unavailable)
    report = performance.Run(Path("config/runtime.json"), scoring_profile="v1,v2,v3,v5")
    assert report["measurements"] == {} and report["memory"] == {}
    assert report["failures"] == ["model_unavailable"]
    assert report["profile_results"] == {
        profile: {"status": "unverified", "reason": reason} for profile in ("v1", "v2", "v3", "v5")
    }


@pytest.mark.parametrize("changed", ("models_sha256", "fixture_sha256", "cost_policy_sha256", "cpu", "dependencies"))
def test_relative_baseline_rejects_different_workloads_or_environments(tmp_path, changed):
    identity = {**_baseline_identity(), changed: "current"}
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"identity": {**identity, changed: "other"}, "measurements": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="identity does not match"):
        performance._baseline(path, identity)


def test_relative_baseline_allows_source_changes_for_the_same_workload(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps({"identity": {**_baseline_identity(), "source_sha256": "old"}, "measurements": {}}), encoding="utf-8"
    )
    assert performance._baseline(path, {**_baseline_identity(), "source_sha256": "new"}) == {}


def test_relative_baseline_rejects_missing_contract_identity(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"identity": {}, "measurements": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="identity does not match"):
        performance._baseline(path, _baseline_identity())
