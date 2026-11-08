"""Parse only the selected download/history action and validate its inputs."""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import time
from collections.abc import Sequence
from contextlib import closing
from datetime import date
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT, ExternalPath
from scripts.download.history.options import HistoryOptions
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection
from trader.recommendation.infra.performance.candidate_selection import PRELOAD_CONTEXT_CANDIDATES


def BuildParser(
    action_identity: str, *, parents: Sequence[argparse.ArgumentParser] = ()
) -> argparse.ArgumentParser:
    if action_identity not in ("history","history-daily-capability","baostock-concurrency","baostock-qfq-shadow","tencent-download","tencent-history-window","history-sqlite","history-publication","history-source-compare","price-repair-audit","history-write","download-performance","history-space","history-industry","qfq-sqlite","download-failures",):
        raise ValueError("unsupported download/history action")
    parser = argparse.ArgumentParser(description=__doc__, parents=list(parents), allow_abbrev=False)
    if not parents:
        parser.add_argument("--command-timeout-seconds", type=float, default=180.0)
    parser.set_defaults(
        action_identity=action_identity,
        command_timeout_seconds=180.0,
        codes=('600519', '300750', '688981'),
        source_samples=1,
        history_workers=5,
        history_days=61,
        history_source='composite',
        tencent_history_host='proxy',
        source_timeout_seconds=4.5,
        runtime_config=PROJECT_ROOT / 'config/runtime.json',
        baostock_serial_only=False,
        baostock_intervals=(2.0,),
        baostock_sizes=(10, 50, 100),
        baostock_rounds=1,
        baostock_sessions=0,
        baostock_production=False,
        history_root=None,
        download_sizes=(10, 50, 100),
        database_probe_directory=None,
        repair_profile_year=None,
        repair_profile_code=None,
        repair_codes=(),
        price_field_audit=False,
        audit_checkpoint=None,
        audit_budget_seconds=None,
        sqlite_page_sample_count=1,
        sqlite_query_rounds=3,
        sqlite_revision_write_sample_count=512,
        history_resume_samples=0,
        history_preload_audit=False,
        history_preload_cpu_profile=False,
        history_preload_feature_audit=False,
        history_preload_profiles=('v1', 'v2', 'v3'),
        history_preload_context_candidate='baseline',
        baseline_checkout=None,
        history_publication_profile=False,
        qfq_database=None,
        capability_universe_size=5453,
        capability_code="600519",
        capability_days=120,
        capability_timeout_seconds=8.0,
        baostock_interval_seconds=2.0,
        history_comparison_rounds=None,
        history_flush_codes=None,
        history_flush_cells=None,
        official_announcements=(),
        industry_work_root=None,
        cninfo_query_codes=(),
        cninfo_query_from=None,
        cninfo_query_to=None,
        cninfo_cache_evidence=None,
        cninfo_cache_codes=(),
        cninfo_cache_candidate="baseline",
        report="-",
    )
    if action_identity in ("history","baostock-concurrency","tencent-download","tencent-history-window","history-source-compare",):
        parser.add_argument('--codes', nargs='+', default=('600519', '300750', '688981'), help='one to 50 A-share codes, or up to 100 for baostock-concurrency')
    if action_identity in ("history",):
        parser.add_argument('--source-samples', type=int, default=1, help='history and Tencent source sample rounds')
    if action_identity in ("history","tencent-download",):
        parser.add_argument('--history-workers', type=int, default=5, help='history requests per bounded worker wave')
    if action_identity in ("history","baostock-concurrency","history-source-compare","history-write",):
        parser.add_argument('--history-days', type=int, default=61, help='history rows per stock; calendar-day window for BaoStock experiments')
    if action_identity in ("history",):
        parser.add_argument('--history-source', choices=('composite', 'tencent', 'eastmoney'), default='composite', help='supplier route sampled by the selected history sources action')
    if action_identity in ("history","tencent-download",):
        parser.add_argument('--tencent-history-host', choices=('proxy', 'direct'), default='proxy', help='Tencent K-line host for bounded history probes')
    if action_identity in ("history","baostock-concurrency","tencent-download","tencent-history-window","history-source-compare","history-industry",):
        parser.add_argument('--source-timeout-seconds', type=float, default=4.5, help='vendor HTTP timeout; SDK socket timeout for BaoStock experiments')
    if action_identity in ("history-daily-capability","baostock-qfq-shadow","tencent-download","tencent-history-window","history-sqlite","history-publication","history-source-compare","price-repair-audit","history-write","download-performance","history-space","history-industry","qfq-sqlite","download-failures",):
        parser.add_argument('--runtime-config', default=str(PROJECT_ROOT / 'config/runtime.json'), help='runtime JSON configuration')
    if action_identity in ("baostock-concurrency",):
        rate_mode = parser.add_mutually_exclusive_group()
        rate_mode.add_argument('--baostock-serial-only', action='store_true', help='single-session rate experiment')
        rate_mode.add_argument('--interval-seconds', dest='baostock_interval_seconds', type=float, default=2.0, help='parallel-query start interval, at least two seconds')
    if action_identity in ("baostock-concurrency",):
        parser.add_argument('--baostock-intervals', nargs='+', type=float, default=(2.0,), help='candidate start intervals')
    if action_identity in ("baostock-concurrency",):
        parser.add_argument('--baostock-sizes', nargs='+', type=int, default=(10, 50, 100), help='sample sizes within 1..100')
    if action_identity in ("baostock-concurrency",):
        parser.add_argument('--baostock-rounds', type=int, default=1, help='serial experiment repetitions within 1..3')
        parser.add_argument('--baostock-sessions', type=int, default=0, help='exact recent trading sessions within 1..2500; zero uses calendar days')
        parser.add_argument('--baostock-production', action='store_true', help='production subprocess and validation using timeout, interval and retries from current config/download.json; requires serial-only and sessions')
    if action_identity in ("baostock-qfq-shadow","tencent-download","tencent-history-window","history-sqlite","history-publication","history-source-compare","price-repair-audit","history-write","history-space",):
        parser.add_argument('--history-root', type=Path, default=None, help='published history input or explicit isolated publication replay source')
    if action_identity in ("tencent-download","history-write",):
        parser.add_argument('--download-sizes', nargs='+', type=int, default=(10, 50, 100), help='Tencent updater sample sizes')
    if action_identity in ("history-sqlite","price-repair-audit","history-write","download-performance",):
        parser.add_argument('--database-probe-directory', type=Path, help='new external directory for SQLite benchmarks')
    if action_identity == "price-repair-audit":
        parser.add_argument('--repair-codes', nargs='+', help='explicit 1..5 unique six-digit repair sample codes; external probe directory required')
        parser.add_argument('--repair-profile-year', type=int, help='profile one annual replacement on a complete isolated copy; no network')
        parser.add_argument('--repair-profile-code', help='six-digit code with an unpublished repair journal candidate')
    if action_identity in ("history-sqlite",):
        parser.add_argument('--sqlite-page-sample-count', type=int, default=1, help='annual files sampled for expensive dbstat page classification')
    if action_identity in ("history-sqlite",):
        parser.add_argument('--sqlite-query-rounds', type=int, default=3, help='cold and warm rounds per history-sqlite query workload')
    if action_identity in ("history-sqlite",):
        parser.add_argument('--sqlite-revision-write-sample-count', type=int, default=512, help='rows copied into the disposable revision batch-write probe')
    if action_identity in ("history-sqlite",):
        parser.add_argument('--history-resume-samples', type=int, default=0, help='only measure up to 8 pending history reads')
    if action_identity in ("history-sqlite",):
        parser.add_argument('--history-preload-audit', action='store_true', help='read-only shared startup timing')
    if action_identity in ("history-sqlite",):
        parser.add_argument('--history-preload-cpu-profile', action='store_true', help='include CPU profiling overhead in preload timing')
    if action_identity in ("history-sqlite",):
        parser.add_argument('--history-preload-feature-audit', action='store_true', help='profile concurrent feature builds with published inputs and synthetic quotes')
    if action_identity in ("history-sqlite",):
        parser.add_argument('--history-preload-profiles', type=ParseProfileSelection, default=('v1', 'v2', 'v3'))
    if action_identity in ("history-sqlite",):
        parser.add_argument('--history-preload-context-candidate', choices=PRELOAD_CONTEXT_CANDIDATES, default='baseline')
    if action_identity in ("history-write","download-performance",):
        parser.add_argument('--baseline-checkout', type=Path, help='isolated checkout for history-write, download-performance or database-baseline comparisons')
    if action_identity in ("history-write",):
        parser.add_argument('--history-publication-profile', action='store_true', help='history-write: profile isolated publication; optional history-root replays published files, no network')
    if action_identity in ("qfq-sqlite","download-failures",):
        parser.add_argument('--qfq-database', type=Path)
    if action_identity in ("history-publication", "qfq-sqlite"):
        parser.add_argument('--price-field-audit', action='store_true', help='explicit full published field and per-stock date scan, no network or source writes')
        parser.add_argument('--audit-checkpoint', type=Path, help='external checkpoint; exact source/config/publication/file fingerprints required for resume')
        parser.add_argument('--audit-budget-seconds', type=float, help='total deep-audit deadline; reserve at least 10 seconds inside command timeout')
    if action_identity == "history-daily-capability":
        parser.add_argument('--universe-size', dest='capability_universe_size', type=int, default=5453)
        parser.add_argument('--code', dest='capability_code', default='600519')
        parser.add_argument('--days', dest='capability_days', type=int, default=120)
        parser.add_argument('--timeout-seconds', dest='capability_timeout_seconds', type=float, default=8.0)
    if action_identity == "history-write":
        parser.add_argument('--comparison-rounds', dest='history_comparison_rounds', type=int, default=None, help='5..10 rounds with an explicit baseline checkout; default 5')
        parser.add_argument('--flush-codes', dest='history_flush_codes', type=int)
        parser.add_argument('--flush-cells', dest='history_flush_cells', type=int)
    if action_identity == "history-industry":
        source = parser.add_mutually_exclusive_group()
        source.add_argument('--official-announcements', nargs='+', help='CAPCO structured release URLs for isolated candidates')
        source.add_argument('--cninfo-query-codes', nargs='+', help='2..50 unique sorted codes for an isolated batch protocol probe')
        source.add_argument('--cninfo-cache-evidence', type=Path, help='existing external CNINFO probe originals; offline five-round cache measurement')
        parser.add_argument('--cninfo-cache-codes', nargs='+', help='2..5 unique sorted codes with single-stock receipts in the evidence directory')
        parser.add_argument('--cninfo-cache-candidate', choices=("baseline", "batched", "compare"), default="baseline")
        parser.add_argument('--cninfo-query-from', type=date.fromisoformat)
        parser.add_argument('--cninfo-query-to', type=date.fromisoformat)
        parser.add_argument('--industry-work-root', type=Path, help='external directory for original files and candidates')
    return parser


def ParseOptions(args: argparse.Namespace) -> HistoryOptions:
    _prevalidate_audit_outputs(args)
    if args.audit_checkpoint is not None and (
        args.audit_checkpoint.is_symlink() or any(parent.is_symlink() for parent in args.audit_checkpoint.parents)
    ):
        raise ValueError("price_audit_checkpoint_unsafe")
    options = HistoryOptions(
        action_identity=args.action_identity,
        command_timeout_seconds=args.command_timeout_seconds,
        codes=tuple(dict.fromkeys(args.codes)),
        source_samples=args.source_samples,
        history_workers=args.history_workers,
        history_days=args.history_days,
        history_source=args.history_source,
        tencent_history_host=args.tencent_history_host,
        source_timeout_seconds=args.source_timeout_seconds,
        runtime_config=Path(args.runtime_config).expanduser().resolve(),
        baostock_serial_only=args.baostock_serial_only,
        baostock_intervals=tuple(args.baostock_intervals),
        baostock_sizes=tuple(args.baostock_sizes),
        baostock_rounds=args.baostock_rounds,
        baostock_sessions=args.baostock_sessions,
        baostock_production=args.baostock_production,
        history_root=args.history_root.expanduser().resolve() if args.history_root is not None else None,
        download_sizes=tuple(args.download_sizes),
        database_probe_directory=ExternalPath(args.database_probe_directory, "--database-probe-directory"),
        repair_profile_year=args.repair_profile_year,
        repair_profile_code=args.repair_profile_code,
        repair_codes=tuple(args.repair_codes or ()),
        price_field_audit=args.price_field_audit,
        audit_checkpoint=ExternalPath(args.audit_checkpoint, "--audit-checkpoint"),
        audit_budget_seconds=args.audit_budget_seconds,
        sqlite_page_sample_count=args.sqlite_page_sample_count,
        sqlite_query_rounds=args.sqlite_query_rounds,
        sqlite_revision_write_sample_count=args.sqlite_revision_write_sample_count,
        history_resume_samples=args.history_resume_samples,
        history_preload_audit=args.history_preload_audit,
        history_preload_cpu_profile=args.history_preload_cpu_profile,
        history_preload_feature_audit=args.history_preload_feature_audit,
        history_preload_profiles=tuple(args.history_preload_profiles),
        history_preload_context_candidate=args.history_preload_context_candidate,
        baseline_checkout=ExternalPath(args.baseline_checkout, "--baseline-checkout"),
        history_publication_profile=args.history_publication_profile,
        qfq_database=args.qfq_database.expanduser().resolve() if args.qfq_database is not None else None,
        capability_universe_size=args.capability_universe_size,
        capability_code=args.capability_code,
        capability_days=args.capability_days,
        capability_timeout_seconds=args.capability_timeout_seconds,
        baostock_interval_seconds=args.baostock_interval_seconds,
        history_comparison_rounds=5 if args.history_comparison_rounds is None else args.history_comparison_rounds,
        history_flush_codes=args.history_flush_codes,
        history_flush_cells=args.history_flush_cells,
        official_announcements=tuple(args.official_announcements or ()),
        industry_work_root=ExternalPath(args.industry_work_root, "--industry-work-root"),
        cninfo_query_codes=tuple(args.cninfo_query_codes or ()),
        cninfo_query_from=args.cninfo_query_from,
        cninfo_query_to=args.cninfo_query_to,
        cninfo_cache_evidence=ExternalPath(args.cninfo_cache_evidence, "--cninfo-cache-evidence"),
        cninfo_cache_codes=tuple(args.cninfo_cache_codes or ()),
        cninfo_cache_candidate=args.cninfo_cache_candidate,
    )
    if not math.isfinite(options.command_timeout_seconds) or not 0 < options.command_timeout_seconds <= 7200:
        raise ValueError("command timeout must be positive and must not exceed 7200 seconds")
    if options.repair_codes and (
        options.action_identity != "price-repair-audit" or options.database_probe_directory is None
        or not 1 <= len(options.repair_codes) <= 5 or len(set(options.repair_codes)) != len(options.repair_codes)
        or any(re.fullmatch(r"[0-9]{6}", code) is None for code in options.repair_codes)
    ):
        raise ValueError("--repair-codes requires 1..5 unique six-digit codes and an external --database-probe-directory")
    if (options.audit_checkpoint is not None or options.audit_budget_seconds is not None) and not options.price_field_audit:
        raise ValueError("audit checkpoint and budget require --price-field-audit")
    if options.price_field_audit:
        budget = options.audit_budget_seconds if options.audit_budget_seconds is not None else options.command_timeout_seconds - 10
        if not math.isfinite(budget) or not 0 < budget <= options.command_timeout_seconds - 10:
            raise ValueError("audit budget must be positive with ten seconds reserved inside command timeout")
    selected_source = bool(options.official_announcements or options.cninfo_query_codes or options.cninfo_cache_evidence)
    if selected_source != (options.industry_work_root is not None):
        raise ValueError("industry source selection and --industry-work-root must be set together")
    if options.action_identity == "history-industry" and not selected_source:
        raise ValueError("industry diagnostic requires an explicit source and --industry-work-root")
    if options.cninfo_query_codes:
        if (
            not 2 <= len(options.cninfo_query_codes) <= 50
            or options.cninfo_query_codes != tuple(sorted(set(options.cninfo_query_codes)))
            or any(re.fullmatch(r"[0-9]{6}", code) is None for code in options.cninfo_query_codes)
            or options.cninfo_query_from is None or options.cninfo_query_to is None
            or options.cninfo_query_from > options.cninfo_query_to
            or not 1 <= options.source_timeout_seconds <= 45
        ):
            raise ValueError("CNINFO probe requires 2..50 unique sorted codes, an ordered date window and timeout within 1..45")
    elif options.cninfo_query_from is not None or options.cninfo_query_to is not None:
        raise ValueError("CNINFO date window requires --cninfo-query-codes")
    if options.cninfo_cache_evidence is not None:
        if (
            not 2 <= len(options.cninfo_cache_codes) <= 5
            or options.cninfo_cache_codes != tuple(sorted(set(options.cninfo_cache_codes)))
            or any(re.fullmatch(r"[0-9]{6}", code) is None for code in options.cninfo_cache_codes)
        ):
            raise ValueError("CNINFO cache measurement requires 2..5 unique sorted --cninfo-cache-codes")
    elif options.cninfo_cache_codes:
        raise ValueError("--cninfo-cache-codes requires --cninfo-cache-evidence")
    if options.cninfo_cache_candidate != "baseline" and options.cninfo_cache_evidence is None:
        raise ValueError("--cninfo-cache-candidate requires --cninfo-cache-evidence")
    if options.official_announcements:
        from trader.download.infra.official_industry_release import ValidateOfficialIndustryUrl

        if len(options.official_announcements) > 64 or len(set(options.official_announcements)) != len(options.official_announcements):
            raise ValueError("official announcements must be unique, with at most 64 sources")
        for url in options.official_announcements:
            ValidateOfficialIndustryUrl(url)
    if not options.codes or any(len(code) != 6 or not code.isdigit() for code in options.codes):
        raise ValueError("--codes must contain six-digit A-share codes")
    maximum_codes = 100 if options.action_identity == "baostock-concurrency" else 50
    if len(options.codes) > maximum_codes:
        raise ValueError(f"--codes accepts at most {maximum_codes} codes for this action")
    if any(not math.isfinite(value) or value <= 0 for value in (
        options.source_samples, options.history_workers, options.history_days, options.source_timeout_seconds,
    )):
        raise ValueError("sample, worker, day and timeout values must be positive")
    if not 1 <= options.sqlite_page_sample_count <= 100:
        raise ValueError("--sqlite-page-sample-count must be within 1..100")
    if not 1 <= options.sqlite_query_rounds <= 9:
        raise ValueError("--sqlite-query-rounds must be within 1..9")
    if not 1 <= options.sqlite_revision_write_sample_count <= 5000:
        raise ValueError("--sqlite-revision-write-sample-count must be within 1..5000")
    if not 0 <= options.history_resume_samples <= 8:
        raise ValueError("--history-resume-samples must be within 0..8")
    if options.repair_profile_year is not None or options.repair_profile_code is not None:
        if (options.repair_profile_year is None or not 1990 <= options.repair_profile_year <= 2100
            or options.repair_profile_code is None or re.fullmatch(r"[0-9]{6}", options.repair_profile_code) is None
            or options.database_probe_directory is None or options.database_probe_directory.exists()):
            raise ValueError("repair profile requires valid year, six-digit code and a new --database-probe-directory")
    if options.history_preload_context_candidate != "baseline" and not options.history_preload_audit:
        raise ValueError("--history-preload-context-candidate requires the history preload audit")
    if (options.history_preload_cpu_profile or options.history_preload_feature_audit) and not options.history_preload_audit:
        raise ValueError("detailed preload probes require --history-preload-audit")
    if not 1 <= options.baostock_rounds <= 3 or not options.baostock_sizes or any(not 1 <= size <= 100 for size in options.baostock_sizes):
        raise ValueError("BaoStock rounds/sizes must be within 1..3 and 1..100")
    if not options.baostock_intervals or len(options.baostock_intervals) > 3 or any(
        not math.isfinite(value) or value < 1 for value in options.baostock_intervals
    ):
        raise ValueError("BaoStock accepts at most three finite intervals of at least 1 second")
    if len(set(options.baostock_intervals)) != len(options.baostock_intervals) or len(options.baostock_sizes) > 3:
        raise ValueError("BaoStock intervals must be unique and at most three sizes are allowed")
    if not options.baostock_serial_only and (options.baostock_intervals != (2.0,) or options.baostock_rounds != 1):
        raise ValueError("BaoStock interval/round comparisons require --baostock-serial-only")
    if not 0 <= options.baostock_sessions <= 2500 or options.baostock_sessions and not options.baostock_serial_only:
        raise ValueError("--baostock-sessions must be within 0..2500 and requires --baostock-serial-only")
    if options.baostock_production and (
        not options.baostock_serial_only or not 1 <= options.baostock_sessions <= 2000
        or options.baostock_rounds != 1 or len(options.baostock_intervals) != 1 or len(options.baostock_sizes) != 1
    ):
        raise ValueError("--baostock-production requires serial-only, sessions within 1..2000, one size, round and interval")
    maximum_size = 5000 if options.action_identity == "history-write" and options.baseline_checkout is not None else 100
    if not options.download_sizes or len(options.download_sizes) > 3 or any(not 1 <= size <= maximum_size for size in options.download_sizes):
        raise ValueError(f"--download-sizes accepts up to three sizes within 1..{maximum_size}")
    if options.history_publication_profile and options.baseline_checkout is not None:
        raise ValueError("--history-publication-profile cannot be used as a layout comparison")
    if options.action_identity == "history-write" and options.history_root is not None and not options.history_publication_profile:
        raise ValueError("--history-root requires --history-publication-profile for history write")
    if options.capability_universe_size < 1 or options.capability_days < 1 or not math.isfinite(options.capability_timeout_seconds) or options.capability_timeout_seconds <= 0:
        raise ValueError("daily capability universe, days and timeout must be positive")
    if len(options.capability_code) != 6 or not options.capability_code.isdigit():
        raise ValueError("--code must be a six-digit A-share code")
    if not math.isfinite(options.baostock_interval_seconds) or options.baostock_interval_seconds < 2:
        raise ValueError("--interval-seconds must be finite and at least two seconds")
    if not 5 <= options.history_comparison_rounds <= 10:
        raise ValueError("--comparison-rounds must be within 5..10")
    if args.history_comparison_rounds is not None and options.baseline_checkout is None:
        raise ValueError("--comparison-rounds requires --baseline-checkout")
    if (options.history_flush_codes is None) != (options.history_flush_cells is None):
        raise ValueError("--flush-codes and --flush-cells must be set together")
    if options.history_flush_codes is not None:
        if options.history_flush_cells is None or not 1 <= options.history_flush_codes <= 64 or not 1 <= options.history_flush_cells <= 32768:
            raise ValueError("flush thresholds must fit the bounded pipeline")
        if options.baseline_checkout is not None or options.history_publication_profile and options.history_root is not None:
            raise ValueError("flush thresholds require the synthetic history writer, not a layout comparison or published replay")
    _validate_audit_outputs(options, args.report)
    return options


def _validate_audit_outputs(options: HistoryOptions, report: str) -> None:
    if options.action_identity not in ("history-publication", "qfq-sqlite"):
        return
    outputs = ([Path(report)] if report != "-" else []) + ([options.audit_checkpoint] if options.audit_checkpoint is not None else [])
    if not outputs:
        return
    from trader.infra.settings import LoadRuntimeSettings

    directories = [options.runtime_config.parent.resolve()]
    if options.history_root is not None:
        directories.append(options.history_root.resolve())
    if options.qfq_database is not None:
        directories.append(options.qfq_database.parent.resolve())
    _require_audit_output_separation(outputs, directories, options.action_identity)
    try:
        settings = LoadRuntimeSettings(options.runtime_config)
    except (OSError, RuntimeError, ValueError):
        BuildParser(options.action_identity).error("audit source/output separation requires readable runtime configuration")
    if options.action_identity == "qfq-sqlite":
        database = options.qfq_database if options.qfq_database is not None else settings.data_paths.daily_window
        directories.extend((database.parent.resolve(), settings.data_paths.reference_database.parent.resolve()))
    else:
        root = options.history_root if options.history_root is not None else settings.data_paths.history
        root = root.resolve()
        directories.append(root)
        reference = root.parent / "reference" / "company_st.sqlite3"
        catalog = root / "catalog.sqlite3"
        if catalog.is_symlink():
            BuildParser(options.action_identity).error("audit source/output separation cannot use a linked catalog")
        if catalog.is_file():
            deadline = time.monotonic() + 1.0
            try:
                with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True, timeout=1.0)) as connection:
                    connection.execute("PRAGMA query_only=ON")
                    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                    rows = connection.execute("SELECT reference_file FROM schema_contract LIMIT 2").fetchall()
                if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
                    BuildParser(options.action_identity).error("audit source/output reference binding is unavailable")
                reference = (root / rows[0][0]).resolve()
            except (OSError, sqlite3.Error):
                BuildParser(options.action_identity).error("audit source/output reference binding cannot be read")
        directories.append(reference.parent.resolve())
    _require_audit_output_separation(outputs, directories, options.action_identity)


def _require_audit_output_separation(outputs: Sequence[Path], directories: Sequence[Path], action: str) -> None:
    for output in outputs:
        target = output.resolve()
        if any(target == directory or target.is_relative_to(directory) or directory.is_relative_to(target) for directory in directories):
            # SystemExit prevents core.Main from serializing even an error
            # report into the rejected source path after a business ValueError.
            BuildParser(action).error("audit report and checkpoint must be separate from history, market, reference and configuration source directories")


def _prevalidate_audit_outputs(args: argparse.Namespace) -> None:
    if args.action_identity not in ("history-publication", "qfq-sqlite") or args.report == "-" and args.audit_checkpoint is None:
        return
    try:
        inputs = HistoryOptions(
            action_identity=args.action_identity,
            runtime_config=Path(args.runtime_config).expanduser().resolve(),
            history_root=args.history_root.expanduser().resolve() if args.history_root is not None else None,
            qfq_database=args.qfq_database.expanduser().resolve() if args.qfq_database is not None else None,
            audit_checkpoint=args.audit_checkpoint.expanduser().resolve() if args.audit_checkpoint is not None else None,
        )
        _validate_audit_outputs(inputs, args.report)
    except (OSError, RuntimeError, TypeError, ValueError):
        BuildParser(args.action_identity).error("audit output/source separation must be approved before validating other options")
