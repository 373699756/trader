"""Build only the selected download/market invocation."""

from __future__ import annotations

import sys
from dataclasses import replace

from scripts.core.catalog import WorkerArguments
from scripts.core.contracts import DiagnosticCommand
from scripts.download.market.options import MarketOptions


def BuildCommands(
    options: MarketOptions, *, python_executable: str = sys.executable
) -> tuple[DiagnosticCommand, ...]:
    common_timeout = options.command_timeout_seconds
    commands: tuple[DiagnosticCommand, ...]
    if options.action_identity == "market-factor-compare" and options.qfq_database is None:
        from trader.infra.settings import LoadRuntimeSettings

        options = replace(options, qfq_database=LoadRuntimeSettings(options.runtime_config).data_paths.daily_window)
    if options.action_identity == "security-master":
        commands = (DiagnosticCommand(
            "exchange_security_master",
            (
                python_executable,
                "-m",
                "scripts.download.market.exchange_security_master",
                "--timeout-seconds",
                str(max(15.0, options.source_timeout_seconds)),
                "--minimum-rows", str(options.security_master_minimum_rows),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "tencent":
        commands = (DiagnosticCommand(
            "tencent_quotes",
            (
                python_executable,
                "-m",
                "scripts.download.market.tencent_quotes",
                *options.codes,
                "--samples",
                str(options.source_samples),
                "--interval-seconds",
                str(options.source_interval_seconds),
                "--timeout-seconds",
                str(options.source_timeout_seconds),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "tencent-transport":
        commands = (DiagnosticCommand(
            "tencent_transport",
            (python_executable, "-m", "scripts.download.market.tencent_quotes",
             "--transport-audit", "--runtime-config", str(options.runtime_config),
             "--timeout-seconds", str(options.source_timeout_seconds)),
            common_timeout,
        ),)
    elif options.action_identity == "tushare":
        commands = (DiagnosticCommand(
            "tushare_daily",
            (
                python_executable,
                "-m",
                "scripts.download.market.tushare_daily",
                "--runtime-config",
                str(options.runtime_config),
                "--codes",
                *options.codes,
                "--days",
                str(options.history_days),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "market-factor-compare":
        commands = (DiagnosticCommand(
            "market_factor_compare",
            (python_executable, "-m", "scripts.download.market.market_factor_compare")
            + ("--config", str(options.runtime_config), "--qfq-database", str(options.qfq_database))
            + ("--sources", *options.market_sources)
            + ("--profiles", *options.market_profiles)
            + ("--sizes", *(str(size) for size in options.market_sizes))
            + ("--mode", options.market_mode, "--rounds", str(options.source_samples))
            + ("--interval-seconds", str(options.source_interval_seconds))
            + ("--timeout-seconds", str(options.source_timeout_seconds))
            + ("--batch-seconds", str(options.market_batch_seconds))
            + ("--source-loop-seconds", str(options.market_source_loop_seconds))
            + ("--task-seconds", str(options.market_task_seconds))
            + (("--cpu-profile-dir", str(options.cpu_profile_dir)) if options.cpu_profile_dir is not None else ()),
            min(common_timeout, options.market_task_seconds),
        ),)
    else:
        raise ValueError("unsupported download/market action")
    return tuple(replace(command, argv=WorkerArguments(command.argv)) for command in commands)
