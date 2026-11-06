"""Build only the selected recommendation/web invocation."""

from __future__ import annotations

import sys
from dataclasses import replace

from scripts.core.catalog import WorkerArguments
from scripts.core.contracts import DiagnosticCommand
from scripts.recommendation.web.options import WebOptions


def BuildCommands(
    options: WebOptions, *, python_executable: str = sys.executable
) -> tuple[DiagnosticCommand, ...]:
    common_timeout = options.command_timeout_seconds
    commands: tuple[DiagnosticCommand, ...]
    if options.action_identity == "web":
        commands = (DiagnosticCommand(
            "web_health",
            (
                python_executable,
                "-m",
                "scripts.recommendation.web.web_health",
                "--base-url",
                options.base_url,
                "--samples",
                str(options.web_samples),
                "--interval-seconds",
                str(options.web_interval_seconds),
                "--timeout-seconds",
                str(options.web_timeout_seconds),
                "--consecutive-zero-threshold",
                str(options.consecutive_zero_threshold),
                *(argument for strategy in options.strategies for argument in ("--strategy", strategy)),
                *(("--complete-rounds", str(options.web_complete_rounds),
                   "--observation-seconds", str(options.web_observation_seconds)) if options.web_complete_rounds else ()),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "browser":
        commands = (DiagnosticCommand(
            "browser_refresh",
            (
                python_executable,
                "-m",
                "scripts.recommendation.web.browser_refresh",
                "--duration-seconds",
                str(options.browser_duration_seconds),
                "--minimum-updates",
                str(options.browser_minimum_updates),
                "--runtime-config",
                str(options.runtime_config),
                "--strategy",
                options.browser_strategy,
                "--simulated-start",
                options.simulated_start.isoformat(),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "browser-live":
        commands = (DiagnosticCommand(
            "browser_live",
            (python_executable, "-m", "scripts.recommendation.web.browser_refresh", "--live-url", options.base_url),
            common_timeout,
        ),)
    elif options.action_identity == "company-ledger":
        commands = (DiagnosticCommand(
            "company_ledger_browser",
            (python_executable, "-m", "scripts.recommendation.web.company_ledger_browser",
             "--runtime-config", str(options.runtime_config)),
            common_timeout,
        ),)
    else:
        raise ValueError("unsupported recommendation/web action")
    if options.action_identity == "web" and options.scoring_profile is not None:
        command = commands[0]
        commands = (replace(command, name=f"{command.name}:{options.scoring_profile}", argv=command.argv + ("--profile", options.scoring_profile)),)
    return tuple(replace(command, argv=WorkerArguments(command.argv)) for command in commands)
