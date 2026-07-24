"""Maintenance CLI for the v2 application."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from trader.application.recommendations import RecommendationEngine
from trader.application.threshold_report import build_threshold_report
from trader.domain.recommendation.models import RecommendationSnapshot
from trader.entrypoints.performance import run_performance_check, write_report
from trader.infra.persistence.migration import migrate_v17_archive
from trader.infra.persistence.recommendation_archive import export_bundle, list_bundles, verify_bundle
from trader.infra.persistence.snapshots import snapshot_from_dict
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
    migrate = subparsers.add_parser("migrate-v17", help="Import committed v2 freezes into the isolated v17 runtime.")
    migrate.add_argument("--source-runtime", required=True, help="Absolute read-only v2 runtime directory.")
    perf = subparsers.add_parser("perf-check", help="Run the fixed offline v17 performance acceptance suite.")
    perf.add_argument("--fixture", required=True, help="Absolute fixture directory.")
    perf.add_argument(
        "--suite",
        required=True,
        choices=("market-data", "board-scoring", "api-sse", "end-to-end", "all"),
    )
    perf.add_argument("--output", required=True, help="Absolute JSON report path.")
    perf.add_argument("--baseline", help="Absolute prior report with the same identity.")
    recommendation_archive = subparsers.add_parser(
        "recommendation-archive",
        help="List, verify, or export recommendation bundles older than the active 20 dates.",
    )
    archive_commands = recommendation_archive.add_subparsers(dest="archive_command", required=True)
    archive_list = archive_commands.add_parser("list", help="List archived recommendation bundles.")
    _add_archive_identity_arguments(archive_list, required=False)
    archive_verify = archive_commands.add_parser("verify", help="Verify an archived recommendation bundle.")
    _add_archive_identity_arguments(archive_verify, required=True)
    archive_export = archive_commands.add_parser("export", help="Export a verified recommendation bundle.")
    _add_archive_identity_arguments(archive_export, required=True)
    archive_export.add_argument("--output", required=True, help="Absolute export directory.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    direct_handler = {
        "validate-config": _run_validate_config,
        "recommendation-archive": _run_recommendation_archive,
    }.get(args.command)
    if direct_handler is not None:
        return direct_handler(args)
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
    if args.command == "migrate-v17":
        config_path = _absolute_config_path(args.config)
        runtime = load_runtime_settings(config_path)
        source = _absolute_directory_path(args.source_runtime, argument="--source-runtime")
        try:
            result = migrate_v17_archive(source, runtime.runtime_dir)
        except (OSError, ValueError, RuntimeError) as exc:
            raise SystemExit(f"v17 migration failed: {exc}") from exc
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "perf-check":
        config_path = _absolute_config_path(args.config)
        runtime = load_runtime_settings(config_path)
        fixture = _absolute_directory_path(args.fixture, argument="--fixture")
        output = _absolute_output_path(args.output, argument="--output")
        baseline = _absolute_file_path(args.baseline, argument="--baseline") if args.baseline else None
        try:
            result = run_performance_check(
                fixture,
                suite=args.suite,
                budgets=runtime.performance_budgets,
                config_path=config_path,
                baseline_path=baseline,
            )
            write_report(output, result)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"performance check failed: {exc}") from exc
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] == "passed" else 1
    return 2


def _add_archive_identity_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument("--strategy", required=required, choices=("today", "tomorrow", "d25"))
    parser.add_argument("--trade-date", required=required, help="Recommendation date in YYYY-MM-DD format.")
    parser.add_argument("--snapshot-id", help="Optional exact snapshot identity.")


def _run_validate_config(args: argparse.Namespace) -> int:
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


def _run_recommendation_archive(args: argparse.Namespace) -> int:
    config_path = _absolute_config_path(args.config)
    runtime = load_runtime_settings(config_path)
    bundles = tuple(
        item
        for item in list_bundles(runtime.runtime_dir)
        if (not args.strategy or item["strategy"] == args.strategy)
        and (not args.trade_date or item["trade_date"] == args.trade_date)
        and (not args.snapshot_id or item["snapshot_id"] == args.snapshot_id)
    )
    if args.archive_command == "list":
        print(json.dumps({"bundles": bundles}, ensure_ascii=False, sort_keys=True))
        return 0
    if len(bundles) != 1:
        raise SystemExit(f"archive identity matched {len(bundles)} bundles; expected exactly one")
    bundle = runtime.runtime_dir / bundles[0]["relative_path"]
    try:
        manifest = verify_bundle(bundle)
        if args.archive_command == "verify":
            print(
                json.dumps(
                    {"status": "ok", "bundle": bundles[0], "schema": manifest["schema"]},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        output = _absolute_output_path(args.output, argument="--output")
        exported = export_bundle(bundle, output)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"recommendation archive operation failed: {exc}") from exc
    print(json.dumps({"status": "ok", "exported": str(exported)}, ensure_ascii=False, sort_keys=True))
    return 0


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


def _absolute_directory_path(raw_path: str, *, argument: str) -> Path:
    path = _absolute_file_path(raw_path, argument=argument)
    if not path.is_dir():
        raise SystemExit(f"{argument} directory does not exist")
    return path


def _absolute_output_path(raw_path: str, *, argument: str) -> Path:
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
