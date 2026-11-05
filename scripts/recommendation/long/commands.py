"""Build only the selected recommendation/long invocation."""

from __future__ import annotations

import sys
from dataclasses import replace

from scripts.core.catalog import WorkerArguments
from scripts.core.contracts import DiagnosticCommand
from scripts.recommendation.long.options import LongOptions


def BuildCommands(
    options: LongOptions, *, python_executable: str = sys.executable
) -> tuple[DiagnosticCommand, ...]:
    common_timeout = options.command_timeout_seconds
    commands: tuple[DiagnosticCommand, ...]
    if options.company_evidence_database is None:
        from trader.infra.settings import LoadRuntimeSettings

        options = replace(options, company_evidence_database=LoadRuntimeSettings(options.runtime_config).data_paths.company)
    if options.action_identity == "long-watchlist":
        commands = (DiagnosticCommand(
            "long_watchlist_admission",
            (
                python_executable,
                "-m",
                "scripts.recommendation.long.long_watchlist",
                "--watchlist",
                str(options.long_watchlist),
                "--company-evidence-database",
                str(options.company_evidence_database),
                "--runtime-config",
                str(options.runtime_config),
                "--strategy",
                str(options.strategy_config),
                "--workers",
                str(options.workers),
                *(
                    argument
                    for path in options.eligibility_evidence
                    for argument in ("--eligibility-evidence", str(path))
                ),
                *(("--evidence-output", str(options.long_evidence_output)) if options.long_evidence_output else ()),
            ),
            common_timeout,
        ),)
    else:
        raise ValueError("unsupported recommendation/long action")
    return tuple(replace(command, argv=WorkerArguments(command.argv)) for command in commands)
