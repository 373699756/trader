"""Pipeline snapshot and readiness status, evidence and local degradation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import cast

from scripts.core.contracts import DiagnosticResult
from scripts.core.projection_values import ProjectMapping, ProjectSummary, SafeStringList
from scripts.core.projections import ResultStatus


def NormalizeResult(result: DiagnosticResult, *, local: bool = False) -> DiagnosticResult:
    source = result.payload
    if result.error_code is not None or source is None:
        return result
    status = source.get("status")
    if status in ("timed_out", "cancelled", "skipped", "empty"):
        return result
    if result.name == "configuration" and status == "ok" and not isinstance(source.get("passed"), bool):
        return replace(result, payload=dict(source, passed=True))
    if result.name == "history_maintenance" and status is None and not isinstance(source.get("passed"), bool):
        return replace(result, payload=dict(source, passed=True))
    if result.name != "research_readiness":
        return result
    valid = _valid_research_status(source)
    tomorrow = ProjectMapping(source.get("tomorrow_research"))
    blocked = (
        bool(SafeStringList(source.get("blockers"), limit=1))
        or tomorrow.get("status") == "blocked"
        or bool(SafeStringList(tomorrow.get("input_blockers"), limit=1))
        or bool(SafeStringList(tomorrow.get("production_blockers"), limit=1))
    )
    if not valid or blocked:
        result = replace(result, payload=dict(source, status="failed"))
    elif status is None and not isinstance(source.get("passed"), bool):
        result = replace(result, payload=dict(source, passed=True))
    if local and valid and result.return_code in (0, 1) and ResultStatus(result) == "failed":
        return replace(result, return_code=0, payload=dict(result.payload or {}, status="degraded"))
    return result


def CompleteReport(report: dict[str, object], results: Sequence[DiagnosticResult]) -> None:
    return None


def _research_details(result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    if result.payload is not None and not _valid_research_status(source):
        payload["findings"] = [
            {
                "severity": "error",
                "code": "research_status_shape_invalid",
                "message": "The research status schema or active window projection is invalid.",
            }
        ]
        return
    history = ProjectMapping(source.get("history_archive"))
    tomorrow = ProjectMapping(source.get("tomorrow_research"))
    input_blockers = SafeStringList(tomorrow.get("input_blockers"), limit=20)
    production_blockers = SafeStringList(tomorrow.get("production_blockers"), limit=20)
    target = ProjectMapping(source.get("target_selection"))
    target_blockers = SafeStringList(target.get("blockers"), limit=20)
    blockers = target_blockers + input_blockers + production_blockers
    if blockers:
        payload["findings"] = [
            {
                "severity": "error",
                "code": blockers[0],
                "message": "Research requires qualified evidence for its declared selection identity.",
                "evidence": {
                    "target_blockers": target_blockers,
                    "input_blockers": input_blockers,
                    "production_blockers": production_blockers,
                    "production_authority": tomorrow.get("production_authority"),
                },
            }
        ]
    payload["summary"] = {
        "target_selection": {
            "research_identity": target.get("research_identity"),
            "status": target.get("status"),
            "strategies": SafeStringList(target.get("strategies"), limit=2),
            "population": target.get("population"),
            "validation_command": target.get("validation_command"),
            "blockers": target_blockers,
            "production_authority": target.get("production_authority"),
        },
        "history_archive": {
            "state": history.get("state"),
            "snapshot_hash": history.get("snapshot_hash"),
            "data_cutoff": history.get("data_cutoff"),
            "label_cutoff": history.get("label_cutoff"),
            "calendar_sessions": history.get("calendar_sessions"),
            "universe_count": history.get("universe_count"),
            "partition_count": history.get("partition_count"),
            "reason": history.get("reason"),
            "production_authority": history.get("production_authority"),
            "point_in_time_parity": history.get("point_in_time_parity"),
        },
        "v3": {
            "status": tomorrow.get("status"),
            "next_stage": tomorrow.get("next_stage"),
            "input_prerequisite_status": tomorrow.get("input_prerequisite_status"),
            "input_blockers": input_blockers,
            "production_blockers": production_blockers,
            "production_readiness": tomorrow.get("production_readiness"),
            "production_authority": tomorrow.get("production_authority"),
        },
        "production_authority": tomorrow.get("production_authority"),
    }


def _configuration_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = {
        key: source.get(key)
        for key in (
            "status",
            "scoring_profile",
            "runtime_version",
            "strategy_version",
            "history_sessions",
            "training_fit_dates",
            "research_arms",
            "failure",
            "category",
        )
    }


def _maintenance_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = {
        key: source.get(key)
        for key in (
            "state",
            "reason",
            "data_cutoff",
            "label_cutoff",
            "training_due",
            "training_due_reason",
            "reminder_state",
        )
    }


def _performance_details(_result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    payload["summary"] = {
        "workload": ProjectMapping(source.get("workload")),
        "measurements": ProjectMapping(source.get("measurements")),
        "memory": ProjectMapping(source.get("memory")),
        "identity": ProjectMapping(source.get("identity")),
        "network_calls": source.get("network_calls"),
        "model_workload": ProjectMapping(source.get("model_workload")),
        "profile_results": ProjectMapping(source.get("profile_results")),
        "failures": SafeStringList(source.get("failures"), limit=30),
    }
    if isinstance(source.get("continuous_pipeline"), Mapping):
        summary = cast(dict[str, object], payload["summary"])
        summary["continuous_pipeline"] = ProjectMapping(source["continuous_pipeline"])


def _valid_research_status(payload: Mapping[str, object]) -> bool:
    history = payload.get("history_archive")
    tomorrow = payload.get("tomorrow_research")
    if not isinstance(history, dict) or not isinstance(tomorrow, dict):
        return False
    if payload.get("production_authority") is not False:
        return False
    if history.get("production_authority") is not False or history.get("point_in_time_parity") is not False:
        return False
    if tomorrow.get("production_authority") is not False:
        return False
    if not all(
        isinstance(history.get(name), (str, int))
        for name in (
            "state",
            "calendar_sessions",
            "universe_count",
            "partition_count",
        )
    ):
        return False
    if history.get("reason") is not None and not isinstance(history.get("reason"), str):
        return False
    if not isinstance(tomorrow.get("status"), str) or not isinstance(tomorrow.get("production_readiness"), str):
        return False
    if not all(
        isinstance(tomorrow.get(name), list) and all(isinstance(item, str) for item in tomorrow[name])
        for name in ("input_blockers", "production_blockers")
    ):
        return False
    return payload.get("schema_version") == "research_readiness"


_DETAILS: Mapping[str, Callable[[DiagnosticResult, Mapping[str, object], dict[str, object]], None]] = {
    "research_readiness": _research_details,
    "configuration": _configuration_details,
    "history_maintenance": _maintenance_details,
    "production_performance": _performance_details,
}


def ProjectDetails(result: DiagnosticResult, source: Mapping[str, object], payload: dict[str, object]) -> None:
    _DETAILS.get(result.name, ProjectSummary)(result, source, payload)
    findings = payload.get("findings")
    if result.name == "research_readiness" and source.get("status") == "degraded" and isinstance(findings, list):
        payload["findings"] = [dict(item, severity="warning") for item in findings if isinstance(item, dict)]
