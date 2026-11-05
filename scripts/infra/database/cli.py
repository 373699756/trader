"""Parse only the selected infra/database action and validate its inputs."""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT, ExternalPath
from scripts.infra.database.options import DatabaseOptions


def BuildParser(
    action_identity: str, *, parents: Sequence[argparse.ArgumentParser] = ()
) -> argparse.ArgumentParser:
    if action_identity not in ("database-baseline",):
        raise ValueError("unsupported infra/database action")
    parser = argparse.ArgumentParser(description=__doc__, parents=list(parents), allow_abbrev=False)
    if not parents:
        parser.add_argument("--command-timeout-seconds", type=float, default=180.0)
    parser.set_defaults(
        action_identity=action_identity,
        command_timeout_seconds=180.0,
        runtime_config=PROJECT_ROOT / 'config/runtime.json',
        database_probe_directory=None,
        database_rounds=5,
        database_training=False,
        database_acceptance_focus='history',
        download_sizes=(8,),
        history_days=61,
        baseline_checkout=None,
    )
    parser.add_argument('--runtime-config', default=str(PROJECT_ROOT / 'config/runtime.json'), help='runtime JSON configuration')
    parser.add_argument('--database-probe-directory', type=Path, help='new external directory for SQLite benchmarks')
    parser.add_argument('--database-rounds', type=int, choices=range(1, 6), default=5, help='database baseline repetitions')
    parser.add_argument('--database-training', action='store_true', help='include four-profile sample I/O in the isolated database baseline')
    parser.add_argument('--database-acceptance-focus', choices=('history', 'market-company'), default='history')
    parser.add_argument('--download-sizes', nargs='+', type=int, default=(8,), help='one isolated database workload size within 1..32')
    parser.add_argument('--history-days', type=int, default=61, help='history rows per stock; calendar-day window for BaoStock experiments')
    parser.add_argument('--baseline-checkout', type=Path, help='isolated checkout for history-write, download-performance or database-baseline comparisons')
    return parser


def ParseOptions(args: argparse.Namespace) -> DatabaseOptions:
    options = DatabaseOptions(
        action_identity=args.action_identity,
        command_timeout_seconds=args.command_timeout_seconds,
        runtime_config=Path(args.runtime_config).expanduser().resolve(),
        database_probe_directory=ExternalPath(args.database_probe_directory, "--database-probe-directory"),
        database_rounds=args.database_rounds,
        database_training=args.database_training,
        database_acceptance_focus=args.database_acceptance_focus,
        download_sizes=tuple(args.download_sizes),
        history_days=args.history_days,
        baseline_checkout=ExternalPath(args.baseline_checkout, "--baseline-checkout"),
    )
    if not math.isfinite(options.command_timeout_seconds) or not 0 < options.command_timeout_seconds <= 7200:
        raise ValueError("command timeout must be positive and must not exceed 7200 seconds")
    if options.database_probe_directory is None:
        raise ValueError("database-baseline requires --database-probe-directory")
    if options.database_training and options.baseline_checkout is not None:
        raise ValueError("--database-training requires database-baseline without a comparison checkout")
    if not 5 <= options.history_days <= 2000:
        raise ValueError("database-baseline requires 5..2000 history days")
    if len(options.download_sizes) != 1 or not 1 <= options.download_sizes[0] <= 32:
        raise ValueError("database-baseline requires one download size in 1..32")
    return options
