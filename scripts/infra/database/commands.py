"""Build only the selected infra/database invocation."""

from __future__ import annotations

import sys
from dataclasses import replace
from functools import partial

from scripts.core.catalog import WorkerArguments
from scripts.core.contracts import DiagnosticCommand
from scripts.infra.database.options import DatabaseOptions
from scripts.infra.database.database_workspace import PrepareDatabaseCommand


def BuildCommands(
    options: DatabaseOptions, *, python_executable: str = sys.executable
) -> tuple[DiagnosticCommand, ...]:
    common_timeout = options.command_timeout_seconds
    commands: tuple[DiagnosticCommand, ...]
    if options.action_identity == "database-baseline":
        commands = (DiagnosticCommand(
            "database_baseline",
            (
                python_executable,
                "-m",
                "scripts.infra.database.database_baseline",
                "--runtime-config", str(options.runtime_config),
                "--size",
                str(options.download_sizes[0]),
                "--sessions",
                str(options.history_days),
                "--rounds",
                str(options.database_rounds),
            )
            + (("--include-training",) if options.database_training else ())
            + (
                ("--acceptance-focus", options.database_acceptance_focus)
                if options.database_acceptance_focus != "history"
                else ()
            )
            + (("--baseline-checkout", str(options.baseline_checkout)) if options.baseline_checkout else ()),
            common_timeout,
        ),)
    else:
        raise ValueError("unsupported infra/database action")
    if options.database_probe_directory is None:
        raise ValueError("database-baseline requires --database-probe-directory")
    commands = tuple(replace(
        command,
        prepare=partial(PrepareDatabaseCommand, options.database_probe_directory, options.runtime_config, command.name),
    ) for command in commands)
    return tuple(replace(command, argv=WorkerArguments(command.argv)) for command in commands)
