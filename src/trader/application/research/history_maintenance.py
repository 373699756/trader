"""Typed command status for the zero-argument historical-data workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from trader.domain.research.history_control import HistoryTrainingDueReason

HistoryMaintenanceState = Literal["blocked", "already_current", "already_running", "completed"]


@dataclass(frozen=True)
class HistoryMaintenanceStatus:
    state: HistoryMaintenanceState
    reason: str | None
    archive_root: Path
    selected_baseline_source: str
    efficient_daily_source: str | None
    active_snapshot_hash: str | None
    data_cutoff: date | None
    label_cutoff: date | None
    matured_label_days_since_training: int
    training_due: bool
    training_due_reason: HistoryTrainingDueReason
    automatic_training: bool

    def __post_init__(self) -> None:
        if self.matured_label_days_since_training < 0:
            raise ValueError("matured label days must not be negative")
        due_reasons = {"initial_training_required", "cadence_due", "input_revision_due"}
        if self.training_due != (self.training_due_reason in due_reasons):
            raise ValueError("training due flag and reason disagree")


def blocked_history_maintenance_status() -> HistoryMaintenanceStatus:
    """Fail closed until the zero-argument synchronization workflow is implemented."""
    return HistoryMaintenanceStatus(
        state="blocked",
        reason="history_sync_pending",
        archive_root=Path("data/history/baostock"),
        selected_baseline_source="baostock",
        efficient_daily_source=None,
        active_snapshot_hash=None,
        data_cutoff=None,
        label_cutoff=None,
        matured_label_days_since_training=0,
        training_due=False,
        training_due_reason="data_incomplete",
        automatic_training=False,
    )


__all__ = [
    "HistoryMaintenanceState",
    "HistoryMaintenanceStatus",
    "HistoryTrainingDueReason",
    "blocked_history_maintenance_status",
]
