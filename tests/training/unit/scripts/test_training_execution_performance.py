from __future__ import annotations



from collections.abc import Sequence

from copy import deepcopy

from dataclasses import replace

from pathlib import Path

import pytest


from scripts.training.jobs.training_performance import BuildTrainingBenchmarkCommands, SummarizeTrainingBenchmark


from tests.helpers.training.scripts.training_execution_performance import (
    _MODES,
    _digest,
    _mapping,
    _options,
    _records,
    _results,
    _summary,
)


def test_five_rounds_plan_fifteen_fresh_children_with_candidate_flags_only_on_candidates(tmp_path: Path) -> None:
    options = _options(tmp_path)
    commands = BuildTrainingBenchmarkCommands(options, "/test/venv/bin/python3")
    assert len(commands) == 15
    assert [command.name for command in commands] == [
        f"training_alignment:round_{number}:{mode}:v1,v2,v3,v5"
        for number in range(1, 6)
        for mode in _MODES
    ]
    assert len({command.argv[command.argv.index("--work-root") + 1] for command in commands}) == 15
    assert len({command.argv[command.argv.index("--history-root") + 1] for command in commands}) == 1
    assert len({command.argv[command.argv.index("--runtime-config") + 1] for command in commands}) == 1
    for command in commands:
        assert command.argv[0] == "/test/venv/bin/python3"
        assert "--internal-worker" in command.argv
        assert "scripts.training.jobs.training_alignment" in command.argv
        candidate = ":candidate:" in command.name
        assert ("--execution-candidate" in command.argv) is candidate
        assert ("--cpu-candidate" in command.argv) is candidate
        if candidate:
            assert command.argv[command.argv.index("--execution-candidate") + 1] == "combined"
            assert command.argv[command.argv.index("--cpu-candidate") + 1] == "two_plus_one"
        else:
            assert command.argv[command.argv.index("--product-checkout") + 1] == str(options.baseline_checkout)
    assert options.training_probe_directory is not None and not options.training_probe_directory.exists()


def test_all_comparison_workers_share_explicit_source_guard_and_cache_transfer(tmp_path: Path) -> None:
    options = _options(tmp_path, published_history_read_only=True, sample_cache_transfer="hardlink")
    commands = BuildTrainingBenchmarkCommands(options, "/test/venv/bin/python3")
    assert len(commands) == 15
    for command in commands:
        assert "--published-history-read-only" in command.argv
        assert command.argv[command.argv.index("--sample-cache-transfer") + 1] == "hardlink"


@pytest.mark.parametrize("guarded", (False, True))
def test_published_history_under_tool_checkout_requires_explicit_read_only_mode(tmp_path: Path, monkeypatch, guarded: bool) -> None:
    from scripts.training.jobs import training_performance

    tool = tmp_path / "tool"
    options = _options(tmp_path, history_root=tool / "data/history", published_history_read_only=guarded)
    monkeypatch.setattr(training_performance, "TOOL_ROOT", tool)
    if guarded:
        commands = BuildTrainingBenchmarkCommands(options, "/test/venv/bin/python3")
        assert all("--published-history-read-only" in command.argv for command in commands)
    else:
        with pytest.raises(ValueError, match="external_history_copy"):
            BuildTrainingBenchmarkCommands(options, "/test/venv/bin/python3")


@pytest.mark.parametrize("rounds", (1, 2, 3, 4, 5))
def test_round_count_controls_checkout_process_count(tmp_path: Path, rounds: int) -> None:
    commands = BuildTrainingBenchmarkCommands(_options(tmp_path, training_benchmark_rounds=rounds), "python3")
    assert len(commands) == 3 * rounds


@pytest.mark.parametrize(
    "execution,cpu,rounds", (("resources", "baseline", 1), ("baseline", "three_single", 1), ("baseline", "baseline", 2)),
)
def test_explicit_candidates_and_repeated_rounds_require_baseline_checkout(
    tmp_path: Path, execution: str, cpu: str, rounds: int,
) -> None:
    options = _options(
        tmp_path, baseline_checkout=None, execution_candidate=execution, cpu_candidate=cpu,
        training_benchmark_rounds=rounds,
    )
    with pytest.raises(ValueError, match="training_execution_comparison_requires_baseline_checkout"):
        BuildTrainingBenchmarkCommands(options, "python3")


@pytest.mark.parametrize("rounds", (0, 6, True))
def test_invalid_round_counts_are_rejected(tmp_path: Path, rounds: int) -> None:
    with pytest.raises(ValueError, match="training_benchmark_rounds_invalid"):
        BuildTrainingBenchmarkCommands(_options(tmp_path, training_benchmark_rounds=rounds), "python3")


def test_four_wins_and_exact_timing_thresholds_never_grant_performance_or_production_authority() -> None:
    comparison = SummarizeTrainingBenchmark(_results())
    assert comparison["comparable"] is True
    assert comparison["required_rounds"] == comparison["observed_rounds"] == 5
    assert comparison["initial_wins"] == 4
    assert comparison["candidate_median_ratio_vs_best_baseline"] == {
        "initial_seconds": 0.9, "sample_reuse_seconds": 1.05, "repeat_seconds": 1.05,
    }
    assert comparison["timing_acceptance"] is True
    assert comparison["performance_acceptance"] is False
    assert comparison["production_authority"] is False
    assert comparison["concurrency_enablement_gate"] is False
    assert "uncontended_window_and_resource_peaks_require_review" in str(comparison["acceptance_scope"])


@pytest.mark.parametrize(
    "initial,sample,repeat,wins",
    (
        ((0.8, 0.8, 0.8, 1.0, 1.0), 1.0, 1.0, 3),
        ((0.90001,) * 5, 1.0, 1.0, 5),
        ((0.8,) * 5, 1.05001, 1.0, 5),
        ((0.8,) * 5, 1.0, 1.05001, 5),
    ),
    ids=("only-three-wins", "less-than-ten-percent", "sample-refit-regression", "model-reuse-regression"),
)
def test_comparable_results_below_any_timing_gate_remain_unaccepted(
    initial: Sequence[float], sample: float, repeat: float, wins: int,
) -> None:
    comparison = SummarizeTrainingBenchmark(_results(initial, sample, repeat))
    assert comparison["comparable"] is True
    assert comparison["initial_wins"] == wins
    assert comparison["timing_acceptance"] is False
    assert comparison["performance_acceptance"] is False


def test_four_complete_groups_cannot_satisfy_five_round_acceptance() -> None:
    comparison = SummarizeTrainingBenchmark(_results((0.8,) * 4))
    assert comparison["comparable"] is True
    assert comparison["observed_rounds"] == 4
    assert comparison["timing_acceptance"] is False


@pytest.mark.parametrize("change", ("input", "configuration", "numeric_values"))
def test_same_group_inputs_cannot_change_between_rounds(change: str) -> None:
    results = _results()
    for result in results[-3:]:
        summary = _summary(result)
        if change == "configuration":
            summary["training_configuration_sha256"] = _digest("new-configuration")
        elif change == "input":
            for load in ("initial", "repeat"):
                _records(_mapping(summary[load])["profiles"])[0]["training_input_hash"] = _digest("new-v1-input")
        else:
            for load in ("initial", "repeat"):
                _records(_mapping(summary[load])["numeric_fingerprints"])[0]["model_values_and_metrics_sha256"] = _digest("changed-v1-values")
    comparison = SummarizeTrainingBenchmark(results)
    assert comparison["comparable"] is False
    assert comparison["timing_acceptance"] is False
    assert all(value is None for value in _mapping(comparison["candidate_median_ratio_vs_best_baseline"]).values())


@pytest.mark.parametrize("change", ("execution", "cpu", "options", "source", "implementation", "computation", "scheduling"))
def test_candidate_implementation_and_options_are_frozen_across_rounds(change: str) -> None:
    results = _results()
    summary = _summary(results[-2])
    candidate = _mapping(summary["execution_candidate"])
    if change == "execution":
        candidate["execution"] = "resources"
    elif change == "cpu":
        candidate["cpu"] = "three_single"
    elif change == "options":
        _mapping(candidate["options"])["shared_validation"] = False
    elif change == "source":
        candidate["candidate_source_hash"] = _digest("changed-candidate-source")
    elif change == "implementation":
        summary["training_implementation_sha256"] = _digest("changed-implementation")
    else:
        _mapping(summary[change + "_candidate"])["name"] = "combined" if change == "computation" else "fixed"
    comparison = SummarizeTrainingBenchmark(results)
    assert comparison["comparable"] is False
    assert comparison["timing_acceptance"] is False


@pytest.mark.parametrize("change", ("source_identity", "source_files", "source_writes", "reuse_scan", "sample_models", "missing_head", "duplicate_head"))
def test_child_pass_status_cannot_replace_source_reuse_or_eight_head_evidence(change: str) -> None:
    results = _results()
    summary = _summary(results[1])
    if change == "source_identity":
        summary["source_identity_unchanged"] = False
    elif change == "source_files":
        summary["source_files_unchanged"] = False
    elif change == "source_writes":
        summary["source_data_writes"] = 1
    elif change == "reuse_scan":
        _mapping(_mapping(summary["repeat"])["execution"])["history_scan_passes"] = 1
    elif change == "sample_models":
        _mapping(summary["sample_reuse"])["model_files_copied"] = 1
    else:
        numeric = _records(_mapping(summary["initial"])["numeric_fingerprints"])
        if change == "missing_head":
            numeric.pop()
        else:
            numeric[-1] = deepcopy(numeric[0])
        _mapping(summary["initial"])["numeric_fingerprints"] = numeric
    comparison = SummarizeTrainingBenchmark(results)
    assert comparison["comparable"] is False
    assert comparison["timing_acceptance"] is False


@pytest.mark.parametrize("load", ("initial", "repeat", "sample_reuse"))
@pytest.mark.parametrize("seconds", (float("inf"), float("nan"), -1.0, 0.0, True))
def test_all_three_workload_durations_must_be_finite_positive_numbers(load: str, seconds: object) -> None:
    results = _results()
    run = _mapping(_summary(results[1])[load])
    if load == "sample_reuse":
        run = _mapping(run["run"])
    run["seconds"] = seconds
    comparison = SummarizeTrainingBenchmark(results)
    assert comparison["comparable"] is False
    assert comparison["timing_acceptance"] is False


@pytest.mark.parametrize("change", ("missing_mode", "duplicate_mode", "failed_child", "incomplete_initial", "incomplete_repeat", "missing_sample"))
def test_incomplete_rounds_are_rejected(change: str) -> None:
    results = list(_results())
    if change == "missing_mode":
        results.pop()
    elif change == "duplicate_mode":
        results.append(deepcopy(results[-1]))
    elif change == "failed_child":
        results[1] = replace(results[1], return_code=1)
    else:
        summary = _summary(results[1])
        if change == "missing_sample":
            del summary["sample_reuse"]
        else:
            _mapping(summary["initial" if change == "incomplete_initial" else "repeat"])["complete"] = False
    comparison = SummarizeTrainingBenchmark(results)
    assert comparison["comparable"] is False
    assert comparison["timing_acceptance"] is False
