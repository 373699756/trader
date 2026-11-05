"""Parse only the selected recommendation/pipeline action and validate its inputs."""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from scripts.core.paths import PROJECT_ROOT, ExternalPath
from scripts.recommendation.pipeline.options import PipelineOptions
from scripts.recommendation.pipeline.options import ScoringProfileSelection
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection
from trader.recommendation.infra.performance.candidate_selection import SCORING_CANDIDATES


def BuildParser(
    action_identity: str, *, parents: Sequence[argparse.ArgumentParser] = ()
) -> argparse.ArgumentParser:
    if action_identity not in ("snapshot","research","performance",):
        raise ValueError("unsupported recommendation/pipeline action")
    parser = argparse.ArgumentParser(description=__doc__, parents=list(parents), allow_abbrev=False)
    if not parents:
        parser.add_argument("--command-timeout-seconds", type=float, default=180.0)
    parser.set_defaults(
        action_identity=action_identity,
        command_timeout_seconds=180.0,
        runtime_config=PROJECT_ROOT / 'config/runtime.json',
        scoring_profile=None,
        performance_steady_only=False,
        performance_factor_candidate='baseline',
        performance_scoring_candidate='baseline',
        performance_replay_ticks=0,
        performance_replay_rows=None,
        cpu_profile_dir=None,
        cpu_profile_mode='functions',
    )
    parser.add_argument('--runtime-config', default=str(PROJECT_ROOT / 'config/runtime.json'), help='runtime JSON configuration')
    parser.add_argument('--scoring-profile', type=ParseScoringProfile, default=None, help='one of v1/v2/v3/v5, or all four comma-separated profiles for performance')
    if action_identity in ("performance",):
        parser.add_argument('--performance-steady-only', action='store_true', help='only the existing 100-tick model RSS measurement')
    if action_identity in ("performance",):
        parser.add_argument('--performance-factor-candidate', choices=('baseline', 'raw_factor_reuse'), default='baseline', help='explicit offline raw-factor reuse candidate; requires continuous replay')
    if action_identity in ("performance",):
        parser.add_argument('--performance-scoring-candidate', choices=SCORING_CANDIDATES, default='baseline', help='offline qualification/reference reuse or independent V5 industry executor; requires continuous replay')
    if action_identity in ("performance",):
        parser.add_argument('--performance-replay-ticks', type=int, default=0, help='1..1000 continuous ticks per scenario instead of model microbenchmarks')
    if action_identity in ("performance",):
        parser.add_argument('--performance-replay-rows', type=int, help='bounded diagnostic population; requires replay ticks, excludes full-market acceptance')
    if action_identity in ("performance",):
        parser.add_argument('--cpu-profile-dir', type=Path, help='external CPU evidence directory for market-factor-compare or performance')
    if action_identity in ("performance",):
        parser.add_argument('--cpu-profile-mode', choices=('functions', 'gc'), default='functions')
    return parser


def ParseOptions(args: argparse.Namespace) -> PipelineOptions:
    options = PipelineOptions(
        action_identity=args.action_identity,
        command_timeout_seconds=args.command_timeout_seconds,
        runtime_config=Path(args.runtime_config).expanduser().resolve(),
        scoring_profile=args.scoring_profile,
        performance_steady_only=args.performance_steady_only,
        performance_factor_candidate=args.performance_factor_candidate,
        performance_scoring_candidate=args.performance_scoring_candidate,
        performance_replay_ticks=args.performance_replay_ticks,
        performance_replay_rows=args.performance_replay_rows,
        cpu_profile_dir=ExternalPath(args.cpu_profile_dir, "--cpu-profile-dir"),
        cpu_profile_mode=args.cpu_profile_mode,
    )
    if not math.isfinite(options.command_timeout_seconds) or not 0 < options.command_timeout_seconds <= 7200:
        raise ValueError("command timeout must be positive and must not exceed 7200 seconds")
    if options.scoring_profile == "v1,v2,v3,v5" and options.action_identity != "performance":
        raise ValueError("four-profile scoring is only available for performance")
    if options.performance_steady_only and (options.performance_replay_ticks or options.cpu_profile_dir is not None):
        raise ValueError("--performance-steady-only requires performance without replay or CPU tracing")
    if not 0 <= options.performance_replay_ticks <= 1000:
        raise ValueError("--performance-replay-ticks requires a value within 0..1000")
    if options.performance_replay_rows is not None and (options.performance_replay_rows < 1 or not options.performance_replay_ticks):
        raise ValueError("--performance-replay-rows requires positive rows and --performance-replay-ticks")
    if options.performance_factor_candidate != "baseline" and (not options.performance_replay_ticks or options.performance_steady_only):
        raise ValueError("--performance-factor-candidate requires performance continuous replay")
    if options.performance_scoring_candidate != "baseline" and (not options.performance_replay_ticks or options.performance_steady_only):
        raise ValueError("--performance-scoring-candidate requires performance continuous replay")
    if options.cpu_profile_dir is not None and options.performance_replay_ticks != 1:
        raise ValueError("--cpu-profile-dir requires --performance-replay-ticks 1")
    if options.cpu_profile_mode == "gc" and options.cpu_profile_dir is None:
        raise ValueError("--cpu-profile-mode gc requires performance and --cpu-profile-dir")
    return options


def ParseScoringProfile(value: str) -> ScoringProfileSelection:
    try:
        profiles = ParseProfileSelection(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if len(profiles) == 1:
        return cast(ScoringProfileSelection, profiles[0])
    if set(profiles) == set(SCORING_PROFILE_IDS):
        return "v1,v2,v3,v5"
    raise argparse.ArgumentTypeError("select one scoring profile or all four profiles")
