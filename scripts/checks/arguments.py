"""Parse one explicit business/module/check selection and its bounded inputs."""

from __future__ import annotations

import argparse
import math
from datetime import date
from pathlib import Path
from typing import cast

from scripts.checks.contracts import PROJECT_ROOT, DiagnosticOptions, ScoringProfileSelection
from scripts.checks.catalog import SelectCheck
from scripts.checks.download.market.market_factor_budget import MARKET_SOURCE_LOOP_LIMIT_SECONDS, MARKET_TASK_LIMIT_SECONDS
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection
from trader.recommendation.infra.performance.candidate_selection import PRELOAD_CONTEXT_CANDIDATES, SCORING_CANDIDATES

_DEFAULT_CONFIG = PROJECT_ROOT / "config/runtime.json"
_MARKET_SOURCES = ("tencent", "eastmoney", "sina", "baostock")

def BuildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Use --list for valid selections. Engineering checks and training/resources/memory accept "
        "native arguments after --; append -- --help to inspect their inputs and side effects. "
        "Only the selected check runs; format/rename and paid skill runs require explicit authorization.",
    )
    parser.add_argument("--company-pagination-audit", action="store_true", help="read-only CNInfo page-size comparison")
    parser.add_argument("--company-ocr-audit", action="store_true", help="compare OCR on up to seven failed announcements without active writes")
    parser.add_argument("--company-document-audit", action="store_true", help="read-only failed PDF request comparison")
    parser.add_argument("--company-directory-audit", action="store_true", help="repair failed directories only in an external backup")
    parser.add_argument("--history-preload-audit", action="store_true", help="read-only shared startup timing")
    parser.add_argument(
        "--history-publication-profile", action="store_true",
        help="history-write: profile isolated publication; optional history-root replays published files, no network",
    )
    parser.add_argument("--history-preload-cpu-profile", action="store_true", help="include CPU profiling overhead in preload timing")
    parser.add_argument("--history-preload-feature-audit", action="store_true", help="profile concurrent feature builds with published inputs and synthetic quotes")
    parser.add_argument("--history-preload-profiles", type=ParseProfileSelection, default=("v1", "v2", "v3"))
    parser.add_argument("--history-preload-context-candidate", choices=PRELOAD_CONTEXT_CANDIDATES, default="baseline")
    parser.add_argument(
        "--company-pipeline-audit", action="store_true", help="isolated same-PDF serial/parallel comparison"
    )
    parser.add_argument(
        "--company-query-audit", action="store_true", help="read-only bounded CNInfo query coverage audit"
    )
    parser.add_argument(
        "--company-database-audit", action="store_true", help="read-only local company text and retry counts; no network"
    )
    parser.add_argument(
        "--company-replay-directory",
        type=Path,
        help="explicit repository-external directory for an isolated company backup and resumable evidence",
    )
    parser.add_argument("--business", required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("--check", required=True)
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
    parser.add_argument("--wide-sample-source", type=Path, help="bounded read-only V5 wide-cache database comparison")
    parser.add_argument("--v5-sample-root", type=Path, help="sealed compact V5 cache for isolated lite comparison")
    parser.add_argument("--v5-candidate-config", type=Path, help="explicit training JSON candidate for v5-lite")
    parser.add_argument(
        "--disk-path", type=Path, action="append", default=[], help="additional read-only disk probe path"
    )
    parser.add_argument("--disk-policy-audit", action="store_true", help="audit SQLite policy consumers without opening business databases")
    parser.add_argument("--database-training", action="store_true", help="include four-profile sample I/O in the isolated database baseline")
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
    parser.add_argument("--performance-steady-only", action="store_true", help="only the existing 100-tick model RSS measurement")
    parser.add_argument(
        "--performance-factor-candidate",
        choices=("baseline", "raw_factor_reuse"),
        default="baseline",
        help="explicit offline raw-factor reuse candidate; requires continuous replay",
    )
    parser.add_argument(
        "--performance-scoring-candidate",
        choices=SCORING_CANDIDATES,
        default="baseline",
        help="offline qualification/reference reuse or independent V5 industry executor; requires continuous replay",
    )
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


def ValidateOptions(args: argparse.Namespace) -> tuple[DiagnosticOptions, str]:
    if args.history_publication_profile and (args.check != "write" or args.business != "download" or args.module != "history"):
        raise ValueError("--history-publication-profile requires download/history/write")
    if args.history_publication_profile and args.baseline_checkout is not None:
        raise ValueError("--history-publication-profile cannot be used as a layout comparison")
    args.check_id = SelectCheck(args.business, args.module, args.check).identity
    if args.performance_steady_only and (
        args.check_id != "performance" or args.performance_replay_ticks or args.cpu_profile_dir is not None
    ):
        raise ValueError("--performance-steady-only requires performance without replay or CPU tracing")
    if args.check_id == "database-baseline" and args.download_sizes == (10, 50, 100):
        args.download_sizes = (8,)
    if args.database_probe_directory is not None and not args.database_probe_directory.expanduser().is_absolute():
        raise ValueError("--database-probe-directory must be absolute")
    if args.database_probe_directory is not None and args.check_id not in {
        "database-baseline",
        "history-write",
        "history-sqlite",
        "download-performance",
        "price-repair-audit",
    }:
        raise ValueError("--database-probe-directory requires a database performance profile")
    if args.check_id == "database-baseline" and args.database_probe_directory is None:
        raise ValueError("database-baseline requires --database-probe-directory")
    if args.disk_policy_audit and args.check_id != "disk":
        raise ValueError("--disk-policy-audit requires --business infra --module disk --check profile")
    if args.database_training and (args.check_id != "database-baseline" or args.baseline_checkout is not None):
        raise ValueError("--database-training requires database-baseline without a comparison checkout")
    if args.check_id == "database-baseline" and not 5 <= args.history_days <= 2000:
        raise ValueError("database-baseline requires 5..2000 history days")
    if args.check_id == "database-baseline" and (len(args.download_sizes) != 1 or not 1 <= args.download_sizes[0] <= 32):
        raise ValueError("database-baseline requires one download size in 1..32")
    if args.performance_replay_rows is not None and (
        args.performance_replay_rows < 1 or not args.performance_replay_ticks
    ):
        raise ValueError("--performance-replay-rows requires positive rows and --performance-replay-ticks")
    if not 0 <= args.performance_replay_ticks <= 1000 or (
        args.performance_replay_ticks and args.check_id != "performance"
    ):
        raise ValueError("--performance-replay-ticks requires performance and a value within 0..1000")
    if args.scoring_profile == "v1,v2,v3,v5" and args.check_id != "performance":
        raise ValueError("four-profile scoring is only available for performance")
    if args.performance_factor_candidate != "baseline" and (
        args.check_id != "performance" or not args.performance_replay_ticks or args.performance_steady_only
    ):
        raise ValueError("--performance-factor-candidate requires performance continuous replay")
    if args.performance_scoring_candidate != "baseline" and (
        args.check_id != "performance" or not args.performance_replay_ticks or args.performance_steady_only
    ):
        raise ValueError("--performance-scoring-candidate requires performance continuous replay")
    if args.history_preload_context_candidate != "baseline" and (args.check_id != "history-sqlite" or not args.history_preload_audit):
        raise ValueError("--history-preload-context-candidate requires the history preload audit")
    codes = tuple(dict.fromkeys(args.codes))
    if not codes or any(len(code) != 6 or not code.isdigit() for code in codes):
        raise ValueError("--codes must contain six-digit A-share codes")
    maximum_codes = 100 if args.check_id == "baostock-concurrency" else 50
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
    if any(not math.isfinite(value) or value <= 0 for value in positive):
        raise ValueError("sample, worker, duration and timeout values must be positive")
    if args.command_timeout_seconds > 7200:
        raise ValueError("command timeout must not exceed 7200 seconds")
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
    maximum_size = 5000 if args.check_id == "history-write" and args.baseline_checkout is not None else 100
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
        ExternalPath(args.long_evidence_output, "--long-evidence-output") if args.long_evidence_output else None
    )
    company_replay_directory = ExternalPath(args.company_replay_directory, "--company-replay-directory")
    if (
        sum(
            (
                args.company_query_audit,
                args.company_database_audit,
                args.company_pagination_audit,
                args.company_pipeline_audit,
                args.company_ocr_audit,
                args.company_document_audit,
                args.company_directory_audit,
            )
        )
        > 1
    ):
        raise ValueError("company audit modes are mutually exclusive")
    if (
        args.check_id == "company-resume"
        and company_replay_directory is None
        and not (args.company_query_audit or args.company_database_audit or args.company_pagination_audit or args.company_document_audit)
    ):
        raise ValueError("company-resume requires --company-replay-directory")
    output = args.output
    if output != "-":
        output = str(ExternalPath(Path(output), "--output"))
    output_format = (
        "text" if args.format == "auto" and args.check_id == "market-factor-compare" and output == "-" else args.format
    )
    if output_format == "auto":
        output_format = "json"
    if output_format == "text" and args.check_id != "market-factor-compare":
        raise ValueError("--format text is only available for market-factor-compare")
    if args.baseline_checkout is not None and args.check_id not in {
        "history-write",
        "download-performance",
        "database-baseline",
        "training-performance",
        "training-samples",
    }:
        raise ValueError(
            "--baseline-checkout is only available for history-write, download-performance, database-baseline "
            "or training-performance/training-samples"
        )
    if args.cpu_profile_dir is not None:
        if args.check_id not in {"market-factor-compare", "performance"}:
            raise ValueError("--cpu-profile-dir requires market-factor-compare or performance")
        if args.check_id == "performance" and args.performance_replay_ticks != 1:
            raise ValueError("--cpu-profile-dir requires --performance-replay-ticks 1")
    if args.cpu_profile_mode == "gc" and (args.check_id != "performance" or args.cpu_profile_dir is None):
        raise ValueError("--cpu-profile-mode gc requires performance and --cpu-profile-dir")
    if args.v5_kernel_probe and args.check_id != "v5-training":
        raise ValueError("--v5-kernel-probe requires --check v5-training")
    if args.check_id == "v5-training" and not args.v5_kernel_probe and args.v5_probe_directory is None:
        raise ValueError("v5-training requires --v5-probe-directory")
    if args.check_id == "training-samples" and args.baseline_checkout is None:
        raise ValueError("training-samples requires --baseline-checkout")
    if args.check_id in {"v5-samples", "v5-lite", "training-samples"} and args.training_probe_directory is None:
        raise ValueError(f"{args.check_id} requires --training-probe-directory")
    if args.wide_sample_source is not None and args.check_id != "v5-samples":
        raise ValueError("--wide-sample-source requires --check v5-samples")
    if args.v5_sample_root is not None and args.check_id != "v5-lite":
        raise ValueError("--v5-sample-root requires --check v5-lite")
    if args.v5_candidate_config is not None and args.check_id != "v5-lite":
        raise ValueError("--v5-candidate-config requires --check v5-lite")
    if args.check_id == "v5-lite" and args.v5_sample_root is not None and args.v5_candidate_config is None:
        raise ValueError("--v5-sample-root requires --v5-candidate-config")
    if args.check_id in {"training-alignment", "training-performance"} and (
        args.training_probe_directory is None or args.history_root is None
    ):
        raise ValueError(f"{args.check_id} requires --history-root and --training-probe-directory")
    return (
        DiagnosticOptions(
            training_signal_date=args.training_signal_date,
            training_profiles=args.training_profiles,
            check_id=args.check_id,
            database_probe_directory=ExternalPath(args.database_probe_directory, "--database-probe-directory")
            if args.database_probe_directory is not None
            else None,
            database_rounds=args.database_rounds,
            database_training=args.database_training,
            database_acceptance_focus=args.database_acceptance_focus,
            base_url=args.base_url,
            runtime_config=Path(args.runtime_config).expanduser().resolve(),
            disk_paths=tuple(path.expanduser().resolve() for path in args.disk_path),
            disk_policy_audit=args.disk_policy_audit,
            wide_sample_source=args.wide_sample_source.expanduser().resolve() if args.wide_sample_source else None,
            v5_sample_root=args.v5_sample_root.expanduser().resolve() if args.v5_sample_root else None,
            v5_candidate_config=args.v5_candidate_config.expanduser().resolve() if args.v5_candidate_config else None,
            codes=codes,
            web_samples=args.web_samples,
            web_interval_seconds=args.web_interval_seconds,
            source_samples=args.source_samples,
            source_interval_seconds=args.source_interval_seconds,
            history_workers=args.history_workers,
            history_days=args.history_days,
            baseline_checkout=ExternalPath(args.baseline_checkout, "--baseline-checkout")
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
            company_database_audit=args.company_database_audit,
            history_preload_audit=args.history_preload_audit,
            history_publication_profile=args.history_publication_profile,
            history_preload_cpu_profile=args.history_preload_cpu_profile,
            history_preload_feature_audit=args.history_preload_feature_audit,
            history_preload_profiles=args.history_preload_profiles,
            history_preload_context_candidate=args.history_preload_context_candidate,
            company_pagination_audit=args.company_pagination_audit,
            company_pipeline_audit=args.company_pipeline_audit,
            company_ocr_audit=args.company_ocr_audit,
            company_document_audit=args.company_document_audit,
            company_directory_audit=args.company_directory_audit,
            v5_kernel_probe=args.v5_kernel_probe,
            v5_probe_directory=ExternalPath(args.v5_probe_directory, "--v5-probe-directory")
            if args.v5_probe_directory is not None
            else None,
            training_probe_directory=ExternalPath(args.training_probe_directory, "--training-probe-directory")
            if args.training_probe_directory is not None
            else None,
            eligibility_evidence=tuple(args.eligibility_evidence),
            download_sizes=tuple(args.download_sizes),
            qfq_database=args.qfq_database.expanduser().resolve() if args.qfq_database else None,
            scoring_profile=args.scoring_profile,
            performance_replay_ticks=args.performance_replay_ticks,
            performance_steady_only=args.performance_steady_only,
            performance_replay_rows=args.performance_replay_rows,
            performance_factor_candidate=args.performance_factor_candidate,
            performance_scoring_candidate=args.performance_scoring_candidate,
            market_sources=tuple(args.market_sources),
            market_profiles=tuple(args.market_profiles),
            market_sizes=tuple(args.market_sizes),
            market_mode=args.market_mode,
            market_batch_seconds=args.market_batch_seconds,
            market_source_loop_seconds=args.market_source_loop_seconds,
            market_task_seconds=args.market_task_seconds,
            output_format=output_format,
            cpu_profile_dir=ExternalPath(args.cpu_profile_dir, "--cpu-profile-dir")
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


def ExternalPath(value: Path | None, option: str) -> Path | None:
    if value is None:
        return None
    if not value.expanduser().is_absolute():
        raise ValueError(f"{option} must be an absolute path outside the repository")
    resolved = value.expanduser().resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise ValueError(f"{option} must be an absolute path outside the repository")
    return resolved
