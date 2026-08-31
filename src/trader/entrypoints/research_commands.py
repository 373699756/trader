"""Explicit offline research command handlers loaded on demand by the CLI."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.application.research.score_r7 import build_score_r7_promotion_dossier
from trader.bootstrap import build_historical_research_services
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.domain.research.score_r6 import SCORE_R6_HISTORICAL_SPEC
from trader.domain.research.score_r6_daily import SCORE_R6_DAILY_SPEC
from trader.domain.research.score_r6_stability import SCORE_R6_STABILITY_SPEC
from trader.domain.research.specification import (
    ACTIVE_SCORE_RESEARCH_SPEC,
    SCORE_P0_V1_SPEC,
    SCORE_RESEARCH_OBSERVATION_CUTOFF,
    ScoreResearchWindowCoverage,
    assess_score_research_coverage,
)
from trader.domain.research.tomorrow_historical_p2 import TOMORROW_HISTORICAL_P2_SPEC
from trader.domain.research.tomorrow_profile_comparison import (
    TOMORROW_PROFILE_COMPARISON_SPEC,
    TomorrowProfileComparisonStatus,
)
from trader.infra.persistence.outcomes import SQLiteOutcomeEvidenceRepository
from trader.infra.persistence.research_trace import SQLiteV2ResearchTraceStore
from trader.infra.persistence.tomorrow_profile_comparison import (
    SQLiteTomorrowProfileEvidenceStore,
    TomorrowProfileEvidenceConflictError,
)
from trader.infra.research.history_archive import SQLiteHistoricalArchive
from trader.infra.research.score_r6_artifacts import ScoreR6ArtifactConflictError, ScoreR6ArtifactStore
from trader.infra.research.score_r6_daily_artifacts import (
    ScoreR6DailyArtifactConflictError,
    ScoreR6DailyArtifactStore,
)
from trader.infra.research.score_r6_stability_artifacts import (
    ScoreR6StabilityArtifactConflictError,
    ScoreR6StabilityArtifactStore,
)
from trader.infra.research.score_r7_artifacts import ScoreR7ArtifactConflictError, ScoreR7ArtifactStore
from trader.infra.research.tomorrow_historical_p2_artifacts import (
    TomorrowHistoricalP2ArtifactConflictError,
    TomorrowHistoricalP2ArtifactStore,
)
from trader.infra.research.tomorrow_profile_holdout_artifacts import (
    TomorrowProfileHoldoutArtifactConflictError,
    TomorrowProfileHoldoutArtifactStore,
    holdout_report_payload,
)
from trader.infra.settings import RuntimeSettings


@dataclass(frozen=True)
class ResearchCommandOptions:
    research_identity: str
    workers: int = 5


def run_research_command(
    command: str,
    config_path: Path,
    runtime: RuntimeSettings,
    options: ResearchCommandOptions,
) -> int:
    if command == "research-status":
        trace = SQLiteV2ResearchTraceStore(runtime.runtime_dir)
        status = trace.inspect_status()
        first_observations = trace.inspect_first_observations(limit=120)
        dates = tuple(item.trade_date for item in first_observations)
        recorded_dates = frozenset(
            item.trade_date
            for item in first_observations
            if item.observed_at.timetz().replace(tzinfo=None) <= SCORE_RESEARCH_OBSERVATION_CUTOFF
        )
        active = ACTIVE_SCORE_RESEARCH_SPEC
        coverage = assess_score_research_coverage(active, recorded_dates, as_of=_shanghai_now())
        history_complete = coverage.historical.complete
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
        try:
            score_r6_daily = ScoreR6DailyArtifactStore(runtime.runtime_dir / "score-r6-daily").inspect()
        except ScoreR6DailyArtifactConflictError:
            score_r6_daily = {
                "report_hash": "",
                "status": "artifact_invalid",
                "historical_gate_passed": False,
                "selected_candidate_hash": "",
                "failure_reasons": ["score_r6_daily_artifact_invalid"],
                "promotion_authority": False,
            }
        score_r6_stability = _read_score_r6_stability_status(runtime)
        tomorrow_p2 = _read_tomorrow_p2_status(runtime)
        tomorrow_holdout = _read_tomorrow_profile_holdout_status(runtime)
        tomorrow_profiles = SQLiteTomorrowProfileEvidenceStore(
            runtime.runtime_dir,
            TOMORROW_PROFILE_COMPARISON_SPEC,
        ).inspect_status()
        promotion_ready = bool(score_r6["promotion_eligible"])
        try:
            score_r7 = ScoreR7ArtifactStore(runtime.runtime_dir / "score-r7").inspect()
        except ScoreR7ArtifactConflictError:
            score_r7 = {"dossiers": [], "dossier_count": 0, "artifact_error": "score_r7_artifact_invalid"}
        print(
            json.dumps(
                {
                    "schema_version": "v2_research_readiness_v5",
                    "research_state": _research_state(coverage.historical),
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
                    "score_r6_daily": score_r6_daily,
                    "score_r6_stability": score_r6_stability,
                    "tomorrow_p2": tomorrow_p2,
                    "tomorrow_profile_holdout": tomorrow_holdout,
                    "tomorrow_profile_comparison": _tomorrow_profile_status_payload(tomorrow_profiles),
                    "score_r7": score_r7,
                    "recorded_trade_dates": [value.isoformat() for value in dates],
                    "active_research": {
                        "research_identity": active.research_identity,
                        "research_spec_hash": active.content_hash,
                        "preregistered_on": active.preregistered_on.isoformat(),
                        "evaluation_blocker": (
                            "score_p0_v2_historical_planned_dates_missed"
                            if not coverage.historical.recoverable
                            else (
                                "score_p0_v2_r2_r5_not_run"
                                if history_complete
                                else "score_p0_v2_historical_observations_incomplete"
                            )
                        ),
                        "historical_window": _window_status(active.historical_dates, coverage.historical),
                        "forward_window": _window_status(active.forward_dates, coverage.forward),
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
    if command == "research-history-download":
        services = build_historical_research_services(config_path, workers=options.workers)

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
    if command in {
        "research-backtest",
        "research-r6-screen",
        "research-r6-daily-screen",
        "research-r6-stability-screen",
        "research-tomorrow-p2-screen",
        "research-tomorrow-v1-v2-holdout",
        "research-tomorrow-profile-report",
        "research-r7-dossier",
    }:
        return _run_offline_report(
            command,
            config_path,
            runtime,
            options,
        )
    raise ValueError(f"unsupported research command: {command}")


def _run_offline_report(
    command: str,
    config_path: Path,
    runtime: RuntimeSettings,
    options: ResearchCommandOptions,
) -> int:
    runners: dict[str, Callable[[], int]] = {
        "research-backtest": lambda: _run_historical_backtest(config_path),
        "research-r7-dossier": lambda: _run_r7_dossier(runtime, options.research_identity),
        "research-tomorrow-v1-v2-holdout": lambda: _run_tomorrow_profile_holdout(config_path, runtime),
        "research-tomorrow-profile-report": lambda: _run_tomorrow_profile_report(runtime),
        "research-r6-daily-screen": lambda: _run_r6_daily_screen(config_path, runtime),
        "research-r6-stability-screen": lambda: _run_r6_stability_screen(config_path, runtime),
        "research-tomorrow-p2-screen": lambda: _run_tomorrow_p2_screen(config_path, runtime),
        "research-r6-screen": lambda: _run_r6_screen(config_path, runtime),
    }
    return runners[command]()


def _run_historical_backtest(config_path: Path) -> int:
    services = build_historical_research_services(config_path)
    report = services.backtest.execute(SCORE_H0_V1_SPEC)
    print(json.dumps(asdict(report), default=_json_default, ensure_ascii=False, sort_keys=True))
    return 0 if report.status == "screened" else 1


def _run_r6_daily_screen(config_path: Path, runtime: RuntimeSettings) -> int:
    daily_store = ScoreR6DailyArtifactStore(runtime.runtime_dir / "score-r6-daily")
    existing = daily_store.read_payload()
    if existing is not None:
        print(json.dumps(existing, ensure_ascii=False, sort_keys=True))
        return 0 if bool(existing.get("historical_gate_passed", False)) else 1
    print("daily trend screen: computing immutable H0 factors and replay", file=sys.stderr, flush=True)
    services = build_historical_research_services(config_path)
    daily_report = services.score_r6_daily.execute(SCORE_R6_DAILY_SPEC)
    if daily_report.status != "insufficient_coverage":
        daily_store.seal(daily_report)
    print(json.dumps(asdict(daily_report), default=_json_default, ensure_ascii=False, sort_keys=True))
    return 0 if daily_report.historical_gate_passed else 1


def _run_r6_stability_screen(config_path: Path, runtime: RuntimeSettings) -> int:
    stability_store = ScoreR6StabilityArtifactStore(runtime.runtime_dir / "score-r6-stability")
    try:
        existing = stability_store.read_payload()
    except ScoreR6StabilityArtifactConflictError:
        print(
            json.dumps(
                {
                    "status": "artifact_invalid",
                    "diagnostic_gate_passed": False,
                    "failure_reasons": ["score_r6_stability_artifact_invalid"],
                    "promotion_authority": False,
                },
                sort_keys=True,
            )
        )
        return 1
    if existing is not None:
        print(json.dumps(existing, ensure_ascii=False, sort_keys=True))
        return 0 if bool(existing.get("diagnostic_gate_passed", False)) else 1
    print("daily stability screen: computing immutable H0 factors and replay", file=sys.stderr, flush=True)
    services = build_historical_research_services(config_path)
    stability_report = services.score_r6_stability.execute(SCORE_R6_STABILITY_SPEC)
    if stability_report.status not in {"insufficient_coverage", "parent_mismatch"}:
        stability_store.seal(stability_report)
    print(json.dumps(asdict(stability_report), default=_json_default, ensure_ascii=False, sort_keys=True))
    return 0 if stability_report.diagnostic_gate_passed else 1


def _run_r6_screen(config_path: Path, runtime: RuntimeSettings) -> int:
    r6_store = ScoreR6ArtifactStore(runtime.runtime_dir / "score-r6")
    existing = r6_store.read_historical_payload()
    if existing is not None:
        print(json.dumps(existing, ensure_ascii=False, sort_keys=True))
        return 0 if bool(existing.get("historical_gate_passed", False)) else 1
    services = build_historical_research_services(config_path)
    r6_report = services.score_r6.execute(SCORE_R6_HISTORICAL_SPEC)
    if r6_report.status == "historical_screened":
        r6_store.seal_historical(r6_report)
    print(json.dumps(asdict(r6_report), default=_json_default, ensure_ascii=False, sort_keys=True))
    return 0 if r6_report.historical_gate_passed else 1


def _run_tomorrow_p2_screen(config_path: Path, runtime: RuntimeSettings) -> int:
    store = TomorrowHistoricalP2ArtifactStore(runtime.runtime_dir / "score-tomorrow-p2")
    try:
        existing = store.read_report_payload()
    except TomorrowHistoricalP2ArtifactConflictError:
        print(
            json.dumps(
                {
                    "status": "artifact_invalid",
                    "failure_reasons": ["tomorrow_p2_artifact_invalid"],
                    "production_authority": False,
                },
                sort_keys=True,
            )
        )
        return 1
    if existing is not None:
        print(json.dumps(existing, ensure_ascii=False, sort_keys=True))
        return 0 if existing.get("status") == "historical_passed" else 1
    print("Tomorrow P2: reading immutable H0 rows and fitting the only candidate", file=sys.stderr, flush=True)
    services = build_historical_research_services(config_path)
    execution = services.tomorrow_historical_p2.execute(TOMORROW_HISTORICAL_P2_SPEC)
    store.seal(execution.report, execution.model_artifact)
    print(json.dumps(asdict(execution.report), default=_json_default, ensure_ascii=False, sort_keys=True))
    return 0 if execution.report.status == "historical_passed" else 1


def _read_score_r6_stability_status(runtime: RuntimeSettings) -> dict[str, object]:
    try:
        return ScoreR6StabilityArtifactStore(runtime.runtime_dir / "score-r6-stability").inspect()
    except ScoreR6StabilityArtifactConflictError:
        return {
            "report_hash": "",
            "status": "artifact_invalid",
            "diagnostic_gate_passed": False,
            "selected_candidate_hash": "",
            "failure_reasons": ["score_r6_stability_artifact_invalid"],
            "evidence_class": SCORE_R6_STABILITY_SPEC.evidence_class,
            "promotion_authority": False,
        }


def _read_tomorrow_p2_status(runtime: RuntimeSettings) -> dict[str, object]:
    try:
        return TomorrowHistoricalP2ArtifactStore(runtime.runtime_dir / "score-tomorrow-p2").inspect()
    except TomorrowHistoricalP2ArtifactConflictError:
        return {
            "report_hash": "",
            "status": "artifact_invalid",
            "candidate_id": TOMORROW_HISTORICAL_P2_SPEC.candidate.candidate_id,
            "failure_reasons": ["tomorrow_p2_artifact_invalid"],
            "forward_preregistration_eligible": False,
            "production_authority": False,
        }


def _read_tomorrow_profile_holdout_status(runtime: RuntimeSettings) -> dict[str, object]:
    try:
        return TomorrowProfileHoldoutArtifactStore(runtime.runtime_dir).inspect()
    except TomorrowProfileHoldoutArtifactConflictError:
        return {
            "status": "artifact_invalid",
            "report_hash": "",
            "production_authority": False,
        }


def _run_tomorrow_profile_holdout(config_path: Path, runtime: RuntimeSettings) -> int:
    store = TomorrowProfileHoldoutArtifactStore(runtime.runtime_dir)
    try:
        existing = store.read_payload()
    except TomorrowProfileHoldoutArtifactConflictError:
        print(json.dumps({"status": "artifact_invalid", "production_authority": False}, sort_keys=True))
        return 1
    if existing is not None:
        print(json.dumps(existing, ensure_ascii=False, sort_keys=True))
        return 0 if existing.get("content_hash") == TOMORROW_PROFILE_COMPARISON_SPEC.historical_evidence_hash else 1
    archive = SQLiteHistoricalArchive(runtime.runtime_dir).inspect(SCORE_H0_V1_SPEC.research_identity)
    if archive.spec_hash != SCORE_H0_V1_SPEC.content_hash:
        print(
            json.dumps(
                {
                    "status": "insufficient_coverage",
                    "failure_reasons": ["score_h0_archive_coverage_incomplete"],
                    "production_authority": False,
                },
                sort_keys=True,
            )
        )
        return 1
    print("Tomorrow V1/V2: evaluating sealed profiles on paired H0 validation rows", file=sys.stderr, flush=True)
    report = build_historical_research_services(config_path).tomorrow_profile_holdout.execute()
    if report.content_hash != TOMORROW_PROFILE_COMPARISON_SPEC.historical_evidence_hash:
        print(
            json.dumps(
                {
                    "status": "historical_evidence_mismatch",
                    "report_hash": report.content_hash,
                    "expected_hash": TOMORROW_PROFILE_COMPARISON_SPEC.historical_evidence_hash,
                    "production_authority": False,
                },
                sort_keys=True,
            )
        )
        return 1
    store.seal(report)
    print(json.dumps(holdout_report_payload(report), ensure_ascii=False, sort_keys=True))
    return 0


def _run_tomorrow_profile_report(runtime: RuntimeSettings) -> int:
    evidence = SQLiteTomorrowProfileEvidenceStore(runtime.runtime_dir, TOMORROW_PROFILE_COMPARISON_SPEC)
    try:
        payload = evidence.inspect_terminal_report_bytes()
    except TomorrowProfileEvidenceConflictError:
        print(json.dumps({"status": "artifact_invalid", "production_authority": False}, sort_keys=True))
        return 1
    if payload is not None:
        print(payload.decode())
        raw = json.loads(payload.decode())
        return 0 if raw.get("manual_review_eligible") is True else 1
    status = evidence.inspect_status()
    print(json.dumps(_tomorrow_profile_status_payload(status), ensure_ascii=False, sort_keys=True))
    return 1


def _tomorrow_profile_status_payload(status: TomorrowProfileComparisonStatus) -> dict[str, object]:
    return {
        "initialized": status.initialized,
        "spec_hash": status.spec_hash,
        "prediction_manifests": status.prediction_manifests,
        "paired_predictions": status.paired_predictions,
        "formal_manifests": status.formal_manifests,
        "settled_pairs": status.settled_pairs,
        "complete_pairs": status.complete_pairs,
        "independent_days": status.independent_days,
        "required_independent_days": status.required_independent_days,
        "minimum_paired_candidates": status.minimum_paired_candidates,
        "state": status.state,
        "latest_prediction_date": (
            status.latest_prediction_date.isoformat() if status.latest_prediction_date is not None else None
        ),
        "latest_settlement_date": (
            status.latest_settlement_date.isoformat() if status.latest_settlement_date is not None else None
        ),
        "production_authority": status.production_authority,
        "automatic_profile_switch": status.automatic_profile_switch,
        "error_code": status.error_code,
    }


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


def _research_state(coverage: ScoreResearchWindowCoverage) -> str:
    if coverage.state == "failed":
        return "historical_collection_failed"
    if coverage.state == "complete":
        return "historical_ready_for_offline_evaluation"
    return "historical_collecting"


def _window_status(
    planned_dates: tuple[date, ...],
    coverage: ScoreResearchWindowCoverage,
) -> dict[str, object]:
    return {
        "start": str(planned_dates[0]),
        "end": str(planned_dates[-1]),
        "planned_trade_dates": len(planned_dates),
        "recorded_trade_dates": len(coverage.recorded_dates),
        "missed_trade_dates": [value.isoformat() for value in coverage.missed_dates],
        "maximum_attainable_trade_dates": coverage.maximum_attainable_days,
        "next_planned_trade_date": (
            coverage.next_planned_date.isoformat() if coverage.next_planned_date is not None else None
        ),
        "complete": coverage.complete,
        "recoverable": coverage.recoverable,
    }


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _json_default(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


__all__ = ["ResearchCommandOptions", "run_research_command"]
