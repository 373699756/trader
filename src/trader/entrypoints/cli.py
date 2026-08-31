"""V2 configuration, performance, and explicit research command entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import cast

from trader.application.ports.tomorrow_model import TomorrowScoringProfile
from trader.infra.settings import RuntimeSettings, load_long_watchlist, load_runtime_settings, load_strategy_settings

_COMMAND_GROUPS = {
    "check": ("validate-config", "research-status", "performance-check"),
    "research-history": ("research-history-download", "research-backtest"),
    "research-screen": (
        "research-r6-screen",
        "research-r6-daily-screen",
        "research-r6-stability-screen",
        "research-tomorrow-p2-screen",
        "research-tomorrow-v1-v2-holdout",
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader-cli")
    parser.add_argument(
        "--config",
        default=os.environ.get("TRADER_CONFIG", ""),
        help="Absolute path to config/v2/runtime.json.",
    )
    parser.add_argument(
        "--profile",
        choices=("v1", "v2"),
        help="Effective Tomorrow scoring profile for this process; config value is used when omitted.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "check",
        help="Run config validation, research readiness, and the active-profile performance gate.",
    )
    history = subparsers.add_parser(
        "research-history",
        help="Resume the fixed history archive and then run its read-only backtest.",
    )
    history.add_argument("--workers", type=int, choices=range(1, 6), default=5)
    subparsers.add_parser(
        "research-screen",
        help="Run all immutable historical screening, stability, and profile-holdout stages in order.",
    )
    subparsers.add_parser("validate-config", help="Validate runtime and strategy configuration.")
    performance = subparsers.add_parser(
        "performance-check",
        help="Run the offline active-production performance gate without supplier network access.",
    )
    performance.add_argument("--output", type=Path)
    performance.add_argument("--baseline", type=Path)
    subparsers.add_parser("research-status", help="Read immutable research coverage and capacity status.")
    download = subparsers.add_parser(
        "research-history-download",
        help="Download the fixed retrospective qfq history archive; resumable and separate from serve.",
    )
    download.add_argument("--workers", type=int, choices=range(1, 6), default=5)
    subparsers.add_parser("research-backtest", help="Run the read-only fixed train/validation bar diagnostic.")
    subparsers.add_parser("research-r6-screen", help="Run and immutably seal the preregistered Score-R6 screen.")
    subparsers.add_parser(
        "research-r6-daily-screen",
        help="Run and seal the preregistered risk-adjusted daily trend screen.",
    )
    subparsers.add_parser(
        "research-r6-stability-screen",
        help="Run and seal the preregistered daily ranking stability diagnostic.",
    )
    subparsers.add_parser(
        "research-tomorrow-p2-screen",
        help="Run and immutably seal the single frozen Tomorrow P2 historical candidate.",
    )
    subparsers.add_parser(
        "research-tomorrow-v1-v2-holdout",
        help="Evaluate and seal both packaged Tomorrow profiles on the same H0 validation rows.",
    )
    subparsers.add_parser(
        "research-tomorrow-profile-report",
        help="Read paired forward evidence and emit the preregistered manual-review report.",
    )
    dossier = subparsers.add_parser(
        "research-r7-dossier",
        help="Recompute eligible Score-R6 evidence and seal a pending human-review dossier.",
    )
    dossier.add_argument("--research-identity", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = _absolute_config_path(args.config)
    runtime = load_runtime_settings(config_path)
    profile_override = cast(TomorrowScoringProfile | None, args.profile)
    if args.command in _COMMAND_GROUPS:
        return _run_command_group(
            args.command,
            config_path,
            _effective_profile(runtime, profile_override),
            workers=int(getattr(args, "workers", 5)),
        )
    if args.command == "performance-check":
        return _run_performance_report(
            config_path,
            output=args.output,
            baseline=args.baseline,
            tomorrow_scoring_profile=profile_override,
        )
    if args.command.startswith("research-"):
        from trader.entrypoints.research_commands import ResearchCommandOptions, run_research_command

        return run_research_command(
            args.command,
            config_path,
            runtime,
            ResearchCommandOptions(
                workers=int(getattr(args, "workers", 5)),
                research_identity=str(getattr(args, "research_identity", "")),
            ),
        )
    return _run_config_validation(runtime, profile_override)


def _run_config_validation(
    runtime: RuntimeSettings,
    profile_override: TomorrowScoringProfile | None,
) -> int:
    strategy = load_strategy_settings(
        runtime.strategy_config_path,
        tomorrow_scoring_profile=profile_override,
    )
    watchlist = load_long_watchlist(runtime.long_watchlist_path)
    print(
        json.dumps(
            {
                "status": "ok",
                "runtime_version": runtime.config_version,
                "strategy_version": strategy.strategy_version,
                "tomorrow_scoring_profile": strategy.tomorrow_scoring_profile,
                "watchlist_version": watchlist.watchlist_version,
                "runtime_dir": str(runtime.runtime_dir),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _effective_profile(
    runtime: RuntimeSettings,
    profile_override: TomorrowScoringProfile | None,
) -> TomorrowScoringProfile:
    if profile_override is not None:
        return profile_override
    return load_strategy_settings(runtime.strategy_config_path).tomorrow_scoring_profile


def _run_performance_report(
    config_path: Path,
    *,
    output: Path | None,
    baseline: Path | None,
    tomorrow_scoring_profile: TomorrowScoringProfile | None,
) -> int:
    from trader.entrypoints.performance import run as run_performance

    report = run_performance(
        config_path,
        baseline_path=baseline,
        tomorrow_scoring_profile=tomorrow_scoring_profile,
    )
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if output is not None:
        output.write_text(f"{payload}\n", encoding="utf-8")
    print(payload)
    return 0 if report["status"] == "passed" else 1


def _run_command_group(
    command: str,
    config_path: Path,
    profile: TomorrowScoringProfile,
    *,
    workers: int,
) -> int:
    results: list[dict[str, str | int]] = []
    stages = _COMMAND_GROUPS[command]
    for index, stage in enumerate(stages, start=1):
        print(f"[{index}/{len(stages)}] {stage}", file=sys.stderr, flush=True)
        stage_argv = ["--config", str(config_path), "--profile", profile, stage]
        if command == "research-history" and stage == "research-history-download":
            stage_argv.extend(("--workers", str(workers)))
        exit_code = _run_group_stage(stage_argv)
        results.append({"command": stage, "exit_code": exit_code})
    failed = any(int(item["exit_code"]) != 0 for item in results)
    print(
        json.dumps(
            {
                "schema_version": "trader_command_group_v1",
                "command": command,
                "profile": profile,
                "status": "completed_with_failures" if failed else "passed",
                "stages": results,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 1 if failed else 0


def _run_group_stage(argv: list[str]) -> int:
    return main(argv)


def _absolute_config_path(raw_path: str) -> Path:
    if not raw_path:
        raise SystemExit("--config or TRADER_CONFIG is required")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise SystemExit("configuration path must be absolute")
    return path.resolve()


if __name__ == "__main__":
    raise SystemExit(main())
