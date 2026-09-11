"""Explicit offline research command handlers loaded on demand by the CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from trader.application.research.historical_label import HistoricalLabelPreregistrationService
from trader.application.research.history_archive_status import HistoryArchiveStatus
from trader.application.research.tomorrow_research_artifacts import (
    TomorrowResearchStage,
    derive_tomorrow_research_run_id,
    next_research_stage,
    production_readiness_audit,
)
from trader.application.research.tomorrow_research_orchestrator import (
    TomorrowResearchAdvanceResult,
    TomorrowResearchProgressPort,
)
from trader.application.research.tomorrow_research_prerequisites import TomorrowLabelReadinessInspector
from trader.domain.research.historical_screening import HISTORICAL_SCREENING_SPEC
from trader.domain.research.tomorrow_historical import TOMORROW_HISTORICAL_SPEC
from trader.infra.persistence.outcomes import SQLiteOutcomeEvidenceRepository
from trader.infra.persistence.research_trace import SQLiteResearchTraceStore
from trader.infra.research.h1_point_in_time_archive import H1ArchiveConflictError, SQLiteH1PointInTimeArchive
from trader.infra.research.history_archive import SQLiteHistoricalArchive
from trader.infra.research.history_archive_status import inspect_history_archive
from trader.infra.research.tomorrow_historical_artifacts import (
    TomorrowHistoricalArtifactConflictError,
    TomorrowHistoricalArtifactStore,
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


def run_research_command(
    command: str,
    config_path: Path,
    runtime: RuntimeSettings,
    options: ResearchCommandOptions,
) -> int:
    if command == "train-tomorrow":
        return _run_tomorrow_research_orchestrator(runtime)
    if command == "research-status":
        trace = SQLiteResearchTraceStore(runtime.runtime_dir)
        status = trace.inspect_status()
        first_observations = trace.inspect_first_observations(limit=120)
        dates = tuple(item.trade_date for item in first_observations)
        historical_archive = SQLiteHistoricalArchive(runtime.runtime_dir).inspect(
            HISTORICAL_SCREENING_SPEC.research_identity
        )
        screening_coverage = (
            historical_archive.completed_codes / historical_archive.universe_count
            if historical_archive.universe_count
            else 0.0
        )
        screening_ready = (
            historical_archive.spec_hash == HISTORICAL_SCREENING_SPEC.content_hash and screening_coverage >= 0.95
        )
        tomorrow_historical = _read_tomorrow_historical_status(runtime)
        tomorrow_holdout = _read_tomorrow_profile_holdout_status(runtime)
        tomorrow_risk = _read_tomorrow_historical_risk_status(runtime)
        tomorrow_research = _read_tomorrow_research_status(runtime)
        history_status = _project_history_archive_status(inspect_history_archive(_history_data_root()))
        print(
            json.dumps(
                {
                    "schema_version": "research_readiness",
                    "production_authority": False,
                    "validation_mode": "historical_only",
                    "blockers": [] if screening_ready else ["score_h0_archive_coverage_incomplete"],
                    "tomorrow_historical": tomorrow_historical,
                    "tomorrow_profile_holdout": tomorrow_holdout,
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
                        **asdict(historical_archive),
                        "coverage_ratio": round(screening_coverage, 6),
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


def _project_history_archive_status(status: HistoryArchiveStatus) -> dict[str, object]:
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


def _run_tomorrow_research_orchestrator(
    runtime: RuntimeSettings,
) -> int:
    del runtime
    from trader.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress
    from trader.infra.scoring.profiles.v3.training import run_tomorrow_training

    with StderrTomorrowTrainingProgress() as progress:
        try:
            result = run_tomorrow_training(
                _history_data_root(),
                _train_data_root(),
                progress=progress,
                source_commit=_repository_source_commit(),
            )
            progress.publish_result(result.status, result.failure_reasons[0] if result.failure_reasons else None)
        except KeyboardInterrupt:
            progress.publish_cancelled()
            return 130
    payload = {
        "schema_version": "tomorrow_training_result",
        "status": result.status,
        "run_id": result.run_id,
        "training_input_scope": result.training_input_scope,
        "training_input_hash": result.training_input_hash,
        "label_cutoff": result.label_cutoff.isoformat() if result.label_cutoff is not None else None,
        "matured_label_days_since_training": result.matured_label_days_since_training,
        "training_due": result.training_due,
        "training_due_reason": result.training_due_reason,
        "invalidated_cache_dates": [value.isoformat() for value in result.invalidated_cache_dates],
        "training_input_codes": result.training_input_codes,
        "training_universe_codes": result.training_universe_codes,
        "report_hash": result.report_hash,
        "model_hash": result.model_hash,
        "industry_count": result.industry_count,
        "training_rows": result.training_rows,
        "validation_rows": result.validation_rows,
        "failure_reasons": list(result.failure_reasons),
        "blockers": list(result.failure_reasons),
        "next_stage": "data_manifest" if result.status == "blocked" and not result.training_input_hash else None,
        "training_anchor": "15:00_close_proxy",
        "runtime_anchor": "14:50",
        "point_in_time_parity": False,
        "production_authority": False,
        "automatic_model_update": False,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if result.status in {"trial_ready", "engineering_ready", "already_current", "not_due"} else 1


def _repository_source_commit() -> str:
    root = Path(__file__).resolve().parents[3]
    try:
        result = subprocess.run(
            ("git", "rev-parse", "--verify", "HEAD"),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    commit = result.stdout.strip().lower()
    return commit if len(commit) == 40 and all(character in "0123456789abcdef" for character in commit) else ""


def _train_data_root() -> Path:
    """Committed training artifacts live beside the source tree's data contract."""
    return Path(__file__).resolve().parents[3] / "data" / "train"


def _history_data_root() -> Path:
    """BaoStock history is a project-root data input, separate from service runtime state."""
    return Path(__file__).resolve().parents[3] / "data" / "history"


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


def _tomorrow_research_prerequisite(runtime: RuntimeSettings) -> TomorrowLabelReadinessInspector:
    archive = SQLiteH1PointInTimeArchive(runtime.runtime_dir)
    return TomorrowLabelReadinessInspector(HistoricalLabelPreregistrationService(archive))


def _read_tomorrow_historical_status(runtime: RuntimeSettings) -> dict[str, object]:
    try:
        return TomorrowHistoricalArtifactStore(runtime.runtime_dir / "tomorrow-historical").inspect()
    except TomorrowHistoricalArtifactConflictError:
        return {
            "report_hash": "",
            "status": "artifact_invalid",
            "candidate_id": TOMORROW_HISTORICAL_SPEC.candidate.candidate_id,
            "failure_reasons": ["tomorrow_historical_artifact_invalid"],
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
