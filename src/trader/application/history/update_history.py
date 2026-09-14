"""Use case for minimal history gap and recent-session repair."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from trader.application.history.history_ports import HistoryArchivePort, HistorySupplierPort
from trader.application.research.history_maintenance import HistoryMaintenanceStatus
from trader.application.research.history_sync import HistorySyncConfiguration, HistorySyncProgressPort


@dataclass(frozen=True)
class UpdateHistoryUseCase:
    """Request an incremental archive update while preserving the active snapshot on failure."""

    archive: HistoryArchivePort

    def execute(
        self,
        configuration: HistorySyncConfiguration,
        supplier: HistorySupplierPort,
        *,
        progress: HistorySyncProgressPort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> HistoryMaintenanceStatus:
        return self.archive.update(configuration, supplier, progress=progress, clock=clock)


def update_history(
    archive: HistoryArchivePort,
    configuration: HistorySyncConfiguration,
    supplier: HistorySupplierPort,
    *,
    progress: HistorySyncProgressPort | None = None,
    clock: Callable[[], datetime] | None = None,
) -> HistoryMaintenanceStatus:
    """Functional entry point for a bounded, gap-only update."""

    return UpdateHistoryUseCase(archive).execute(
        configuration,
        supplier,
        progress=progress,
        clock=clock,
    )


__all__ = ["UpdateHistoryUseCase", "update_history"]
