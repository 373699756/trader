"""Infrastructure adapter for application history maintenance use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from trader.download.application.history_ports import HistorySupplierPort
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import HistorySyncConfiguration, HistorySyncProgressPort
from trader.infra.workers import WorkerExecutor


@dataclass(frozen=True)
class HistorySyncGateway:
    """Route initial and incremental downloads to the atomic synchronizer.

    The synchronizer derives the operation from the active snapshot: absence
    creates the complete 2000-session archive, while an existing snapshot only
    downloads missing or reread data.  Keeping that decision in one owner
    prevents two writers from evolving different publication semantics.
    """

    worker_pool: WorkerExecutor | None = None
    writer_pool: WorkerExecutor | None = None

    def download(
        self,
        configuration: HistorySyncConfiguration,
        supplier: HistorySupplierPort,
        *,
        progress: HistorySyncProgressPort | None = None,
        clock: Callable[[], datetime] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> HistoryMaintenanceStatus:
        from trader.download.infra.history_sync_runner import run_history_sync

        return run_history_sync(
            configuration,
            supplier,
            progress=progress,
            clock=clock,
            cancel_requested=cancel_requested,
            worker_pool=self.worker_pool,
            writer_pool=self.writer_pool,
        )


__all__ = ["HistorySyncGateway"]
