"""Read-only access to the persisted history automation control state."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

from datetime import datetime
from pathlib import Path

from trader.download.infra.history_control_index import (
    HistoryControlError,
    HistoryControlIndex,
)
from trader.training.domain.training_reminder import DeriveTrainingReminderStatus, TrainingReminderStatus
from trader.training.infra.history.training_maintenance_index import TrainingMaintenanceError, TrainingMaintenanceIndex


def ReadTrainingReminderStatus(history_root: Path, observed_at: datetime, *, sqlite_policy: SqliteIoPolicy) -> TrainingReminderStatus:
    publication = HistoryControlIndex(history_root / "catalog.sqlite3", sqlite_policy=sqlite_policy)
    try:
        published = publication.load_published_state()
        control = TrainingMaintenanceIndex(history_root / "catalog.sqlite3", sqlite_policy=sqlite_policy).load_state(
            published_snapshot=published.snapshot if published is not None else None
        )
    except (HistoryControlError, TrainingMaintenanceError):
        control = None
    return DeriveTrainingReminderStatus(control, observed_at, history_root=history_root)


__all__ = ["ReadTrainingReminderStatus"]
