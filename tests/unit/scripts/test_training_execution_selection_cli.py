"""Explicit component, computation and fanout selections stay confined to candidates."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from scripts.core.contracts import DiagnosticCommand, DiagnosticResult
from scripts.training.cli import BuildParser, ParseOptions
from scripts.training.commands import BuildCommands
from scripts.training.jobs.training_performance import BuildTrainingBenchmarkCommands, SummarizeTrainingBenchmark
from scripts.training.options import TrainingOptions
from scripts.training.resources.execution_selection import FanoutCandidate
from tests.unit.scripts.test_training_execution_performance import _results


def _parse(tmp_path: Path, action: str, arguments: tuple[str, ...] = ()) -> TrainingOptions:
    parser = BuildParser(action)
    return ParseOptions(parser.parse_args((
        "--history-root", str(tmp_path / "history"),
        "--training-probe-directory", str(tmp_path / "work"),
        *arguments,
    )))


def _baseline(tmp_path: Path) -> tuple[str, ...]:
    checkout = tmp_path / "baseline"
    (checkout / "src/trader").mkdir(parents=True, exist_ok=True)
    return ("--baseline-checkout", str(checkout))


def _candidate(commands: tuple[DiagnosticCommand, ...]) -> DiagnosticCommand:
    return next(command for command in commands if ":candidate:" in command.name)


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _summary(result: DiagnosticResult) -> dict[str, object]:
    assert result.payload is not None
    return _mapping(result.payload["summary"])


def _selected_results() -> tuple[DiagnosticResult, ...]:
    results = _results((0.8,) * 5, 1.0, 1.0)
    for result in results:
        summary = _summary(result)
        candidate = ":candidate:" in result.name
        execution = _mapping(summary["execution_candidate"])
        execution.update(
            execution="selected" if candidate else "baseline",
            cpu="baseline",
            components=["resources", "shared_validation"] if candidate else [],
            fanout="batch_double" if candidate else "baseline",
            runs=[
                {
                    "profiles": list(profiles),
                    "cpu_capacity": 3,
                    "native_threads": 1,
                    "profile_threads": {"v1": 2, "v2": 2, "v3": 2},
                    "memory_capacity_bytes": 1024,
                    "batch_rows": 512 if candidate else 256,
                    "queue_batches": 2,
                }
                for profiles in (
                    ("v1", "v2", "v3", "v5"), ("v1", "v2", "v3", "v5"),
                    ("v2", "v3", "v5"), ("v2", "v3", "v5"),
                )
            ],
        )
        if candidate:
            _mapping(summary["computation_candidate"])["name"] = "combined"
    return results


@pytest.mark.parametrize("action", ("training-alignment", "training-performance"))
def test_selected_components_are_canonicalized_and_forwarded_only_to_candidate(
    tmp_path: Path, action: str,
) -> None:
    options = _parse(tmp_path, action, (
        *(_baseline(tmp_path) if action == "training-performance" else ()),
        "--execution-candidate", "selected",
        "--execution-components", "shared_validation, resources",
        "--fanout-candidate", "queue_double",
    ))
    assert options.execution_components == ("resources", "shared_validation")
    commands = BuildCommands(options, python_executable="python3")
    selected = _candidate(commands) if action == "training-performance" else commands[0]
    assert selected.argv[selected.argv.index("--execution-candidate") + 1] == "selected"
    assert selected.argv[selected.argv.index("--execution-components") + 1] == "resources,shared_validation"
    assert selected.argv[selected.argv.index("--fanout-candidate") + 1] == "queue_double"
    assert "--cpu-candidate" not in selected.argv
    for command in commands:
        if command is not selected:
            assert "--execution-components" not in command.argv
            assert "--fanout-candidate" not in command.argv
            assert "--execution-candidate" not in command.argv


@pytest.mark.parametrize("action", ("training-alignment", "training-performance"))
def test_selected_execution_requires_explicit_nonempty_components(tmp_path: Path, action: str) -> None:
    with pytest.raises(ValueError, match="training_execution_components_required_for_selected"):
        _parse(tmp_path, action, ("--execution-candidate", "selected"))


@pytest.mark.parametrize("execution", ("baseline", "resources", "combined", "normalized_tcn"))
def test_components_cannot_silently_modify_a_named_execution(tmp_path: Path, execution: str) -> None:
    with pytest.raises(ValueError, match="training_execution_components_require_selected"):
        _parse(tmp_path, "training-alignment", (
            "--execution-candidate", execution, "--execution-components", "resources",
        ))


@pytest.mark.parametrize("components", ("", "unknown", "resources,", "resources,resources"))
def test_invalid_component_subsets_are_rejected_at_the_parser(tmp_path: Path, components: str) -> None:
    with pytest.raises(SystemExit):
        _parse(tmp_path, "training-alignment", (
            "--execution-candidate", "selected", "--execution-components", components,
        ))


@pytest.mark.parametrize("action", ("training-alignment", "training-performance"))
@pytest.mark.parametrize("fanout", ("batch_double", "queue_double"))
def test_fanout_alone_does_not_add_cpu_or_execution_candidates(tmp_path: Path, action: str, fanout: str) -> None:
    options = _parse(tmp_path, action, (
        *(_baseline(tmp_path) if action == "training-performance" else ()), "--fanout-candidate", fanout,
    ))
    assert options.execution_candidate == options.cpu_candidate == "baseline"
    commands = BuildCommands(options, python_executable="python3")
    selected = _candidate(commands) if action == "training-performance" else commands[0]
    assert selected.argv[selected.argv.index("--fanout-candidate") + 1] == fanout
    assert "--cpu-candidate" not in selected.argv
    assert "--execution-candidate" not in selected.argv


@pytest.mark.parametrize("computation", ("indexed_anchors", "schema_cache", "combined"))
def test_performance_computation_selection_does_not_rewrite_baseline_arguments(
    tmp_path: Path, computation: str,
) -> None:
    defaults = _parse(tmp_path, "training-performance", _baseline(tmp_path))
    selected = _parse(tmp_path, "training-performance", (
        *_baseline(tmp_path), "--computation-candidate", computation,
    ))
    original = BuildCommands(defaults, python_executable="python3")
    commands = BuildCommands(selected, python_executable="python3")
    assert commands[0] == original[0] and commands[-1] == original[-1]
    command = _candidate(commands)
    assert command.argv[command.argv.index("--computation-candidate") + 1] == computation
    assert "--execution-candidate" not in command.argv and "--cpu-candidate" not in command.argv


@pytest.mark.parametrize("action", ("training-alignment", "training-performance"))
def test_explicit_baseline_defaults_preserve_existing_worker_arguments(tmp_path: Path, action: str) -> None:
    defaults = _parse(tmp_path, action)
    explicit = _parse(tmp_path, action, (
        "--execution-candidate", "baseline", "--fanout-candidate", "baseline", "--computation-candidate", "baseline",
    ))
    assert defaults == explicit
    commands = BuildCommands(defaults, python_executable="python3")
    assert commands == BuildCommands(explicit, python_executable="python3")
    assert all("--execution-components" not in command.argv and "--fanout-candidate" not in command.argv for command in commands)
    if action == "training-performance":
        assert all("--computation-candidate" not in command.argv for command in commands)


@pytest.mark.parametrize("arguments", (
    ("--fanout-candidate", "batch_double"),
    ("--computation-candidate", "indexed_anchors"),
    ("--execution-candidate", "selected", "--execution-components", "resources"),
    ("--training-benchmark-rounds", "2"),
))
def test_all_explicit_performance_candidates_and_repeated_rounds_require_checkout(
    tmp_path: Path, arguments: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="require --baseline-checkout"):
        _parse(tmp_path, "training-performance", arguments)


@pytest.mark.parametrize("fanout,computation", (("batch_double", "baseline"), ("baseline", "schema_cache")))
def test_direct_performance_command_builder_also_enforces_checkout(
    tmp_path: Path,
    fanout: FanoutCandidate,
    computation: Literal["baseline", "indexed_anchors", "schema_cache", "combined"],
) -> None:
    options = _parse(tmp_path, "training-performance")
    changed = replace(options, fanout_candidate=fanout, computation_candidate=computation)
    with pytest.raises(ValueError, match="training_execution_comparison_requires_baseline_checkout"):
        BuildTrainingBenchmarkCommands(changed, "python3")


def test_five_rounds_keep_all_new_flags_off_the_ten_baseline_children(tmp_path: Path) -> None:
    options = _parse(tmp_path, "training-performance", (
        *_baseline(tmp_path), "--training-benchmark-rounds", "5",
        "--execution-candidate", "selected", "--execution-components", "resources,shared_validation",
        "--fanout-candidate", "batch_double", "--computation-candidate", "combined",
    ))
    commands = BuildCommands(options, python_executable="python3")
    assert len(commands) == 15
    for command in commands:
        for flag in ("--execution-candidate", "--execution-components", "--fanout-candidate", "--computation-candidate"):
            assert (flag in command.argv) is (":candidate:" in command.name)


def test_intentional_candidate_fanout_and_computation_differences_preserve_numeric_comparability() -> None:
    comparison = SummarizeTrainingBenchmark(_selected_results())
    assert comparison["comparable"] is True
    assert comparison["timing_acceptance"] is True
    assert comparison["performance_acceptance"] is False


@pytest.mark.parametrize("change", ("components", "fanout", "computation", "batch_rows", "queue_batches", "baseline_batch"))
def test_each_role_freezes_components_computation_and_actual_fanout_across_rounds(change: str) -> None:
    results = _selected_results()
    summary = _summary(results[-3] if change == "baseline_batch" else results[-2])
    execution = _mapping(summary["execution_candidate"])
    if change == "components":
        execution["components"] = ["resources"]
    elif change == "fanout":
        execution["fanout"] = "queue_double"
    elif change == "computation":
        _mapping(summary["computation_candidate"])["name"] = "schema_cache"
    else:
        runs = execution["runs"]
        assert isinstance(runs, list)
        _mapping(runs[0])["batch_rows" if change in {"batch_rows", "baseline_batch"} else "queue_batches"] = 1024
    comparison = SummarizeTrainingBenchmark(results)
    assert comparison["comparable"] is False
    assert comparison["timing_acceptance"] is False


@pytest.mark.parametrize("role", ("baseline", "candidate"))
@pytest.mark.parametrize("field", ("cpu_capacity", "native_threads", "profile_threads", "memory_capacity_bytes"))
def test_actual_cpu_and_memory_budget_drift_is_rejected_for_each_role(role: str, field: str) -> None:
    results = _selected_results()
    execution = _mapping(_summary(results[-3] if role == "baseline" else results[-2])["execution_candidate"])
    runs = execution["runs"]
    assert isinstance(runs, list)
    run = _mapping(runs[-1])
    if field == "profile_threads":
        _mapping(run[field])["v2"] = 1
    else:
        run[field] = 1 if field == "memory_capacity_bytes" else 4
    comparison = SummarizeTrainingBenchmark(results)
    assert comparison["comparable"] is False
    assert comparison["timing_acceptance"] is False


def test_candidate_and_baseline_may_use_different_fixed_effective_budgets() -> None:
    results = _selected_results()
    for result in results:
        if ":candidate:" not in result.name:
            continue
        execution = _mapping(_summary(result)["execution_candidate"])
        execution["cpu"] = "three_single"
        runs = execution["runs"]
        assert isinstance(runs, list)
        for value in runs:
            _mapping(value).update(
                cpu_capacity=2, native_threads=2, profile_threads={"v1": 1, "v2": 1, "v3": 1},
                memory_capacity_bytes=512,
            )
    comparison = SummarizeTrainingBenchmark(results)
    assert comparison["comparable"] is True
    assert comparison["timing_acceptance"] is True


def test_explicit_null_memory_budget_is_legal_when_fixed_within_each_role() -> None:
    results = _selected_results()
    for result in results:
        runs = _mapping(_summary(result)["execution_candidate"])["runs"]
        assert isinstance(runs, list)
        for value in runs:
            _mapping(value)["memory_capacity_bytes"] = None
    comparison = SummarizeTrainingBenchmark(results)
    assert comparison["comparable"] is True
    assert comparison["timing_acceptance"] is True


@pytest.mark.parametrize("field", (
    "profiles", "cpu_capacity", "native_threads", "profile_threads", "memory_capacity_bytes", "batch_rows", "queue_batches",
))
def test_missing_actual_budget_fields_cannot_be_guessed(field: str) -> None:
    results = _selected_results()
    runs = _mapping(_summary(results[1])["execution_candidate"])["runs"]
    assert isinstance(runs, list)
    del _mapping(runs[0])[field]
    comparison = SummarizeTrainingBenchmark(results)
    assert comparison["comparable"] is False


@pytest.mark.parametrize("records", ("missing", "empty", "malformed"))
def test_execution_policy_requires_nonempty_actual_run_records(records: str) -> None:
    results = _selected_results()
    execution = _mapping(_summary(results[1])["execution_candidate"])
    if records == "missing":
        del execution["runs"]
    else:
        execution["runs"] = [] if records == "empty" else [{}]
    comparison = SummarizeTrainingBenchmark(results)
    assert comparison["comparable"] is False


@pytest.mark.parametrize("field,value", (
    ("cpu_capacity", True), ("native_threads", 0), ("batch_rows", "256"), ("queue_batches", -1),
    ("memory_capacity_bytes", True), ("memory_capacity_bytes", 0), ("profile_threads", {"v1": 2, "v2": True, "v3": 2}),
    ("profile_threads", {"v1": 2, "v2": 2}),
))
def test_actual_budget_records_require_real_positive_integers(field: str, value: object) -> None:
    results = _selected_results()
    runs = _mapping(_summary(results[1])["execution_candidate"])["runs"]
    assert isinstance(runs, list)
    _mapping(runs[0])[field] = value
    comparison = SummarizeTrainingBenchmark(results)
    assert comparison["comparable"] is False
