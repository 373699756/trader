"""Read-only access to the persisted history automation control state."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from trader.download.domain.history_automation import (
    DeriveHistoryAutomationStatus,
    HistoryAutomationStatus,
)
from trader.download.infra.history_control_index import (
    HistoryControlError,
    HistoryControlIndex,
)


def ReadHistoryAutomationStatus(archive_root: Path, observed_at: datetime) -> HistoryAutomationStatus:
    repository = HistoryControlIndex(archive_root / "catalog.sqlite3")
    try:
        control = repository.load_automation_state()
    except HistoryControlError:
        control = None
    return DeriveHistoryAutomationStatus(control, observed_at, archive_root=archive_root)


__all__ = ["ReadHistoryAutomationStatus"]
