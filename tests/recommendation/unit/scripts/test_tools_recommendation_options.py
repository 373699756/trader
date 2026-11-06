"""Recommendation option routing uses parser and command fixtures without running workers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from scripts.recommendation.long.cli import BuildParser as BuildLongParser
from scripts.recommendation.long.cli import ParseOptions as ParseLongOptions
from scripts.recommendation.long.commands import BuildCommands as BuildLongCommands
from scripts.recommendation.web.cli import BuildParser as BuildWebParser
from scripts.recommendation.web.cli import ParseOptions as ParseWebOptions
from scripts.recommendation.web.commands import BuildCommands as BuildWebCommands


def test_web_strategy_filters_reach_the_selected_read_only_worker() -> None:
    options = ParseWebOptions(BuildWebParser("web").parse_args(["--strategy", "d25", "--strategy", "tomorrow", "--strategy", "d25"]))
    assert options.strategies == ("d25", "tomorrow")
    command, = BuildWebCommands(options, python_executable="/python3")
    assert "scripts.recommendation.web.web_health" in command.argv
    assert command.argv[-4:] == ("--strategy", "d25", "--strategy", "tomorrow")
    assert command.timeout_seconds == 180


def test_web_default_keeps_the_workers_all_strategy_selection() -> None:
    options = ParseWebOptions(BuildWebParser("web").parse_args([]))
    command, = BuildWebCommands(options, python_executable="/python3")
    assert options.strategies == () and "--strategy" not in command.argv
    assert options.web_complete_rounds == 0 and options.web_observation_seconds is None
    assert "--complete-rounds" not in command.argv and "--observation-seconds" not in command.argv


def _complete_round_arguments() -> list[str]:
    return [
        "--web-complete-rounds", "5",
        "--web-observation-seconds", "900",
        "--web-interval-seconds", "15",
        "--web-timeout-seconds", "4",
        "--command-timeout-seconds", "918",
        "--scoring-profile", "v2",
    ]


def test_complete_round_controls_reach_only_the_existing_health_worker() -> None:
    options = ParseWebOptions(BuildWebParser("web").parse_args(_complete_round_arguments()))
    command, = BuildWebCommands(options, python_executable="/python3")

    assert options.web_complete_rounds == 5 and options.web_observation_seconds == 900
    assert options.web_interval_seconds == 15 and options.web_timeout_seconds == 4
    assert options.scoring_profile == "v2" and not options.strategies
    assert command.name == "web_health:v2" and command.timeout_seconds == 918
    assert "scripts.recommendation.web.web_health" in command.argv
    for key, value in (
        ("--complete-rounds", "5"), ("--observation-seconds", "900.0"),
        ("--interval-seconds", "15.0"), ("--timeout-seconds", "4.0"), ("--profile", "v2"),
    ):
        assert command.argv[command.argv.index(key) + 1] == value


@pytest.mark.parametrize("arguments,reason", (
    (["--web-complete-rounds", "-1"], "requires 1..20"),
    (["--web-complete-rounds", "21"], "requires 1..20"),
    (["--web-observation-seconds", "0"], "requires 1..20"),
    (["--web-observation-seconds", "-1"], "requires 1..20"),
    (["--web-observation-seconds", "901"], "requires 1..20"),
    (["--web-observation-seconds", "nan"], "requires 1..20"),
    (["--web-observation-seconds", "inf"], "requires 1..20"),
    (["--web-interval-seconds", "14.999"], "at least 15"),
    (["--command-timeout-seconds", "917.9"], "leave HTTP and cleanup time"),
    (["--strategy", "tomorrow"], "requires both short strategies"),
))
def test_complete_round_controls_reject_invalid_budgets_and_partial_strategy_selection(arguments, reason) -> None:
    with pytest.raises(ValueError, match=reason):
        ParseWebOptions(BuildWebParser("web").parse_args([*_complete_round_arguments(), *arguments]))


@pytest.mark.parametrize("arguments", (
    ["--web-complete-rounds", "5", "--web-interval-seconds", "15"],
    ["--web-observation-seconds", "900"],
    ["--web-complete-rounds", "0", "--web-observation-seconds", "900"],
))
def test_complete_round_mode_requires_an_explicit_goal_and_observation_bound(arguments) -> None:
    with pytest.raises(ValueError, match="requires"):
        ParseWebOptions(BuildWebParser("web").parse_args(arguments))


@pytest.mark.parametrize("rounds", (1, 20))
def test_complete_round_goal_accepts_only_the_declared_inclusive_bounds(rounds) -> None:
    options = ParseWebOptions(BuildWebParser("web").parse_args([
        *_complete_round_arguments(), "--web-complete-rounds", str(rounds),
    ]))
    assert options.web_complete_rounds == rounds


@pytest.mark.parametrize("action", ("browser", "browser-live", "company-ledger"))
@pytest.mark.parametrize("arguments", (
    ["--web-complete-rounds", "5"], ["--web-observation-seconds", "900"],
))
def test_complete_round_parameters_are_not_accepted_by_other_web_actions(action, arguments) -> None:
    with pytest.raises(SystemExit) as error:
        BuildWebParser(action).parse_args(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize("samples,expected", ((1, 1), (2, 2), (3, 3), (6, 3)))
def test_web_threshold_default_retains_the_original_sample_dependent_bound(samples: int, expected: int) -> None:
    options = ParseWebOptions(BuildWebParser("web").parse_args(["--web-samples", str(samples)]))
    assert options.consecutive_zero_threshold == expected


def test_explicit_web_threshold_reaches_the_existing_health_worker() -> None:
    options = ParseWebOptions(BuildWebParser("web").parse_args(["--web-samples", "6", "--consecutive-zero-threshold", "4"]))
    command, = BuildWebCommands(options, python_executable="/python3")
    index = command.argv.index("--consecutive-zero-threshold")
    assert command.argv[index + 1] == "4"


@pytest.mark.parametrize("value", (0, 4))
def test_web_threshold_rejects_values_outside_the_existing_sample_budget(value: int) -> None:
    with pytest.raises(ValueError, match="within 1..web-samples"):
        ParseWebOptions(BuildWebParser("web").parse_args(["--web-samples", "3", "--consecutive-zero-threshold", str(value)]))


def test_browser_strategy_and_typed_fixture_time_reach_only_the_browser_command() -> None:
    start = "2026-11-05T13:30:00+08:00"
    options = ParseWebOptions(BuildWebParser("browser").parse_args(["--strategy", "d25", "--simulated-start", start]))
    assert isinstance(options.simulated_start, datetime) and options.simulated_start.utcoffset() is not None
    command, = BuildWebCommands(options, python_executable="/python3")
    assert command.argv[-4:] == ("--strategy", "d25", "--simulated-start", start)
    assert command.timeout_seconds == 180


@pytest.mark.parametrize("value", ("not-a-date", "2026-11-05T13:30:00"))
def test_browser_fixture_time_rejects_invalid_or_naive_values(value: str) -> None:
    with pytest.raises(SystemExit) as error:
        BuildWebParser("browser").parse_args(["--simulated-start", value])
    assert error.value.code == 2


@pytest.mark.parametrize("action,arguments", (
    ("browser-live", ["--strategy", "d25"]),
    ("web", ["--simulated-start", "2026-11-05T13:30:00+08:00"]),
    ("company-ledger", ["--strategy", "d25"]),
))
def test_strategy_controls_do_not_enter_unrelated_web_actions(action: str, arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        BuildWebParser(action).parse_args(arguments)
    assert error.value.code == 2


def test_long_strategy_configuration_and_worker_limit_reach_the_existing_owner(tmp_path: Path) -> None:
    strategy = tmp_path / "strategy.json"
    database = tmp_path / "published-company.sqlite3"
    options = ParseLongOptions(BuildLongParser("long-watchlist").parse_args([
        "--strategy", str(strategy), "--workers", "3", "--company-evidence-database", str(database),
    ]))
    assert options.strategy_config == strategy and options.workers == 3
    command, = BuildLongCommands(options, python_executable="/python3")
    index = command.argv.index("--strategy")
    assert command.argv[index:index + 4] == ("--strategy", str(strategy), "--workers", "3")
    assert command.timeout_seconds == 180 and not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("workers", (0, 9))
def test_long_rejects_workers_outside_the_existing_budget(workers: int) -> None:
    with pytest.raises(ValueError, match="workers must be 1..8"):
        ParseLongOptions(BuildLongParser("long-watchlist").parse_args(["--workers", str(workers)]))
