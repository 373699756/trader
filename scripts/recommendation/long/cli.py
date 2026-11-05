"""Parse only the selected recommendation/long action and validate its inputs."""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT, ExternalPath
from scripts.recommendation.long.options import LongOptions


def BuildParser(
    action_identity: str, *, parents: Sequence[argparse.ArgumentParser] = ()
) -> argparse.ArgumentParser:
    if action_identity not in ("long-watchlist",):
        raise ValueError("unsupported recommendation/long action")
    parser = argparse.ArgumentParser(description=__doc__, parents=list(parents), allow_abbrev=False)
    if not parents:
        parser.add_argument("--command-timeout-seconds", type=float, default=180.0)
    parser.set_defaults(
        action_identity=action_identity,
        command_timeout_seconds=180.0,
        runtime_config=PROJECT_ROOT / 'config/runtime.json',
        long_watchlist=PROJECT_ROOT / 'config/long_watchlist.json',
        long_evidence_output=None,
        company_evidence_database=None,
        eligibility_evidence=(),
        strategy_config=PROJECT_ROOT / 'config/strategy.json',
        workers=8,
    )
    parser.add_argument('--runtime-config', default=str(PROJECT_ROOT / 'config/runtime.json'), help='runtime JSON configuration')
    parser.add_argument('--long-watchlist', type=Path, default=PROJECT_ROOT / 'config/long_watchlist.json')
    parser.add_argument('--long-evidence-output', type=Path, help='stock evidence report outside the repository')
    parser.add_argument('--company-evidence-database', type=Path, default=None, help='published company evidence database (read-only)')
    parser.add_argument('--eligibility-evidence', type=Path, action='append', default=[], help='read-only historical permanent-fact database (repeatable)')
    parser.add_argument('--strategy', type=Path, default=PROJECT_ROOT / 'config/strategy.json', dest='strategy_config', help='strategy JSON supplying the Long admission policy')
    parser.add_argument('--workers', type=int, default=8, help='bounded read-only admission workers (1..8)')
    return parser


def ParseOptions(args: argparse.Namespace) -> LongOptions:
    options = LongOptions(
        action_identity=args.action_identity,
        command_timeout_seconds=args.command_timeout_seconds,
        runtime_config=Path(args.runtime_config).expanduser().resolve(),
        long_watchlist=args.long_watchlist.expanduser().resolve(),
        long_evidence_output=ExternalPath(args.long_evidence_output, "--long-evidence-output"),
        company_evidence_database=args.company_evidence_database.expanduser().resolve() if args.company_evidence_database is not None else None,
        eligibility_evidence=tuple(args.eligibility_evidence),
        strategy_config=args.strategy_config.expanduser().resolve(),
        workers=args.workers,
    )
    if not math.isfinite(options.command_timeout_seconds) or not 0 < options.command_timeout_seconds <= 7200:
        raise ValueError("command timeout must be positive and must not exceed 7200 seconds")
    if not 1 <= options.workers <= 8:
        raise ValueError("workers must be 1..8")
    return options
