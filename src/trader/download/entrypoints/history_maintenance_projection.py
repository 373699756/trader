"""JSON projection owned by the historical-maintenance CLI boundary."""

from __future__ import annotations

from trader.download.domain.history_maintenance import HistoryMaintenanceStatus


def ProjectHistoryMaintenanceStatus(status: HistoryMaintenanceStatus) -> dict[str, object]:
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
        "unresolved_price_cells": status.unresolved_price_cells,
        "price_gaps": None
        if status.price_gap_summary is None
        else {
            "stocks": status.price_gap_summary.stocks,
            "cells": status.price_gap_summary.cells,
            "first_date": str(status.price_gap_summary.first_date) if status.price_gap_summary.first_date else None,
            "last_date": str(status.price_gap_summary.last_date) if status.price_gap_summary.last_date else None,
            "reasons": [{"reason": item.reason, "cells": item.cells} for item in status.price_gap_summary.reasons],
        },
    }


__all__ = ["ProjectHistoryMaintenanceStatus"]
