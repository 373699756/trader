"""Explicit offline research command handlers loaded on demand by the CLI."""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from trader.download.domain.history_snapshot_status import HistorySnapshotStatus
from trader.download.infra.history_snapshot_inspection import InspectHistorySnapshot
from trader.infra.settings import LoadStrategySettings, RuntimeSettings
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.historical_label import HistoricalLabelPreregistrationService
from trader.training.application.tomorrow_research_evidence import (
    DeriveTomorrowResearchRunId,
    NextResearchStage,
    ProductionReadinessAudit,
    TomorrowResearchStage,
)
from trader.training.application.tomorrow_research_orchestrator import (
    TomorrowResearchAdvanceResult,
    TomorrowResearchProgressPort,
)
from trader.training.application.tomorrow_research_prerequisites import TomorrowLabelReadinessInspector
from trader.training.domain.evaluation.historical_screening import HISTORICAL_SCREENING_SPEC
from trader.training.domain.evaluation.tomorrow_historical import TOMORROW_HISTORICAL_SPEC
from trader.training.infra.research.h1_point_in_time_snapshot import (
    H1PointInTimeSnapshotConflictError,
    SQLiteH1PointInTimeSnapshot,
)
from trader.training.infra.research.historical_screening_snapshot import SQLiteHistoricalScreeningSnapshot
from trader.training.infra.research.outcome_evidence_index import SQLiteOutcomeEvidenceIndex
from trader.training.infra.research.research_trace_index import SQLiteResearchTraceIndex
from trader.training.infra.research.tomorrow_historical_evidence import (
    TomorrowHistoricalEvidenceConflictError,
    TomorrowHistoricalEvidenceIndex,
)
from trader.training.infra.research.tomorrow_historical_risk_evidence import (
    TomorrowHistoricalRiskEvidenceConflictError,
    TomorrowHistoricalRiskEvidenceIndex,
)
from trader.training.infra.research.tomorrow_research_evidence import (
    TomorrowResearchEvidenceIndex,
    TomorrowResearchEvidenceIndexError,
)

_TRAINING_PRIORITY_LOWERED = False


@dataclass(frozen=True)
class ResearchCommandOptions:
    workers: int = 5
    profile: ScoringProfileId | None = None


def ConfigureTrainingResources() -> None:
    """Apply the bounded process policy before loading either training head."""

    from trader.training.application.tomorrow_training import TOMORROW_TRAINING_COMPUTE_THREADS

    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = str(TOMORROW_TRAINING_COMPUTE_THREADS)
    _lower_training_priority()


def _lower_training_priority() -> None:
    global _TRAINING_PRIORITY_LOWERED  # noqa: PLW0603 - one process-level resource policy
    if _TRAINING_PRIORITY_LOWERED:
        return
    if os.name == "posix":
        os.nice(10)
        _TRAINING_PRIORITY_LOWERED = True
        return
    if os.name != "nt":
        _TRAINING_PRIORITY_LOWERED = True
        return
    below_normal_priority_class = 0x00004000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    if not kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), below_normal_priority_class):
        raise OSError(ctypes.__dict__["get_last_error"](), "could not lower Tomorrow training priority")
    _TRAINING_PRIORITY_LOWERED = True


class _TomorrowResearchProgress(TomorrowResearchProgressPort):
    def __init__(self) -> None:
        self._started_at: dict[TomorrowResearchStage, float] = {}

    def update(self, stage: TomorrowResearchStage, status: str) -> None:
        now = time.monotonic()
        started_at = self._started_at.setdefault(stage, now)
        print(
            json.dumps(
                {
                    "schema_version": "tomorrow_research_progress",
                    "stage": stage,
                    "status": status,
                    "elapsed_seconds": round(now - started_at, 3),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )


def RunResearchCommand(
    command: str,
    config_path: Path,
    runtime: RuntimeSettings,
    options: ResearchCommandOptions,
) -> int:
    profile = options.profile or LoadStrategySettings(runtime.strategy_config_path).scoring_profile
    research_root = runtime.data_paths.research(profile)
    if command.startswith("research-"):
        from trader.recommendation.infra.scoring.cost_settings import LoadEvaluationCostScenarios
        from trader.training.infra.research_settings import LoadResearchSettings

        LoadResearchSettings(config_path.parent)
        LoadEvaluationCostScenarios(config_path.parent)
    if command == "research-status":
        from trader.recommendation.domain.evidence.pipeline import PIPELINE_STAGE_ORDER
        from trader.training.domain.evaluation.selection_experiment import SELECTION_RESEARCH_IDENTITY

        trace = SQLiteResearchTraceIndex(research_root)
        status = trace.inspect_status()
        first_observations = trace.inspect_first_observations(limit=120)
        dates = tuple(item.trade_date for item in first_observations)
        screening_snapshot = SQLiteHistoricalScreeningSnapshot(research_root).inspect(
            HISTORICAL_SCREENING_SPEC.research_identity
        )
        screening_coverage = (
            screening_snapshot.completed_codes / screening_snapshot.universe_count
            if screening_snapshot.universe_count
            else 0.0
        )
        screening_ready = (
            screening_snapshot.spec_hash == HISTORICAL_SCREENING_SPEC.content_hash and screening_coverage >= 0.95
        )
        tomorrow_historical = _read_tomorrow_historical_status(research_root)
        tomorrow_risk = _read_tomorrow_historical_risk_status(research_root)
        tomorrow_research = _read_tomorrow_research_status(research_root)
        history_status = _project_history_snapshot_status(InspectHistorySnapshot(runtime.data_paths.history))
        print(
            json.dumps(
                {
                    "schema_version": "research_readiness",
                    "production_authority": False,
                    "validation_mode": "historical_only",
                    "blockers": ["target_selection_external_validation_required"],
                    "target_selection": {
                        "research_identity": SELECTION_RESEARCH_IDENTITY,
                        "status": "external_validation_required",
                        "strategies": ["tomorrow", "d25"],
                        "pipeline_stages": list(PIPELINE_STAGE_ORDER),
                        "population": "full_eligible_population",
                        "validation_command": "research-selection",
                        "blockers": ["target_selection_external_validation_required"],
                        "production_authority": False,
                    },
                    "tomorrow_historical": tomorrow_historical,
                    "tomorrow_historical_risk": tomorrow_risk,
                    "tomorrow_research": tomorrow_research,
                    "history_archive": history_status,
                    "recorded_trade_dates": [value.isoformat() for value in dates],
                    "retired_research": (
                        {
                            "research_identity": "historical_research_baseline",
                            "status": "historical_rejected",
                            "blocker": "historical_point_in_time_missing",
                        },
                        {
                            "research_identity": "preregistered_research",
                            "status": "historical_collection_failed",
                            "blocker": "fixed_historical_dates_missed",
                        },
                    ),
                    "archive": asdict(status),
                    "historical_screening": {
                        **asdict(screening_snapshot),
                        "coverage_ratio": round(screening_coverage, 6),
                        "ready": screening_ready,
                        "target_selection_evidence": False,
                        "research_spec_hash": HISTORICAL_SCREENING_SPEC.content_hash,
                        "training_window": {
                            "start": HISTORICAL_SCREENING_SPEC.training_start.isoformat(),
                            "end": HISTORICAL_SCREENING_SPEC.training_end.isoformat(),
                        },
                        "validation_window": {
                            "start": HISTORICAL_SCREENING_SPEC.validation_start.isoformat(),
                            "end": HISTORICAL_SCREENING_SPEC.validation_end.isoformat(),
                        },
                        "promotion_authority": False,
                    },
                    "outcomes": asdict(SQLiteOutcomeEvidenceIndex.inspect_status(research_root)),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if command == "research-baseline-audit":
        return _run_baseline_identity_audit(runtime)
    raise ValueError(f"unsupported research command: {command}")


def _project_history_snapshot_status(status: HistorySnapshotStatus) -> dict[str, object]:
    return {
        "state": status.state,
        "active_snapshot_hash": status.active_snapshot_hash,
        "data_cutoff": status.data_cutoff.isoformat() if status.data_cutoff is not None else None,
        "label_cutoff": status.label_cutoff.isoformat() if status.label_cutoff is not None else None,
        "calendar_sessions": status.calendar_sessions,
        "universe_count": status.universe_count,
        "partition_count": status.partition_count,
        "reason": status.reason,
        "production_authority": status.production_authority,
        "point_in_time_parity": status.point_in_time_parity,
    }


def _run_baseline_identity_audit(runtime: RuntimeSettings) -> int:
    from trader.training.application.baseline_identity_audit import BaselineIdentityAuditService
    from trader.training.infra.research.baseline_identity_sources import LoadBaselineIdentityEvidence

    audit = BaselineIdentityAuditService(LoadBaselineIdentityEvidence(runtime)).execute()
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


def _read_tomorrow_research_status(research_root: Path) -> dict[str, object]:
    evidence = TomorrowResearchEvidenceIndex(research_root / "experiments/tomorrow_research")
    try:
        prerequisite = _tomorrow_research_prerequisite(research_root).inspect()
    except H1PointInTimeSnapshotConflictError:
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
        graph = evidence.load_graph()
    except TomorrowResearchEvidenceIndexError:
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
    stage = NextResearchStage(graph)
    readiness = ProductionReadinessAudit(graph, manual_authorization_hash=None)
    return {
        "status": (
            "terminal"
            if stage is None
            else "blocked"
            if prerequisite.status == "blocked" or graph.artifacts
            else "not_started"
        ),
        "run_id": DeriveTomorrowResearchRunId(graph),
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
            "max_peak_rss_mb": 2048,
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


def _tomorrow_research_prerequisite(research_root: Path) -> TomorrowLabelReadinessInspector:
    snapshot = SQLiteH1PointInTimeSnapshot(research_root)
    return TomorrowLabelReadinessInspector(HistoricalLabelPreregistrationService(snapshot))


def _read_tomorrow_historical_status(research_root: Path) -> dict[str, object]:
    try:
        return TomorrowHistoricalEvidenceIndex(research_root / "experiments").inspect()
    except TomorrowHistoricalEvidenceConflictError:
        return {
            "report_hash": "",
            "status": "artifact_invalid",
            "candidate_id": TOMORROW_HISTORICAL_SPEC.candidate.candidate_id,
            "failure_reasons": ["tomorrow_historical_artifact_invalid"],
            "validation_mode": "historical_only",
            "production_authority": False,
        }


def _read_tomorrow_historical_risk_status(research_root: Path) -> dict[str, object]:
    try:
        return TomorrowHistoricalRiskEvidenceIndex(research_root).inspect()
    except TomorrowHistoricalRiskEvidenceConflictError:
        return {
            "status": "artifact_invalid",
            "report_hash": "",
            "model_artifact_hash": "",
            "production_authority": False,
        }


__all__ = ["ConfigureTrainingResources", "ResearchCommandOptions", "RunResearchCommand"]
