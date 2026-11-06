"""The public diagnostic router forwards only explicitly selected execution experiments."""

from pathlib import Path

import pytest

from scripts.core.catalog import SelectionArguments
from scripts.core.contracts import DiagnosticCommand
from scripts.training.commands import BuildCommands
from scripts.training.options import TrainingOptions
from tests.helpers.shared.tool_cli import ParseBusinessOptions, ParseToolArguments


def _parse(tmp_path: Path, action: str, arguments: tuple[str, ...] = ()) -> TrainingOptions:
    parsed = ParseBusinessOptions(ParseToolArguments([
        *SelectionArguments(action),
        "--history-root", str(tmp_path / "history"),
        "--training-probe-directory", str(tmp_path / "probe"),
        *arguments,
    ]))
    assert isinstance(parsed, TrainingOptions)
    return parsed


@pytest.mark.parametrize(
    "candidate", ("baseline", "resources", "ram_matrices", "shared_validation", "normalized_tcn", "combined")
)
def test_alignment_execution_choices_are_parsed_and_only_explicit_experiments_are_forwarded(
    tmp_path: Path, candidate: str
) -> None:
    options = _parse(tmp_path, "training-alignment", ("--execution-candidate", candidate))
    assert options.execution_candidate == candidate
    command, = BuildCommands(options, python_executable="/python3")
    if candidate == "baseline":
        assert "--execution-candidate" not in command.argv
    else:
        assert command.argv[command.argv.index("--execution-candidate") + 1] == candidate
    assert "--cpu-candidate" not in command.argv


@pytest.mark.parametrize("candidate", ("baseline", "two_plus_one", "three_single"))
def test_alignment_cpu_choices_are_parsed_and_only_explicit_experiments_are_forwarded(
    tmp_path: Path, candidate: str
) -> None:
    options = _parse(tmp_path, "training-alignment", ("--cpu-candidate", candidate))
    assert options.cpu_candidate == candidate
    command, = BuildCommands(options, python_executable="/python3")
    if candidate == "baseline":
        assert "--cpu-candidate" not in command.argv
    else:
        assert command.argv[command.argv.index("--cpu-candidate") + 1] == candidate
    assert "--execution-candidate" not in command.argv


def test_alignment_defaults_retain_the_existing_worker_arguments(tmp_path: Path) -> None:
    defaults = _parse(tmp_path, "training-alignment")
    explicit = _parse(tmp_path, "training-alignment", (
        "--execution-candidate", "baseline", "--cpu-candidate", "baseline",
    ))
    assert defaults == explicit
    assert defaults.training_benchmark_rounds == 1
    command, = BuildCommands(defaults, python_executable="/python3")
    assert "--execution-candidate" not in command.argv
    assert "--cpu-candidate" not in command.argv
    assert "--training-benchmark-rounds" not in command.argv
    assert BuildCommands(explicit, python_executable="/python3") == (command,)


def test_alignment_can_forward_execution_and_cpu_experiments_together(tmp_path: Path) -> None:
    command, = BuildCommands(_parse(tmp_path, "training-alignment", (
        "--execution-candidate", "combined", "--cpu-candidate", "two_plus_one",
    )), python_executable="/python3")
    assert command.argv[command.argv.index("--execution-candidate") + 1] == "combined"
    assert command.argv[command.argv.index("--cpu-candidate") + 1] == "two_plus_one"


@pytest.mark.parametrize("action", ("training-alignment", "training-performance"))
@pytest.mark.parametrize("flag", ("--execution-candidate", "--cpu-candidate"))
def test_public_training_parser_rejects_unknown_execution_and_cpu_choices(
    tmp_path: Path, action: str, flag: str
) -> None:
    with pytest.raises(SystemExit):
        _parse(tmp_path, action, (flag, "unknown"))


@pytest.mark.parametrize("arguments", (
    ("--execution-candidate", "combined"),
    ("--cpu-candidate", "three_single"),
    ("--training-benchmark-rounds", "2"),
))
def test_performance_experiments_require_an_explicit_baseline_checkout(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError, match="require --baseline-checkout"):
        _parse(tmp_path, "training-performance", arguments)


@pytest.mark.parametrize("rounds", (1, 5))
def test_performance_accepts_explicit_candidates_and_bounded_round_counts(tmp_path: Path, rounds: int) -> None:
    options = _parse(tmp_path, "training-performance", (
        "--baseline-checkout", str(tmp_path / "baseline"),
        "--execution-candidate", "normalized_tcn", "--cpu-candidate", "three_single",
        "--training-benchmark-rounds", str(rounds),
    ))
    assert options.execution_candidate == "normalized_tcn"
    assert options.cpu_candidate == "three_single"
    assert options.training_benchmark_rounds == rounds
    assert options.baseline_checkout == tmp_path / "baseline"


@pytest.mark.parametrize("rounds", ("0", "-1", "6"))
def test_performance_rejects_round_counts_outside_one_to_five(tmp_path: Path, rounds: str) -> None:
    with pytest.raises(ValueError, match="must be within 1..5"):
        _parse(tmp_path, "training-performance", ("--training-benchmark-rounds", rounds))


@pytest.mark.parametrize("rounds", ("1.5", "True"))
def test_performance_round_count_requires_an_integer(tmp_path: Path, rounds: str) -> None:
    with pytest.raises(SystemExit):
        _parse(tmp_path, "training-performance", ("--training-benchmark-rounds", rounds))


def test_alignment_does_not_accept_performance_round_options(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _parse(tmp_path, "training-alignment", ("--training-benchmark-rounds", "2"))


def test_performance_command_building_delegates_without_rewriting_baseline_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.training.jobs import training_performance

    options = _parse(tmp_path, "training-performance", (
        "--baseline-checkout", str(tmp_path / "baseline"),
        "--execution-candidate", "combined", "--cpu-candidate", "two_plus_one",
        "--training-benchmark-rounds", "3",
    ))
    expected = (DiagnosticCommand("baseline", ("/python3", "baseline-worker"), 180.0),)

    def planned(selected: TrainingOptions, executable: str) -> tuple[DiagnosticCommand, ...]:
        assert selected == options
        assert executable == "/python3"
        return expected

    monkeypatch.setattr(training_performance, "BuildTrainingBenchmarkCommands", planned)
    assert BuildCommands(options, python_executable="/python3") == expected


def test_performance_defaults_allow_the_existing_single_round_without_a_baseline_checkout(tmp_path: Path) -> None:
    options = _parse(tmp_path, "training-performance")
    assert options.execution_candidate == options.cpu_candidate == "baseline"
    assert options.training_benchmark_rounds == 1
    assert options.baseline_checkout is None
    assert options.published_history_read_only is False
    assert options.sample_cache_transfer == "copy"


@pytest.mark.parametrize("transfer", ("copy", "hardlink"))
def test_performance_accepts_guarded_published_history_and_explicit_cache_transfer(
    tmp_path: Path, transfer: str
) -> None:
    options = _parse(tmp_path, "training-performance", (
        "--published-history-read-only", "--sample-cache-transfer", transfer,
    ))
    assert options.published_history_read_only is True
    assert options.sample_cache_transfer == transfer


def test_performance_rejects_unknown_cache_transfer_methods(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _parse(tmp_path, "training-performance", ("--sample-cache-transfer", "move"))
