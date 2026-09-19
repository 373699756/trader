"""Public command orchestration for the download business."""

from __future__ import annotations

import json
from pathlib import Path

from trader.download.application.download_history import DownloadHistoryUseCase
from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.download.entrypoints.history_maintenance_projection import project_history_maintenance_status
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier
from trader.download.infra.history_archive_gateway import HistoryArchiveGateway


def run_download(repository_root: Path) -> int:
    """Run zero-argument initial or incremental archive maintenance."""

    configuration = history_sync_configuration(repository_root)
    progress = StderrHistorySyncProgress()
    with BaoStockHistorySupplier(configuration, progress=progress) as supplier:
        status = DownloadHistoryUseCase(HistoryArchiveGateway()).execute(
            configuration,
            supplier,
            progress=progress,
        )
    progress.publish_result(status)
    print(json.dumps(project_history_maintenance_status(status), ensure_ascii=False, sort_keys=True))
    return 0 if status.state in {"completed", "already_current"} else 1


def history_sync_configuration(repository_root: Path) -> HistorySyncConfiguration:
    return HistorySyncConfiguration(
        archive_root=repository_root / "data" / "history" / "baostock",
        training_root=repository_root / "data" / "train",
    )


__all__ = ["history_sync_configuration", "run_download"]
