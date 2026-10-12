"""Read-only access to the persisted history automation control state."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from trader.download.domain.history_automation import (
    HistoryAutomationStatus,
    derive_history_automation_status,
)
from trader.download.infra.history_control_index import (
    HistoryControlError,
    HistoryControlIndex,
)


def read_history_automation_status(archive_root: Path, observed_at: datetime) -> HistoryAutomationStatus:
    repository = HistoryControlIndex(archive_root / "control.sqlite3")
    try:
        control = repository.load_automation_state()
    except HistoryControlError:
        control = None
    return derive_history_automation_status(control, observed_at, archive_root=archive_root)


__all__ = ["read_history_automation_status"]
