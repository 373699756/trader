"""Small V2 configuration CLI."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import date
from pathlib import Path

from trader.domain.research.specification import ACTIVE_SCORE_RESEARCH_SPEC, SCORE_P0_V1_SPEC
from trader.infra.persistence.outcomes import SQLiteOutcomeEvidenceRepository
from trader.infra.persistence.research_trace import SQLiteV2ResearchTraceStore
from trader.infra.settings import load_long_watchlist, load_runtime_settings, load_strategy_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader-cli")
    parser.add_argument(
        "--config",
        default=os.environ.get("TRADER_CONFIG", ""),
        help="Absolute path to config/v2/runtime.json.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-config", help="Validate runtime and strategy configuration.")
    subparsers.add_parser("research-status", help="Read immutable research coverage and capacity status.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = _absolute_config_path(args.config)
    runtime = load_runtime_settings(config_path)
    if args.command == "research-status":
        trace = SQLiteV2ResearchTraceStore(runtime.runtime_dir)
        status = trace.inspect_status()
        dates = trace.inspect_trade_dates(limit=120)
        recorded_dates = frozenset(dates)
        active = ACTIVE_SCORE_RESEARCH_SPEC
        historical_count = len(recorded_dates.intersection(active.historical_dates))
        forward_count = len(recorded_dates.intersection(active.forward_dates))
        history_complete = historical_count == len(active.historical_dates)
        print(
            json.dumps(
                {
                    "schema_version": "v2_research_readiness_v2",
                    "research_state": (
                        "historical_ready_for_offline_evaluation" if history_complete else "historical_collecting"
                    ),
                    "score_r6_executable": False,
                    "blockers": [
                        (
                            "score_p0_v2_r2_r5_not_run"
                            if history_complete
                            else "score_p0_v2_historical_observations_incomplete"
                        ),
                        "score_r5_promotion_eligible_missing",
                    ],
                    "recorded_trade_dates": [value.isoformat() for value in dates],
                    "active_research": {
                        "research_identity": active.research_identity,
                        "research_spec_hash": active.content_hash,
                        "preregistered_on": active.preregistered_on.isoformat(),
                        "historical_window": _window_status(active.historical_dates, historical_count),
                        "forward_window": _window_status(active.forward_dates, forward_count),
                    },
                    "legacy_research": {
                        "research_identity": SCORE_P0_V1_SPEC.research_identity,
                        "research_spec_hash": SCORE_P0_V1_SPEC.content_hash,
                        "research_state": "historical_rejected",
                        "blocker": "historical_point_in_time_missing",
                    },
                    "archive": asdict(status),
                    "outcomes": asdict(SQLiteOutcomeEvidenceRepository.inspect_status(runtime.runtime_dir)),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command != "validate-config":
        return 2
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


def _absolute_config_path(raw_path: str) -> Path:
    if not raw_path:
        raise SystemExit("--config or TRADER_CONFIG is required")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise SystemExit("configuration path must be absolute")
    return path.resolve()


def _window_status(planned_dates: tuple[date, ...], recorded_count: int) -> dict[str, object]:
    return {
        "start": str(planned_dates[0]),
        "end": str(planned_dates[-1]),
        "planned_trade_dates": len(planned_dates),
        "recorded_trade_dates": recorded_count,
    }


if __name__ == "__main__":
    raise SystemExit(main())
