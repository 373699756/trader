"""Build only the selected infra/disk invocation."""

from __future__ import annotations

import sys
from dataclasses import replace

from scripts.core.catalog import WorkerArguments
from scripts.core.contracts import DiagnosticCommand
from scripts.infra.disk.options import DiskOptions


def BuildCommands(
    options: DiskOptions, *, python_executable: str = sys.executable
) -> tuple[DiagnosticCommand, ...]:
    common_timeout = options.command_timeout_seconds
    commands: tuple[DiagnosticCommand, ...]
    if options.action_identity == "disk":
        commands = (DiagnosticCommand(
            "disk_profile",
            (
                python_executable,
                "-m",
                "scripts.infra.disk.disk_profile",
                "--config",
                str(options.runtime_config),
            )
            + tuple(value for path in options.disk_paths for value in ("--path", str(path)))
            + (("--policy-audit",) if options.disk_policy_audit else ()),
            common_timeout,
        ),)
    else:
        raise ValueError("unsupported infra/disk action")
    return tuple(replace(command, argv=WorkerArguments(command.argv)) for command in commands)
