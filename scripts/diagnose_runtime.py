#!/usr/bin/env python3
"""Run bounded Trader diagnostics through one command and emit a sanitized combined report."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.runtime_diagnostics.database_workspace import (  # noqa: E402
    DatabaseWorkerEnvironment,
    PrepareDatabaseProbeDirectory,
    ProjectDatabaseProbeLocation,
    RunDatabaseProbe,
)
from scripts.runtime_diagnostics.diagnostic_types import (  # noqa: E402
    CheckStatus,
    DiagnosticCommand,
    DiagnosticOptions,
    DiagnosticResult,
    Profile,
    Runner,
    ScoringProfileSelection,
)
from scripts.runtime_diagnostics.market_factor_budget import (  # noqa: E402
    MARKET_SOURCE_LOOP_LIMIT_SECONDS,
    MARKET_TASK_LIMIT_SECONDS,
)
from scripts.runtime_diagnostics.market_factor_text import RenderMarketFactorText  # noqa: E402
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS  # noqa: E402
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection  # noqa: E402

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DEFAULT_CONFIG = PROJECT_ROOT / "config" / "runtime.json"
_MARKET_SOURCES = ("tencent", "eastmoney", "sina", "baostock")

_PROFILE_CHECKS: Mapping[Profile, tuple[str, ...]] = {
    "storage": ("storage_profile",),
    "local": ("configuration", "history_maintenance", "research_readiness", "production_performance"),
    "web": ("web_health",),
    "history": ("history_sources",),
    "security-master": ("exchange_security_master",),
    "tencent": ("tencent_quotes",),
    "tushare": ("tushare_daily",),
    "history-daily-capability": ("history_daily_capability",),
    "baostock-concurrency": ("baostock_concurrency",),
    "baostock-qfq-shadow": ("baostock_qfq_shadow",),
    "tencent-download": ("tencent_download",),
    "tencent-history-window": ("tencent_history_window",),
    "v5-training": ("v5_training",),
    "v5-samples": ("v5_samples",),
    "v5-lite": ("v5_lite",),
    "training-alignment": ("training_alignment",),
    "training-coverage": ("training_coverage",),
    "training-performance": ("training_alignment",),
    "model-inputs": ("model_inputs",),
    "history-sqlite": ("history_sqlite_performance",),
    "history-source-compare": ("history_source_compare",),
    "market-factor-compare": ("market_factor_compare",),
    "research-source-compare": ("research_source_compare",),
    "history-write": ("history_write_benchmark",),
    "download-performance": ("download_performance",),
    "database-baseline": ("database_baseline",),
    "company-resume": ("company_resume",),
    "company-ledger": ("company_ledger_browser",),
    "history-space": ("history_space",),
    "history-industry": ("history_industry",),
    "qfq-sqlite": ("qfq_sqlite_audit",),
    "research": ("research_readiness",),
    "long-watchlist": ("long_watchlist_admission",),
    "browser": ("browser_refresh",),
    "performance": ("production_performance",),
    "runtime": ("web_health",),
    "sources": (
        "exchange_security_master",
        "history_sources",
        "tencent_quotes",
        "tushare_daily",
        "history_daily_capability",
    ),
    "live": (
        "web_health",
        "exchange_security_master",
        "history_sources",
        "tencent_quotes",
        "tushare_daily",
        "history_daily_capability",
    ),
    "full": (
        "web_health",
        "exchange_security_master",
        "history_sources",
        "tencent_quotes",
        "tushare_daily",
        "history_daily_capability",
        "browser_refresh",
        "production_performance",
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company-pagination-audit", action="store_true", help="read-only CNInfo page-size comparison")
    parser.add_argument("--history-preload-audit", action="store_true", help="read-only shared startup CPU profile")
    parser.add_argument(
        "--company-pipeline-audit", action="store_true", help="isolated same-PDF serial/parallel comparison"
    )
    parser.add_argument(
        "--company-query-audit", action="store_true", help="read-only bounded CNInfo query coverage audit"
    )
    parser.add_argument(
        "--company-storage-audit", action="store_true", help="read-only local company text and retry counts; no network"
    )
    parser.add_argument(
        "--company-replay-directory",
        type=Path,
        help="explicit repository-external directory for an isolated company backup and resumable evidence",
    )
    parser.add_argument(
        "--profile",
        choices=tuple(_PROFILE_CHECKS),
        default="live",
        help="single-check or combined runtime, research, sources, live, and full diagnostic profile",
    )
    parser.add_argument(
        "--tencent-history-host",
        choices=("proxy", "direct"),
        default="proxy",
        help="Tencent K-line host for bounded history probes",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:5000", help="running trader-server base URL")
    parser.add_argument(
        "--cpu-profile-dir", type=Path, help="external CPU evidence directory for market-factor-compare or performance"
    )
    parser.add_argument("--cpu-profile-mode", choices=("functions", "gc"), default="functions")
    parser.add_argument("--qfq-database", type=Path)
    parser.add_argument("--wide-sample-source", type=Path, help="bounded read-only V5 wide-cache storage comparison")
    parser.add_argument("--v5-sample-root", type=Path, help="sealed compact V5 cache for isolated lite comparison")
    parser.add_argument(
        "--storage-path", type=Path, action="append", default=[], help="additional read-only storage probe path"
    )
    parser.add_argument("--training-signal-date", type=date.fromisoformat, help="read-only V5 label horizon probe")
    parser.add_argument(
        "--training-profiles",
        type=ParseProfileSelection,
        default=SCORING_PROFILE_IDS,
        help="comma-separated profiles for training-alignment and training-performance",
    )
    parser.add_argument(
        "--baseline-checkout",
        type=Path,
        help="isolated checkout for history-write, download-performance or database-baseline comparisons",
    )
    parser.add_argument(
        "--scoring-profile",
        type=_scoring_profile_selection,
        default=None,
        help="one of v1/v2/v3/v5, or all four comma-separated profiles for performance",
    )
    parser.add_argument("--runtime-config", default=str(_DEFAULT_CONFIG), help="runtime JSON configuration")
    parser.add_argument(
        "--performance-replay-ticks",
        type=int,
        default=0,
        help="1..1000 continuous ticks per scenario instead of model microbenchmarks",
    )
    parser.add_argument(
        "--performance-replay-rows",
        type=int,
        help="bounded diagnostic population; requires replay ticks, excludes full-market acceptance",
    )
    parser.add_argument("--market-sources", nargs="+", choices=_MARKET_SOURCES, default=_MARKET_SOURCES)
    parser.add_argument("--market-profiles", nargs="+", choices=("v2", "v3"), default=("v2", "v3"))
    parser.add_argument("--market-sizes", nargs="+", type=int, choices=(0, 120, 720), default=(120, 720, 0))
    parser.add_argument("--market-mode", choices=("single", "combined", "all"), default="all")
    parser.add_argument("--market-batch-seconds", type=float, default=30)
    parser.add_argument(
        "--market-source-loop-seconds",
        type=float,
        default=MARKET_SOURCE_LOOP_LIMIT_SECONDS,
        help="cumulative test budget per market source, capped at 120 seconds",
    )
    parser.add_argument(
        "--market-task-seconds",
        type=float,
        default=MARKET_TASK_LIMIT_SECONDS,
        help="hard market comparison task budget, capped at 600 seconds",
    )
    parser.add_argument(
        "--codes",
        nargs="+",
        default=("600519", "300750", "688981"),
        help="one to 50 A-share codes, or up to 100 for baostock-concurrency",
    )
    parser.add_argument("--web-samples", type=int, default=3, help="Web status/current sample rounds")
    parser.add_argument("--web-interval-seconds", type=float, default=2.0, help="delay between Web samples")
    parser.add_argument("--source-samples", type=int, default=1, help="history and Tencent source sample rounds")
    parser.add_argument("--source-interval-seconds", type=float, default=1.0, help="delay between source route samples")
    parser.add_argument("--history-workers", type=int, default=5, help="history requests per bounded worker wave")
    parser.add_argument(
        "--history-days",
        type=int,
        default=61,
        help="history rows per stock; calendar-day window for BaoStock experiments",
    )
    parser.add_argument(
        "--history-source",
        choices=("composite", "tencent", "eastmoney"),
        default="composite",
        help="history route sampled by history/sources/live/full profiles",
    )
    parser.add_argument("--web-timeout-seconds", type=float, default=3.0, help="timeout per Web API request")
    parser.add_argument(
        "--source-timeout-seconds",
        type=float,
        default=4.5,
        help="vendor HTTP timeout; SDK socket timeout for BaoStock experiments",
    )
    parser.add_argument("--browser-duration-seconds", type=float, default=35.0, help="full-profile browser duration")
    parser.add_argument("--browser-minimum-updates", type=int, default=3, help="required browser DOM updates")
    parser.add_argument(
        "--command-timeout-seconds",
        type=float,
        default=180.0,
        help="hard wall-clock timeout for each child diagnostic",
    )
    parser.add_argument(
        "--history-root",
        type=Path,
        default=None,
        help="published annual SQLite data used by the read-only history-sqlite profile",
    )
    parser.add_argument(
        "--sqlite-page-sample-count",
        type=int,
        default=1,
        help="annual files sampled for expensive dbstat page classification",
    )
    parser.add_argument(
        "--history-resume-samples", type=int, default=0, help="only measure up to 8 pending history reads"
    )
    parser.add_argument(
        "--sqlite-query-rounds",
        type=int,
        default=3,
        help="cold and warm rounds per history-sqlite query workload",
    )
    parser.add_argument(
        "--sqlite-revision-write-sample-count",
        type=int,
        default=512,
        help="rows copied into the disposable revision batch-write probe",
    )
    parser.add_argument("--output", default="-", help="report output path outside the repository, or -")
    v5_probe = parser.add_mutually_exclusive_group()
    v5_probe.add_argument("--v5-probe-directory", type=Path, help="new external directory for isolated V5 training")
    v5_probe.add_argument(
        "--v5-kernel-probe", action="store_true", help="synthetic CPU kernel comparison; no history reads"
    )
    parser.add_argument("--training-probe-directory", type=Path, help="new external directory for all-profile training")
    parser.add_argument("--database-probe-directory", type=Path, help="new external directory for SQLite benchmarks")
    parser.add_argument(
        "--database-rounds", type=int, choices=range(1, 6), default=5, help="database baseline repetitions"
    )
    parser.add_argument("--database-acceptance-focus", choices=("history", "market-company"), default="history")
    parser.add_argument(
        "--format",
        choices=("auto", "json", "text"),
        default="auto",
        help="market-factor-compare defaults to terminal text when --output is -; other cases default to JSON",
    )
    parser.add_argument("--long-watchlist", type=Path, default=PROJECT_ROOT / "config/long_watchlist.json")
    parser.add_argument("--long-evidence-output", type=Path, help="stock evidence report outside the repository")
    parser.add_argument(
        "--company-evidence-database",
        type=Path,
        default=None,
        help="published company evidence database (read-only)",
    )
    parser.add_argument(
        "--company-collection-database", type=Path, default=None, help="raw company collection index (read-only source)"
    )
    parser.add_argument(
        "--eligibility-evidence",
        type=Path,
        action="append",
        default=[],
        help="read-only historical permanent-fact database (repeatable)",
    )
    parser.add_argument("--baostock-serial-only", action="store_true", help="single-session rate experiment")
    parser.add_argument("--baostock-intervals", nargs="+", type=float, default=(2.0,), help="candidate start intervals")
    parser.add_argument(
        "--baostock-sizes", nargs="+", type=int, default=(10, 50, 100), help="sample sizes within 1..100"
    )
    parser.add_argument("--baostock-rounds", type=int, default=1, help="serial experiment repetitions within 1..3")
    parser.add_argument(
        "--download-sizes", nargs="+", type=int, default=(10, 50, 100), help="Tencent updater sample sizes"
    )
    return parser


def _scoring_profile_selection(value: str) -> ScoringProfileSelection:
    try:
        profiles = ParseProfileSelection(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if len(profiles) == 1:
        return cast(ScoringProfileSelection, profiles[0])
    if set(profiles) == set(SCORING_PROFILE_IDS):
        return "v1,v2,v3,v5"
    raise argparse.ArgumentTypeError("select one scoring profile or all four profiles")


def _validate(args: argparse.Namespace) -> tuple[DiagnosticOptions, str]:
    if args.profile == "database-baseline" and args.download_sizes == (10, 50, 100):
        args.download_sizes = (8,)
    if args.database_probe_directory is not None and not args.database_probe_directory.expanduser().is_absolute():
        raise ValueError("--database-probe-directory must be absolute")
    if args.database_probe_directory is not None and args.profile not in {
        "database-baseline",
        "history-write",
        "history-sqlite",
        "download-performance",
    }:
        raise ValueError("--database-probe-directory requires a database performance profile")
    if args.profile == "database-baseline" and args.database_probe_directory is None:
        raise ValueError("database-baseline requires --database-probe-directory")
    if args.profile == "database-baseline" and not 5 <= args.history_days <= 2000:
        raise ValueError("database-baseline requires 5..2000 history days")
    if args.profile == "database-baseline" and (len(args.download_sizes) != 1 or not 1 <= args.download_sizes[0] <= 32):
        raise ValueError("database-baseline requires one download size in 1..32")
    if args.performance_replay_rows is not None and (
        args.performance_replay_rows < 1 or not args.performance_replay_ticks
    ):
        raise ValueError("--performance-replay-rows requires positive rows and --performance-replay-ticks")
    if not 0 <= args.performance_replay_ticks <= 1000 or (
        args.performance_replay_ticks and args.profile != "performance"
    ):
        raise ValueError("--performance-replay-ticks requires performance and a value within 0..1000")
    if args.scoring_profile == "v1,v2,v3,v5" and args.profile != "performance":
        raise ValueError("four-profile scoring is only available for performance")
    codes = tuple(dict.fromkeys(args.codes))
    if not codes or any(len(code) != 6 or not code.isdigit() for code in codes):
        raise ValueError("--codes must contain six-digit A-share codes")
    maximum_codes = 100 if args.profile == "baostock-concurrency" else 50
    if len(codes) > maximum_codes:
        raise ValueError(f"--codes accepts at most {maximum_codes} codes for this profile")
    positive = (
        args.web_samples,
        args.source_samples,
        args.history_workers,
        args.history_days,
        args.web_timeout_seconds,
        args.source_timeout_seconds,
        args.browser_duration_seconds,
        args.browser_minimum_updates,
        args.command_timeout_seconds,
    )
    if any(value <= 0 for value in positive):
        raise ValueError("sample, worker, duration and timeout values must be positive")
    if args.web_interval_seconds < 0 or args.source_interval_seconds < 0:
        raise ValueError("sample intervals must not be negative")
    if not 1 <= args.sqlite_page_sample_count <= 100:
        raise ValueError("--sqlite-page-sample-count must be within 1..100")
    if not 0 <= args.history_resume_samples <= 8:
        raise ValueError("--history-resume-samples must be within 0..8")
    if not 1 <= args.sqlite_query_rounds <= 9:
        raise ValueError("--sqlite-query-rounds must be within 1..9")
    if not 1 <= args.sqlite_revision_write_sample_count <= 5_000:
        raise ValueError("--sqlite-revision-write-sample-count must be within 1..5000")
    _validate_baostock_options(args)
    maximum_size = 5000 if args.profile == "history-write" and args.baseline_checkout is not None else 100
    if len(args.download_sizes) > 3 or any(not 1 <= size <= maximum_size for size in args.download_sizes):
        raise ValueError(f"--download-sizes accepts up to three sizes within 1..{maximum_size}")
    if len(set(args.market_sources)) != len(args.market_sources):
        raise ValueError("--market-sources must be distinct")
    if args.market_mode != "single" and "tencent" not in args.market_sources:
        raise ValueError("combined market comparisons require Tencent as the full-market primary")
    if not math.isfinite(args.market_batch_seconds) or not 0 < args.market_batch_seconds <= 120:
        raise ValueError("--market-batch-seconds must be within 0..120")
    if (
        not math.isfinite(args.market_source_loop_seconds)
        or not 0 < args.market_source_loop_seconds <= MARKET_SOURCE_LOOP_LIMIT_SECONDS
    ):
        raise ValueError("--market-source-loop-seconds must be within 0..120")
    if not math.isfinite(args.market_task_seconds) or not 0 < args.market_task_seconds <= MARKET_TASK_LIMIT_SECONDS:
        raise ValueError("--market-task-seconds must be within 0..600")
    long_evidence_output = (
        _external_path(args.long_evidence_output, "--long-evidence-output") if args.long_evidence_output else None
    )
    company_replay_directory = _external_path(args.company_replay_directory, "--company-replay-directory")
    if (
        sum(
            (
                args.company_query_audit,
                args.company_storage_audit,
                args.company_pagination_audit,
                args.company_pipeline_audit,
            )
        )
        > 1
    ):
        raise ValueError("company audit modes are mutually exclusive")
    if (
        args.profile == "company-resume"
        and company_replay_directory is None
        and not (args.company_query_audit or args.company_storage_audit or args.company_pagination_audit)
    ):
        raise ValueError("company-resume requires --company-replay-directory")
    output = args.output
    if output != "-":
        output = str(_external_path(Path(output), "--output"))
    output_format = (
        "text" if args.format == "auto" and args.profile == "market-factor-compare" and output == "-" else args.format
    )
    if output_format == "auto":
        output_format = "json"
    if output_format == "text" and args.profile != "market-factor-compare":
        raise ValueError("--format text is only available for market-factor-compare")
    if args.baseline_checkout is not None and args.profile not in {
        "history-write",
        "download-performance",
        "database-baseline",
        "training-performance",
    }:
        raise ValueError(
            "--baseline-checkout is only available for history-write, download-performance, database-baseline "
            "or training-performance"
        )
    if args.cpu_profile_dir is not None:
        if args.profile not in {"market-factor-compare", "performance"}:
            raise ValueError("--cpu-profile-dir requires market-factor-compare or performance")
        if args.profile == "performance" and args.performance_replay_ticks != 1:
            raise ValueError("--cpu-profile-dir requires --performance-replay-ticks 1")
    if args.cpu_profile_mode == "gc" and (args.profile != "performance" or args.cpu_profile_dir is None):
        raise ValueError("--cpu-profile-mode gc requires performance and --cpu-profile-dir")
    if args.v5_kernel_probe and args.profile != "v5-training":
        raise ValueError("--v5-kernel-probe requires --profile v5-training")
    if args.profile == "v5-training" and not args.v5_kernel_probe and args.v5_probe_directory is None:
        raise ValueError("v5-training requires --v5-probe-directory")
    if args.profile in {"v5-samples", "v5-lite"} and args.training_probe_directory is None:
        raise ValueError(f"{args.profile} requires --training-probe-directory")
    if args.wide_sample_source is not None and args.profile != "v5-samples":
        raise ValueError("--wide-sample-source requires --profile v5-samples")
    if args.v5_sample_root is not None and args.profile != "v5-lite":
        raise ValueError("--v5-sample-root requires --profile v5-lite")
    if args.profile in {"training-alignment", "training-performance"} and (
        args.training_probe_directory is None or args.history_root is None
    ):
        raise ValueError(f"{args.profile} requires --history-root and --training-probe-directory")
    return (
        DiagnosticOptions(
            training_signal_date=args.training_signal_date,
            training_profiles=args.training_profiles,
            profile=args.profile,
            database_probe_directory=_external_path(args.database_probe_directory, "--database-probe-directory")
            if args.database_probe_directory is not None
            else None,
            database_rounds=args.database_rounds,
            database_acceptance_focus=args.database_acceptance_focus,
            base_url=args.base_url,
            runtime_config=Path(args.runtime_config).expanduser().resolve(),
            storage_paths=tuple(path.expanduser().resolve() for path in args.storage_path),
            wide_sample_source=args.wide_sample_source.expanduser().resolve() if args.wide_sample_source else None,
            v5_sample_root=args.v5_sample_root.expanduser().resolve() if args.v5_sample_root else None,
            codes=codes,
            web_samples=args.web_samples,
            web_interval_seconds=args.web_interval_seconds,
            source_samples=args.source_samples,
            source_interval_seconds=args.source_interval_seconds,
            history_workers=args.history_workers,
            history_days=args.history_days,
            baseline_checkout=_external_path(args.baseline_checkout, "--baseline-checkout")
            if args.baseline_checkout is not None
            else None,
            history_source=args.history_source,
            tencent_history_host=args.tencent_history_host,
            web_timeout_seconds=args.web_timeout_seconds,
            source_timeout_seconds=args.source_timeout_seconds,
            browser_duration_seconds=args.browser_duration_seconds,
            browser_minimum_updates=args.browser_minimum_updates,
            command_timeout_seconds=args.command_timeout_seconds,
            history_root=args.history_root.expanduser().resolve() if args.history_root else None,
            sqlite_page_sample_count=args.sqlite_page_sample_count,
            history_resume_samples=args.history_resume_samples,
            sqlite_query_rounds=args.sqlite_query_rounds,
            sqlite_revision_write_sample_count=args.sqlite_revision_write_sample_count,
            baostock_serial_only=args.baostock_serial_only,
            baostock_intervals=tuple(args.baostock_intervals),
            baostock_sizes=tuple(args.baostock_sizes),
            baostock_rounds=args.baostock_rounds,
            long_watchlist=args.long_watchlist,
            long_evidence_output=long_evidence_output,
            company_evidence_database=args.company_evidence_database,
            company_collection_database=args.company_collection_database,
            company_replay_directory=company_replay_directory,
            company_query_audit=args.company_query_audit,
            company_storage_audit=args.company_storage_audit,
            history_preload_audit=args.history_preload_audit,
            company_pagination_audit=args.company_pagination_audit,
            company_pipeline_audit=args.company_pipeline_audit,
            v5_kernel_probe=args.v5_kernel_probe,
            v5_probe_directory=_external_path(args.v5_probe_directory, "--v5-probe-directory")
            if args.v5_probe_directory is not None
            else None,
            training_probe_directory=_external_path(args.training_probe_directory, "--training-probe-directory")
            if args.training_probe_directory is not None
            else None,
            eligibility_evidence=tuple(args.eligibility_evidence),
            download_sizes=tuple(args.download_sizes),
            qfq_database=args.qfq_database.expanduser().resolve() if args.qfq_database else None,
            scoring_profile=args.scoring_profile,
            performance_replay_ticks=args.performance_replay_ticks,
            performance_replay_rows=args.performance_replay_rows,
            market_sources=tuple(args.market_sources),
            market_profiles=tuple(args.market_profiles),
            market_sizes=tuple(args.market_sizes),
            market_mode=args.market_mode,
            market_batch_seconds=args.market_batch_seconds,
            market_source_loop_seconds=args.market_source_loop_seconds,
            market_task_seconds=args.market_task_seconds,
            output_format=output_format,
            cpu_profile_dir=_external_path(args.cpu_profile_dir, "--cpu-profile-dir")
            if args.cpu_profile_dir is not None
            else None,
            cpu_profile_mode=args.cpu_profile_mode,
        ),
        output,
    )


def _validate_baostock_options(args: argparse.Namespace) -> None:
    import math

    if not 1 <= args.baostock_rounds <= 3 or any(not 1 <= size <= 100 for size in args.baostock_sizes):
        raise ValueError("BaoStock rounds/sizes must be within 1..3 and 1..100")
    if len(args.baostock_intervals) > 3 or any(
        not math.isfinite(value) or value < 1 for value in args.baostock_intervals
    ):
        raise ValueError("BaoStock accepts at most three finite intervals of at least 1 second")
    if len(set(args.baostock_intervals)) != len(args.baostock_intervals) or len(args.baostock_sizes) > 3:
        raise ValueError("BaoStock intervals must be unique and at most three sizes are allowed")
    if not args.baostock_serial_only and (tuple(args.baostock_intervals) != (2.0,) or args.baostock_rounds != 1):
        raise ValueError("BaoStock interval/round comparisons require --baostock-serial-only")


def _external_path(value: Path | None, option: str) -> Path | None:
    if value is None:
        return None
    if not value.expanduser().is_absolute():
        raise ValueError(f"{option} must be an absolute path outside the repository")
    resolved = value.expanduser().resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise ValueError(f"{option} must be an absolute path outside the repository")
    return resolved


def build_commands(
    options: DiagnosticOptions, *, python_executable: str = sys.executable
) -> tuple[DiagnosticCommand, ...]:
    common_timeout = options.command_timeout_seconds
    if not options.v5_kernel_probe and options.profile not in {
        "storage",
        "v5-samples",
        "v5-lite",
        "local",
        "research",
        "performance",
        "web",
        "runtime",
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
    if options.profile == "training-performance":
        from scripts.runtime_diagnostics.training_performance import BuildTrainingBenchmarkCommands

        return BuildTrainingBenchmarkCommands(options, python_executable)
    cli = (python_executable, "-m", "trader.entrypoints.cli", "--config", str(options.runtime_config))
    commands: dict[str, DiagnosticCommand] = {
        "v5_lite": DiagnosticCommand(
            "v5_lite",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.v5_lite",
                "--work-root",
                str(options.training_probe_directory),
            )
            + (("--sample-root", str(options.v5_sample_root)) if options.v5_sample_root else ()),
            common_timeout,
        ),
        "v5_samples": DiagnosticCommand(
            "v5_samples",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.v5_samples",
                "--work-root",
                str(options.training_probe_directory),
                "--config",
                str(options.runtime_config),
            )
            + (("--wide-sample-source", str(options.wide_sample_source)) if options.wide_sample_source else ()),
            common_timeout,
        ),
        "storage_profile": DiagnosticCommand(
            "storage_profile",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.storage_profile",
                "--config",
                str(options.runtime_config),
            )
            + tuple(value for path in options.storage_paths for value in ("--path", str(path))),
            common_timeout,
        ),
        "training_coverage": DiagnosticCommand(
            "training_coverage",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.training_coverage",
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
                "scripts.runtime_diagnostics.training_alignment",
                "--history-root",
                str(options.history_root),
                "--inputs-only",
            ),
            common_timeout,
        ),
        "training_alignment": DiagnosticCommand(
            "training_alignment",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.training_alignment",
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
                "scripts.runtime_diagnostics.v5_training",
                *(
                    ("--kernel-probe",)
                    if options.v5_kernel_probe
                    else ("--history-root", str(options.history_root), "--work-root", str(options.v5_probe_directory))
                ),
            ),
            common_timeout,
        ),
        "configuration": DiagnosticCommand("configuration", cli + ("validate-config",), common_timeout),
        "history_maintenance": DiagnosticCommand(
            "history_maintenance", cli + ("history-automation-status",), common_timeout
        ),
        "qfq_sqlite_audit": DiagnosticCommand(
            "qfq_sqlite_audit",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.qfq_sqlite",
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
                "scripts.runtime_diagnostics.tencent_download",
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
                "scripts.runtime_diagnostics.tencent_download",
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
                "scripts.runtime_diagnostics.long_watchlist",
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
                "scripts.runtime_diagnostics.web_health",
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
                "scripts.runtime_diagnostics.exchange_security_master",
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
                "scripts.runtime_diagnostics.tencent_quotes",
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
        "tushare_daily": DiagnosticCommand(
            "tushare_daily",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.tushare_daily",
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
                "scripts.runtime_diagnostics.history_daily_capability",
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
                "scripts.runtime_diagnostics.baostock_concurrency",
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
        "history_sqlite_performance": DiagnosticCommand(
            "history_sqlite_performance",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.history_sqlite_performance",
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
            ),
            common_timeout,
        ),
        "baostock_qfq_shadow": DiagnosticCommand(
            "baostock_qfq_shadow",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.baostock_qfq_shadow",
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
                "scripts.runtime_diagnostics.browser_refresh",
                "--duration-seconds",
                str(options.browser_duration_seconds),
                "--minimum-updates",
                str(options.browser_minimum_updates),
                "--runtime-config",
                str(options.runtime_config),
            ),
            common_timeout,
        ),
        "history_write_benchmark": DiagnosticCommand(
            "history_write_benchmark",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.history_write_benchmark",
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
            (python_executable, "-m", "scripts.runtime_diagnostics.company_ledger_browser"),
            common_timeout,
        ),
        "download_performance": DiagnosticCommand(
            "download_performance",
            (python_executable, "-m", "scripts.runtime_diagnostics.download_performance")
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
                "scripts.runtime_diagnostics.database_baseline",
                "--size",
                str(options.download_sizes[0]),
                "--sessions",
                str(options.history_days),
                "--rounds",
                str(options.database_rounds),
            )
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
                "scripts.runtime_diagnostics.company_resume",
                "--database",
                str(options.company_collection_database),
                "--facts-database",
                str(options.company_evidence_database),
                *(("--directory", str(options.company_replay_directory)) if options.company_replay_directory else ()),
                "--budget-seconds",
                str(max(30.0, common_timeout - 30)),
                *(("--audit-only",) if options.company_query_audit else ()),
                *(("--storage-only",) if options.company_storage_audit else ()),
                *(("--pagination-only",) if options.company_pagination_audit else ()),
                *(("--pipeline-only",) if options.company_pipeline_audit else ()),
            ),
            common_timeout,
        ),
        "history_source_compare": DiagnosticCommand(
            "history_source_compare",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.history_source_compare",
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
        "history_space": DiagnosticCommand(
            "history_space",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.history_sqlite_performance",
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
                "scripts.runtime_diagnostics.history_industry_preparation",
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
            (python_executable, "-m", "scripts.runtime_diagnostics.market_factor_compare")
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
                "scripts.runtime_diagnostics.research_source_compare",
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
    if options.profile == "performance" and selection is None:
        selection = "v1,v2,v3,v5"
    for name in _PROFILE_CHECKS[options.profile]:
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
    if options.database_probe_directory is not None:
        selected = [
            replace(
                command,
                database_probe_directory=options.database_probe_directory,
                runtime_config=options.runtime_config,
            )
            for command in selected
        ]
    return tuple(selected)


def _history_command(options: DiagnosticOptions, python_executable: str) -> tuple[str, ...]:
    return (
        python_executable,
        "-m",
        "scripts.runtime_diagnostics.history_sources",
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


def execute_command(command: DiagnosticCommand) -> DiagnosticResult:
    started = time.monotonic()
    location = None
    try:
        argv = command.argv
        if command.database_probe_directory is not None:
            if command.runtime_config is None:
                raise ValueError("database probe requires runtime configuration")
            location = PrepareDatabaseProbeDirectory(command.database_probe_directory, command.runtime_config)
            environment = DatabaseWorkerEnvironment(location.directory)
            if command.name in {"history_write_benchmark", "database_baseline"}:
                argv += ("--storage-kind", location.storage_kind)
            completed = RunDatabaseProbe(argv, timeout=command.timeout_seconds, environment=environment)
        else:
            completed = subprocess.run(
                argv,
                cwd=PROJECT_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=command.timeout_seconds,
            )
    except subprocess.TimeoutExpired:
        return DiagnosticResult(command.name, 124, _elapsed_ms(started), None, "command_timeout")
    except OSError:
        return DiagnosticResult(command.name, 126, _elapsed_ms(started), None, "command_launch_failed")
    except UnicodeError:
        return DiagnosticResult(command.name, 1, _elapsed_ms(started), None, "invalid_output_encoding")
    except ValueError:
        return DiagnosticResult(command.name, 2, _elapsed_ms(started), None, "database_probe_directory_invalid")
    try:
        decoded = json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError):
        return DiagnosticResult(command.name, completed.returncode, _elapsed_ms(started), None, "invalid_json")
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        return DiagnosticResult(command.name, completed.returncode, _elapsed_ms(started), None, "invalid_json_root")
    if location is not None:
        summary = dict(_mapping(decoded.get("summary")))
        summary["database_workspace"] = ProjectDatabaseProbeLocation(location)
        decoded["summary"] = summary
    return DiagnosticResult(command.name, completed.returncode, _elapsed_ms(started), decoded, None)


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 1)


def run_diagnostics(
    profile: Profile,
    commands: Sequence[DiagnosticCommand],
    *,
    runner: Runner = execute_command,
) -> dict[str, object]:
    results: list[DiagnosticResult] = []
    configuration_failed = False
    for command in commands:
        name = command.name.split(":")[0]
        if profile == "local" and configuration_failed and name != "configuration":
            results.append(
                DiagnosticResult(
                    command.name,
                    0,
                    0.0,
                    {"status": "degraded", "executed": False, "reason": "configuration_invalid"},
                    None,
                )
            )
            continue
        result = runner(command)
        results.append(result)
        if profile == "local" and name == "configuration" and _status(result) == "failed":
            configuration_failed = True
    return build_report(profile, results)


def build_report(profile: Profile, results: Sequence[DiagnosticResult]) -> dict[str, object]:
    checks = [_check_payload(result, local=profile == "local") for result in results]
    statuses = [check["status"] for check in checks]
    summary = {
        "passed": statuses.count("passed"),
        "degraded": statuses.count("degraded"),
        "failed": statuses.count("failed"),
        "total": len(statuses),
    }
    report: dict[str, object] = {
        "schema_version": "trader-runtime-diagnostics",
        "status": "failed" if summary["failed"] else "degraded" if summary["degraded"] else "passed",
        "collected_at": datetime.now(_SHANGHAI).isoformat(),
        "profile": profile,
        "summary": summary,
        "findings": [finding for check in checks for finding in _findings(check)],
        "checks": checks,
    }
    if profile == "training-performance":
        from scripts.runtime_diagnostics.training_performance import SummarizeTrainingBenchmark

        benchmark = SummarizeTrainingBenchmark(results)
        report["benchmark"] = benchmark
        if not benchmark["comparable"] and report["status"] == "passed":
            report["status"] = "degraded"
    return report


def _check_payload(result: DiagnosticResult, *, local: bool = False) -> dict[str, object]:
    original_name = result.name
    result = replace(result, name=result.name.split(":")[0])
    if result.payload is not None and result.payload.get("executed") is False:
        return {
            "name": original_name,
            "status": "degraded",
            "executed": False,
            "reason": result.payload.get("reason"),
            "duration_ms": 0.0,
        }
    status = _status(result)
    if (
        local
        and result.name == "research_readiness"
        and result.error_code is None
        and result.payload is not None
        and _valid_research_status(result.payload)
        and result.return_code in (0, 1)
        and status == "failed"
    ):
        status = "degraded"
    payload: dict[str, object] = {
        "name": original_name,
        "status": status,
        "executed": True,
        "duration_ms": result.duration_ms,
        "schema_version": result.payload.get("schema_version") if result.payload is not None else None,
    }
    if result.error_code is not None:
        payload["error_code"] = result.error_code
    source = result.payload or {}
    handler = _CHECK_DETAILS.get(result.name)
    if handler is not None:
        handler(result, source, payload)
    findings = payload.get("findings")
    if local and status == "degraded" and isinstance(findings, list):
        payload["findings"] = [dict(item, severity="warning") for item in findings if isinstance(item, dict)]
    return payload


def _web_health_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = _mapping(source.get("summary"))
    payload["findings"] = _bounded_findings(source.get("findings"))
    samples = source.get("samples")
    if isinstance(samples, list) and samples and isinstance(samples[-1], dict):
        latest = samples[-1]
        payload["latest_runtime"] = {
            "runtime_status": latest.get("runtime_status"),
            "runtime_version": latest.get("runtime_version"),
            "phase": latest.get("phase"),
            "degraded_reasons": _safe_string_list(latest.get("degraded_reasons"), limit=32),
            "recent_errors": [
                {
                    key: issue.get(key)
                    for key in (
                        "code",
                        "strategy",
                        "stage",
                        "severity",
                        "count",
                        "recovery_status",
                        "occurred_at",
                        "last_occurred_at",
                        "resolved_at",
                    )
                }
                for issue in latest.get("recent_errors", [])[:32]
                if isinstance(issue, dict)
            ]
            if isinstance(latest.get("recent_errors"), list)
            else [],
            "scoring_profile": _scoring_profile_summary(latest.get("scoring_profile")),
            "candidate_quote_age": _mapping(_mapping(latest.get("market")).get("candidate_quote_age")),
            "history_archive": _mapping(_mapping(latest.get("market")).get("history_archive")),
            "company_research": _mapping(latest.get("company_research")),
            "strategies": _mapping(latest.get("strategies")),
        }


def _scoring_profile_summary(value: object) -> dict[str, object]:
    profile = _mapping(value)
    heads = _mapping(profile.get("heads"))
    projected_heads: dict[str, object] = {}
    for strategy in ("tomorrow", "d25"):
        head = _mapping(heads.get(strategy))
        if not head:
            continue
        projected_heads[strategy] = {
            "profile_id": _safe_text(head.get("profile_id")),
            "is_enable": head.get("is_enable") if isinstance(head.get("is_enable"), bool) else None,
            "model_id": _safe_text(head.get("model_id")),
            "model_hash": _safe_text(head.get("model_hash")),
            "score_scale": _safe_text(head.get("score_scale")),
            "production_authority": head.get("production_authority")
            if isinstance(head.get("production_authority"), bool)
            else None,
            "training_label_cutoff": _safe_text(head.get("training_label_cutoff")),
            "request_count": _safe_nonnegative_int(head.get("request_count")),
            "candidate_count": _safe_nonnegative_int(head.get("candidate_count")),
            "predictor_batch_count": _safe_nonnegative_int(head.get("predictor_batch_count")),
            "cache_hit_count": _safe_nonnegative_int(head.get("cache_hit_count")),
        }
    return {"profile_id": _safe_text(profile.get("profile_id")), "heads": projected_heads}


def _history_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = _mapping(source.get("summary"))


def _security_master_details(
    _result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]
) -> None:
    payload["summary"] = _mapping(source.get("summary")) or {"error": source.get("error")}
    if source.get("benchmark_mode") is not None:
        payload["benchmark_mode"] = source["benchmark_mode"]


def _tencent_quote_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    versions = _mapping(source.get("distinct_source_versions"))
    payload["summary"] = {
        "latency": _mapping(source.get("latency")),
        "tracked_codes": len(versions),
        "changing_codes": sum(isinstance(value, int) and value > 1 for value in versions.values()),
        "source_changed": source.get("source_changed"),
    }


def _tushare_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    summary = _mapping(source.get("summary"))
    payload["summary"] = {
        "requested_codes": summary.get("requested_codes"),
        "successful_codes": summary.get("successful_codes"),
        "latency_ms": summary.get("latency_ms"),
        "degraded_reason": summary.get("degraded_reason"),
        "capability": _mapping(source.get("capability")),
        "usage": _mapping(source.get("usage")),
    }


def _history_daily_capability_details(
    _result: DiagnosticResult,
    source: Mapping[str, object],
    payload: dict[str, object],
) -> None:
    candidates = source.get("candidates")
    candidate_values = candidates if isinstance(candidates, list) else []
    payload["summary"] = {
        "decision": _mapping(source.get("decision")),
        "baostock_lower_bound": _mapping(source.get("baostock_lower_bound")),
        "candidates": [
            {
                "source": item.get("source"),
                "request_scope": item.get("request_scope"),
                "market_day_batch": item.get("market_day_batch"),
                "market_day_raw": item.get("market_day_raw"),
                "adjustment_factor": item.get("adjustment_factor"),
                "raw_qfq_semantics_observed": item.get("raw_qfq_semantics_observed"),
                "probe_status": item.get("probe_status"),
                "probe_error": item.get("probe_error"),
            }
            for item in candidate_values[:10]
            if isinstance(item, dict)
        ],
    }


def _baostock_concurrency_details(
    _result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]
) -> None:
    payload["production_eligible"] = False
    payload["parallel_sessions"] = source.get("parallel_sessions")
    payload["history_calendar_days"] = source.get("history_calendar_days")
    payload["rate_semantics"] = source.get("rate_semantics")
    payload["retry_policy"] = source.get("retry_policy")
    payload["error"] = _safe_text(source.get("error"))
    values = source.get("experiments")
    fields = (
        "mode",
        "sample_size",
        "success_count",
        "failure_count",
        "success_rate",
        "network_receive_errors",
        "timeouts",
        "retries",
        "raw_rows",
        "qfq_rows",
        "raw_qfq_inconsistencies",
        "error_categories",
        "elapsed_ms",
        "average_code_ms",
        "successful_codes_per_minute",
        "interval_seconds",
        "round",
    )
    payload["experiments"] = [
        {name: item.get(name) for name in fields}
        for item in (values[:27] if isinstance(values, list) else [])
        if isinstance(item, dict)
    ]


def _history_sqlite_performance_details(
    _result: DiagnosticResult,
    source: Mapping[str, object],
    payload: dict[str, object],
) -> None:
    payload["summary"] = _mapping(source.get("summary"))


def _summary_with_findings(
    _result: DiagnosticResult,
    source: Mapping[str, object],
    payload: dict[str, object],
) -> None:
    payload["summary"] = _mapping(source.get("summary"))
    payload["findings"] = _bounded_findings(source.get("findings"))


def _research_source_compare_details(
    _result: DiagnosticResult,
    source: Mapping[str, object],
    payload: dict[str, object],
) -> None:
    payload["market_pagination"] = _mapping(source.get("market_pagination"))
    payload["structured_samples"] = source.get("structured_samples", [])
    sources = source.get("sources")
    source_values = sources if isinstance(sources, list) else []
    projected_sources: list[dict[str, object]] = []
    for item in source_values[:4]:
        if not isinstance(item, dict):
            continue
        samples = item.get("samples")
        sample_values = samples if isinstance(samples, list) else []
        missing_field_counts: dict[str, int] = {}
        component_error_counts: dict[str, int] = {}
        for sample in sample_values[:50]:
            if not isinstance(sample, dict):
                continue
            for field in _safe_string_list(sample.get("missing_fields"), limit=12):
                missing_field_counts[field] = missing_field_counts.get(field, 0) + 1
            for error in _safe_string_list(sample.get("component_errors"), limit=12):
                component_error_counts[error] = component_error_counts.get(error, 0) + 1
        projected_sources.append(
            {
                "source": _safe_text(item.get("source")),
                "tested": item.get("tested") is True,
                "excluded_by_budget": item.get("excluded_by_budget") is True,
                "physical_http_requests": _safe_nonnegative_int(item.get("physical_http_requests")),
                "sample_count": _safe_nonnegative_int(item.get("sample_count")),
                "field_coverage": _mapping(item.get("field_coverage")),
                "interface_status": _safe_text(item.get("interface_status")),
                "production_role": _safe_text(item.get("production_role")),
                "regulatory_history_complete": item.get("regulatory_history_complete") is True,
                "exchange_cross_check_status": _safe_text(item.get("exchange_cross_check_status")),
                "missing_field_counts": missing_field_counts,
                "component_error_counts": component_error_counts,
            }
        )
    payload["summary"] = {
        "required_fields": _safe_string_list(source.get("required_fields"), limit=12),
        "sources": projected_sources,
        "selected_route": _mapping(source.get("selected_route")),
        "budget": _mapping(source.get("budget")),
    }


def _baostock_qfq_shadow_details(
    _result: DiagnosticResult,
    source: Mapping[str, object],
    payload: dict[str, object],
) -> None:
    payload["production_eligible"] = source.get("production_eligible") is True
    payload["summary"] = {
        "published_snapshot": _mapping(source.get("published_snapshot")),
        "target_window": _mapping(source.get("target_window")),
        "raw_qfq_integrity": _mapping(source.get("raw_qfq_integrity")),
        "request_baseline": _mapping(source.get("request_baseline")),
        "production_eligibility_reason": source.get("production_eligibility_reason"),
    }


def _research_details(result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    if result.payload is not None and not _valid_research_status(source):
        payload["findings"] = [
            {
                "severity": "error",
                "code": "research_status_shape_invalid",
                "message": "The research status schema or active window projection is invalid.",
            }
        ]
        return
    history = _mapping(source.get("history_archive"))
    tomorrow = _mapping(source.get("tomorrow_research"))
    input_blockers = _safe_string_list(tomorrow.get("input_blockers"), limit=20)
    production_blockers = _safe_string_list(tomorrow.get("production_blockers"), limit=20)
    target = _mapping(source.get("target_selection"))
    target_blockers = _safe_string_list(target.get("blockers"), limit=20)
    blockers = target_blockers + input_blockers + production_blockers
    if blockers:
        payload["findings"] = [
            {
                "severity": "error",
                "code": blockers[0],
                "message": "Research requires qualified evidence for its declared selection identity.",
                "evidence": {
                    "target_blockers": target_blockers,
                    "input_blockers": input_blockers,
                    "production_blockers": production_blockers,
                    "production_authority": tomorrow.get("production_authority"),
                },
            }
        ]
    payload["summary"] = {
        "target_selection": {
            "research_identity": target.get("research_identity"),
            "status": target.get("status"),
            "strategies": _safe_string_list(target.get("strategies"), limit=2),
            "population": target.get("population"),
            "validation_command": target.get("validation_command"),
            "blockers": target_blockers,
            "production_authority": target.get("production_authority"),
        },
        "history_archive": {
            "state": history.get("state"),
            "snapshot_hash": history.get("snapshot_hash"),
            "data_cutoff": history.get("data_cutoff"),
            "label_cutoff": history.get("label_cutoff"),
            "calendar_sessions": history.get("calendar_sessions"),
            "universe_count": history.get("universe_count"),
            "partition_count": history.get("partition_count"),
            "reason": history.get("reason"),
            "production_authority": history.get("production_authority"),
            "point_in_time_parity": history.get("point_in_time_parity"),
        },
        "v3": {
            "status": tomorrow.get("status"),
            "next_stage": tomorrow.get("next_stage"),
            "input_prerequisite_status": tomorrow.get("input_prerequisite_status"),
            "input_blockers": input_blockers,
            "production_blockers": production_blockers,
            "production_readiness": tomorrow.get("production_readiness"),
            "production_authority": tomorrow.get("production_authority"),
        },
        "production_authority": tomorrow.get("production_authority"),
    }


def _browser_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = {
        "passed": source.get("passed"),
        "browser_dom": _mapping(_mapping(source.get("browser_dom")).get("summary")),
        "patch_to_paint": _mapping(source.get("browser_patch_to_paint")),
        "decision_patch": _mapping(source.get("browser_decision_patch")),
        "retention": _mapping(source.get("web_snapshot_retention")),
    }


def _performance_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = {
        "workload": _mapping(source.get("workload")),
        "measurements": _mapping(source.get("measurements")),
        "memory": _mapping(source.get("memory")),
        "identity": _mapping(source.get("identity")),
        "network_calls": source.get("network_calls"),
        "model_workload": _mapping(source.get("model_workload")),
        "profile_results": _mapping(source.get("profile_results")),
        "failures": _safe_string_list(source.get("failures"), limit=30),
    }
    if isinstance(source.get("continuous_pipeline"), Mapping):
        summary = cast(dict[str, object], payload["summary"])
        summary["continuous_pipeline"] = _mapping(source["continuous_pipeline"])


def _long_admission_details(
    _result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]
) -> None:
    summary = _mapping(source.get("summary"))
    payload["summary"] = {
        key: summary.get(key) for key in ("requested", "retained", "excluded", "pending", "collected_at", "coverage")
    }


def _company_resume_details(
    _result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]
) -> None:
    summary = _mapping(source.get("summary"))
    selected = {
        key: summary.get(key)
        for key in (
            "isolated_backup",
            "implementation_digest",
            "selected_slices",
            "completed_slices",
            "covered_slices",
            "saved_pages_replayed",
            "document_sample_limit",
        )
    }
    runs = summary.get("runs")
    pipeline = _mapping(summary.get("pipeline_audit"))
    if pipeline:
        pipeline_runs = pipeline.get("runs")
        selected["pipeline_audit"] = {
            "isolated_sample": pipeline.get("isolated_sample"),
            "equivalent_facts": pipeline.get("equivalent_facts"),
            "runs": [
                {
                    key: row.get(key)
                    for key in (
                        "workers",
                        "sample_documents",
                        "attempts",
                        "pending_documents",
                        "published_codes",
                        "seconds",
                        "first_publication_seconds",
                        "resume_changed_rows",
                        "resume_pending_documents",
                        "resume_attempts",
                        "failures",
                        "facts_digest",
                    )
                }
                for row in (pipeline_runs[:2] if isinstance(pipeline_runs, list) else [])
                if isinstance(row, dict)
            ],
        }
    storage = _mapping(summary.get("local_storage"))
    compaction = _mapping(summary.get("compaction"))
    if compaction:
        selected["compaction"] = {
            key: compaction.get(key)
            for key in (
                "isolated_copy",
                "before_bytes",
                "after_bytes",
                "removed_completed_members",
                "retained_data_equal",
            )
        }
    if storage:
        projected_storage = {
            key: storage.get(key)
            for key in (
                "read_only",
                "pdf_archived",
                "database_bytes",
                "free_bytes",
                "announcement_records",
                "extracted_bodies",
                "retained_bodies",
                "pending_ranges",
                "pending_documents",
                "stored_retry_cooling",
                "stored_retry_ready",
            )
        }
        tables = storage.get("table_bytes")
        projected_storage["table_bytes"] = [
            {"name": row.get("name"), "bytes": row.get("bytes")}
            for row in (tables[:64] if isinstance(tables, list) else [])
            if isinstance(row, dict)
        ]
        formal = _mapping(storage.get("formal"))
        projected_formal = {key: formal.get(key) for key in ("available", "database_bytes", "stocks")}
        formal_tables = formal.get("table_bytes")
        projected_formal["table_bytes"] = [
            {"name": row.get("name"), "bytes": row.get("bytes")}
            for row in (formal_tables[:64] if isinstance(formal_tables, list) else [])
            if isinstance(row, dict)
        ]
        projected_storage["formal"] = projected_formal
        for name, count_key in (("partition_failures", "ranges"), ("document_failures", "documents")):
            rows = storage.get(name)
            projected_storage[name] = [
                {"failure": row.get("failure"), count_key: row.get(count_key)}
                for row in (rows[:64] if isinstance(rows, list) else [])
                if isinstance(row, dict)
            ]
        selected["local_storage"] = projected_storage
    selected["runs"] = (
        [
            {
                key: run.get(key)
                for key in (
                    "interrupted",
                    "pages",
                    "document_attempts",
                    "pending_slices",
                    "sampled_pending_documents",
                    "failures",
                    "elapsed_seconds",
                )
            }
            for run in runs[:3]
            if isinstance(run, dict)
        ]
        if isinstance(runs, list)
        else []
    )
    audit = _mapping(summary.get("query_audit"))
    if audit:
        audit_pages = audit.get("pages")
        audit_totals = audit.get("totals")
        pagination = audit.get("pagination")
        selected["query_audit"] = {
            "pagination": [
                {
                    key: row.get(key)
                    for key in ("market", "date", "page_size", "total", "page_rows", "equivalent", "failure", "seconds")
                }
                for row in (pagination[:9] if isinstance(pagination, list) else [])
                if isinstance(row, dict)
            ],
            "category_filter": audit.get("category_filter"),
            "read_only": audit.get("read_only"),
            "pages": [
                {
                    key: row.get(key)
                    for key in (
                        "market",
                        "start",
                        "end",
                        "plate",
                        "page",
                        "members",
                        "total",
                        "rows",
                        "outside_codes",
                        "same_issuer",
                        "b_share_rows",
                        "failure",
                        "order",
                    )
                }
                for row in (audit_pages[:192] if isinstance(audit_pages, list) else [])
                if isinstance(row, dict)
            ],
            "totals": [
                {
                    key: row.get(key)
                    for key in (
                        "market",
                        "start",
                        "parent_total",
                        "board_total",
                        "group_total",
                        "sample_complete",
                    )
                }
                for row in (audit_totals[:3] if isinstance(audit_totals, list) else [])
                if isinstance(row, dict)
            ],
        }
    payload["summary"] = selected


def _configuration_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = {
        key: source.get(key)
        for key in (
            "status",
            "scoring_profile",
            "runtime_version",
            "strategy_version",
            "history_sessions",
            "training_fit_dates",
            "research_arms",
            "failure",
            "category",
        )
    }


def _maintenance_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = {
        key: source.get(key)
        for key in (
            "state",
            "reason",
            "data_cutoff",
            "label_cutoff",
            "training_due",
            "training_due_reason",
            "reminder_state",
        )
    }


_CHECK_DETAILS: Mapping[str, Callable[[DiagnosticResult, Mapping[str, object], dict[str, object]], None]] = {
    "storage_profile": _security_master_details,
    "v5_samples": _security_master_details,
    "v5_lite": _security_master_details,
    "configuration": _configuration_details,
    "history_maintenance": _maintenance_details,
    "long_watchlist_admission": _long_admission_details,
    "web_health": _web_health_details,
    "history_sources": _history_details,
    "tencent_download": _security_master_details,
    "tencent_history_window": _security_master_details,
    "v5_training": _security_master_details,
    "training_alignment": _security_master_details,
    "training_coverage": _security_master_details,
    "model_inputs": _security_master_details,
    "qfq_sqlite_audit": _security_master_details,
    "exchange_security_master": _security_master_details,
    "tencent_quotes": _tencent_quote_details,
    "tushare_daily": _tushare_details,
    "history_daily_capability": _history_daily_capability_details,
    "baostock_concurrency": _baostock_concurrency_details,
    "history_sqlite_performance": _history_sqlite_performance_details,
    "history_source_compare": _history_sqlite_performance_details,
    "market_factor_compare": _summary_with_findings,
    "company_ledger_browser": _summary_with_findings,
    "research_source_compare": _research_source_compare_details,
    "history_write_benchmark": _history_sqlite_performance_details,
    "download_performance": _history_sqlite_performance_details,
    "database_baseline": _history_sqlite_performance_details,
    "company_resume": _company_resume_details,
    "history_space": _history_sqlite_performance_details,
    "history_industry": _history_sqlite_performance_details,
    "baostock_qfq_shadow": _baostock_qfq_shadow_details,
    "research_readiness": _research_details,
    "browser_refresh": _browser_details,
    "production_performance": _performance_details,
}


def _status(result: DiagnosticResult) -> CheckStatus:
    if result.error_code is not None or result.payload is None or result.return_code != 0:
        return "failed"
    if result.name == "research_readiness" and not _valid_research_status(result.payload):
        return "failed"
    if result.name == "research_readiness":
        if _safe_string_list(result.payload.get("blockers"), limit=1):
            return "failed"
        tomorrow = _mapping(result.payload.get("tomorrow_research"))
        if tomorrow.get("status") == "blocked":
            return "failed"
        if _safe_string_list(tomorrow.get("input_blockers"), limit=1) or _safe_string_list(
            tomorrow.get("production_blockers"), limit=1
        ):
            return "failed"
    status = result.payload.get("status")
    if status == "passed":
        return "passed"
    if status == "degraded":
        return "degraded"
    if status == "failed":
        return "failed"
    passed = result.payload.get("passed")
    if isinstance(passed, bool):
        return "passed" if passed else "failed"
    return "passed"


def _valid_research_status(payload: Mapping[str, object]) -> bool:
    history = payload.get("history_archive")
    tomorrow = payload.get("tomorrow_research")
    if not isinstance(history, dict) or not isinstance(tomorrow, dict):
        return False
    if payload.get("production_authority") is not False:
        return False
    if history.get("production_authority") is not False or history.get("point_in_time_parity") is not False:
        return False
    if tomorrow.get("production_authority") is not False:
        return False
    if not all(
        isinstance(history.get(name), (str, int))
        for name in (
            "state",
            "calendar_sessions",
            "universe_count",
            "partition_count",
        )
    ):
        return False
    if history.get("reason") is not None and not isinstance(history.get("reason"), str):
        return False
    if not isinstance(tomorrow.get("status"), str) or not isinstance(tomorrow.get("production_readiness"), str):
        return False
    if not all(
        isinstance(tomorrow.get(name), list) and all(isinstance(item, str) for item in tomorrow[name])
        for name in ("input_blockers", "production_blockers")
    ):
        return False
    return payload.get("schema_version") == "research_readiness"


def _findings(check: Mapping[str, object]) -> list[dict[str, object]]:
    existing = check.get("findings")
    if isinstance(existing, list):
        return [dict(item, check=check["name"]) for item in existing if isinstance(item, dict)][:50]
    status = check.get("status")
    if status == "passed":
        return []
    return [
        {
            "severity": "error" if status == "failed" else "warning",
            "code": f"{check['name']}_{status}",
            "check": check["name"],
            "evidence": check.get("summary", {"error_code": check.get("error_code")}),
        }
    ]


def _bounded_findings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)][:50]


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return value
    return {}


def _safe_string_list(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item[:160] for item in value if isinstance(item, str)][:limit]


def _safe_text(value: object) -> str | None:
    return value[:160] if isinstance(value, str) and value else None


def _safe_nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _write_report(report: Mapping[str, object], output: str, output_format: str) -> None:
    rendered = (
        RenderMarketFactorText(report)
        if output_format == "text"
        else json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    if output == "-":
        sys.stdout.write(rendered)
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    output = args.output
    output_format = "json"
    try:
        options, output = _validate(args)
        output_format = options.output_format
        if options.cpu_profile_dir is not None:
            options.cpu_profile_dir.mkdir(parents=True, exist_ok=True)
        report = run_diagnostics(options.profile, build_commands(options))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        output = args.output if args.output == "-" else "-"
        report = {
            "schema_version": "trader-runtime-diagnostics",
            "status": "failed",
            "error": type(exc).__name__,
        }
    _write_report(report, output, output_format)
    return 0 if report.get("status") in {"passed", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
