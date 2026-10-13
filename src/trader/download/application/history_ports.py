"""Ports shared by history download, update, and status use cases."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import (
    HistorySyncConfiguration,
    HistorySyncProgressPort,
    HistoryTwoStageSupplier,
)


class HistorySupplierPort(HistoryTwoStageSupplier, Protocol):
    """Supplier capability for calendar, identity, and daily history facts."""


class HistorySyncPort(Protocol):
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


__all__ = [
    "HistorySyncPort",
    "HistorySupplierPort",
]
