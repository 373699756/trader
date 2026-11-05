from __future__ import annotations

import sys
from dataclasses import replace

from scripts.checks.contracts import DiagnosticCommand, DiagnosticOptions
from scripts.checks.catalog import CheckByIdentity, WorkerArguments

def BuildCommands(
    options: DiagnosticOptions, *, python_executable: str = sys.executable
) -> tuple[DiagnosticCommand, ...]:
    definition = CheckByIdentity(options.check_id)
    if definition.worker is not None:
        raise ValueError("worker arguments must be passed through the unified CLI")
    common_timeout = options.command_timeout_seconds
    if not options.v5_kernel_probe and options.check_id not in {
        "disk",
        "v5-samples",
        "v5-lite",
        "training-samples",
        "snapshot",
        "research",
        "performance",
        "web",
        "browser",
    }:
        from trader.infra.settings import LoadRuntimeSettings

        paths = LoadRuntimeSettings(options.runtime_config).data_paths
        options = replace(
            options,
            history_root=options.history_root or paths.history,
            qfq_database=options.qfq_database or paths.daily_window,
            company_evidence_database=options.company_evidence_database or paths.company,
            company_collection_database=options.company_collection_database or paths.company_collection,
        )
    if options.check_id == "training-performance":
        from scripts.checks.training.training_performance import BuildTrainingBenchmarkCommands

        return tuple(replace(command, argv=WorkerArguments(command.argv)) for command in BuildTrainingBenchmarkCommands(options, python_executable))
    cli = (python_executable, "-m", "trader.entrypoints.cli", "--config", str(options.runtime_config))
    commands: dict[str, DiagnosticCommand] = {
        "training_samples": DiagnosticCommand(
            "training_samples",
            (python_executable, "-m", "scripts.checks.training.training_samples",
             "--work-root", str(options.training_probe_directory), "--baseline-checkout", str(options.baseline_checkout),
             "--config", str(options.runtime_config)),
            common_timeout,
        ),
        "v5_lite": DiagnosticCommand(
            "v5_lite",
            (
                python_executable,
                "-m",
                "scripts.checks.training.v5_lite",
                "--work-root",
                str(options.training_probe_directory),
            )
            + (("--candidate-config", str(options.v5_candidate_config)) if options.v5_candidate_config else ())
            + (("--sample-root", str(options.v5_sample_root)) if options.v5_sample_root else ()),
            common_timeout,
        ),
        "v5_samples": DiagnosticCommand(
            "v5_samples",
            (
                python_executable,
                "-m",
                "scripts.checks.training.v5_samples",
                "--work-root",
                str(options.training_probe_directory),
                "--config",
                str(options.runtime_config),
            )
            + (("--wide-sample-source", str(options.wide_sample_source)) if options.wide_sample_source else ()),
            common_timeout,
        ),
        "disk_profile": DiagnosticCommand(
            "disk_profile",
            (
                python_executable,
                "-m",
                "scripts.checks.infra.disk_profile",
                "--config",
                str(options.runtime_config),
            )
            + tuple(value for path in options.disk_paths for value in ("--path", str(path)))
            + (("--policy-audit",) if options.disk_policy_audit else ()),
            common_timeout,
        ),
        "training_coverage": DiagnosticCommand(
            "training_coverage",
            (
                python_executable,
                "-m",
                "scripts.checks.training.training_coverage",
                "--config",
                str(options.runtime_config),
            )
            + (("--signal-date", options.training_signal_date.isoformat()) if options.training_signal_date else ()),
            common_timeout,
        ),
        "model_inputs": DiagnosticCommand(
            "model_inputs",
            (
                python_executable,
                "-m",
                "scripts.checks.training.training_alignment",
                "--history-root",
                str(options.history_root),
                "--inputs-only",
                "--runtime-config",
                str(options.runtime_config),
            ),
            common_timeout,
        ),
        "training_alignment": DiagnosticCommand(
            "training_alignment",
            (
                python_executable,
                "-m",
                "scripts.checks.training.training_alignment",
                "--history-root",
                str(options.history_root),
                "--work-root",
                str(options.training_probe_directory),
                "--profiles",
                ",".join(options.training_profiles),
            ),
            common_timeout,
        ),
        "v5_training": DiagnosticCommand(
            "v5_training",
            (
                python_executable,
                "-m",
                "scripts.checks.training.v5_training",
                *(
                    ("--kernel-probe",)
                    if options.v5_kernel_probe
                    else ("--history-root", str(options.history_root), "--work-root", str(options.v5_probe_directory))
                ),
            ),
            common_timeout,
        ),
        "training_resources": DiagnosticCommand(
            "training_resources",
            (
                python_executable, "-m", "scripts.checks.training.training_resources",
                "--profiles", ",".join(options.training_profiles),
            ),
            min(common_timeout, 30.0),
        ),
        "configuration": DiagnosticCommand("configuration", cli + ("validate-config",), common_timeout),
        "history_maintenance": DiagnosticCommand(
            "history_maintenance", cli + ("history-automation-status",), common_timeout
        ),
        "download_failures": DiagnosticCommand(
            "download_failures",
            (python_executable, "-m", "scripts.checks.download.history.qfq_sqlite",
             "--database", str(options.qfq_database), "--failures-only",
             "--runtime-config", str(options.runtime_config)),
            common_timeout,
        ),
        "qfq_sqlite_audit": DiagnosticCommand(
            "qfq_sqlite_audit",
            (
                python_executable,
                "-m",
                "scripts.checks.download.history.qfq_sqlite",
                "--runtime-config", str(options.runtime_config),
                "--database",
                str(options.qfq_database),
            ),
            common_timeout,
        ),
        "tencent_history_window": DiagnosticCommand(
            "tencent_history_window",
            (
                python_executable,
                "-m",
                "scripts.checks.download.history.tencent_download",
                "--whole-window",
                "--history-root",
                str(options.history_root),
                "--codes",
                *options.codes,
                "--timeout-seconds",
                str(options.source_timeout_seconds),
            ),
            common_timeout,
        ),
        "tencent_download": DiagnosticCommand(
            "tencent_download",
            (
                python_executable,
                "-m",
                "scripts.checks.download.history.tencent_download",
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
        ),
        "long_watchlist_admission": DiagnosticCommand(
            "long_watchlist_admission",
            (
                python_executable,
                "-m",
                "scripts.checks.recommendation.long_watchlist",
                "--watchlist",
                str(options.long_watchlist),
                "--timeout-seconds",
                str(min(options.source_timeout_seconds, 15)),
                "--company-evidence-database",
                str(options.company_evidence_database),
                "--runtime-config",
                str(options.runtime_config),
                *(
                    argument
                    for path in options.eligibility_evidence
                    for argument in ("--eligibility-evidence", str(path))
                ),
                *(("--evidence-output", str(options.long_evidence_output)) if options.long_evidence_output else ()),
            ),
            common_timeout,
        ),
        "web_health": DiagnosticCommand(
            "web_health",
            (
                python_executable,
                "-m",
                "scripts.checks.recommendation.web_health",
                "--base-url",
                options.base_url,
                "--samples",
                str(options.web_samples),
                "--interval-seconds",
                str(options.web_interval_seconds),
                "--timeout-seconds",
                str(options.web_timeout_seconds),
                "--consecutive-zero-threshold",
                str(min(3, options.web_samples)),
            ),
            common_timeout,
        ),
        "history_sources": DiagnosticCommand(
            "history_sources",
            _history_command(options, python_executable),
            common_timeout,
        ),
        "exchange_security_master": DiagnosticCommand(
            "exchange_security_master",
            (
                python_executable,
                "-m",
                "scripts.checks.download.market.exchange_security_master",
                "--timeout-seconds",
                str(max(15.0, options.source_timeout_seconds)),
            ),
            common_timeout,
        ),
        "tencent_quotes": DiagnosticCommand(
            "tencent_quotes",
            (
                python_executable,
                "-m",
                "scripts.checks.download.market.tencent_quotes",
                *options.codes,
                "--samples",
                str(options.source_samples),
                "--interval-seconds",
                str(options.source_interval_seconds),
                "--timeout-seconds",
                str(options.source_timeout_seconds),
            ),
            common_timeout,
        ),
        "tencent_transport": DiagnosticCommand(
            "tencent_transport",
            (python_executable, "-m", "scripts.checks.download.market.tencent_quotes",
             "--transport-audit", "--runtime-config", str(options.runtime_config),
             "--timeout-seconds", str(options.source_timeout_seconds)),
            common_timeout,
        ),
        "tushare_daily": DiagnosticCommand(
            "tushare_daily",
            (
                python_executable,
                "-m",
                "scripts.checks.download.market.tushare_daily",
                "--runtime-config",
                str(options.runtime_config),
                "--codes",
                *options.codes,
                "--days",
                str(options.history_days),
            ),
            common_timeout,
        ),
        "history_daily_capability": DiagnosticCommand(
            "history_daily_capability",
            (
                python_executable,
                "-m",
                "scripts.checks.download.history.history_daily_capability",
                "--runtime-config",
                str(options.runtime_config),
            ),
            common_timeout,
        ),
        "baostock_concurrency": DiagnosticCommand(
            "baostock_concurrency",
            (
                python_executable,
                "-m",
                "scripts.checks.download.history.baostock_concurrency",
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
                    else ()
                ),
            ),
            common_timeout if options.baostock_serial_only else max(common_timeout, 1800.0),
        ),
        "history_publication_audit": DiagnosticCommand(
            "history_publication_audit",
            (
                python_executable, "-m", "scripts.checks.download.history.history_publication_audit",
                "--history-root", str(options.history_root), "--runtime-config", str(options.runtime_config),
            ),
            common_timeout,
        ),
        "history_sqlite_performance": DiagnosticCommand(
            "history_sqlite_performance",
            (
                python_executable,
                "-m",
                "scripts.checks.download.history.history_sqlite_performance",
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
                "--runtime-config", str(options.runtime_config),
            ),
            common_timeout,
        ),
        "baostock_qfq_shadow": DiagnosticCommand(
            "baostock_qfq_shadow",
            (
                python_executable,
                "-m",
                "scripts.checks.download.history.baostock_qfq_shadow",
                "--history-root",
                str(options.history_root),
            ),
            max(common_timeout, 600.0),
        ),
        "research_readiness": DiagnosticCommand(
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
        "browser_refresh": DiagnosticCommand(
            "browser_refresh",
            (
                python_executable,
                "-m",
                "scripts.checks.recommendation.browser_refresh",
                "--duration-seconds",
                str(options.browser_duration_seconds),
                "--minimum-updates",
                str(options.browser_minimum_updates),
                "--runtime-config",
                str(options.runtime_config),
            ),
            common_timeout,
        ),
        "browser_live": DiagnosticCommand(
            "browser_live",
            (python_executable, "-m", "scripts.checks.recommendation.browser_refresh", "--live-url", options.base_url),
            common_timeout,
        ),
        "history_write_benchmark": DiagnosticCommand(
            "history_write_benchmark",
            (
                python_executable,
                "-m",
                "scripts.checks.download.history.history_write_benchmark",
                "--size",
                str(
                    options.download_sizes[0]
                    if options.baseline_checkout is not None
                    else min(options.download_sizes[0], 64)
                ),
                "--sessions",
                str(options.history_days),
            )
            + (
                ("--baseline-checkout", str(options.baseline_checkout)) if options.baseline_checkout is not None else ()
            ),
            common_timeout,
        ),
        "company_ledger_browser": DiagnosticCommand(
            "company_ledger_browser",
            (python_executable, "-m", "scripts.checks.download.company.company_ledger_browser"),
            common_timeout,
        ),
        "download_performance": DiagnosticCommand(
            "download_performance",
            (python_executable, "-m", "scripts.checks.download.history.download_performance")
            + (
                ("--baseline-checkout", str(options.baseline_checkout)) if options.baseline_checkout is not None else ()
            ),
            common_timeout,
        ),
        "database_baseline": DiagnosticCommand(
            "database_baseline",
            (
                python_executable,
                "-m",
                "scripts.checks.infra.database_baseline",
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
        ),
        "company_resume": DiagnosticCommand(
            "company_resume",
            (
                python_executable,
                "-m",
                "scripts.checks.download.company.company_resume",
                "--database",
                str(options.company_collection_database),
                "--facts-database",
                str(options.company_evidence_database),
                *(("--directory", str(options.company_replay_directory)) if options.company_replay_directory else ()),
                "--budget-seconds",
                str(max(30.0, common_timeout - 30)),
                *(("--audit-only",) if options.company_query_audit else ()),
                *(("--database-only",) if options.company_database_audit else ()),
                *(("--pagination-only",) if options.company_pagination_audit else ()),
                *(("--pipeline-only",) if options.company_pipeline_audit else ()),
                *(("--ocr-only",) if options.company_ocr_audit else ()),
                *(("--document-only",) if options.company_document_audit else ()),
                *(("--directory-only",) if options.company_directory_audit else ()),
            ),
            common_timeout,
        ),
        "history_source_compare": DiagnosticCommand(
            "history_source_compare",
            (
                python_executable,
                "-m",
                "scripts.checks.download.history.history_source_compare",
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
        ),
        "price_repair_audit": DiagnosticCommand(
            "price_repair_audit",
            (python_executable, "-m", "scripts.checks.download.history.price_repair_audit", "--history-root", str(options.history_root), *(("--work-root", str(options.database_probe_directory)) if options.database_probe_directory is not None else ())),
            common_timeout,
        ),
        "history_space": DiagnosticCommand(
            "history_space",
            (
                python_executable,
                "-m",
                "scripts.checks.download.history.history_sqlite_performance",
                "--history-root",
                str(options.history_root),
                "--inventory-only",
            ),
            common_timeout,
        ),
        "history_industry": DiagnosticCommand(
            "history_industry",
            (
                python_executable,
                "-m",
                "scripts.checks.download.history.history_industry_preparation",
                "--history-root",
                str(options.history_root),
                "--timeout-seconds",
                str(options.source_timeout_seconds),
            ),
            common_timeout,
        ),
        "production_performance": DiagnosticCommand(
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
        "market_factor_compare": DiagnosticCommand(
            "market_factor_compare",
            (python_executable, "-m", "scripts.checks.download.market.market_factor_compare")
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
        ),
        "research_source_compare": DiagnosticCommand(
            "research_source_compare",
            (
                python_executable,
                "-m",
                "scripts.checks.download.company.research_source_compare",
                "--config",
                str(options.runtime_config),
                "--codes",
                *options.codes,
                "--timeout-seconds",
                str(options.source_timeout_seconds),
                "--source-loop-seconds",
                str(options.market_source_loop_seconds),
                "--task-seconds",
                str(options.market_task_seconds),
            ),
            min(common_timeout, options.market_task_seconds),
        ),
    }
    selected: list[DiagnosticCommand] = []
    selection = options.scoring_profile
    if options.check_id == "performance" and selection is None:
        selection = "v1,v2,v3,v5"
    for name in CheckByIdentity(options.check_id).commands:
        command = commands[name]
        if selection is None or name not in {
            "configuration",
            "research_readiness",
            "production_performance",
            "web_health",
        }:
            selected.append(command)
            continue
        arguments = ("--profile", selection)
        argv = (
            command.argv + arguments
            if name in {"production_performance", "web_health"}
            else command.argv[:-1] + arguments + command.argv[-1:]
        )
        selected.append(replace(command, name=f"{name}:{selection}", argv=argv))
    if options.database_probe_directory is not None and options.check_id != "price-repair-audit":
        selected = [
            replace(
                command,
                database_probe_directory=options.database_probe_directory,
                runtime_config=options.runtime_config,
            )
            for command in selected
        ]
    return tuple(replace(command, argv=WorkerArguments(command.argv)) for command in selected)


def _history_command(options: DiagnosticOptions, python_executable: str) -> tuple[str, ...]:
    return (
        python_executable,
        "-m",
        "scripts.checks.download.history.history_sources",
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
