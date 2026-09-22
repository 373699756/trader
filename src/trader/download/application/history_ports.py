"""Ports shared by history download, update, and status use cases."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import (
    HistorySyncConfiguration,
    HistorySyncProgressPort,
    HistorySyncSupplier,
)


class HistorySupplierPort(HistorySyncSupplier, Protocol):
    """Supplier capability for calendar, identity, and daily history facts."""


class HistoryCalendarPort(Protocol):
    """Read-only calendar capability used when planning an update."""

    def last_completed_session(self, as_of: date) -> date | None: ...

    def missing_sessions(self, active_snapshot_hash: str | None, as_of: date) -> tuple[date, ...]: ...


class HistoryArchivePort(Protocol):
    """Atomic archive maintenance capability owned by infrastructure."""

    def download(
        self,
        configuration: HistorySyncConfiguration,
        supplier: HistorySupplierPort,
        *,
        progress: HistorySyncProgressPort | None = None,
        clock: Callable[[], datetime] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> HistoryMaintenanceStatus: ...

    def update(
        self,
        configuration: HistorySyncConfiguration,
        supplier: HistorySupplierPort,
        *,
        progress: HistorySyncProgressPort | None = None,
        clock: Callable[[], datetime] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> HistoryMaintenanceStatus: ...


class HistoryStatusPort(Protocol):
    """Read-only status projection; it must not refresh or mutate an archive."""

    def read(self, archive_root: Path) -> HistoryMaintenanceStatus: ...


__all__ = [
    "HistoryArchivePort",
    "HistoryCalendarPort",
    "HistoryStatusPort",
    "HistorySupplierPort",
]
