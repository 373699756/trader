"""Explicit JSON projections for history automation observability boundaries."""

from __future__ import annotations

from trader.download.domain.history_automation import HistoryAutomationStatus


def ProjectHistoryAutomationStatus(status: HistoryAutomationStatus) -> dict[str, object]:
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


__all__ = ["ProjectHistoryAutomationStatus"]
