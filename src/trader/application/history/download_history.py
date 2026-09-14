"""Use case for initial and integrity-checked history archive creation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from trader.application.history.history_ports import HistoryArchivePort, HistorySupplierPort
from trader.application.research.history_maintenance import HistoryMaintenanceStatus
from trader.application.research.history_sync import HistorySyncConfiguration, HistorySyncProgressPort


@dataclass(frozen=True)
class DownloadHistoryUseCase:
    """Request a complete archive build without invoking training or recommendations."""

    archive: HistoryArchivePort

    def execute(
        self,
        configuration: HistorySyncConfiguration,
        supplier: HistorySupplierPort,
        *,
        progress: HistorySyncProgressPort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> HistoryMaintenanceStatus:
        return self.archive.download(configuration, supplier, progress=progress, clock=clock)


def download_history(
    archive: HistoryArchivePort,
    configuration: HistorySyncConfiguration,
    supplier: HistorySupplierPort,
    *,
    progress: HistorySyncProgressPort | None = None,
    clock: Callable[[], datetime] | None = None,
) -> HistoryMaintenanceStatus:
    """Functional entry point kept thin so callers can inject an archive port."""

    return DownloadHistoryUseCase(archive).execute(
        configuration,
        supplier,
        progress=progress,
        clock=clock,
    )


__all__ = ["DownloadHistoryUseCase", "download_history"]
