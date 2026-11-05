"""The opt-in V5 probe requires an explicit external output directory."""

from scripts.checks.catalog import SelectionArguments

from pathlib import Path

import pytest

from scripts.checks.arguments import BuildParser
from scripts.checks.arguments import ValidateOptions
from scripts.checks.commands import BuildCommands


def test_v5_probe_requires_explicit_external_directory(tmp_path: Path):
    with pytest.raises(ValueError, match="v5-training requires"):
        ValidateOptions(BuildParser().parse_args([*SelectionArguments('v5-training')]))
    options, _output = ValidateOptions(
        BuildParser().parse_args([*SelectionArguments('v5-training'), "--v5-probe-directory", str(tmp_path / "probe")])
    )
    assert options.v5_probe_directory == tmp_path / "probe"
    assert options.check_id == "v5-training"


def test_v5_kernel_probe_needs_no_history_and_has_a_bounded_child():
    options, _output = ValidateOptions(
        BuildParser().parse_args(
            [
                *SelectionArguments('v5-training'),
                "--v5-kernel-probe",
                "--runtime-config",
                "/nonexistent/trader-v5-kernel-runtime.json",
                "--command-timeout-seconds",
                "180",
            ]
        )
    )
    assert options.v5_kernel_probe and options.v5_probe_directory is None
    commands = BuildCommands(options)
    assert len(commands) == 1
    assert commands[0].argv[-1] == "--kernel-probe"
    assert "--history-root" not in commands[0].argv and "--work-root" not in commands[0].argv
    with pytest.raises(ValueError, match="requires --check"):
        ValidateOptions(BuildParser().parse_args([*SelectionArguments('snapshot'), "--v5-kernel-probe"]))
    with pytest.raises(SystemExit):
        BuildParser().parse_args([*SelectionArguments('v5-training'), "--v5-kernel-probe", "--v5-probe-directory", "/tmp/probe"])
