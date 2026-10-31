"""Benchmark isolation, direct supervision, and honest comparison admission."""

from tests.helpers.configuration import SqlitePolicyFixture

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.diagnose_runtime import _parser, _validate, build_commands, build_report
from scripts.runtime_diagnostics import training_alignment
from scripts.runtime_diagnostics.diagnostic_types import DiagnosticResult
from scripts.runtime_diagnostics.training_alignment import _numeric_fingerprints
from scripts.runtime_diagnostics.training_performance import SummarizeTrainingBenchmark


def test_benchmark_plans_direct_children_with_distinct_external_outputs(tmp_path):
    args = _parser().parse_args(
        [
            "--profile",
            "training-performance",
            "--history-root",
            str(tmp_path / "history"),
            "--training-probe-directory",
            str(tmp_path / "work"),
            "--training-profiles",
            "v1,v5",
        ]
    )
    options, _ = _validate(args)
    commands = build_commands(options)
    assert len(commands) == 5
    assert [command.name for command in commands] == [
        "training_alignment:serial_before:v1",
        "training_alignment:serial_before:v5",
        "training_alignment:parallel:v1,v5",
        "training_alignment:serial_after:v1",
        "training_alignment:serial_after:v5",
    ]
    assert all("scripts.runtime_diagnostics.training_alignment" in command.argv for command in commands)
    assert len({command.argv[command.argv.index("--work-root") + 1] for command in commands}) == 5
    (tmp_path / "work").mkdir()
    with pytest.raises(ValueError, match="new_output"):
        build_commands(options)


def _serial_results():
    profile = {
        "profile": "v5",
        "training_input_hash": "a" * 64,
        "heads": [
            {"strategy": strategy, "model_hash": "b" * 64, "report_hash": "c" * 64} for strategy in ("tomorrow", "d25")
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
                    "initial": {"complete": True, "profiles": [profile], "seconds": duration / 1000},
                    "repeat": {"complete": True, "profiles": [profile], "seconds": duration / 10000},
                    "process_peak_rss_bytes": 100,
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
    assert build_report("training-performance", mismatch)["status"] == "degraded"


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
    options, _ = _validate(
        _parser().parse_args(
            [
                "--profile",
                "training-performance",
                "--history-root",
                str(tmp_path / "history"),
                "--training-probe-directory",
                str(tmp_path / "work"),
                "--training-profiles",
                "v1,v5",
            ]
        )
    )
    commands = build_commands(replace(options, baseline_checkout=baseline))
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
        build_commands(replace(options, baseline_checkout=tmp_path))


def _checkout_results():
    results = []
    for mode, initial_seconds, repeat_seconds in (
        ("baseline_before", 10.0, 10.0),
        ("candidate", 5.0, 0.1),
        ("baseline_after", 9.0, 9.0),
    ):
        profile = {
            "profile": "v1",
            "training_input_hash": "a" * 64,
            "heads": [
                {"strategy": strategy, "model_hash": mode, "report_hash": mode} for strategy in ("tomorrow", "d25")
            ],
        }
        numeric = [
            {"profile": "v1", "strategy": strategy, "model_values_and_metrics_sha256": "b" * 64}
            for strategy in ("tomorrow", "d25")
        ]
        initial = {"complete": True, "profiles": [profile], "seconds": initial_seconds, "numeric_fingerprints": numeric}
        repeat = dict(initial, seconds=repeat_seconds, profiles=[dict(profile, reused=mode == "candidate")])
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
                    },
                },
                None,
            )
        )
    return tuple(results)


def test_checkout_comparison_uses_numeric_values_instead_of_source_sensitive_hashes():
    comparison = SummarizeTrainingBenchmark(_checkout_results())
    assert comparison["comparable"]
    assert comparison["candidate_ratio_vs_best_baseline"] == {"initial_seconds": 5.0 / 9.0, "repeat_seconds": 0.1 / 9.0}
    assert build_report("training-performance", _checkout_results())["status"] == "passed"


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
    paths = []
    for strategy in ("tomorrow", "d25"):
        root = tmp_path / "v1" / strategy
        root.mkdir(parents=True)
        model = {"components": {"ridge_coefficients": [0.1, 0.2]}, "content_hash": "old-source"}
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
def test_alignment_repeats_complete_training_in_the_same_workspace(tmp_path, monkeypatch, initial_complete):
    from trader.infra import settings
    from trader.training.entrypoints import commands
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot

    source = SimpleNamespace(snapshot=SimpleNamespace(snapshot_hash="a" * 64, training_codes=("600001",)))
    monkeypatch.setattr(SQLiteHistoryTrainingInputSnapshot, "open", lambda _path, *, sqlite_policy: source)
    monkeypatch.setattr(
        settings, "LoadRuntimeSettings", lambda _path: SimpleNamespace(project_root=Path(__file__).resolve().parents[3])
    )
    monkeypatch.setattr(commands, "ConfigureTrainingResources", lambda: None)
    calls = []

    def train_once(runtime, history, work, profiles):
        calls.append((history, work, profiles))
        return {
            "complete": initial_complete,
            "profiles": [{"profile": "v1", "reused": len(calls) == 2}],
            "execution": {"history_scan_passes": int(len(calls) == 1)},
            "reloaded_heads": 2 if initial_complete else 0,
            "seconds": 1.0,
            "numeric_fingerprints": ("unchanged",),
        }

    monkeypatch.setattr(training_alignment, "_train_once", train_once)
    report = training_alignment._probe(tmp_path / "history", tmp_path / "work", ("v1",), sqlite_policy=SqlitePolicyFixture())
    assert len(calls) == (2 if initial_complete else 1)
    assert all(call == (tmp_path / "history", tmp_path / "work", ("v1",)) for call in calls)
    assert report["status"] == ("passed" if initial_complete else "degraded")
    if initial_complete:
        assert report["summary"]["repeat"]["profiles"] == [{"profile": "v1", "reused": True}]
        assert report["summary"]["repeat"]["execution"] == {"history_scan_passes": 0}
    else:
        assert report["summary"]["repeat"] is None
