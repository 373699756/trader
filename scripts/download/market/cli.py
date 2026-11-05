"""Parse only the selected download/market action and validate its inputs."""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT, ExternalPath
from scripts.download.market.options import MarketOptions
from scripts.download.market.market_factor_budget import MARKET_SOURCE_LOOP_LIMIT_SECONDS, MARKET_TASK_LIMIT_SECONDS

_MARKET_SOURCES = ("tencent", "eastmoney", "sina", "baostock")


def BuildParser(
    action_identity: str, *, parents: Sequence[argparse.ArgumentParser] = ()
) -> argparse.ArgumentParser:
    if action_identity not in ("security-master","tencent","tencent-transport","tushare","market-factor-compare",):
        raise ValueError("unsupported download/market action")
    parser = argparse.ArgumentParser(description=__doc__, parents=list(parents), allow_abbrev=False)
    if not parents:
        parser.add_argument("--command-timeout-seconds", type=float, default=180.0)
    parser.set_defaults(
        action_identity=action_identity,
        command_timeout_seconds=180.0,
        source_timeout_seconds=4.5,
        codes=('600519', '300750', '688981'),
        source_samples=1,
        source_interval_seconds=1.0,
        runtime_config=PROJECT_ROOT / 'config/runtime.json',
        history_days=61,
        qfq_database=None,
        market_sources=('tencent', 'eastmoney', 'sina', 'baostock'),
        market_profiles=('v2', 'v3'),
        market_sizes=(120, 720, 0),
        market_mode='all',
        market_batch_seconds=30.0,
        market_source_loop_seconds=120.0,
        market_task_seconds=600.0,
        cpu_profile_dir=None,
        security_master_minimum_rows=4000,
    )
    if action_identity in ("security-master","tencent","tencent-transport","market-factor-compare",):
        parser.add_argument('--source-timeout-seconds', type=float, default=4.5, help='vendor HTTP timeout; SDK socket timeout for BaoStock experiments')
    if action_identity in ("tencent","tushare",):
        parser.add_argument('--codes', nargs='+', default=('600519', '300750', '688981'), help='one to 50 six-digit A-share codes')
    if action_identity in ("tencent","market-factor-compare",):
        parser.add_argument('--source-samples', type=int, default=1, help='history and Tencent source sample rounds')
    if action_identity in ("tencent","market-factor-compare",):
        parser.add_argument('--source-interval-seconds', type=float, default=1.0, help='delay between source route samples')
    if action_identity in ("tencent-transport","tushare","market-factor-compare",):
        parser.add_argument('--runtime-config', default=str(PROJECT_ROOT / 'config/runtime.json'), help='runtime JSON configuration')
    if action_identity in ("tushare",):
        parser.add_argument('--history-days', type=int, default=61, help='history rows per stock; calendar-day window for BaoStock experiments')
    if action_identity in ("market-factor-compare",):
        parser.add_argument('--qfq-database', type=Path)
    if action_identity in ("market-factor-compare",):
        parser.add_argument('--market-sources', nargs='+', choices=_MARKET_SOURCES, default=_MARKET_SOURCES)
    if action_identity in ("market-factor-compare",):
        parser.add_argument('--market-profiles', nargs='+', choices=('v2', 'v3'), default=('v2', 'v3'))
    if action_identity in ("market-factor-compare",):
        parser.add_argument('--market-sizes', nargs='+', type=int, choices=(0, 120, 720), default=(120, 720, 0))
    if action_identity in ("market-factor-compare",):
        parser.add_argument('--market-mode', choices=('single', 'combined', 'all'), default='all')
    if action_identity in ("market-factor-compare",):
        parser.add_argument('--market-batch-seconds', type=float, default=30)
    if action_identity in ("market-factor-compare",):
        parser.add_argument('--market-source-loop-seconds', type=float, default=MARKET_SOURCE_LOOP_LIMIT_SECONDS, help='cumulative test budget per market source, capped at 120 seconds')
    if action_identity in ("market-factor-compare",):
        parser.add_argument('--market-task-seconds', type=float, default=MARKET_TASK_LIMIT_SECONDS, help='hard market comparison task budget, capped at 600 seconds')
    if action_identity in ("market-factor-compare",):
        parser.add_argument('--cpu-profile-dir', type=Path, help='external CPU evidence directory for market-factor-compare or performance')
    if action_identity == "security-master":
        parser.add_argument('--minimum-rows', dest='security_master_minimum_rows', type=int, default=4000)
    return parser


def ParseOptions(args: argparse.Namespace) -> MarketOptions:
    options = MarketOptions(
        action_identity=args.action_identity,
        command_timeout_seconds=args.command_timeout_seconds,
        source_timeout_seconds=args.source_timeout_seconds,
        codes=tuple(dict.fromkeys(args.codes)),
        source_samples=args.source_samples,
        source_interval_seconds=args.source_interval_seconds,
        runtime_config=Path(args.runtime_config).expanduser().resolve(),
        history_days=args.history_days,
        qfq_database=args.qfq_database.expanduser().resolve() if args.qfq_database is not None else None,
        market_sources=tuple(args.market_sources),
        market_profiles=tuple(args.market_profiles),
        market_sizes=tuple(args.market_sizes),
        market_mode=args.market_mode,
        market_batch_seconds=args.market_batch_seconds,
        market_source_loop_seconds=args.market_source_loop_seconds,
        market_task_seconds=args.market_task_seconds,
        cpu_profile_dir=ExternalPath(args.cpu_profile_dir, "--cpu-profile-dir"),
        security_master_minimum_rows=args.security_master_minimum_rows,
    )
    if not math.isfinite(options.command_timeout_seconds) or not 0 < options.command_timeout_seconds <= 7200:
        raise ValueError("command timeout must be positive and must not exceed 7200 seconds")
    if not options.codes or any(len(code) != 6 or not code.isdigit() for code in options.codes) or len(options.codes) > 50:
        raise ValueError("--codes must contain at most 50 six-digit A-share codes")
    if any(not math.isfinite(value) or value <= 0 for value in (
        options.source_samples, options.history_days, options.source_timeout_seconds,
    )):
        raise ValueError("sample, day and timeout values must be positive")
    if not math.isfinite(options.source_interval_seconds) or options.source_interval_seconds < 0:
        raise ValueError("sample intervals must be finite and nonnegative")
    if len(set(options.market_sources)) != len(options.market_sources):
        raise ValueError("--market-sources must be distinct")
    if options.market_mode != "single" and "tencent" not in options.market_sources:
        raise ValueError("combined market comparisons require Tencent as the full-market primary")
    if not math.isfinite(options.market_batch_seconds) or not 0 < options.market_batch_seconds <= 120:
        raise ValueError("--market-batch-seconds must be within 0..120")
    if not math.isfinite(options.market_source_loop_seconds) or not 0 < options.market_source_loop_seconds <= MARKET_SOURCE_LOOP_LIMIT_SECONDS:
        raise ValueError("--market-source-loop-seconds must be within 0..120")
    if not math.isfinite(options.market_task_seconds) or not 0 < options.market_task_seconds <= MARKET_TASK_LIMIT_SECONDS:
        raise ValueError("--market-task-seconds must be within 0..600")
    if options.security_master_minimum_rows <= 0:
        raise ValueError("--minimum-rows must be positive")
    return options
