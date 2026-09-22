"""Infrastructure adapter for application history maintenance use cases."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from trader.download.application.history_ports import HistorySupplierPort
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import HistorySyncConfiguration, HistorySyncProgressPort
from trader.download.infra.history_control_repository import SQLiteHistoryControlRepository


class HistoryArchiveGateway:
    """Route both initial download and incremental update to the atomic synchronizer.

    The synchronizer derives the operation from the active snapshot: absence
    creates the complete 2000-session archive, while an existing snapshot only
    downloads missing or reread data.  Keeping that decision in one owner
    prevents two writers from evolving different publication semantics.
    """

    def download(
        self,
        configuration: HistorySyncConfiguration,
        supplier: HistorySupplierPort,
        *,
        progress: HistorySyncProgressPort | None = None,
        clock: Callable[[], datetime] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> HistoryMaintenanceStatus:
        from trader.download.infra.history_archive_sync import run_history_sync

        return run_history_sync(
            configuration,
            supplier,
            progress=progress,
            clock=clock,
            cancel_requested=cancel_requested,
        )

    def update(
        self,
        configuration: HistorySyncConfiguration,
        supplier: HistorySupplierPort,
        *,
        progress: HistorySyncProgressPort | None = None,
        clock: Callable[[], datetime] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> HistoryMaintenanceStatus:
        from trader.download.infra.history_archive_sync import run_history_sync

        control_path = configuration.archive_root / "control.sqlite3"
        if not control_path.exists():
            return _blocked_without_active(configuration)
        control = SQLiteHistoryControlRepository(control_path)
        try:
            control.initialize()
            if control.load_state().active_snapshot is None:
                return _blocked_without_active(configuration)
        except (OSError, RuntimeError, ValueError):
            return _blocked_without_active(configuration, reason="history_control_unavailable")
        return run_history_sync(
            configuration,
            supplier,
            progress=progress,
            clock=clock,
            cancel_requested=cancel_requested,
        )


def _blocked_without_active(
    configuration: HistorySyncConfiguration,
    *,
    reason: str = "history_archive_missing",
) -> HistoryMaintenanceStatus:
    """Return a stable update error without contacting the supplier."""

    return HistoryMaintenanceStatus(
        state="blocked",
        reason=reason,
        archive_root=configuration.archive_root,
        selected_baseline_source="baostock",
        efficient_daily_source=None,
        active_snapshot_hash=None,
        data_cutoff=None,
        label_cutoff=None,
        matured_label_days_since_training=0,
        training_due=False,
        training_due_reason="data_incomplete",
        automatic_training=False,
    )


__all__ = ["HistoryArchiveGateway"]
