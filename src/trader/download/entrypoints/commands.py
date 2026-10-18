"""Public command orchestration for the download business."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import HistorySyncConfiguration, HistorySyncProgressPort
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
    ready = (
        status.state in {"completed", "already_current"}
        and not status.unresolved_price_cells
        and status.training_due_reason != "data_incomplete"
    )
    if not ready or not quiet_success:
        print(json.dumps(ProjectHistoryMaintenanceStatus(status), ensure_ascii=False, sort_keys=True))
    if not ready:
        print("完整历史未通过训练准入；未启动双档训练。", file=sys.stderr)
    return 0 if ready else 1


def BuildHistorySyncConfiguration(repository_root: Path, config_path: Path) -> HistorySyncConfiguration:
    from trader.infra.settings import LoadRuntimeSettings

    runtime = LoadRuntimeSettings(config_path)
    return LoadHistorySyncConfiguration(repository_root, runtime.reference_dir)


def RunDownloadCommand(command: str, *, config_path: Path | None = None) -> int:
    """Run one download-owned public command after root dispatch."""

    repository_root = _repository_root()
    if command == "history-automation-status":
        from trader.download.entrypoints.history_automation_projection import ProjectHistoryAutomationStatus
        from trader.download.infra.history_automation_status import ReadHistoryAutomationStatus

        automation_status = ReadHistoryAutomationStatus(repository_root / "data/history", _shanghai_now())
        print(
            json.dumps(
                ProjectHistoryAutomationStatus(automation_status),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if command != "download_scheduled" or config_path is None:
        raise ValueError(f"unsupported download command: {command}")
    from trader.bootstrap import ExecuteHistoryDownload
    from trader.download.entrypoints.history_automation_projection import ProjectHistoryAutomationRunStatus
    from trader.download.infra.history_maintenance_runner import (
        PlatformHistoryDesktopNotifier,
        RotatingHistoryAutomationLog,
        RunScheduledHistoryMaintenance,
    )
    from trader.infra.settings import LoadRuntimeSettings

    runtime = LoadRuntimeSettings(config_path)
    observed_at = _shanghai_now()
    configuration = BuildHistorySyncConfiguration(repository_root, config_path)
    task_log = RotatingHistoryAutomationLog(runtime.runtime_dir / "logs/history-automation.log")

    def synchronize(progress: HistorySyncProgressPort | None) -> HistoryMaintenanceStatus:
        return ExecuteHistoryDownload(
            configuration,
            supplier_progress=task_log,
            clock=lambda: observed_at,
            progress=progress,
        )

    try:
        run_status = RunScheduledHistoryMaintenance(
            configuration,
            synchronize,
            PlatformHistoryDesktopNotifier(),
            observed_at,
            progress=task_log,
        )
        try:
            task_log.publish_run(run_status)
        except OSError:
            print(
                '{"reason":"log_write_failed","schema_version":"history_automation_log","state":"degraded"}',
                file=sys.stderr,
                flush=True,
            )
        print(
            json.dumps(
                ProjectHistoryAutomationRunStatus(run_status),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if run_status.successful else 1
    finally:
        task_log.close()


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _repository_root() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "src/trader").is_dir():
            return candidate
    return Path("/__trader_source_not_found__")


__all__ = ["BuildHistorySyncConfiguration", "RunDownload", "RunDownloadCommand"]
