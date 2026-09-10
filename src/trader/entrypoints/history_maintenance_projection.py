"""JSON projection owned by the historical-maintenance CLI boundary."""

from __future__ import annotations

from trader.application.research.history_maintenance import HistoryMaintenanceStatus


def project_history_maintenance_status(status: HistoryMaintenanceStatus) -> dict[str, object]:
    return {
        "schema_version": "history_maintenance_status",
        "state": status.state,
        "reason": status.reason,
        "archive_root": str(status.archive_root),
        "selected_baseline_source": status.selected_baseline_source,
        "efficient_daily_source": status.efficient_daily_source,
        "active_snapshot_hash": status.active_snapshot_hash,
        "data_cutoff": status.data_cutoff.isoformat() if status.data_cutoff is not None else None,
        "label_cutoff": status.label_cutoff.isoformat() if status.label_cutoff is not None else None,
        "matured_label_days_since_training": status.matured_label_days_since_training,
        "training_due": status.training_due,
        "training_due_reason": status.training_due_reason,
        "automatic_training": status.automatic_training,
    }


__all__ = ["project_history_maintenance_status"]
