"""Benchmark isolation, direct supervision, and honest comparison admission."""

from tests.helpers.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments

from scripts.core.catalog import SelectionArguments

from tests.helpers.configuration import SqlitePolicyFixture

import json
import hashlib
import shutil
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.core.projections import BuildReport
from scripts.training.jobs import training_alignment
from scripts.core.contracts import DiagnosticResult
from scripts.training.jobs.training_alignment import _numeric_fingerprints
from scripts.training.jobs.training_performance import SummarizeTrainingBenchmark
from scripts.training.jobs.training_performance import TrainingReuseFailures
from scripts.training.jobs.training_performance import TrainingSampleReuseFailures


def test_benchmark_plans_direct_children_with_distinct_external_outputs(tmp_path):
    args = ParseToolArguments(
        [
            *SelectionArguments('training-performance'),
            "--history-root",
            str(tmp_path / "history"),
            "--training-probe-directory",
            str(tmp_path / "work"),
            "--training-profiles",
            "v1,v5",
        ]
    )
    options = ParseBusinessOptions(args)
    commands = BuildToolCommands(options)
    assert len(commands) == 5
    assert [command.name for command in commands] == [
        "training_alignment:serial_before:v1",
        "training_alignment:serial_before:v5",
        "training_alignment:parallel:v1,v5",
        "training_alignment:serial_after:v1",
        "training_alignment:serial_after:v5",
    ]
    assert all("scripts.training.jobs.training_alignment" in command.argv for command in commands)
    assert len({command.argv[command.argv.index("--work-root") + 1] for command in commands}) == 5
    (tmp_path / "work").mkdir()
    with pytest.raises(ValueError, match="new_output"):
        BuildToolCommands(options)


def _sample_scenario(profile):
    selected = profile["profile"]
    if selected == "v1":
        return {"profiles": [], "not_applicable_profiles": ["v1"], "run": None}
    return {
        "profiles": [selected],
        "not_applicable_profiles": [],
        "cache_verified": True,
        "source_cache_unchanged": True,
        "target_cache_cleared": True,
        "copied_bytes": 4096,
        "model_files_copied": 0,
        "copy_verification_seconds": 0.25,
        "preparation": {
            "method": "controlled_failure_before_fit", "fault_injected": True, "fitter_restored": True,
            "fit_entry_counts": {selected: 1}, "unexpected_workspaces": 0, "model_files_created": 0,
            "exit_code": 1, "seconds": 1.0,
            "execution": {
                "status": "failed", "profiles": [selected], "history_scan_passes": 1, "history_rows_read": 100,
            },
            "profiles": [{
                "profile": selected, "training_input_hash": profile["training_input_hash"], "reused": False,
                "status": "blocked", "heads": [
                    {"strategy": strategy, "status": "blocked", "model_hash": "", "report_hash": "",
                     "failure_reasons": ["profile_training_failed"]}
                    for strategy in ("tomorrow", "d25")
                ],
            }],
        },
        "run": {
            "complete": True,
            "seconds": 2.0,
            "profiles": [dict(deepcopy(profile), reused=False)],
            "execution": {
                "status": "completed", "profiles": [selected], "history_scan_passes": 0, "history_rows_read": 0,
            },
            "numeric_fingerprints": [
                {"profile": selected, "strategy": strategy, "model_values_and_metrics_sha256": "b" * 64}
                for strategy in ("tomorrow", "d25")
            ],
        },
    }


def _serial_results():
    profile = {
        "profile": "v5",
        "status": "engineering_ready",
        "training_input_hash": "a" * 64,
        "heads": [
            {"strategy": strategy, "status": "engineering_ready", "model_hash": "b" * 64, "report_hash": "c" * 64} for strategy in ("tomorrow", "d25")
        ],
    }
    return tuple(
        DiagnosticResult(
            "training_alignment:" + mode + ":v5",
            0,
            duration,
            {
                "status": "passed",
                "summary": {
                    "profiles": [profile],
                    "initial": {"complete": True, "profiles": [profile], "seconds": duration / 1000,
                                "numeric_fingerprints": _sample_scenario(profile)["run"]["numeric_fingerprints"]},
                    "repeat": {"complete": True, "profiles": [dict(deepcopy(profile), reused=True)], "seconds": duration / 10000,
                               "execution": {"status": "completed", "profiles": ["v5"], "history_scan_passes": 0, "history_rows_read": 0}},
                    "process_peak_rss_bytes": 100,
                    "sample_reuse": _sample_scenario(profile),
                },
            },
            None,
        )
        for mode, duration in (("serial_before", 10000), ("parallel", 5000), ("serial_after", 9000))
    )


def test_benchmark_requires_all_rounds_and_matching_model_identity():
    results = _serial_results()
    assert SummarizeTrainingBenchmark(results)["speedup_vs_best_serial"] == 1.8
    assert not SummarizeTrainingBenchmark(results[:2])["comparable"]
    blocked = replace(results[1], payload={"status": "degraded", "summary": {"profiles": []}})
    assert SummarizeTrainingBenchmark((results[0], blocked, results[2]))["speedup_vs_best_serial"] is None
    summary = results[1].payload["summary"]
    initial = summary["initial"]
    changed = replace(
        results[1],
        payload={
            "status": "passed",
            "summary": dict(
                summary,
                initial=dict(initial, profiles=[dict(initial["profiles"][0], training_input_hash="different")]),
            ),
        },
    )
    mismatch = (results[0], changed, results[2])
    assert not SummarizeTrainingBenchmark(mismatch)["comparable"]
    assert BuildReport("training-performance", mismatch)["status"] == "degraded"


def test_serial_comparison_times_initial_and_repeat_separately():
    results = _serial_results()
    original = SummarizeTrainingBenchmark(results)
    changed = []
    for result, repeat_seconds in zip(results, (1000.0, 2000.0, 3000.0), strict=True):
        summary = result.payload["summary"]
        changed.append(
            replace(
                result,
                duration_ms=999999.0,
                payload={
                    "status": "passed",
                    "summary": dict(summary, repeat=dict(summary["repeat"], seconds=repeat_seconds)),
                },
            )
        )
    comparison = SummarizeTrainingBenchmark(changed)
    assert original["speedup_vs_best_serial"] == comparison["speedup_vs_best_serial"] == 1.8
    assert [item["seconds"] for item in comparison["rounds"]] == [10.0, 5.0, 9.0]
    assert [item["repeat_seconds"] for item in comparison["rounds"]] == [1000.0, 2000.0, 3000.0]


@pytest.mark.parametrize("missing", ("initial", "repeat"))
def test_serial_comparison_rejects_missing_run_instead_of_using_total_duration(missing):
    results = list(_serial_results())
    summary = dict(results[1].payload["summary"])
    del summary[missing]
    results[1] = replace(results[1], payload={"status": "passed", "summary": summary})
    comparison = SummarizeTrainingBenchmark(results)
    assert not comparison["comparable"] and comparison["speedup_vs_best_serial"] is None


def test_checkout_comparison_uses_same_harness_config_and_distinct_outputs(tmp_path):
    baseline = tmp_path / "baseline"
    (baseline / "src/trader").mkdir(parents=True)
    options = ParseBusinessOptions(
        ParseToolArguments(
            [
                *SelectionArguments('training-performance'),
                "--history-root",
                str(tmp_path / "history"),
                "--training-probe-directory",
                str(tmp_path / "work"),
                "--training-profiles",
                "v1,v5",
            ]
        )
    )
    commands = BuildToolCommands(replace(options, baseline_checkout=baseline))
    assert [command.name for command in commands] == [
        "training_alignment:baseline_before:v1,v5",
        "training_alignment:candidate:v1,v5",
        "training_alignment:baseline_after:v1,v5",
    ]
    selected = [command.argv[command.argv.index("--product-checkout") + 1] for command in commands]
    assert selected[0] == selected[-1] == str(baseline)
    assert selected[1] != str(baseline)
    assert len({command.argv[command.argv.index("--work-root") + 1] for command in commands}) == 3
    assert len({command.argv[command.argv.index("--runtime-config") + 1] for command in commands}) == 1
    with pytest.raises(ValueError, match="external_product_checkout"):
        BuildToolCommands(replace(options, baseline_checkout=tmp_path))


def _checkout_results():
    results = []
    for mode, initial_seconds, repeat_seconds in (
        ("baseline_before", 10.0, 10.0),
        ("candidate", 5.0, 0.1),
        ("baseline_after", 9.0, 9.0),
    ):
        profile = {
            "profile": "v1",
            "status": "engineering_ready",
            "training_input_hash": "a" * 64,
            "heads": [
                {"strategy": strategy, "status": "engineering_ready", "model_hash": mode, "report_hash": mode} for strategy in ("tomorrow", "d25")
            ],
        }
        numeric = [
            {"profile": "v1", "strategy": strategy, "model_values_and_metrics_sha256": "b" * 64}
            for strategy in ("tomorrow", "d25")
        ]
        initial = {"complete": True, "profiles": [profile], "seconds": initial_seconds, "numeric_fingerprints": numeric}
        repeat = dict(initial, seconds=repeat_seconds, profiles=[dict(deepcopy(profile), reused=mode == "candidate")],
                      execution={"status": "completed", "profiles": ["v1"], "history_scan_passes": int(mode != "candidate"),
                                 "history_rows_read": 0 if mode == "candidate" else 10})
        results.append(
            DiagnosticResult(
                "training_alignment:" + mode + ":v1",
                0,
                100000.0,
                {
                    "status": "passed",
                    "summary": {
                        "initial": initial,
                        "repeat": repeat,
                        "source_identity_unchanged": True,
                        "training_configuration_sha256": "d" * 64,
                        "sample_reuse": _sample_scenario(profile),
                    },
                },
                None,
            )
        )
    return tuple(results)


def test_checkout_comparison_uses_numeric_values_instead_of_source_sensitive_hashes():
    comparison = SummarizeTrainingBenchmark(_checkout_results())
    assert comparison["comparable"]
    assert comparison["candidate_ratio_vs_best_baseline"] == {
        "initial_seconds": 5.0 / 9.0, "sample_reuse_seconds": None, "repeat_seconds": 0.1 / 9.0,
    }
    assert BuildReport("training-performance", _checkout_results())["status"] == "passed"
    assert comparison["rounds"][0]["reuse_failures"]
    assert comparison["rounds"][1]["reuse_failures"] == ()


@pytest.mark.parametrize(
    "change",
    (
        "missing_round",
        "different_input",
        "different_values",
        "repeat_values",
        "invalid_fingerprint",
        "nonfinite_time",
        "source_changed",
        "configuration_changed",
    ),
)
def test_checkout_comparison_rejects_incomplete_or_different_results(change):
    results = list(_checkout_results())
    if change == "missing_round":
        results.pop()
    else:
        summary = results[1].payload["summary"]
        if change == "different_input":
            summary["initial"]["profiles"][0]["training_input_hash"] = "different"
        elif change == "different_values":
            summary["initial"]["numeric_fingerprints"] = [
                dict(item, model_values_and_metrics_sha256="c" * 64)
                for item in summary["initial"]["numeric_fingerprints"]
            ]
            summary["repeat"]["numeric_fingerprints"] = summary["initial"]["numeric_fingerprints"]
        elif change == "repeat_values":
            summary["repeat"]["numeric_fingerprints"] = []
        elif change == "invalid_fingerprint":
            summary["initial"]["numeric_fingerprints"] = [None, None]
            summary["repeat"]["numeric_fingerprints"] = [None, None]
        elif change == "nonfinite_time":
            summary["initial"]["seconds"] = float("inf")
        elif change == "configuration_changed":
            summary["training_configuration_sha256"] = "e" * 64
        else:
            summary["source_identity_unchanged"] = False
    summary = SummarizeTrainingBenchmark(results)
    assert not summary["comparable"]
    assert all(value is None for value in summary["candidate_ratio_vs_best_baseline"].values())


def test_model_numeric_fingerprints_include_components_and_metrics_but_not_identity(tmp_path):
    from tests.unit.scripts.test_training_model_fingerprints import _fit_models

    rank_model = _fit_models("v1").two_threads
    paths = []
    for strategy in ("tomorrow", "d25"):
        root = tmp_path / "v1" / strategy
        root.mkdir(parents=True)
        model = {"components": {"ridge_coefficients": [0.1, 0.2], "rank_model": rank_model}, "content_hash": "old-source"}
        metadata = {"validation_report": {"validation": {"mean_absolute_error": 0.01}}, "content_hash": "old-source"}
        (root / "model.json").write_text(json.dumps(model))
        (root / "metadata.json").write_text(json.dumps(metadata))
        paths.append((root, model, metadata))
    first = _numeric_fingerprints(tmp_path, ("v1",))
    for root, model, metadata in paths:
        model["content_hash"] = metadata["content_hash"] = "new-source"
        (root / "model.json").write_text(json.dumps(model))
        (root / "metadata.json").write_text(json.dumps(metadata))
    assert _numeric_fingerprints(tmp_path, ("v1",)) == first
    root, model, metadata = paths[0]
    model["components"]["ridge_coefficients"][0] = 0.3
    (root / "model.json").write_text(json.dumps(model))
    assert _numeric_fingerprints(tmp_path, ("v1",)) != first
    model["components"]["ridge_coefficients"][0] = 0.1
    metadata["validation_report"]["validation"]["mean_absolute_error"] = 0.02
    (root / "model.json").write_text(json.dumps(model))
    (root / "metadata.json").write_text(json.dumps(metadata))
    assert _numeric_fingerprints(tmp_path, ("v1",)) != first


@pytest.mark.parametrize("initial_complete", (True, False))
@pytest.mark.parametrize("reused,require_reuse", ((True, True), (False, True), (False, False)))
def test_alignment_repeats_complete_training_in_the_same_workspace(tmp_path, monkeypatch, initial_complete, reused, require_reuse):
    from trader.infra import settings
    from trader.training.entrypoints import commands
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot

    source = SimpleNamespace(snapshot=SimpleNamespace(snapshot_hash="a" * 64, training_codes=("600001",)))
    monkeypatch.setattr(SQLiteHistoryTrainingInputSnapshot, "open", lambda _path, *, sqlite_policy: source)
    monkeypatch.setattr(
        settings, "LoadRuntimeSettings", lambda _path: SimpleNamespace(
            project_root=Path(__file__).resolve().parents[3],
            data_paths=SimpleNamespace(root=tmp_path / "configured-data", history=tmp_path / "configured-data/history"),
        )
    )
    monkeypatch.setattr(commands, "ConfigureTrainingResources", lambda *_args: None)
    calls = []

    def train_once(runtime, history, work, profiles):
        calls.append((history, work, profiles))
        is_repeat = len(calls) == 2
        profile = deepcopy(_checkout_results()[1].payload["summary"]["initial"]["profiles"][0])
        profile["reused"] = is_repeat and reused
        return {
            "complete": initial_complete,
            "profiles": [profile],
            "execution": {"status": "completed", "profiles": ["v1"], "history_scan_passes": int(not is_repeat),
                          "history_rows_read": 0 if is_repeat else 10},
            "reloaded_heads": 2 if initial_complete else 0,
            "seconds": 1.0,
            "numeric_fingerprints": ("unchanged",),
        }

    monkeypatch.setattr(training_alignment, "_train_once", train_once)
    report = training_alignment._probe(tmp_path / "history", tmp_path / "work", ("v1",), sqlite_policy=SqlitePolicyFixture(), require_reuse=require_reuse)
    assert len(calls) == (2 if initial_complete else 1)
    assert all(call == (tmp_path / "history", tmp_path / "work", ("v1",)) for call in calls)
    assert report["status"] == ("passed" if initial_complete and (reused or not require_reuse) else "degraded")
    assert report["summary"]["repeat_reuse_verified"] is (initial_complete and reused)
    if initial_complete:
        assert report["summary"]["repeat"]["profiles"][0]["reused"] is reused
        assert report["summary"]["repeat"]["execution"]["history_scan_passes"] == 0
    else:
        assert report["summary"]["repeat"] is None


@pytest.mark.parametrize("checkout", (False, True))
@pytest.mark.parametrize("change", (
    "missing_execution", "failed_execution", "scan", "rows", "boolean_zero", "missing_counter",
    "wrong_execution_profiles", "malformed_execution_profiles", "not_reused", "boolean_reused",
    "missing_profile", "duplicate_head", "changed_model", "changed_input", "failed_profile", "failed_head",
))
def test_comparison_rejects_false_reuse_even_when_child_claims_passed(checkout, change):
    results = list(deepcopy(_checkout_results() if checkout else _serial_results()))
    repeat = results[1].payload["summary"]["repeat"]
    execution = repeat["execution"]
    profile = repeat["profiles"][0]
    if change == "missing_execution":
        del repeat["execution"]
    elif change == "failed_execution":
        execution["status"] = "failed"
    elif change == "scan":
        execution["history_scan_passes"] = 1
    elif change == "rows":
        execution["history_rows_read"] = 1
    elif change == "boolean_zero":
        execution["history_scan_passes"] = False
    elif change == "missing_counter":
        del execution["history_rows_read"]
    elif change == "wrong_execution_profiles":
        execution["profiles"] = ["v2"]
    elif change == "malformed_execution_profiles":
        execution["profiles"] = [None, "v1"]
    elif change == "not_reused":
        profile["reused"] = False
    elif change == "boolean_reused":
        profile["reused"] = 1
    elif change == "missing_profile":
        repeat["profiles"] = []
    elif change == "duplicate_head":
        profile["heads"][1] = dict(profile["heads"][0])
    elif change == "changed_model":
        profile["heads"][0]["model_hash"] = "changed"
    elif change == "changed_input":
        profile["training_input_hash"] = "changed"
    elif change == "failed_head":
        profile["heads"][0]["status"] = "failed"
    else:
        profile["status"] = "failed"
    report = BuildReport("training-performance", results)
    assert report["status"] == "degraded"
    assert not report["benchmark"]["comparable"]
    assert report["benchmark"]["rounds"][1]["reuse_failures"]


@pytest.mark.parametrize("profile", ("v1", "v2", "v3", "v5"))
def test_four_profile_reuse_requires_every_selected_member(profile):
    profiles = ("v1", "v2", "v3", "v5")
    run = deepcopy(_serial_results()[1].payload["summary"]["repeat"])
    run["profiles"] = [dict(deepcopy(run["profiles"][0]), profile=selected) for selected in profiles]
    run["execution"]["profiles"] = list(profiles)
    initial = deepcopy(run)
    assert TrainingReuseFailures(initial, run, profiles) == ()
    next(item for item in run["profiles"] if item["profile"] == profile)["reused"] = False
    assert TrainingReuseFailures(initial, run, profiles) == ("reuse_profiles_not_reused",)


@pytest.mark.parametrize("change", (
    "missing", "history_scan", "history_rows", "boolean_zero", "model_reused", "different_input",
    "different_weights", "cache_changed", "cache_unverified", "models_copied", "cache_missing", "wrong_profiles",
    "cache_not_cleared", "preparation_missing", "preparation_not_restored", "preparation_unreached",
    "preparation_models", "preparation_success", "preparation_other_failure", "preparation_no_scan",
))
def test_sample_refit_rejects_claimed_success_without_its_workload_evidence(change):
    results = list(deepcopy(_serial_results()))
    summary = results[1].payload["summary"]
    scenario = summary["sample_reuse"]
    run = scenario["run"]
    if change == "missing":
        del summary["sample_reuse"]
    elif change == "history_scan":
        run["execution"]["history_scan_passes"] = 1
    elif change == "history_rows":
        run["execution"]["history_rows_read"] = 1
    elif change == "boolean_zero":
        run["execution"]["history_rows_read"] = False
    elif change == "model_reused":
        run["profiles"][0]["reused"] = True
    elif change == "different_input":
        run["profiles"][0]["training_input_hash"] = "different"
    elif change == "different_weights":
        run["numeric_fingerprints"][0]["model_values_and_metrics_sha256"] = "c" * 64
    elif change == "cache_changed":
        scenario["source_cache_unchanged"] = False
    elif change == "cache_not_cleared":
        scenario["target_cache_cleared"] = False
    elif change == "cache_unverified":
        scenario["cache_verified"] = False
    elif change == "models_copied":
        scenario["model_files_copied"] = 4
    elif change == "cache_missing":
        scenario["copied_bytes"] = 0
    elif change == "preparation_missing":
        del scenario["preparation"]
    elif change == "preparation_not_restored":
        scenario["preparation"]["fitter_restored"] = False
    elif change == "preparation_unreached":
        scenario["preparation"]["fit_entry_counts"]["v5"] = 0
    elif change == "preparation_models":
        scenario["preparation"]["model_files_created"] = 1
    elif change == "preparation_success":
        scenario["preparation"]["exit_code"] = 0
    elif change == "preparation_other_failure":
        scenario["preparation"]["profiles"][0]["heads"][0]["failure_reasons"] = ["history_training_data_incomplete"]
    elif change == "preparation_no_scan":
        scenario["preparation"]["execution"]["history_scan_passes"] = 0
    else:
        scenario["profiles"] = ["v1"]
    comparison = SummarizeTrainingBenchmark(results)
    assert not comparison["comparable"]
    assert comparison["rounds"][1]["sample_reuse_failures"]
    assert BuildReport("training-performance", results)["status"] == "degraded"


@pytest.mark.parametrize("invalid", (None, "digest", "size", "modified", "symlink"))
def test_sample_refit_uses_verified_copy_without_deleting_or_copying_original_models(tmp_path, monkeypatch, invalid):
    work = tmp_path / "work"
    cache = work / "sample-preparation/samples/v5"
    cache.mkdir(parents=True)
    database = cache / "samples.sqlite3"
    database.write_bytes(b"sealed sample fixture")
    stamp = database.stat()
    manifest = {
        "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
        "size_bytes": stamp.st_size,
        "modified_ns": stamp.st_mtime_ns,
    }
    if invalid == "digest":
        manifest["sha256"] = "0" * 64
    elif invalid == "size":
        manifest["size_bytes"] += 1
    elif invalid == "modified":
        manifest["modified_ns"] += 1
    (cache / "complete.json").write_text(json.dumps(manifest))
    if invalid == "symlink":
        database.rename(cache / "original.sqlite3")
        database.symlink_to(cache / "original.sqlite3")
    model = work / "models/v5/model.json"
    model.parent.mkdir(parents=True)
    model.write_text("original model")
    original = (model.read_bytes(), model.stat().st_mtime_ns)
    initial = deepcopy(_serial_results()[1].payload["summary"]["initial"])
    calls = []

    def train(runtime, history, target, profiles):
        calls.append(target)
        assert profiles == ("v5",)
        assert target == work / "sample-reuse"
        assert not (target / "models").exists()
        assert (target / "samples/v5/samples.sqlite3").read_bytes() == database.read_bytes()
        shutil.rmtree(target / "samples/v5")
        return _sample_scenario(initial["profiles"][0])["run"]

    monkeypatch.setattr(training_alignment, "_prepare_sample_reuse", lambda *_args:
                        _sample_scenario(initial["profiles"][0])["preparation"])
    monkeypatch.setattr(training_alignment, "_train_once", train)
    result = training_alignment._sample_reuse(None, tmp_path / "history", work, ("v1", "v5"), initial)
    assert (model.read_bytes(), model.stat().st_mtime_ns) == original
    assert result["not_applicable_profiles"] == ("v1",)
    if invalid is None:
        assert len(calls) == 1
        assert result["failures"] == ()
        assert result["cache_verified"] and result["source_cache_unchanged"] and result["target_cache_cleared"]
        assert result["model_files_copied"] == 0
        assert 0 < result["copy_verification_seconds"] <= result["preparation_seconds"]
    else:
        assert calls == []
        assert result["failures"]


def test_v1_sample_reuse_is_explicitly_not_applicable_and_never_runs_a_fake_cache(tmp_path, monkeypatch):
    def forbidden(*_args):
        pytest.fail("V1 has no sealed sample reuse workload")

    monkeypatch.setattr(training_alignment, "_train_once", forbidden)
    result = training_alignment._sample_reuse(None, tmp_path / "history", tmp_path / "work", ("v1",), {})
    assert result["profiles"] == () and result["not_applicable_profiles"] == ("v1",)
    assert TrainingSampleReuseFailures({}, result, ("v1",)) == ()
    assert not (tmp_path / "work").exists()


@pytest.mark.parametrize("transfer", ("copy", "hardlink"))
def test_sample_preparation_uses_real_engine_retention_then_normal_refit_clears_cache(tmp_path, monkeypatch, transfer):
    from tests.unit.training.infra.profile.v5.test_v5_training import _history
    from trader.infra.settings import LoadRuntimeSettings
    from trader.training.infra import engine, selected_training
    from trader.training.infra.training_settings import LoadProfileTrainingSettings

    project = Path(__file__).resolve().parents[3]
    history = _history(tmp_path / "history")
    monkeypatch.setattr(selected_training.SQLiteHistoryTrainingInputSnapshot, "open", lambda *_a, **_kw: history)
    settings = LoadProfileTrainingSettings(project, "v3")
    settings = replace(settings, fitting=replace(settings.fitting, minimum_industry_training_rows=10))
    monkeypatch.setattr(selected_training, "LoadProfileTrainingSettings", lambda *_args: settings)
    monkeypatch.setattr("trader.training.entrypoints.selected_training.EvaluateAllHistoryTrainingDue", lambda *_a, **_kw: ())
    runtime = LoadRuntimeSettings(project / "config/runtime.json")
    work = tmp_path / "work"
    fitter = engine.FitIndustryModels
    initial = training_alignment._train_once(runtime, history.history_root, work, ("v3",))
    assert initial["complete"]
    assert not (work / "samples/v3").exists()
    result = training_alignment._sample_reuse(runtime, history.history_root, work, ("v3",), initial, transfer=transfer)
    assert result["failures"] == (), result
    assert result["preparation"]["exit_code"] == 1
    assert result["preparation"]["fit_entry_counts"] == {"v3": 1}
    assert result["preparation"]["model_files_created"] == 0
    assert engine.FitIndustryModels is fitter
    assert (work / "sample-preparation/samples/v3/complete.json").is_file() is (transfer == "copy")
    assert result["run"]["execution"]["history_scan_passes"] == 0
    assert result["source_cache_unchanged"] and result["target_cache_cleared"]
    assert not (work / "sample-reuse/samples/v3").exists()
    assert result["cache_transfer_method"] == transfer
    assert result["linked_bytes" if transfer == "hardlink" else "copied_bytes"] > 0
    assert result["copied_bytes" if transfer == "hardlink" else "linked_bytes"] == 0
    assert result["preparation_cache_released_after_verification"] is (transfer == "hardlink")
    assert result["source_cache_signatures_before"] == result["source_cache_signatures_after"]


def test_prepared_cache_release_rejects_unknown_objects_before_removing_anything(tmp_path):
    for profile in ("v2", "v3"):
        root = tmp_path / "samples" / profile
        root.mkdir(parents=True)
        (root / "samples.sqlite3").write_bytes(b"owned fixture")
        (root / "complete.json").write_text("{}")
    (tmp_path / "samples/v3/unowned").write_text("preserve")
    with pytest.raises(ValueError, match="cleanup_unowned"):
        training_alignment._release_prepared_samples(tmp_path, ("v2", "v3"))
    assert (tmp_path / "samples/v2/samples.sqlite3").read_bytes() == b"owned fixture"
    assert (tmp_path / "samples/v3/unowned").read_text() == "preserve"


@pytest.mark.parametrize("transfer", ("copy", "hardlink"))
@pytest.mark.parametrize("opposite_bytes", (0, 1, False, "0"))
def test_cache_transfer_admission_requires_a_single_honest_byte_counter(transfer, opposite_bytes):
    initial = deepcopy(_serial_results()[1].payload["summary"]["initial"])
    scenario = _sample_scenario(initial["profiles"][0])
    scenario["cache_transfer_method"] = transfer
    scenario["copied_bytes"] = opposite_bytes if transfer == "hardlink" else 4096
    scenario["linked_bytes"] = opposite_bytes if transfer == "copy" else 4096
    failures = TrainingSampleReuseFailures(initial, scenario, ("v5",))
    assert (not failures) is (type(opposite_bytes) is int and opposite_bytes == 0)


@pytest.mark.parametrize("transfer", ([], {}, True, None, "unknown"))
def test_cache_transfer_admission_rejects_unknown_json_values_without_crashing(transfer):
    initial = deepcopy(_serial_results()[1].payload["summary"]["initial"])
    scenario = _sample_scenario(initial["profiles"][0])
    scenario["cache_transfer_method"] = transfer
    assert "sample_reuse_cache_copy_missing" in TrainingSampleReuseFailures(initial, scenario, ("v5",))


def test_sample_preparation_restores_fitter_when_execution_raises(tmp_path, monkeypatch):
    from trader.training.infra import engine

    fitter = engine.FitIndustryModels

    def interrupt(*_args, **_kwargs):
        assert engine.FitIndustryModels is not fitter
        raise InterruptedError("fixture")

    monkeypatch.setattr("trader.bootstrap.ExecuteSelectedTraining", interrupt)
    with pytest.raises(InterruptedError, match="fixture"):
        training_alignment._prepare_sample_reuse(None, tmp_path / "history", tmp_path / "prepare", ("v3",))
    assert engine.FitIndustryModels is fitter
