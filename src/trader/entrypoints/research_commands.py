"""Explicit offline research command handlers loaded on demand by the CLI."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from trader.application.research.historical_label import HistoricalLabelPreregistrationService
from trader.application.research.research_tomorrow_orchestrator import (
    TomorrowResearchAdvanceResult,
    TomorrowResearchOrchestrator,
    TomorrowResearchProgressPort,
)
from trader.application.research.tomorrow_profile_holdout import TOMORROW_PROFILE_HOLDOUT_REPORT_HASH
from trader.application.research.tomorrow_research_artifacts import (
    TomorrowResearchStage,
    derive_tomorrow_research_run_id,
    next_research_stage,
    production_readiness_audit,
)
from trader.application.research.tomorrow_research_prerequisites import CodexATomorrowResearchPrerequisite
from trader.bootstrap import build_historical_research_services
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.domain.research.score_r6 import SCORE_R6_HISTORICAL_SPEC
from trader.domain.research.score_r6_daily import SCORE_R6_DAILY_SPEC
from trader.domain.research.score_r6_stability import SCORE_R6_STABILITY_SPEC
from trader.domain.research.tomorrow_historical_p2 import TOMORROW_HISTORICAL_P2_SPEC
from trader.infra.persistence.outcomes import SQLiteOutcomeEvidenceRepository
from trader.infra.persistence.research_trace import SQLiteV2ResearchTraceStore
from trader.infra.research.baostock_history_runtime import inspect_baostock_history, project_baostock_runtime_status
from trader.infra.research.h1_point_in_time_archive import H1ArchiveConflictError, SQLiteH1PointInTimeArchive
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
from trader.infra.research.tomorrow_historical_p2_artifacts import (
    TomorrowHistoricalP2ArtifactConflictError,
    TomorrowHistoricalP2ArtifactStore,
)
from trader.infra.research.tomorrow_historical_risk_artifacts import (
    TomorrowHistoricalRiskArtifactConflictError,
    TomorrowHistoricalRiskArtifactStore,
)
from trader.infra.research.tomorrow_profile_holdout_artifacts import (
    TomorrowProfileHoldoutArtifactConflictError,
    TomorrowProfileHoldoutArtifactStore,
    holdout_report_payload,
)
from trader.infra.research.tomorrow_research_artifacts import (
    TomorrowResearchArtifactStore,
    TomorrowResearchArtifactStoreError,
)
from trader.infra.settings import RuntimeSettings


@dataclass(frozen=True)
class ResearchCommandOptions:
    workers: int = 5


class _TomorrowResearchProgress(TomorrowResearchProgressPort):
    def __init__(self) -> None:
        self._started_at: dict[TomorrowResearchStage, float] = {}

    def update(self, stage: TomorrowResearchStage, status: str) -> None:
        now = time.monotonic()
        started_at = self._started_at.setdefault(stage, now)
        print(
            json.dumps(
                {
                    "schema_version": "tomorrow_research_progress_v1",
                    "stage": stage,
                    "status": status,
                    "elapsed_seconds": round(now - started_at, 3),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )


def run_research_command(
    command: str,
    config_path: Path,
    runtime: RuntimeSettings,
    options: ResearchCommandOptions,
) -> int:
    if command == "train-tomorrow":
        return _run_tomorrow_research_orchestrator(runtime)
    if command == "research-status":
        trace = SQLiteV2ResearchTraceStore(runtime.runtime_dir)
        status = trace.inspect_status()
        first_observations = trace.inspect_first_observations(limit=120)
        dates = tuple(item.trade_date for item in first_observations)
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
                "validation_mode": "historical_only",
                "production_authority": False,
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
        tomorrow_risk = _read_tomorrow_historical_risk_status(runtime)
        tomorrow_research = _read_tomorrow_research_status(runtime)
        baostock_status = project_baostock_runtime_status(inspect_baostock_history(runtime.runtime_dir))
        print(
            json.dumps(
                {
                    "schema_version": "v2_research_readiness_v9",
                    "validation_mode": "historical_only",
                    "score_r6_executable": screening_ready,
                    "score_r6_screening_executable": screening_ready,
                    "blockers": [] if screening_ready else ["score_h0_archive_coverage_incomplete"],
                    "score_r6": score_r6,
                    "score_r6_artifact_error": score_r6_artifact_error or None,
                    "score_r6_daily": score_r6_daily,
                    "score_r6_stability": score_r6_stability,
                    "tomorrow_p2": tomorrow_p2,
                    "tomorrow_profile_holdout": tomorrow_holdout,
                    "tomorrow_v2_historical_risk": tomorrow_risk,
                    "tomorrow_research": tomorrow_research,
                    "baostock_history": baostock_status,
                    "recorded_trade_dates": [value.isoformat() for value in dates],
                    "retired_research": (
                        {
                            "research_identity": "score_p0_v1",
                            "status": "historical_rejected",
                            "blocker": "historical_point_in_time_missing",
                        },
                        {
                            "research_identity": "score_p0_v2",
                            "status": "historical_collection_failed",
                            "blocker": "fixed_historical_dates_missed",
                        },
                    ),
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
    if command == "research-baseline-audit":
        return _run_baseline_identity_audit(runtime)
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
        "research-tomorrow-v2-risk-validation",
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
        "research-tomorrow-v1-v2-holdout": lambda: _run_tomorrow_profile_holdout(config_path, runtime),
        "research-tomorrow-v2-risk-validation": lambda: _run_tomorrow_historical_risk(config_path, runtime),
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


def _run_baseline_identity_audit(runtime: RuntimeSettings) -> int:
    from trader.application.research.baseline_identity_audit import BaselineIdentityAuditService
    from trader.infra.research.baseline_identity_sources import load_baseline_identity_evidence

    audit = BaselineIdentityAuditService(load_baseline_identity_evidence(runtime)).execute()
    payload = {
        "schema_version": audit.schema_version,
        "status": audit.status,
        "static_status": audit.static_status,
        "production_authority": audit.production_authority,
        "conflicts": list(audit.conflicts),
        "unavailable": list(audit.unavailable),
        "content_hash": audit.content_hash,
        "claims": [
            {
                "name": claim.name,
                "expected": claim.expected,
                "actual": claim.actual,
                "source": claim.source,
                "source_hash": claim.source_hash,
                "required": claim.required,
                "status": claim.status,
            }
            for claim in audit.claims
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if audit.status == "baseline_identity_consistent" else 1


def _run_tomorrow_research_orchestrator(runtime: RuntimeSettings) -> int:
    store = TomorrowResearchArtifactStore(runtime.runtime_dir / "research" / "tomorrow-v3")
    prerequisite = _tomorrow_research_prerequisite(runtime)
    try:
        result = TomorrowResearchOrchestrator(store, prerequisite, _TomorrowResearchProgress()).advance()
        payload = _tomorrow_research_result_payload(result, store.host_available_disk_gb())
    except H1ArchiveConflictError:
        payload = {
            "schema_version": "tomorrow_research_advance_result_v1",
            "status": "artifact_conflict",
            "blockers": ["h1_archive_invalid"],
            "input_prerequisite_status": "artifact_conflict",
            "input_prerequisite_hash": "",
            "production_authority": False,
            "automatic_model_update": False,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    except TomorrowResearchArtifactStoreError:
        payload = {
            "schema_version": "tomorrow_research_advance_result_v1",
            "status": "artifact_conflict",
            "blockers": ["tomorrow_research_artifact_invalid"],
            "production_authority": False,
            "automatic_model_update": False,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if result.status in {"advanced", "terminal"} else 1


def _read_tomorrow_research_status(runtime: RuntimeSettings) -> dict[str, object]:
    store = TomorrowResearchArtifactStore(runtime.runtime_dir / "research" / "tomorrow-v3")
    try:
        prerequisite = _tomorrow_research_prerequisite(runtime).inspect()
    except H1ArchiveConflictError:
        return {
            "status": "artifact_conflict",
            "run_id": None,
            "graph_hash": "",
            "artifact_count": 0,
            "next_stage": None,
            "input_prerequisite_status": "artifact_conflict",
            "input_prerequisite_hash": "",
            "input_blockers": ["h1_archive_invalid"],
            "production_readiness": "production_adaptation_blocked",
            "production_blockers": ["tomorrow_research_artifact_invalid"],
            "production_authority": False,
            "automatic_model_update": False,
        }
    try:
        graph = store.load_graph()
    except TomorrowResearchArtifactStoreError:
        return {
            "status": "artifact_conflict",
            "run_id": None,
            "graph_hash": "",
            "artifact_count": 0,
            "next_stage": None,
            "input_prerequisite_status": prerequisite.status,
            "input_prerequisite_hash": prerequisite.content_hash,
            "input_blockers": list(prerequisite.blockers),
            "production_readiness": "production_adaptation_blocked",
            "production_blockers": ["tomorrow_research_artifact_invalid"],
            "production_authority": False,
            "automatic_model_update": False,
        }
    stage = next_research_stage(graph)
    readiness = production_readiness_audit(graph, manual_authorization_hash=None)
    return {
        "status": (
            "terminal"
            if stage is None
            else "blocked"
            if prerequisite.status == "blocked" or graph.artifacts
            else "not_started"
        ),
        "run_id": derive_tomorrow_research_run_id(graph),
        "graph_hash": graph.content_hash,
        "artifact_count": len(graph.artifacts),
        "next_stage": stage,
        "input_prerequisite_status": prerequisite.status,
        "input_prerequisite_hash": prerequisite.content_hash,
        "input_blockers": list(prerequisite.blockers),
        "production_readiness": readiness.status,
        "production_blockers": list(readiness.blockers),
        "production_authority": False,
        "automatic_model_update": False,
    }


def _tomorrow_research_result_payload(
    result: TomorrowResearchAdvanceResult,
    available_disk_gb: float,
) -> dict[str, object]:
    return {
        "schema_version": result.schema_version,
        "status": result.status,
        "run_id": result.run_id,
        "graph_hash": result.graph_hash,
        "completed_stages": list(result.completed_stages),
        "next_stage": result.next_stage,
        "blockers": list(result.blockers),
        "input_prerequisite_hash": result.prerequisite_hash,
        "resource_contract": {
            "pilot_stocks": 100,
            "pilot_trade_dates": 120,
            "max_cpu_threads": 2,
            "max_peak_rss_mb": 4096,
            "minimum_available_disk_gb": 30,
            "maximum_estimated_full_run_hours": 18,
            "host_available_disk_gb": available_disk_gb,
        },
        "production_readiness": {
            "status": result.readiness.status,
            "blockers": list(result.readiness.blockers),
            "audit_hash": result.readiness.content_hash,
        },
        "production_authority": result.production_authority,
        "automatic_model_update": result.automatic_model_update,
        "content_hash": result.content_hash,
    }


def _tomorrow_research_prerequisite(runtime: RuntimeSettings) -> CodexATomorrowResearchPrerequisite:
    archive = SQLiteH1PointInTimeArchive(runtime.runtime_dir)
    return CodexATomorrowResearchPrerequisite(HistoricalLabelPreregistrationService(archive))


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
            "validation_mode": "historical_only",
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


def _read_tomorrow_historical_risk_status(runtime: RuntimeSettings) -> dict[str, object]:
    try:
        return TomorrowHistoricalRiskArtifactStore(runtime.runtime_dir).inspect()
    except TomorrowHistoricalRiskArtifactConflictError:
        return {
            "status": "artifact_invalid",
            "report_hash": "",
            "model_artifact_hash": "",
            "production_authority": False,
        }


def _run_tomorrow_historical_risk(config_path: Path, runtime: RuntimeSettings) -> int:
    store = TomorrowHistoricalRiskArtifactStore(runtime.runtime_dir)
    try:
        existing = store.read_report_payload()
    except TomorrowHistoricalRiskArtifactConflictError:
        print(json.dumps({"status": "artifact_invalid", "production_authority": False}, sort_keys=True))
        return 1
    if existing is not None:
        print(json.dumps(existing, ensure_ascii=False, sort_keys=True))
        return 0 if existing.get("status") == "historical_validated" else 1
    print("Tomorrow V2 risk: fitting ordered historical calibration", file=sys.stderr, flush=True)
    outcome = build_historical_research_services(config_path).tomorrow_historical_risk.execute()
    if outcome.model_artifact is not None:
        store.seal(outcome)
    print(json.dumps(asdict(outcome.report), default=_json_default, ensure_ascii=False, sort_keys=True))
    return 0 if outcome.report.status == "historical_validated" else 1


def _run_tomorrow_profile_holdout(config_path: Path, runtime: RuntimeSettings) -> int:
    store = TomorrowProfileHoldoutArtifactStore(runtime.runtime_dir)
    try:
        existing = store.read_payload()
    except TomorrowProfileHoldoutArtifactConflictError:
        print(json.dumps({"status": "artifact_invalid", "production_authority": False}, sort_keys=True))
        return 1
    if existing is not None:
        print(json.dumps(existing, ensure_ascii=False, sort_keys=True))
        return 0 if existing.get("content_hash") == TOMORROW_PROFILE_HOLDOUT_REPORT_HASH else 1
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
    if report.content_hash != TOMORROW_PROFILE_HOLDOUT_REPORT_HASH:
        print(
            json.dumps(
                {
                    "status": "historical_evidence_mismatch",
                    "report_hash": report.content_hash,
                    "expected_hash": TOMORROW_PROFILE_HOLDOUT_REPORT_HASH,
                    "production_authority": False,
                },
                sort_keys=True,
            )
        )
        return 1
    store.seal(report)
    print(json.dumps(holdout_report_payload(report), ensure_ascii=False, sort_keys=True))
    return 0


def _json_default(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


__all__ = ["ResearchCommandOptions", "run_research_command"]
