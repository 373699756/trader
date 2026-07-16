"""Maintenance CLI for the v2 application."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = _absolute_config_path(args.config)
    runtime = load_runtime_settings(config_path)
    strategy = load_strategy_settings(runtime.strategy_config_path)
    watchlist = load_long_watchlist(runtime.long_watchlist_path)
    if args.command == "validate-config":
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
    return 2


def _absolute_config_path(raw_path: str) -> Path:
    if not raw_path:
        raise SystemExit("--config or TRADER_CONFIG is required")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise SystemExit("configuration path must be absolute")
    return path.resolve()


if __name__ == "__main__":
    raise SystemExit(main())
