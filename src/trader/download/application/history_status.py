"""Read-only history status use case."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from trader.download.application.history_ports import HistoryStatusPort
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus


@dataclass(frozen=True)
class HistoryStatusUseCase:
    """Read status through an injected port; never starts maintenance."""

    status: HistoryStatusPort

    def execute(self, archive_root: Path) -> HistoryMaintenanceStatus:
        return self.status.read(archive_root)


__all__ = ["HistoryStatusUseCase"]
