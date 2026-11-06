"""Parse only the selected recommendation/web action and validate its inputs."""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT
from scripts.recommendation.web.options import DEFAULT_SIMULATED_START, WebOptions
from scripts.recommendation.pipeline.cli import ParseScoringProfile


def BuildParser(
    action_identity: str, *, parents: Sequence[argparse.ArgumentParser] = ()
) -> argparse.ArgumentParser:
    if action_identity not in ("web","browser","browser-live","company-ledger",):
        raise ValueError("unsupported recommendation/web action")
    parser = argparse.ArgumentParser(description=__doc__, parents=list(parents), allow_abbrev=False)
    if not parents:
        parser.add_argument("--command-timeout-seconds", type=float, default=180.0)
    parser.set_defaults(
        action_identity=action_identity,
        command_timeout_seconds=180.0,
        base_url='http://127.0.0.1:5000',
        web_samples=3,
        web_interval_seconds=2.0,
        web_timeout_seconds=3.0,
        scoring_profile=None,
        runtime_config=PROJECT_ROOT / 'config/runtime.json',
        browser_duration_seconds=35.0,
        browser_minimum_updates=3,
        strategies=None,
        browser_strategy="tomorrow",
        simulated_start=DEFAULT_SIMULATED_START,
        consecutive_zero_threshold=None,
        web_complete_rounds=0,
        web_observation_seconds=None,
    )
    if action_identity in ("web","browser-live",):
        parser.add_argument('--base-url', default='http://127.0.0.1:5000', help='running trader-server base URL')
    if action_identity in ("web",):
        parser.add_argument('--web-samples', type=int, default=3, help='Web status/current sample rounds')
    if action_identity in ("web",):
        parser.add_argument('--web-interval-seconds', type=float, default=2.0, help='delay between Web samples')
    if action_identity in ("web",):
        parser.add_argument('--web-timeout-seconds', type=float, default=3.0, help='timeout per Web API request')
    if action_identity in ("web",):
        parser.add_argument('--scoring-profile', type=ParseScoringProfile, default=None, help='one of v1/v2/v3/v5, or all four comma-separated profiles for performance')
        parser.add_argument('--strategy', choices=("tomorrow", "d25"), action='append', dest='strategies', help='short strategy to inspect; repeat to select multiple (default: all)')
        parser.add_argument('--consecutive-zero-threshold', type=int, help='persistent-zero rounds within 1..web-samples (default: min(3, web-samples))')
        parser.add_argument('--web-complete-rounds', type=int, default=0, help='explicit live SSE/atomic eight-head observation; require 1..20 consecutive fresh rounds')
        parser.add_argument('--web-observation-seconds', type=float, help='bounded complete-round observation, at most 900 seconds')
    if action_identity in ("browser","company-ledger",):
        parser.add_argument('--runtime-config', default=str(PROJECT_ROOT / 'config/runtime.json'), help='runtime JSON configuration')
    if action_identity in ("browser",):
        parser.add_argument('--browser-duration-seconds', type=float, default=35.0, help='browser observation duration')
    if action_identity in ("browser",):
        parser.add_argument('--browser-minimum-updates', type=int, default=3, help='required browser DOM updates')
        parser.add_argument('--strategy', choices=("tomorrow", "d25"), default="tomorrow", dest='browser_strategy', help='dashboard strategy to observe')
        parser.add_argument('--simulated-start', type=_parse_simulated_start, default=DEFAULT_SIMULATED_START, help='timezone-aware ISO trading time for the isolated scheduler fixture')
    return parser


def ParseOptions(args: argparse.Namespace) -> WebOptions:
    options = WebOptions(
        action_identity=args.action_identity,
        command_timeout_seconds=args.command_timeout_seconds,
        base_url=args.base_url,
        web_samples=args.web_samples,
        web_interval_seconds=args.web_interval_seconds,
        web_timeout_seconds=args.web_timeout_seconds,
        scoring_profile=args.scoring_profile,
        runtime_config=Path(args.runtime_config).expanduser().resolve(),
        browser_duration_seconds=args.browser_duration_seconds,
        browser_minimum_updates=args.browser_minimum_updates,
        strategies=tuple(dict.fromkeys(args.strategies or ())),
        browser_strategy=args.browser_strategy,
        simulated_start=args.simulated_start,
        consecutive_zero_threshold=min(3, args.web_samples) if args.consecutive_zero_threshold is None else args.consecutive_zero_threshold,
        web_complete_rounds=args.web_complete_rounds,
        web_observation_seconds=args.web_observation_seconds,
    )
    if not math.isfinite(options.command_timeout_seconds) or not 0 < options.command_timeout_seconds <= 7200:
        raise ValueError("command timeout must be positive and must not exceed 7200 seconds")
    if options.scoring_profile == "v1,v2,v3,v5":
        raise ValueError("four-profile scoring is only available for performance")
    if any(not math.isfinite(value) or value <= 0 for value in (
        options.web_samples, options.web_timeout_seconds, options.browser_duration_seconds, options.browser_minimum_updates,
    )):
        raise ValueError("sample, duration and timeout values must be positive")
    if not math.isfinite(options.web_interval_seconds) or options.web_interval_seconds < 0:
        raise ValueError("sample intervals must be finite and nonnegative")
    if not 1 <= options.consecutive_zero_threshold <= options.web_samples:
        raise ValueError("--consecutive-zero-threshold must be within 1..web-samples")
    if options.web_complete_rounds:
        duration = options.web_observation_seconds
        if (
            not 1 <= options.web_complete_rounds <= 20 or duration is None
            or not math.isfinite(duration) or not 0 < duration <= 900
        ):
            raise ValueError("--web-complete-rounds requires 1..20 rounds and --web-observation-seconds within (0, 900]")
        if options.web_interval_seconds < 15:
            raise ValueError("complete-round observation requires --web-interval-seconds at least 15")
        if options.strategies:
            raise ValueError("complete-round observation requires both short strategies")
        if options.command_timeout_seconds < duration + 2 * options.web_timeout_seconds + 10:
            raise ValueError("command timeout must leave HTTP and cleanup time beyond the observation duration")
    elif options.web_observation_seconds is not None:
        raise ValueError("--web-observation-seconds requires --web-complete-rounds")
    return options


def _parse_simulated_start(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--simulated-start must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--simulated-start must include a timezone offset")
    return parsed
