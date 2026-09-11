"""Read-only access to the persisted history automation control state."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from trader.application.research.history_automation import (
    HistoryAutomationStatus,
    derive_history_automation_status,
)
from trader.infra.research.history_control_repository import (
    HistoryControlError,
    SQLiteHistoryControlRepository,
)


def read_history_automation_status(archive_root: Path, observed_at: datetime) -> HistoryAutomationStatus:
    repository = SQLiteHistoryControlRepository(archive_root / "control.sqlite3")
    try:
        control = repository.load_automation_state()
    except HistoryControlError:
        control = None
    return derive_history_automation_status(control, observed_at, archive_root=archive_root)


__all__ = ["read_history_automation_status"]
