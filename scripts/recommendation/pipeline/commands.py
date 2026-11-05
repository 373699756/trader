"""Build only the selected recommendation/pipeline invocation."""

from __future__ import annotations

import sys
from dataclasses import replace

from scripts.core.catalog import WorkerArguments
from scripts.core.contracts import DiagnosticCommand
from scripts.recommendation.pipeline.options import PipelineOptions


def BuildCommands(
    options: PipelineOptions, *, python_executable: str = sys.executable
) -> tuple[DiagnosticCommand, ...]:
    common_timeout = options.command_timeout_seconds
    commands: tuple[DiagnosticCommand, ...]
    cli = (python_executable, "-m", "trader.entrypoints.cli", "--config", str(options.runtime_config))
    if options.action_identity == "snapshot":
        commands = (
            DiagnosticCommand("configuration", cli + ("validate-config",), common_timeout),
            DiagnosticCommand(
                "history_maintenance", cli + ("history-automation-status",), common_timeout
            ),
            DiagnosticCommand(
                "research_readiness",
                (
                    python_executable,
                    "-m",
                    "trader.entrypoints.cli",
                    "--config",
                    str(options.runtime_config),
                    "research-status",
                ),
                common_timeout,
            ),
            DiagnosticCommand(
                "production_performance",
                (
                    python_executable,
                    "-m",
                    "trader.entrypoints.performance",
                    "--config",
                    str(options.runtime_config),
                    "--replay-ticks",
                    str(options.performance_replay_ticks),
                    "--factor-candidate",
                    options.performance_factor_candidate,
                    "--scoring-candidate", options.performance_scoring_candidate,
                    *(("--steady-only",) if options.performance_steady_only else ()),
                    "--cpu-profile-mode",
                    options.cpu_profile_mode,
                    *(("--cpu-profile-dir", str(options.cpu_profile_dir)) if options.cpu_profile_dir is not None else ()),
                    *(
                        ("--replay-rows", str(options.performance_replay_rows))
                        if options.performance_replay_rows is not None
                        else ()
                    ),
                ),
                common_timeout,
            ),
        )
    elif options.action_identity == "research":
        commands = (DiagnosticCommand(
            "research_readiness",
            (
                python_executable,
                "-m",
                "trader.entrypoints.cli",
                "--config",
                str(options.runtime_config),
                "research-status",
            ),
            common_timeout,
        ),)
    elif options.action_identity == "performance":
        commands = (DiagnosticCommand(
            "production_performance",
            (
                python_executable,
                "-m",
                "trader.entrypoints.performance",
                "--config",
                str(options.runtime_config),
                "--replay-ticks",
                str(options.performance_replay_ticks),
                "--factor-candidate",
                options.performance_factor_candidate,
                "--scoring-candidate", options.performance_scoring_candidate,
                *(("--steady-only",) if options.performance_steady_only else ()),
                "--cpu-profile-mode",
                options.cpu_profile_mode,
                *(("--cpu-profile-dir", str(options.cpu_profile_dir)) if options.cpu_profile_dir is not None else ()),
                *(
                    ("--replay-rows", str(options.performance_replay_rows))
                    if options.performance_replay_rows is not None
                    else ()
                ),
            ),
            common_timeout,
        ),)
    else:
        raise ValueError("unsupported recommendation/pipeline action")
    selection = options.scoring_profile
    if options.action_identity == "performance" and selection is None:
        selection = "v1,v2,v3,v5"
    if selection is not None:
        selected = []
        for command in commands:
            if command.name == "history_maintenance":
                selected.append(command)
                continue
            profile_arguments = ("--profile", selection)
            argv = command.argv + profile_arguments if command.name == "production_performance" else command.argv[:-1] + profile_arguments + command.argv[-1:]
            selected.append(replace(command, name=f"{command.name}:{selection}", argv=argv))
        commands = tuple(selected)
    if options.action_identity == "snapshot":
        configuration = commands[0]
        commands = (
            configuration,
            *(replace(command, requires_success=(configuration.name,)) for command in commands[1:]),
        )
    return tuple(replace(command, argv=WorkerArguments(command.argv)) for command in commands)
