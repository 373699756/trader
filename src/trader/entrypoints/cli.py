"""Small V2 configuration CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

from trader.application.research.score_r7 import build_score_r7_promotion_dossier
from trader.bootstrap import build_historical_research_services
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.domain.research.score_r6 import SCORE_R6_HISTORICAL_SPEC
from trader.domain.research.specification import ACTIVE_SCORE_RESEARCH_SPEC, SCORE_P0_V1_SPEC
from trader.infra.persistence.outcomes import SQLiteOutcomeEvidenceRepository
from trader.infra.persistence.research_trace import SQLiteV2ResearchTraceStore
from trader.infra.research.history_archive import SQLiteHistoricalArchive
from trader.infra.research.score_r6_artifacts import ScoreR6ArtifactConflictError, ScoreR6ArtifactStore
from trader.infra.research.score_r7_artifacts import ScoreR7ArtifactConflictError, ScoreR7ArtifactStore
from trader.infra.settings import RuntimeSettings, load_long_watchlist, load_runtime_settings, load_strategy_settings


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
    download = subparsers.add_parser(
        "research-history-download",
        help="Download the fixed retrospective qfq history archive; resumable and separate from serve.",
    )
    download.add_argument("--workers", type=int, choices=range(1, 6), default=5)
    subparsers.add_parser("research-backtest", help="Run the read-only fixed train/validation bar diagnostic.")
    subparsers.add_parser("research-r6-screen", help="Run and immutably seal the preregistered Score-R6 screen.")
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
    if args.command == "research-status":
        trace = SQLiteV2ResearchTraceStore(runtime.runtime_dir)
        status = trace.inspect_status()
        dates = trace.inspect_trade_dates(limit=120)
        recorded_dates = frozenset(dates)
        active = ACTIVE_SCORE_RESEARCH_SPEC
        historical_count = len(recorded_dates.intersection(active.historical_dates))
        forward_count = len(recorded_dates.intersection(active.forward_dates))
        history_complete = historical_count == len(active.historical_dates)
        historical_archive = SQLiteHistoricalArchive(runtime.runtime_dir).inspect(SCORE_H0_V1_SPEC.research_identity)
        screening_coverage = (
            historical_archive.completed_codes / historical_archive.universe_count
            if historical_archive.universe_count
            else 0.0
        )
        screening_ready = historical_archive.spec_hash == SCORE_H0_V1_SPEC.content_hash and screening_coverage >= 0.95
        try:
            score_r6 = ScoreR6ArtifactStore(runtime.runtime_dir / "score-r6").inspect()
            score_r6_artifact_error = ""
        except ScoreR6ArtifactConflictError:
            score_r6 = {
                "historical_report_hash": "",
                "historical_gate_passed": False,
                "forward_research": [],
                "promotion_eligible": False,
            }
            score_r6_artifact_error = "score_r6_artifact_invalid"
        promotion_ready = bool(score_r6["promotion_eligible"])
        try:
            score_r7 = ScoreR7ArtifactStore(runtime.runtime_dir / "score-r7").inspect()
        except ScoreR7ArtifactConflictError:
            score_r7 = {"dossiers": [], "dossier_count": 0, "artifact_error": "score_r7_artifact_invalid"}
        print(
            json.dumps(
                {
                    "schema_version": "v2_research_readiness_v2",
                    "research_state": (
                        "historical_ready_for_offline_evaluation" if history_complete else "historical_collecting"
                    ),
                    "score_r6_executable": screening_ready,
                    "score_r6_screening_executable": screening_ready,
                    "score_r6_promotion_executable": promotion_ready,
                    "blockers": [] if screening_ready else ["score_h0_archive_coverage_incomplete"],
                    "promotion_blockers": (
                        []
                        if promotion_ready
                        else [score_r6_artifact_error or "score_r6_preregistered_forward_evidence_missing"]
                    ),
                    "score_r6": score_r6,
                    "score_r7": score_r7,
                    "recorded_trade_dates": [value.isoformat() for value in dates],
                    "active_research": {
                        "research_identity": active.research_identity,
                        "research_spec_hash": active.content_hash,
                        "preregistered_on": active.preregistered_on.isoformat(),
                        "evaluation_blocker": (
                            "score_p0_v2_r2_r5_not_run"
                            if history_complete
                            else "score_p0_v2_historical_observations_incomplete"
                        ),
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
                    "historical_screening": {
                        **asdict(historical_archive),
                        "coverage_ratio": round(screening_coverage, 6),
                        "research_spec_hash": SCORE_H0_V1_SPEC.content_hash,
                        "training_window": {
                            "start": SCORE_H0_V1_SPEC.training_start.isoformat(),
                            "end": SCORE_H0_V1_SPEC.training_end.isoformat(),
                        },
                        "validation_window": {
                            "start": SCORE_H0_V1_SPEC.validation_start.isoformat(),
                            "end": SCORE_H0_V1_SPEC.validation_end.isoformat(),
                        },
                        "promotion_authority": False,
                    },
                    "outcomes": asdict(SQLiteOutcomeEvidenceRepository.inspect_status(runtime.runtime_dir)),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "research-history-download":
        services = build_historical_research_services(config_path, workers=args.workers)

        def progress(done: int, total: int, code: str) -> None:
            if done == total or done % 100 == 0:
                print(f"history {done}/{total} latest={code}", file=sys.stderr, flush=True)

        result = services.download.execute(SCORE_H0_V1_SPEC, progress=progress)
        print(
            json.dumps(
                {
                    "result": asdict(result),
                    "archive": asdict(services.archive.inspect(SCORE_H0_V1_SPEC.research_identity)),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if result.failed == 0 else 1
    if args.command in {"research-backtest", "research-r6-screen", "research-r7-dossier"}:
        return _run_offline_report(
            args.command,
            config_path,
            runtime,
            research_identity=str(getattr(args, "research_identity", "")),
        )
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


def _run_offline_report(
    command: str,
    config_path: Path,
    runtime: RuntimeSettings,
    *,
    research_identity: str,
) -> int:
    if command == "research-backtest":
        services = build_historical_research_services(config_path)
        report = services.backtest.execute(SCORE_H0_V1_SPEC)
        print(json.dumps(asdict(report), default=_json_default, ensure_ascii=False, sort_keys=True))
        return 0 if report.status == "screened" else 1
    if command == "research-r7-dossier":
        return _run_r7_dossier(runtime, research_identity)
    artifact_store = ScoreR6ArtifactStore(runtime.runtime_dir / "score-r6")
    existing = artifact_store.read_historical_payload()
    if existing is not None:
        print(json.dumps(existing, ensure_ascii=False, sort_keys=True))
        return 0 if bool(existing.get("historical_gate_passed", False)) else 1
    services = build_historical_research_services(config_path)
    r6_report = services.score_r6.execute(SCORE_R6_HISTORICAL_SPEC)
    if r6_report.status == "historical_screened":
        artifact_store.seal_historical(r6_report)
    print(json.dumps(asdict(r6_report), default=_json_default, ensure_ascii=False, sort_keys=True))
    return 0 if r6_report.historical_gate_passed else 1


def _run_r7_dossier(runtime: RuntimeSettings, research_identity: str) -> int:
    r6_store = ScoreR6ArtifactStore(runtime.runtime_dir / "score-r6")
    try:
        spec, days, report, candidate = r6_store.load_dossier_evidence(research_identity)
        dossier = build_score_r7_promotion_dossier(spec, days, report, candidate)
        ScoreR7ArtifactStore(runtime.runtime_dir / "score-r7").seal(dossier)
    except (FileNotFoundError, ScoreR6ArtifactConflictError, ScoreR7ArtifactConflictError, ValueError):
        print(json.dumps({"reason": "score_r7_evidence_invalid", "status": "blocked"}, sort_keys=True))
        return 1
    print(json.dumps(asdict(dossier), default=_json_default, ensure_ascii=False, sort_keys=True))
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


def _json_default(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
