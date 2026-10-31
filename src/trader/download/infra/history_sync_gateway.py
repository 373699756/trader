"""Infrastructure adapter for application history maintenance use cases."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy
from dataclasses import field

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from trader.download.application.history_ports import HistorySupplierPort
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import HistorySyncConfiguration, HistorySyncProgressPort
from trader.infra.storage_types import StorageKind
from trader.infra.workers import WorkerExecutor


@dataclass(frozen=True)
class HistorySyncGateway:
    """Route initial and incremental downloads to the atomic synchronizer.

    The synchronizer derives the operation from the published snapshot: absence
    creates the complete 2000-session archive, while an existing snapshot only
    downloads missing or reread data.  Keeping that freeze in one owner
    prevents two writers from evolving different publication semantics.
    """

    worker_pool: WorkerExecutor | None = None
    writer_pool: WorkerExecutor | None = None
    storage_kind: StorageKind = "unknown"
    repair_only: bool = False

    sqlite_policy: SqliteIoPolicy = field(kw_only=True)

    def download(
        self,
        configuration: HistorySyncConfiguration,
        supplier: HistorySupplierPort,
        *,
        progress: HistorySyncProgressPort | None = None,
        clock: Callable[[], datetime] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> HistoryMaintenanceStatus:
        from trader.download.infra.history_sync_runner import RunHistorySync

        return RunHistorySync(
            configuration,
            supplier,
            progress=progress,
            clock=clock,
            cancel_requested=cancel_requested,
            worker_pool=self.worker_pool,
            writer_pool=self.writer_pool,
            storage_kind=self.storage_kind,
            repair_only=self.repair_only, sqlite_policy=self.sqlite_policy,
        )



__all__ = ["HistorySyncGateway"]
