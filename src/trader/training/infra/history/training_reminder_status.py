"""Read-only access to the persisted history automation control state."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from trader.download.infra.history_control_index import (
    HistoryControlError,
    HistoryControlIndex,
)
from trader.training.domain.training_reminder import DeriveTrainingReminderStatus, TrainingReminderStatus
from trader.training.infra.history.training_maintenance_index import TrainingMaintenanceError, TrainingMaintenanceIndex


def ReadTrainingReminderStatus(archive_root: Path, observed_at: datetime) -> TrainingReminderStatus:
    publication = HistoryControlIndex(archive_root / "catalog.sqlite3")
    try:
        published = publication.load_published_state()
        control = TrainingMaintenanceIndex(archive_root / "catalog.sqlite3").load_state(
            active_snapshot=published.snapshot if published is not None else None
        )
    except (HistoryControlError, TrainingMaintenanceError):
        control = None
    return DeriveTrainingReminderStatus(control, observed_at, archive_root=archive_root)


__all__ = ["ReadTrainingReminderStatus"]
