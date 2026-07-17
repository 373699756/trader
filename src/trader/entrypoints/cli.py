"""Maintenance CLI for the v2 application."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from trader.application.recommendations import RecommendationEngine
from trader.application.threshold_report import build_threshold_report
from trader.domain.models import RecommendationSnapshot
from trader.infrastructure.persistence.snapshots import snapshot_from_dict
from trader.infrastructure.settings import load_long_watchlist, load_runtime_settings, load_strategy_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader-cli")
    parser.add_argument(
        "--config",
        default=os.environ.get("TRADER_CONFIG", ""),
        help="Absolute path to config/v2/runtime.json.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-config", help="Validate runtime and strategy configuration.")
    verify = subparsers.add_parser("verify-freeze", help="Replay and verify a frozen snapshot from its inputs.")
    verify.add_argument("--snapshot", required=True, help="Absolute path to a frozen snapshot JSON file.")
    threshold_report = subparsers.add_parser(
        "threshold-report",
        help="Report pre-registration metrics from one or more frozen snapshots.",
    )
    threshold_report.add_argument(
        "--snapshot",
        required=True,
        action="append",
        help="Absolute frozen snapshot JSON path; repeat for multiple dates.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-config":
        config_path = _absolute_config_path(args.config)
        runtime = load_runtime_settings(config_path)
        strategy = load_strategy_settings(runtime.strategy_config_path)
        watchlist = load_long_watchlist(runtime.long_watchlist_path)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "runtime_version": runtime.config_version,
                    "strategy_version": strategy.strategy_version,
                    "watchlist_version": watchlist.watchlist_version,
                    "runtime_dir": str(runtime.runtime_dir),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "verify-freeze":
        snapshot_path = _absolute_file_path(args.snapshot, argument="--snapshot")
        try:
            snapshot = _load_snapshot(snapshot_path)
            result = RecommendationEngine.verify_frozen(snapshot)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise SystemExit(f"freeze verification failed: {exc}") from exc
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "threshold-report":
        try:
            snapshots = tuple(
                _load_snapshot(_absolute_file_path(raw_path, argument="--snapshot")) for raw_path in args.snapshot
            )
            result = build_threshold_report(snapshots)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise SystemExit(f"threshold report failed: {exc}") from exc
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    return 2


def _absolute_config_path(raw_path: str) -> Path:
    if not raw_path:
        raise SystemExit("--config or TRADER_CONFIG is required")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise SystemExit("configuration path must be absolute")
    return path.resolve()


def _absolute_file_path(raw_path: str, *, argument: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"{argument} path must be absolute")
    return path.resolve()


def _load_snapshot(path: Path) -> RecommendationSnapshot:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("snapshot root must be an object")
    return snapshot_from_dict(raw)


if __name__ == "__main__":
    raise SystemExit(main())
