"""Explicit offline research command handlers loaded on demand by the CLI."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from trader.application.research.historical_label import HistoricalLabelPreregistrationService
from trader.application.research.research_tomorrow_orchestrator import (
    TomorrowResearchAdvanceResult,
    TomorrowResearchOrchestrator,
    TomorrowResearchProgressPort,
)
from trader.application.research.tomorrow_research_artifacts import (
    TomorrowResearchStage,
    derive_tomorrow_research_run_id,
    next_research_stage,
    production_readiness_audit,
)
from trader.application.research.tomorrow_research_prerequisites import CodexATomorrowResearchPrerequisite
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
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
    raise ValueError(f"unsupported research command: {command}")


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
    store = TomorrowResearchArtifactStore(_train_data_root())
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


def _train_data_root() -> Path:
    """Committed training artifacts live beside the source tree's data contract."""
    return Path(__file__).resolve().parents[3] / "trader" / "data" / "train"


def _read_tomorrow_research_status(runtime: RuntimeSettings) -> dict[str, object]:
    store = TomorrowResearchArtifactStore(_train_data_root())
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


__all__ = ["ResearchCommandOptions", "run_research_command"]
