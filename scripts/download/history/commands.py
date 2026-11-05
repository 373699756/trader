"""Build only the selected download/history invocation."""

from __future__ import annotations

import sys
from dataclasses import replace
from functools import partial

from scripts.core.catalog import WorkerArguments
from scripts.core.contracts import DiagnosticCommand
from scripts.download.history.options import HistoryOptions
from scripts.infra.database.database_workspace import PrepareDatabaseCommand


def BuildCommands(
    options: HistoryOptions, *, python_executable: str = sys.executable
) -> tuple[DiagnosticCommand, ...]:
    common_timeout = options.command_timeout_seconds
    commands: tuple[DiagnosticCommand, ...]
    publication_root = options.history_root
    if options.action_identity in {
        "baostock-qfq-shadow", "tencent-download", "tencent-history-window", "history-sqlite",
        "history-publication", "history-source-compare", "price-repair-audit", "history-space", "history-industry",
    } and options.history_root is None:
        from trader.infra.settings import LoadRuntimeSettings

        options = replace(options, history_root=LoadRuntimeSettings(options.runtime_config).data_paths.history)
    if options.action_identity in {"qfq-sqlite", "download-failures"} and options.qfq_database is None:
        from trader.infra.settings import LoadRuntimeSettings

        options = replace(options, qfq_database=LoadRuntimeSettings(options.runtime_config).data_paths.daily_window)
    if options.action_identity == "history":
        commands = (DiagnosticCommand(
            "history_sources",
            _history_command(options, python_executable),
            common_timeout,
        ),)
    elif options.action_identity == "history-daily-capability":
        commands = (DiagnosticCommand(
            "history_daily_capability",
            (
                python_executable,
                "-m",
                "scripts.download.history.history_daily_capability",
                "--runtime-config",
                str(options.runtime_config),
                "--universe-size", str(options.capability_universe_size),
                "--code", options.capability_code,
                "--days", str(options.capability_days),
                "--timeout-seconds", str(options.capability_timeout_seconds),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "baostock-concurrency":
        commands = (DiagnosticCommand(
            "baostock_concurrency",
            (
                python_executable,
                "-m",
                "scripts.download.history.baostock_concurrency",
                "--discover" if len(options.codes) < max(options.baostock_sizes) else "--codes",
                *(() if len(options.codes) < max(options.baostock_sizes) else options.codes),
                "--sizes",
                *(str(size) for size in options.baostock_sizes),
                "--days",
                str(options.history_days),
                "--timeout-seconds",
                str(options.source_timeout_seconds),
                *(
                    (
                        "--serial-only",
                        "--rounds",
                        str(options.baostock_rounds),
                        "--intervals",
                        *(str(value) for value in options.baostock_intervals),
                    )
                    if options.baostock_serial_only
                    else ("--interval-seconds", str(options.baostock_interval_seconds))
                ),
            ),
            common_timeout if options.baostock_serial_only else max(common_timeout, 1800.0),
        ),)
    elif options.action_identity == "baostock-qfq-shadow":
        commands = (DiagnosticCommand(
            "baostock_qfq_shadow",
            (
                python_executable,
                "-m",
                "scripts.download.history.baostock_qfq_shadow",
                "--history-root",
                str(options.history_root),
            ),
            max(common_timeout, 600.0),
        ),)
    elif options.action_identity == "tencent-download":
        commands = (DiagnosticCommand(
            "tencent_download",
            (
                python_executable,
                "-m",
                "scripts.download.history.tencent_download",
                "--sizes",
                *(str(size) for size in options.download_sizes),
                "--workers",
                str(min(options.history_workers, 12)),
                "--timeout-seconds",
                str(options.source_timeout_seconds),
                "--tencent-history-host",
                options.tencent_history_host,
                "--history-root",
                str(options.history_root),
                "--codes",
                *options.codes,
            ),
            common_timeout,
        ),)
    elif options.action_identity == "tencent-history-window":
        commands = (DiagnosticCommand(
            "tencent_history_window",
            (
                python_executable,
                "-m",
                "scripts.download.history.tencent_download",
                "--whole-window",
                "--history-root",
                str(options.history_root),
                "--codes",
                *options.codes,
                "--timeout-seconds",
                str(options.source_timeout_seconds),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "history-sqlite":
        commands = (DiagnosticCommand(
            "history_sqlite_performance",
            (
                python_executable,
                "-m",
                "scripts.download.history.history_sqlite_performance",
                "--history-root",
                str(options.history_root),
                "--page-sample-count",
                str(options.sqlite_page_sample_count),
                "--query-rounds",
                str(options.sqlite_query_rounds),
                "--revision-write-sample-count",
                str(options.sqlite_revision_write_sample_count),
                "--resume-samples",
                str(options.history_resume_samples),
                *(("--preload-audit",) if options.history_preload_audit else ()),
                *(("--preload-cpu-profile",) if options.history_preload_cpu_profile else ()),
                *(("--preload-feature-audit",) if options.history_preload_feature_audit else ()),
                "--preload-profiles", ",".join(options.history_preload_profiles),
                "--preload-context-candidate", options.history_preload_context_candidate,
                "--runtime-config", str(options.runtime_config),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "history-publication":
        commands = (DiagnosticCommand(
            "history_publication_audit",
            (
                python_executable, "-m", "scripts.download.history.history_publication_audit",
                "--history-root", str(options.history_root), "--runtime-config", str(options.runtime_config),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "history-source-compare":
        commands = (DiagnosticCommand(
            "history_source_compare",
            (
                python_executable,
                "-m",
                "scripts.download.history.history_source_compare",
                "--history-root",
                str(options.history_root),
                "--codes",
                *options.codes,
                "--sessions",
                str(options.history_days),
                "--timeout-seconds",
                str(options.source_timeout_seconds),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "price-repair-audit":
        commands = (DiagnosticCommand(
            "price_repair_audit",
            (python_executable, "-m", "scripts.download.history.price_repair_audit", "--history-root", str(options.history_root), *(("--work-root", str(options.database_probe_directory)) if options.database_probe_directory is not None else ())),
            common_timeout,
        ),)
    elif options.action_identity == "history-write":
        commands = (DiagnosticCommand(
            "history_write_benchmark",
            (
                python_executable,
                "-m",
                "scripts.download.history.history_write_benchmark",
                "--size",
                str(
                    options.download_sizes[0]
                    if options.baseline_checkout is not None
                    else min(options.download_sizes[0], 64)
                ),
                "--sessions",
                str(options.history_days),
                "--runtime-config", str(options.runtime_config),
                *(("--publication-profile",) if options.history_publication_profile else ()),
                *(("--history-root", str(publication_root)) if options.history_publication_profile and publication_root is not None else ()),
            )
            + (
                ("--baseline-checkout", str(options.baseline_checkout)) if options.baseline_checkout is not None else ()
            )
            + (("--comparison-rounds", str(options.history_comparison_rounds)) if options.baseline_checkout is not None else ())
            + (("--flush-codes", str(options.history_flush_codes), "--flush-cells", str(options.history_flush_cells)) if options.history_flush_codes is not None else ()),
            common_timeout,
        ),)
    elif options.action_identity == "download-performance":
        commands = (DiagnosticCommand(
            "download_performance",
            (python_executable, "-m", "scripts.download.history.download_performance")
            + (
                ("--baseline-checkout", str(options.baseline_checkout)) if options.baseline_checkout is not None else ()
            ),
            common_timeout,
        ),)
    elif options.action_identity == "history-space":
        commands = (DiagnosticCommand(
            "history_space",
            (
                python_executable,
                "-m",
                "scripts.download.history.history_sqlite_performance",
                "--history-root",
                str(options.history_root),
                "--inventory-only",
            ),
            common_timeout,
        ),)
    elif options.action_identity == "history-industry":
        commands = (DiagnosticCommand(
            "history_industry",
            (
                python_executable,
                "-m",
                "scripts.download.history.history_industry_preparation",
                "--history-root",
                str(options.history_root),
                "--timeout-seconds",
                str(options.source_timeout_seconds),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "qfq-sqlite":
        commands = (DiagnosticCommand(
            "qfq_sqlite_audit",
            (
                python_executable,
                "-m",
                "scripts.download.history.qfq_sqlite",
                "--runtime-config", str(options.runtime_config),
                "--database",
                str(options.qfq_database),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "download-failures":
        commands = (DiagnosticCommand(
            "download_failures",
            (python_executable, "-m", "scripts.download.history.qfq_sqlite",
             "--database", str(options.qfq_database), "--failures-only",
             "--runtime-config", str(options.runtime_config)),
            common_timeout,
        ),)
    else:
        raise ValueError("unsupported download/history action")
    if options.action_identity in {
        "baostock-qfq-shadow", "tencent-download", "tencent-history-window",
        "history-source-compare", "price-repair-audit", "history-space",
        "history-industry", "download-performance",
    }:
        commands = tuple(replace(
            command, argv=command.argv + ("--runtime-config", str(options.runtime_config)),
        ) for command in commands)
    if options.database_probe_directory is not None and options.action_identity != "price-repair-audit":
        commands = tuple(replace(
            command,
            prepare=partial(PrepareDatabaseCommand, options.database_probe_directory, options.runtime_config, command.name),
        ) for command in commands)
    return tuple(replace(command, argv=WorkerArguments(command.argv)) for command in commands)


def _history_command(options: HistoryOptions, python_executable: str) -> tuple[str, ...]:
    return (
        python_executable,
        "-m",
        "scripts.download.history.history_sources",
        "--codes",
        *options.codes,
        "--samples",
        str(options.source_samples),
        "--workers",
        str(min(options.history_workers, len(options.codes))),
        "--days",
        str(options.history_days),
        "--source",
        options.history_source,
        "--tencent-history-host",
        options.tencent_history_host,
        "--timeout-seconds",
        str(options.source_timeout_seconds),
    )
