from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.unit.entrypoints.test_performance_fixtures import _budgets
from tests.unit.entrypoints.test_performance_models import _workload
from trader.entrypoints import performance
from trader.recommendation.infra.scoring.profile_factory import ScoringModelsMissingError


def test_missing_model_produces_bounded_performance_report(tmp_path, monkeypatch, capsys) -> None:
    def unavailable(*_args, **_kwargs):
        raise ScoringModelsMissingError()

    monkeypatch.setattr(performance, "Run", unavailable)
    assert performance.Main(["--config", str(tmp_path / "runtime.json"), "--profile", "v2"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["schema_version"] == "production_performance"
    assert report["status"] == "failed"
    assert report["failures"] == ["model_unavailable"]
    assert "private model path" not in captured.out + captured.err


@pytest.mark.parametrize("skip_pipeline", (False, True))
def test_full_four_profile_report_runs_real_scoring_and_never_claims_unmeasured_equivalence(monkeypatch, skip_pipeline):
    import socket
    from pathlib import Path

    def no_network(*_args, **_kwargs):
        raise AssertionError("offline performance must not connect to a network")

    monkeypatch.setattr(socket.socket, "connect", no_network)

    original = performance.LoadRuntimeSettings(Path("config/runtime.json").resolve())
    settings = replace(original, performance_budgets=_budgets())
    monkeypatch.setattr(performance, "LoadRuntimeSettings", lambda _path: settings)
    monkeypatch.setattr(performance, "LoadScoringProfile", lambda profile, **_kwargs: _workload(profile).profile)
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
    if skip_pipeline:
        assert "quote_to_draft:model_population_not_exercised" in report["failures"]
    assert report["network_calls"] == 0
    assert report["workload"]["model_heads"] == 8
    assert report["workload"]["tick_count"] == 100
    assert report["workload"]["execution_mode"] == "concurrent_heads_same_process"
    assert report["hot_path_baseline"]["status"] == "unverified"
    assert report["memory"]["sampling"] == "current_rss_each_tick_and_process_peak"
    for name, scenario in report["model_workload"].items():
        assert len(scenario["heads"]) == 8
        if not skip_pipeline or name != "quote_to_draft":
            assert all(head["input_count"] == head["predicted_count"] == 12 for head in scenario["heads"])
    assert report["identity"]["fixture_sha256"] and report["identity"]["models_sha256"]


@pytest.mark.parametrize("reason", ("missing", "dependency_missing", "contract_conflict", "corrupt"))
def test_all_profiles_report_load_failures_without_fabricating_measurements(monkeypatch, reason):
    from pathlib import Path

    from trader.recommendation.infra.scoring.profile_factory import ScoringModelsUnavailableError

    def unavailable(*_args, **_kwargs):
        raise ScoringModelsUnavailableError(reason)

    monkeypatch.setattr(performance, "LoadScoringProfile", unavailable)
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


def _baseline_identity():
    return {
        "config_version": "config",
        "scoring_profile": "v3",
        "fixture_sha256": "fixture",
        "models_sha256": "model",
        "strategy_contracts_sha256": "strategy",
        "cost_policy_sha256": "cost",
        "performance_budgets_sha256": "budgets",
        "python": "3.11",
        "dependencies": {"numpy": "2.0"},
        "cpu": "fixture-cpu",
        "cpu_count": 8,
        "machine": "x86_64",
        "system": "Linux",
        "kernel": "fixture-kernel",
    }
