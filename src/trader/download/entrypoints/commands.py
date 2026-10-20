"""Public command orchestration for the download business."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.download.entrypoints.history_maintenance_projection import ProjectHistoryMaintenanceStatus
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra.download_settings import LoadHistorySyncConfiguration


def RunDownload(repository_root: Path, config_path: Path, *, quiet_success: bool = False) -> int:
    """Run zero-argument initial or incremental archive maintenance."""

    from trader.bootstrap import ExecuteHistoryDownload

    configuration = BuildHistorySyncConfiguration(repository_root, config_path)
    progress = StderrHistorySyncProgress()
    status = ExecuteHistoryDownload(configuration, progress=progress)
    progress.publish_result(status)
    # Published price gaps remain visible; training checks effective samples per head.
    ready = status.state in {"completed", "already_current"} and status.training_due_reason != "data_incomplete"
    if not ready or not quiet_success:
        print(json.dumps(ProjectHistoryMaintenanceStatus(status), ensure_ascii=False, sort_keys=True))
    if status.state == "cancelled":
        return 130
    if not ready:
        print("完整历史未通过训练准入；未启动双档训练。", file=sys.stderr)
    elif quiet_success and status.unresolved_price_cells:
        print("历史仍有价格缺口；按有效样本尝试双档训练。", file=sys.stderr)
    return 0 if ready else 1


def BuildHistorySyncConfiguration(repository_root: Path, config_path: Path) -> HistorySyncConfiguration:
    from trader.infra.settings import LoadRuntimeSettings

    runtime = LoadRuntimeSettings(config_path)
    return LoadHistorySyncConfiguration(repository_root, runtime.data_paths)


def RunDownloadCommand(command: str, *, config_path: Path | None = None) -> int:
    """Run one download-owned public command after root dispatch."""

    from trader.infra.settings import LoadRuntimeSettings

    if config_path is None:
        raise ValueError("download command requires an explicit configuration")
    runtime = LoadRuntimeSettings(config_path)
    if command == "history-automation-status":
        from trader.download.entrypoints.history_automation_projection import ProjectHistoryAutomationStatus
        from trader.download.infra.history_automation_status import ReadHistoryAutomationStatus

        automation_status = ReadHistoryAutomationStatus(runtime.data_paths.history, _shanghai_now())
        print(
            json.dumps(
                ProjectHistoryAutomationStatus(automation_status),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
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


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


__all__ = ["BuildHistorySyncConfiguration", "RunDownload", "RunDownloadCommand"]
