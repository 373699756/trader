"""Public command orchestration for the download business."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.download.entrypoints.history_maintenance_projection import ProjectHistoryMaintenanceStatus
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra.download_settings import LoadHistorySyncConfiguration
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.signal_cancellation import ManualCancellation


def RunDownload(
    repository_root: Path, config_path: Path, *, quiet_success: bool = False, allow_price_gaps: bool = False
) -> int:
    """Run zero-argument initial or incremental archive maintenance."""

    from trader.bootstrap import ExecuteHistoryDownload

    runtime = LoadRuntimeSettings(config_path)
    configuration = LoadHistorySyncConfiguration(repository_root, runtime.data_paths)
    progress = StderrHistorySyncProgress()
    with ManualCancellation() as cancelled:
        try:
            status = ExecuteHistoryDownload(
                configuration,
                progress=progress,
                cancel_requested=cancelled.is_set,
                storage_overrides=runtime.storage_overrides,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            if not cancelled.is_set():
                raise
            # Public reference preparation precedes the typed history result.
            raise KeyboardInterrupt from exc
        if cancelled.is_set() and status.state != "cancelled":
            status = replace(status, state="cancelled", reason="cancelled")
    progress.publish_result(status)
    # Published price gaps remain visible; training checks effective samples per head.
    ready = status.published and (allow_price_gaps or not status.needs_retry)
    if not ready or not quiet_success:
        print(json.dumps(ProjectHistoryMaintenanceStatus(status), ensure_ascii=False, sort_keys=True))
    if status.state == "cancelled":
        return 130
    if not ready:
        print("完整历史尚未完成合法发布或仍有价格缺口。", file=sys.stderr)
    elif quiet_success and status.unresolved_price_cells:
        print("历史仍有价格缺口；按有效样本尝试所选档位训练。", file=sys.stderr)
    return 0 if ready else 1


def BuildHistorySyncConfiguration(repository_root: Path, config_path: Path) -> HistorySyncConfiguration:
    runtime = LoadRuntimeSettings(config_path)
    return LoadHistorySyncConfiguration(repository_root, runtime.data_paths)


def RunDownloadCommand(command: str, *, config_path: Path | None = None) -> int:
    """Run one download-owned public command after root dispatch."""

    if config_path is None:
        raise ValueError("download command requires an explicit configuration")
    if command == "history-automation-uninstall":
        from trader.bootstrap import ExecuteHistoryAutomationRemoval

        try:
            removed = ExecuteHistoryAutomationRemoval()
        except (OSError, RuntimeError, ValueError) as exc:
            print(json.dumps({"status": "failed", "category": type(exc).__name__}))
            return 1
        print(json.dumps({"status": "removed", "removed_files": len(removed)}))
        return 0
    raise ValueError(f"unsupported download command: {command}")


__all__ = ["BuildHistorySyncConfiguration", "RunDownload", "RunDownloadCommand"]
