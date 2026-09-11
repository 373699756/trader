"""Explicit JSON projections for history automation observability boundaries."""

from __future__ import annotations

from trader.application.research.history_automation import HistoryAutomationRunStatus, HistoryAutomationStatus
from trader.infra.research.history_automation_installation import HistoryAutomationInstallationResult


def project_history_automation_status(status: HistoryAutomationStatus) -> dict[str, object]:
    return {
        "schema_version": "history_automation_status",
        "state": status.state,
        "reason": status.reason,
        "archive_root": str(status.archive_root),
        "active_snapshot_hash": status.active_snapshot_hash,
        "data_cutoff": status.data_cutoff.isoformat() if status.data_cutoff is not None else None,
        "label_cutoff": status.label_cutoff.isoformat() if status.label_cutoff is not None else None,
        "due_identity": status.due_identity,
        "matured_label_days_since_training": status.matured_label_days_since_training,
        "training_due": status.training_due,
        "training_due_reason": status.training_due_reason,
        "reminder_date": status.reminder_date.isoformat(),
        "reminder_state": status.reminder_state,
        "reminder_error_code": status.reminder_error_code,
        "automatic_model_update": status.automatic_model_update,
    }


def project_history_automation_run_status(status: HistoryAutomationRunStatus) -> dict[str, object]:
    return {
        "schema_version": "history_automation_run_status",
        "observed_at": status.observed_at.isoformat(),
        "archive_root": str(status.archive_root),
        "maintenance_state": status.maintenance_state,
        "maintenance_reason": status.maintenance_reason,
        "due": project_history_automation_status(status.due_status),
        "notification_state": status.notification_state,
        "notification_error_code": status.notification_error_code,
        "automatic_model_update": status.automatic_model_update,
        "successful": status.successful,
    }


def project_history_automation_installation_result(
    result: HistoryAutomationInstallationResult,
) -> dict[str, object]:
    return {
        "schema_version": "history_automation_installation_status",
        "state": result.state,
        "platform": result.platform,
        "managed_paths": [str(path) for path in result.managed_paths],
    }


__all__ = [
    "project_history_automation_installation_result",
    "project_history_automation_run_status",
    "project_history_automation_status",
]
